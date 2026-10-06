"""agent/progress.py - live step progress for the watcher's PowerShell window.

watch_queue.run_step() runs each pipeline step as a child process. This module runs it
with a one-line status that updates in place while the step works, for example:

  [2/6] agent_step  ██████████░░░░░░░░░░  52%  1m 34s / ~3m 00s typical  · 2 files edited, 0 commits · timeout 15m

Honest about what it knows: Copilot CLI and the Salesforce CLI do not report how far
through their work they are, so the bar is ELAPSED TIME against how long this step has
TYPICALLY taken (median of its last successful runs in logs/run-events.jsonl, with
sensible defaults until there is history). It never claims 100% before the step ends;
past the typical time it says "longer than usual" instead. For the agent step it also
shows a real signal every few seconds: files edited and commits made so far (git).

How output stays readable: the child's output is relayed line by line; before each
relayed line the status line is wiped, and redrawn after it, so step output and the bar
never overwrite each other. The HUMAN review gate is run exactly as before (inherited
console, no status line), because it asks for typed input.

.env:
  WATCH_PROGRESS=true            set false to turn the status line off (plain output)
  WATCH_PROGRESS_INTERVAL=1      redraw interval in seconds
"""
import os
import sys
import time
import shutil
import statistics
import subprocess
import threading

import events

ENABLED = os.getenv("WATCH_PROGRESS", "true").strip().lower() == "true"
INTERVAL = max(0.5, float(os.getenv("WATCH_PROGRESS_INTERVAL", "1") or 1))
BAR_WIDTH = 20
HISTORY = 10
# seconds - used until a step has at least one successful run in run-events.jsonl
DEFAULT_TYPICAL = {"prepare": 20, "agent_step": 180, "review": 60, "validate": 90, "pr": 25, "jira": 10}


# ---------------------------------------------------------------- pure helpers (tested)
def typical_seconds(step, evs=None):
    """Median duration of the last HISTORY successful runs of this step, else a default."""
    try:
        evs = events.read_events() if evs is None else evs
    except Exception:
        evs = []
    vals = [e.get("duration_s") for e in evs
            if e.get("step") == step and e.get("status") == "ok"
            and isinstance(e.get("duration_s"), (int, float)) and e.get("duration_s") > 0]
    if vals:
        return max(1.0, float(statistics.median(vals[-HISTORY:])))
    return float(DEFAULT_TYPICAL.get(step, 60))


def fmt_dur(s):
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def status_line(index, total, step, elapsed, typical, extra="", timeout_s=None):
    """One status line. Fraction is elapsed/typical, capped at 99% while running."""
    frac = min(elapsed / typical, 0.99) if typical > 0 else 0.0
    filled = int(round(frac * BAR_WIDTH))
    full, empty = _glyphs()
    bar = full * filled + empty * (BAR_WIDTH - filled)
    if elapsed > typical:
        timing = f"{fmt_dur(elapsed)} · longer than usual (~{fmt_dur(typical)} typical)"
        pct = " ..."
    else:
        timing = f"{fmt_dur(elapsed)} / ~{fmt_dur(typical)} typical"
        pct = f"{int(frac * 100):3d}%"
    parts = [f"[{index}/{total}] {step:<10} {bar} {pct}  {timing}"]
    if extra:
        parts.append(extra)
    if timeout_s:
        parts.append(f"timeout {fmt_dur(timeout_s)}")
    return "  ·  ".join(parts)


def _glyphs():
    """Block characters if the console can show them, else plain ASCII."""
    enc = (getattr(sys.stdout, "encoding", None) or "ascii")
    try:
        "█░".encode(enc)
        return "█", "░"
    except Exception:
        return "#", "-"


def git_activity(repo_root, base):
    """'N files edited, M commits' on the current branch, or '' if git is unavailable."""
    try:
        st = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True,
                            text=True, timeout=10).stdout
        rc = subprocess.run(["git", "rev-list", "--count", f"{base}..HEAD"], cwd=repo_root,
                            capture_output=True, text=True, timeout=10).stdout.strip()
        files = len([l for l in st.splitlines() if l.strip()])
        commits = int(rc) if rc.isdigit() else 0
        return f"{files} file{'s' if files != 1 else ''} edited, {commits} commit{'s' if commits != 1 else ''}"
    except Exception:
        return ""


# ---------------------------------------------------------------- runner
class _Status:
    """Owns the single status line on the console. All writes go through a lock."""

    def __init__(self, out):
        self.out, self.lock, self.width = out, threading.Lock(), 0

    def _w(self, s):
        try:
            self.out.write(s)
        except UnicodeEncodeError:
            self.out.write(s.encode("ascii", "replace").decode("ascii"))
        self.out.flush()

    def clear(self):
        if self.width:
            self._w("\r" + " " * self.width + "\r")
            self.width = 0

    def draw(self, text):
        try:   # never wrap: a wrapped line cannot be redrawn in place
            cols = shutil.get_terminal_size((120, 20)).columns
        except Exception:
            cols = 120
        if len(text) > cols - 1:
            text = text[:max(10, cols - 2)] + "…"
        with self.lock:
            self.clear()
            self._w(text)
            self.width = len(text)

    def print_line(self, line):
        with self.lock:
            self.clear()
            self._w(line if line.endswith("\n") else line + "\n")

    def done(self):
        with self.lock:
            self.clear()


def run_with_progress(cmd, cwd, index, total, step, interactive=False, repo_root=None, base="main",
                      timeout_s=None, out=None):
    """Run one pipeline step. Returns the child's exit code.
    interactive=True (human review gate) or WATCH_PROGRESS=false -> plain inherited run."""
    out = out or sys.stdout
    if interactive or not ENABLED:
        if interactive:
            out.write(f"  [{index}/{total}] {step} - waiting for your answer\n")
            out.flush()
        return subprocess.run(cmd, cwd=cwd).returncode

    typical = typical_seconds(step)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"          # child lines arrive as they are printed
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env)
    status = _Status(out)
    t0 = time.time()
    stop = threading.Event()
    extra = {"text": ""}

    def ticker():
        last_git = 0.0
        while not stop.is_set():
            now = time.time()
            if step == "agent_step" and repo_root and now - last_git >= 5:
                extra["text"] = git_activity(repo_root, base)
                last_git = now
            status.draw(status_line(index, total, step, now - t0, typical, extra["text"], timeout_s))
            stop.wait(INTERVAL)

    th = threading.Thread(target=ticker, daemon=True)
    th.start()
    try:
        for raw in iter(proc.stdout.readline, b""):
            status.print_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        raise
    finally:
        stop.set()
        th.join(timeout=2)
        status.done()
    took = time.time() - t0
    mark = "done" if rc == 0 else f"exit {rc}"
    out.write(f"  [{index}/{total}] {step} {mark} in {fmt_dur(took)} (typical ~{fmt_dur(typical)})\n")
    out.flush()
    return rc
