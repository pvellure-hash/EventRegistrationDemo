#!/usr/bin/env python
"""run_console.py - a local web page that starts the watcher and shows every step live.

  python run_console.py            (from the agent folder; opens your browser)

What it is
  A GitHub-Actions-style run view for the pipeline. One button starts `watch_queue.py`;
  the page then follows it: start-up checks, every ticket step by step (claim, pre-checks,
  prepare, AI agent, review gate, validation, draft PR, Jira), the merge, the deploy
  approval and the deploy, each with its status, timing and its own log. The human review
  gate appears as an Approve / Reject banner.

How it works (and what it does NOT do)
  - It starts the same command you would type, `python watch_queue.py`, as a child process,
    reads its output, and answers the review-gate prompt by writing y or n to the watcher's
    standard input. No pipeline script is changed or imported (except `read_queue`, used
    only to preview the ai-ready queue).
  - It can start only three fixed scripts from the agent folder: watch_queue.py,
    preflight.py and generate_pipeline_dashboard.py. It cannot run anything else.
  - Local only: it listens on 127.0.0.1, every request needs the one-time token printed at
    start-up, and requests whose Host or Origin is not this machine are refused.
  - Secrets: token-like strings are redacted before they reach the page or the transcript.
  - The console runs the watcher with WATCH_PROGRESS=false (it draws its own progress bar).

Files written:  logs/run-console/session-<time>.jsonl  (a replayable transcript)

Standard library only. Usage:
  python run_console.py [--port 8765] [--no-open]
"""
import argparse
import codecs
import copy
import json
import os
import re
import secrets
import signal
import statistics
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DEFAULT_PORT = 8765
ALLOWED_UTILS = {"preflight": ("preflight.py", "Pre-flight check"),
                 "dashboard": ("generate_pipeline_dashboard.py", "Rebuild dashboard")}

# --------------------------------------------------------------------------- pipeline model
STEP_DEFS = [
    (1, "Claim from Jira", "label ai-locked, status In Progress"),
    (2, "Pre-checks", "git sync, production-org guard"),
    (3, "Localise & prepare", "prepare_fix.py"),
    (4, "AI agent", "invoke_copilot.py"),
    (5, "Human review gate", "review_gate.py"),
    (6, "Validation", "validate_fix.py"),
    (7, "Draft pull request", "create_pr.py"),
    (8, "Jira update", "update_jira.py"),
    (9, "Back to main", "git sync, dashboard, code index"),
    (10, "Merge", "a person merges the pull request"),
    (11, "Deploy approval", "a person approves in GitHub Actions"),
    (12, "Deploy to Salesforce", "salesforce-deploy.yml"),
    (13, "Jira → Done", "automatic after a successful deploy"),
]
HUMAN_STEPS = {5, 10, 11}
SCRIPT_STEP = {"prepare_fix.py": 3, "invoke_copilot.py": 4, "review_gate.py": 5, "validate_fix.py": 6,
               "create_pr.py": 7, "update_jira.py": 8}
STEP_NAME = {3: "prepare", 4: "agent_step", 5: "review", 6: "validate", 7: "pr", 8: "jira"}
DEFAULT_TYPICAL = {"prepare": 20, "agent_step": 180, "review": 60, "validate": 90, "pr": 25, "jira": 10}
POST_STEPS = (10, 11, 12, 13)
PIPE_STEPS = (3, 4, 5, 6, 7, 8)

RX_CLAIMED = re.compile(r"^\s*Claimed:\s*(\S+)")
RX_PROC = re.compile(r"^# Processing (\S+)\s+\(run (\S+)\)")
RX_HDR = re.compile(r"^>>> \[(\d+)/(\d+)\] (\S+) (\S+)")
RX_STOP = re.compile(r"^\[(\S+)\] Stopped at (\S+\.py)\.")
RX_DONE = re.compile(r"^\[(\S+)\] Completed in (.+?)\.\s")
RX_WAIT = re.compile(r"^\[(\S+)\] Waiting on an open PR")
RX_GS = re.compile(r"\[git-sync\]\s+(OK|STOP)\s+\((startup|before ticket|end of ticket)\)")
RX_CHECK = re.compile(r"^\s+(PASS|FAIL|WARN)\s+(.*?)\s*$")
RX_TRACK = re.compile(r"\[pr-tracker\]\s+(MERGED|REJECTED|CONFLICT)\s+PR #(\d+)\s+(\S+)")
RX_DEPLOY = re.compile(r"\[deploy\]\s+PR #(\d+)\s+(\S+?):\s+deploy (\w+) -> (\w+)(?:\s+\((.*)\))?")
RX_COST = re.compile(r"^\s+cost:\s+([\d.]+) AI credits \(~\$([\d.]+)\)")
RX_PR = re.compile(r"^(?:PR:|\s+done: DRAFT PR created:)\s+(https?://\S+)")
RX_PROMPT = re.compile(r"\[y/N\]:\s*$")
_SECRET = re.compile(r"(ATATT[0-9A-Za-z_\-=]{20,}|gh[pousr]_[0-9A-Za-z_]{20,}|github_pat_[0-9A-Za-z_]{20,}"
                     r"|force://[^\s'\"]+|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def now_ms():
    return int(time.time() * 1000)


def load_env(path):
    """Minimal .env reader (setdefault, so real environment variables win)."""
    try:
        for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if v[:1] in ("'", '"') and v.count(v[0]) >= 2:      # quoted value: keep what is inside the quotes
                    v = v[1:v.index(v[0], 1)]
                else:                                                  # unquoted: drop a trailing " # comment"
                    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
                os.environ.setdefault(k.strip(), v)
    except OSError:
        pass


def secret_values():
    out = []
    for k, v in os.environ.items():
        if re.search(r"(TOKEN|SECRET|PASSWORD|AUTH_URL|API_KEY)", k, re.I) and len(v) >= 12:
            out.append(v)
    return out


def redact(text, extra=()):
    text = _SECRET.sub("[REDACTED]", text)
    for v in extra:
        if v in text:
            text = text.replace(v, "[REDACTED]")
    return text


# --------------------------------------------------------------------------- parser
class Parser:
    """Turns the watcher's plain-text output into structured events (jobs, steps, lines).
    Pure and deterministic: the same lines always give the same events, so a saved
    transcript can be replayed. Unknown lines are never dropped: they stay in the log of
    whichever step is current."""

    def __init__(self, summary_of=None):
        self.out = []
        self.jobs = {}
        self.steps = {}
        self.order = []
        self.ctx = "startup"
        self.cur = None            # current ticket job id
        self.sidx = {}             # job id -> current step index
        self.nkey = {}
        self.notif = False
        self.summary_of = summary_of or (lambda key: "")
        self.lines = {}
        self._mk_startup()

    # ------------------------------------------------------------------ job / step helpers
    def _pub(self, j):
        return {k: j.get(k) for k in ("id", "kind", "key", "title", "summary", "status", "start", "end", "credits",
                                      "pr", "prNumber", "branch", "alert", "note", "total", "order")}

    def _emit_job(self, j):
        self.out.append({"type": "job", "job": copy.deepcopy(self._pub(j))})

    def _emit_step(self, j, s):
        self.out.append({"type": "step", "job": j["id"], "step": copy.deepcopy(s)})

    def _new_job(self, jid, kind, key, title, steps, t):
        j = {"id": jid, "kind": kind, "key": key, "title": title, "summary": "", "status": "running", "start": t,
             "end": None, "credits": 0.0, "pr": None, "prNumber": None, "branch": None, "alert": [], "note": "",
             "total": None, "order": len(self.order)}
        self.jobs[jid] = j
        self.order.append(jid)
        self.steps[jid] = {}
        for idx, label, sub in steps:
            self.steps[jid][idx] = {"idx": idx, "label": label, "sub": sub, "status": "pending", "start": None,
                                    "end": None, "facts": [], "checks": [], "human": idx in HUMAN_STEPS}
        self.sidx[jid] = steps[0][0]
        self.lines[jid] = 0
        self._emit_job(j)
        for s in self.steps[jid].values():
            self._emit_step(j, s)
        return j

    def _set(self, jid, idx, status, t, note=None, fact=None, emit=True):
        j, s = self.jobs[jid], self.steps[jid][idx]
        if status == "running" and s["start"] is None:
            s["start"] = t
        if status == "waiting" and s["start"] is None:
            s["start"] = t
        if status in ("ok", "failed") and s["end"] is None:
            s["end"] = t
            if s["start"] is None:
                s["start"] = t
        s["status"] = status
        if note is not None:
            s["note"] = note
        if fact:
            self._fact(s, *fact)
        if emit:
            self._emit_step(j, s)

    def _fact(self, s, k, v):
        for f in s["facts"]:
            if f["k"] == k:
                f["v"] = v
                return
        s["facts"].append({"k": k, "v": v})

    def _job_status(self, jid, status, t):
        j = self.jobs[jid]
        j["status"] = status
        if status in ("ok", "failed"):
            j["end"] = j["end"] or t
        self._emit_job(j)

    def _finish_before(self, jid, idx, t, upto=None):
        for i in sorted(self.steps[jid]):
            if i >= idx:
                break
            if self.steps[jid][i]["status"] in ("pending", "running"):
                self._set(jid, i, "ok", t)

    def _skip_rest(self, jid, steps, t):
        for i in steps:
            if self.steps[jid][i]["status"] == "pending":
                self._set(jid, i, "skipped", t)

    # ------------------------------------------------------------------ start-up / poll jobs
    def _mk_startup(self):
        self._new_job("startup", "startup", None, "Watcher start-up", [
            (1, "Sync to main", "git sync"), (2, "Pre-flight & security checks", "preflight.py, security_checks.py"),
            (3, "Index the code", "code index"), (4, "Start watching", "watch loop")], 0)
        self.jobs["startup"]["start"] = None

    def _mk_poll(self, t):
        self._new_job("poll", "poll", None, "Watching for tickets", [
            (1, "Track pull requests & deploys", "GitHub, every poll"), (2, "Claim tickets", "Jira ai-ready queue")], t)
        self._set("poll", 1, "running", t)
        self._job_status("poll", "running", t)

    def _mk_ticket(self, key, t):
        n = self.nkey.get(key, 0) + 1
        self.nkey[key] = n
        jid = f"{key}#{n}"
        j = self._new_job(jid, "ticket", key, key, [(i, l, s) for i, l, s in STEP_DEFS], t)
        j["summary"] = self.summary_of(key) or ""
        self._emit_job(j)
        self._set(jid, 1, "running", t)
        self.ctx, self.cur = "ticket", jid
        return jid

    # ------------------------------------------------------------------ lines
    def _line(self, jid, idx, text, t, lvl):
        self.lines[jid] = self.lines.get(jid, 0) + 1
        self.out.append({"type": "line", "job": jid, "step": idx, "n": self.lines[jid], "t": t, "text": text, "lvl": lvl})

    @staticmethod
    def _lvl(line):
        if line.startswith(">>> "):
            return "dim"
        if line.startswith("$ "):
            return "cmd"
        m = RX_CHECK.match(line)
        if m:
            return {"PASS": "pass", "FAIL": "fail", "WARN": "warn"}[m.group(1)]
        low = line.lower()
        if line.startswith("!") or "traceback" in low or low.startswith("fail") or "blocked:" in low or " error" in low[:20]:
            return "fail"
        if "warning" in low[:30]:
            return "warn"
        if not line.strip() or set(line.strip()) <= set("=-#"):
            return "dim"
        return "info"

    def _target(self):
        if self.ctx == "ticket" and self.cur:
            return self.cur, self.sidx[self.cur]
        if self.ctx == "startup":
            return "startup", self.sidx["startup"]
        return "poll", 1

    def feed(self, text, t):
        line = text.rstrip("\r\n")
        self._transition(line, t)
        jid, idx = self._target()
        self._line(jid, idx, line, t, self._lvl(line))
        self._extract(jid, idx, line, t)
        self._post_merge(line, t)
        out, self.out = self.out, []
        return out

    def begin(self, t):
        """The watcher process was launched: the start-up job is running from this moment."""
        j = self.jobs["startup"]
        if j["start"] is None:
            j["start"] = t
            self._set("startup", 1, "running", t)
            self._emit_job(j)
        out, self.out = self.out, []
        return out

    def raw_line(self, jid, idx, text, t, lvl="info"):
        """Append a line that did not come from the watcher (for example the user's answer)."""
        self._line(jid, idx, text, t, lvl)
        out, self.out = self.out, []
        return out

    # ------------------------------------------------------------------ transitions
    def _transition(self, line, t):
        if self.ctx == "startup":
            self._startup_transition(line, t)
            return
        m = RX_CLAIMED.match(line)
        if m and m.group(1).lower() != "none" and self.ctx != "ticket":
            self._mk_ticket(m.group(1), t)
            return
        m = RX_PROC.match(line)
        if m:
            if self.ctx != "ticket" or (self.cur and self.jobs[self.cur]["key"] != m.group(1)):
                self._mk_ticket(m.group(1), t)
            jid = self.cur
            self.jobs[jid]["note"] = f"run {m.group(2)}"
            self._set(jid, 1, "ok", t)
            self._set(jid, 2, "running", t)
            self.sidx[jid] = 2
            self._emit_job(self.jobs[jid])
            return
        if self.ctx != "ticket" or not self.cur:
            return
        jid = self.cur
        m = RX_HDR.match(line)
        if m:
            script = m.group(3)
            idx = SCRIPT_STEP.get(script)
            if idx:
                self._finish_before(jid, idx, t)
                self._set(jid, idx, "running", t)
                self.sidx[jid] = idx
                self._line(jid, idx, f"$ python {script} {m.group(4)}", t, "cmd")
            return
        m = RX_STOP.match(line)
        if m:
            idx = SCRIPT_STEP.get(m.group(2))
            if idx:
                self._set(jid, idx, "failed", t)
                self._skip_rest(jid, [i for i in PIPE_STEPS if i > idx] + list(POST_STEPS), t)
            self._job_status(jid, "failed", t)
            self._begin_wrapup(jid, t)
            return
        m = RX_WAIT.match(line)
        if m:
            self._set(jid, 3, "waiting", t, note="Waiting on an open PR (no AI spend)")
            self._skip_rest(jid, [4, 5, 6, 7, 8] + list(POST_STEPS), t)
            self._job_status(jid, "waiting", t)
            self.jobs[jid]["note"] = "ai-waiting"
            self._begin_wrapup(jid, t)
            return
        m = RX_DONE.match(line)
        if m:
            self._finish_before(jid, 9, t)
            self.jobs[jid]["total"] = m.group(2)
            self._set(jid, 10, "waiting", t, note="Review and merge the pull request on GitHub")
            self._job_status(jid, "waiting", t)
            self._begin_wrapup(jid, t)
            return
        m = RX_GS.search(line)
        if m and m.group(2) == "end of ticket":
            self._end_of_ticket(jid, m.group(1) == "OK", t)

    def _begin_wrapup(self, jid, t):
        if self.steps[jid][9]["status"] == "pending":
            self._set(jid, 9, "running", t)
        self.sidx[jid] = 9

    def _end_of_ticket(self, jid, ok, t):
        j = self.jobs[jid]
        if self.steps[jid][9]["status"] in ("pending", "running"):
            self._set(jid, 9, "ok" if ok else "failed", t)
        pre = self.steps[jid][2]
        if pre["status"] == "running" and any(c["s"] == "FAIL" for c in pre["checks"]) and self.steps[jid][3]["status"] == "pending":
            self._set(jid, 2, "failed", t)
            self._skip_rest(jid, list(PIPE_STEPS) + list(POST_STEPS), t)
            self._job_status(jid, "failed", t)
        elif j["status"] == "running":
            self._finish_before(jid, 9, t)
            self._job_status(jid, "ok" if all(s["status"] in ("ok", "skipped") for s in self.steps[jid].values()) else "waiting", t)
        self.ctx, self.cur = "poll", None

    def _startup_transition(self, line, t):
        j = self.jobs["startup"]
        if j["start"] is None:
            j["start"] = t
            self._set("startup", 1, "running", t)
            self._emit_job(j)
        m = RX_GS.search(line)
        if m:
            self._set("startup", 1, "ok" if m.group(1) == "OK" else "failed", t)
            if m.group(1) != "OK":
                self._job_status("startup", "failed", t)
            self.sidx["startup"] = 2
            self._set("startup", 2, "running", t)
            return
        low = line.lower()
        if "running pre-flight" in low and self.steps["startup"][2]["status"] == "pending":
            self._set("startup", 1, "ok", t)
            self._set("startup", 2, "running", t)
            self.sidx["startup"] = 2
        elif line.startswith("[index]") or "[index] refreshed" in line:
            if self.steps["startup"][2]["status"] == "running":
                self._set("startup", 2, "failed" if any(c["s"] == "FAIL" for c in self.steps["startup"][2]["checks"]) else "ok", t)
            self.sidx["startup"] = 3
            self._set("startup", 3, "running", t)
        elif line.startswith("Watching "):
            self._finish_before("startup", 4, t)
            self.sidx["startup"] = 4
            self._set("startup", 4, "ok", t)
            self._job_status("startup", "ok", t)
            self._mk_poll(t)
            self.ctx = "poll"
            return
        elif "checks failed" in low and "fix the items" in low:
            self._set("startup", 2, "failed", t)
            self._job_status("startup", "failed", t)

    # ------------------------------------------------------------------ extraction
    def _extract(self, jid, idx, line, t):
        s = self.steps[jid][idx]
        m = RX_CHECK.match(line)
        if m and jid != "poll":
            name = re.sub(r"\s{2,}", " - ", m.group(2), count=1)
            s["checks"].append({"s": m.group(1), "name": name})
            self._emit_step(self.jobs[jid], s)
        if jid == "startup":
            return
        if line.startswith("!!!!"):
            self.notif = not self.notif
            return
        if self.notif and line.startswith("!") and jid != "poll":
            j = self.jobs[jid]
            j["alert"].append(line.lstrip("! ").strip())
            self._emit_job(j)
            return
        if jid == "poll":
            return
        j = self.jobs[jid]
        m = RX_COST.match(line)
        if m:
            j["credits"] = round(j["credits"] + float(m.group(1)), 4)
            self._set(jid, idx, s["status"], t, fact=("AI cost", f"{float(m.group(1)):.2f} credits (~${m.group(2)})"))
            self._emit_job(j)
        m = re.match(r"^\s+(model|tier)\s*:\s*(.+)$", line)
        if m:
            self._set(jid, idx, s["status"], t, fact=(m.group(1).capitalize(), m.group(2).strip()[:60]))
        m = re.match(r"^\s+changes:\s*(.+)$", line)
        if m:
            self._set(jid, idx, s["status"], t, fact=("Changes", m.group(1).strip()[:60]))
        m = re.match(r"^Branch:\s*(\S+)", line)
        if m:
            j["branch"] = m.group(1)
            self._emit_job(j)
        m = re.match(r"^(Files changed|Commits):\s*(\d+)", line)
        if m:
            self._set(jid, idx, s["status"], t, fact=(m.group(1), m.group(2)))
        m = re.search(r"confidence[=: ]+([\d.]+)", line)
        if m and idx == 3:
            self._set(jid, idx, s["status"], t, fact=("Confidence", m.group(1)))
        m = re.search(r"\[batch\]\s+(\S+(?: \+ [\S, ]+)?)", line)
        if m and idx == 3 and "+" in m.group(1):
            self._set(jid, idx, s["status"], t, fact=("Batched with", m.group(1)[:50]))
        m = RX_PR.match(line)
        if m:
            j["pr"] = m.group(1)
            n = re.search(r"/pull/(\d+)", m.group(1))
            j["prNumber"] = int(n.group(1)) if n else None
            self._set(jid, idx, s["status"], t, fact=("Pull request", f"#{j['prNumber']}" if n else "created"))
            if self.steps[jid][10]["status"] in ("pending", "waiting"):
                self.steps[jid][10]["sub"] = f"review and merge PR #{j['prNumber']}" if n else self.steps[jid][10]["sub"]
                self._emit_step(j, self.steps[jid][10])
            self._emit_job(j)
        if idx == 6 and re.match(r"^ALL CHECKS PASSED", line):
            n_ok = sum(1 for c in s["checks"] if c["s"] == "PASS")
            self._set(jid, idx, s["status"], t, fact=("Checks", f"{n_ok} passed"))
        if idx == 6 and line.startswith("BLOCKED:"):
            self._set(jid, idx, s["status"], t, fact=("Result", line[:70]))

    def _find_job(self, num, keys):
        for jid in reversed(self.order):
            j = self.jobs[jid]
            if j["kind"] == "ticket" and j.get("prNumber") == num:
                return jid
        for jid in reversed(self.order):
            j = self.jobs[jid]
            if j["kind"] == "ticket" and j["key"] in keys and j["status"] != "failed":
                return jid
        return None

    def _post_merge(self, line, t):
        m = RX_TRACK.search(line)
        if m:
            action, num, keys = m.group(1), int(m.group(2)), m.group(3).split(",")
            jid = self._find_job(num, keys)
            if jid:
                self._line(jid, 10, line, t, "info")
                if action == "MERGED":
                    self._set(jid, 10, "ok", t, note=f"PR #{num} merged")
                    self._set(jid, 11, "waiting", t, note="Approve the deploy in GitHub Actions: Review deployments")
                    self._job_status(jid, "waiting", t)
                elif action == "REJECTED":
                    self._set(jid, 10, "failed", t, note="PR closed without merging")
                    self._skip_rest(jid, [11, 12, 13], t)
                    self._job_status(jid, "failed", t)
                else:
                    self._fact(self.steps[jid][10], "Conflict", "resolve on GitHub, or close and re-queue")
                    self._emit_step(self.jobs[jid], self.steps[jid][10])
        m = RX_DEPLOY.search(line)
        if m:
            num, keys, old, new, note = int(m.group(1)), m.group(2).split(","), m.group(3), m.group(4), m.group(5)
            jid = self._find_job(num, keys)
            if jid:
                self._line(jid, 11 if new in ("awaiting_approval", "deploying") else 12, line, t, "info")
                if self.steps[jid][10]["status"] in ("pending", "waiting"):
                    self._set(jid, 10, "ok", t)
                if new == "awaiting_approval":
                    self._set(jid, 11, "waiting", t, note="Approve the deploy in GitHub Actions: Review deployments")
                    self._job_status(jid, "waiting", t)
                elif new == "deploying":
                    self._set(jid, 11, "ok", t)
                    self._set(jid, 12, "running", t)
                    self._job_status(jid, "running", t)
                elif new == "deployed":
                    self._set(jid, 11, "ok", t)
                    self._set(jid, 12, "ok", t, fact=("Result", note or "deployed"))
                    self._set(jid, 13, "ok", t)
                    self._job_status(jid, "ok", t)
                elif new == "failed":
                    self._set(jid, 11, "ok", t)
                    self._set(jid, 12, "failed", t, note="Deploy failed: nothing went live")
                    self._skip_rest(jid, [13], t)
                    self._job_status(jid, "failed", t)
                elif new == "no_run":
                    self._set(jid, 12, "failed", t, note=note or "No deploy run found: check GitHub Actions")
                    self._job_status(jid, "failed", t)

    # ------------------------------------------------------------------ interaction
    def prompt(self, text, t):
        """The review gate is waiting for a typed answer."""
        out = []
        jid = self.cur if self.ctx == "ticket" else None
        if jid and self.steps[jid][5]["status"] == "running":
            self._set(jid, 5, "waiting", t, note="Waiting for you: approve or reject the change")
            self._job_status(jid, "attention", t)
            self.out.append({"type": "prompt", "job": jid, "step": 5, "text": text.strip()})
        else:
            self.out.append({"type": "prompt", "job": jid, "step": self.sidx.get(jid) if jid else None, "text": text.strip()})
        out, self.out = self.out, []
        return out

    def answered(self, answer, t):
        jid = self.cur if self.ctx == "ticket" else None
        if jid and self.steps[jid][5]["status"] == "waiting":
            self._set(jid, 5, "running", t, note="")
            self.steps[jid][5]["note"] = ""
            self._emit_step(self.jobs[jid], self.steps[jid][5])
            self._job_status(jid, "running", t)
            self._line(jid, 5, f"> you answered: {answer}", t, "you")
        self.out.append({"type": "prompt_clear"})
        out, self.out = self.out, []
        return out

    def finish(self, code, t, stopped_by_user=False):
        """The process ended. Close anything still open."""
        for jid in list(self.order):
            j = self.jobs[jid]
            if j["status"] in ("running", "attention"):
                if jid == "poll":
                    self._set(jid, 1, "ok", t)
                    self._job_status(jid, "ok", t)
                    continue
                reason = "Stopped by you" if stopped_by_user else f"The watcher exited (code {code})"
                for s in self.steps[jid].values():
                    if s["status"] in ("running", "waiting"):
                        self._set(jid, s["idx"], "failed", t, note=reason)
                self._skip_rest(jid, [i for i in self.steps[jid]], t)
                j["note"] = reason
                self._job_status(jid, "failed", t)
        out, self.out = self.out, []
        return out


# --------------------------------------------------------------------------- hub (process + events)
class Hub:
    def __init__(self, agent_dir, token, python=None):
        self.agent_dir = Path(agent_dir).resolve()
        self.repo_root = self.agent_dir.parent
        self.logs = self.repo_root / "logs"
        self.token = token
        self.python = python or sys.executable
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.events = []
        self.seq = 0
        self.proc = None
        self.parser = None
        self.status = "stopped"
        self.exit_code = None
        self.prompt = None
        self.session_file = None
        self.stopping = False
        self.hint = ""
        self.started_at = None
        self.util_n = 0
        self.utils = {}
        self._q = None
        self.summaries = {}
        self.secrets = secret_values()

    # ---------------------------------------------------------------- events
    def emit(self, evs):
        if not evs:
            return
        with self.cond:
            for ev in evs:
                self.seq += 1
                ev["seq"] = self.seq
                self.events.append(ev)
            self.cond.notify_all()

    def wait_events(self, after, timeout=15.0):
        with self.cond:
            if self.seq <= after:
                self.cond.wait(timeout)
            if self.seq <= after:
                return [], after
            evs = [e for e in self.events if e["seq"] > after]
            return evs, self.seq

    def snapshot(self):
        with self.lock:
            return {"type": "state", "status": self.status, "kind": "watcher" if self.proc else None,
                    "pid": self.proc.pid if self.proc else None, "started": self.started_at, "exit": self.exit_code,
                    "prompt": self.prompt, "hint": self.hint, "typical": self.typical(),
                    "active": self._active_key(), "dashboard": (self.logs / "pipeline-dashboard.html").exists()}

    def _push_state(self):
        self.emit([self.snapshot()])

    def _active_key(self):
        if not self.parser:
            return None
        if self.parser.ctx == "ticket" and self.parser.cur:
            return self.parser.jobs[self.parser.cur]["key"]
        return None

    def context(self):
        env = os.environ.get
        return {"repo": env("GITHUB_REPO", ""), "project": env("JIRA_PROJECT_KEY", "") or env("JIRA_PROJECT", ""),
                "org": env("SF_TARGET_ORG", ""), "jiraBase": env("JIRA_BASE_URL", "").rstrip("/"),
                "dryRun": env("AGENT_DRY_RUN", "false").lower() == "true",
                "enabled": env("AGENT_ENABLED", "true").lower() == "true", "agent": str(self.agent_dir.name),
                "base": env("GITHUB_BASE_BRANCH", "main"),
                "pollSeconds": int(env("WATCH_POLL_INTERVAL_SECONDS", "300") or 300)}

    def typical(self):
        out = dict(DEFAULT_TYPICAL)
        try:
            vals = {}
            for line in (self.logs / "run-events.jsonl").read_text(encoding="utf-8").splitlines()[-4000:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("status") == "ok" and isinstance(e.get("duration_s"), (int, float)) and e.get("duration_s") > 0 \
                        and e.get("step") in DEFAULT_TYPICAL:
                    vals.setdefault(e["step"], []).append(e["duration_s"])
            for k, v in vals.items():
                out[k] = max(1.0, float(statistics.median(v[-10:])))
        except OSError:
            pass
        return out

    # ---------------------------------------------------------------- processes
    def _spawn(self, script, stdin):
        path = self.agent_dir / script
        if not path.exists():
            raise FileNotFoundError(f"{script} not found in {self.agent_dir}")
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "WATCH_PROGRESS": "false", "RUN_CONSOLE": "1"})
        kw = {}
        if os.name != "nt":
            kw["start_new_session"] = True
        return subprocess.Popen([self.python, "-u", script], cwd=str(self.agent_dir), env=env,
                                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)

    def start_watcher(self):
        with self.lock:
            if self.proc is not None:
                raise RuntimeError("The watcher is already running.")
            if any(u["proc"].poll() is None for u in self.utils.values() if u["name"] == "preflight"):
                raise RuntimeError("Wait for the pre-flight check to finish.")
            self.events = []          # sequence numbers keep rising so open pages stay in step
            self.parser = Parser(summary_of=lambda k: self.summaries.get(k, ""))
            self.exit_code, self.prompt, self.hint, self.stopping = None, None, "", False
            popen = self._spawn("watch_queue.py", subprocess.PIPE)
            popen.prompt_open = False
            self.proc = popen
            self.status = "starting"
            self.started_at = now_ms()
            d = self.logs / "run-console"
            d.mkdir(parents=True, exist_ok=True)
            self.session_file = d / time.strftime("session-%Y%m%d-%H%M%S.jsonl")
            self._log_session("meta", "$ python watch_queue.py")
        self.emit([{"type": "reset"}])
        t0 = now_ms()
        self.emit(self.parser.begin(t0) + self.parser.raw_line("startup", 1, "$ python watch_queue.py", t0, "cmd"))
        self._push_state()
        threading.Thread(target=self._reader, args=(popen, "watcher"), daemon=True).start()

    def stop(self):
        with self.lock:
            p = self.proc
            if p is None:
                raise RuntimeError("The watcher is not running.")
            self.stopping = True
            self.status = "stopping"
        self._push_state()
        kill_tree(p.pid)

    def answer(self, ans):
        ans = "y" if str(ans).lower().startswith("y") else "n"
        with self.lock:
            p = self.proc
            if p is None or self.prompt is None:
                raise RuntimeError("Nothing is waiting for an answer.")
            self.prompt = None
            p.prompt_open = False
        t = now_ms()
        self._log_session("you", ans, t)
        try:
            p.stdin.write((ans + "\n").encode())
            p.stdin.flush()
        except (OSError, ValueError):
            raise RuntimeError("Could not send the answer: the watcher is no longer running.")
        self.emit(self.parser.answered(ans, t))
        self._push_state()

    def run_util(self, name):
        if name not in ALLOWED_UTILS:
            raise ValueError("Unknown action.")
        script, label = ALLOWED_UTILS[name]
        with self.lock:
            if name == "preflight" and self.proc is not None:
                raise RuntimeError("Stop the watcher before running the pre-flight check.")
            if any(u["name"] == name and u["proc"].poll() is None for u in self.utils.values()):
                raise RuntimeError(f"{label} is already running.")
            self.util_n += 1
            jid = f"util-{self.util_n}"
            popen = self._spawn(script, subprocess.DEVNULL)
            popen.prompt_open = False
            self.utils[jid] = {"name": name, "proc": popen, "checks": []}
        t = now_ms()
        job = {"id": jid, "kind": "util", "key": None, "title": label, "summary": "", "status": "running", "start": t,
               "end": None, "credits": 0, "pr": None, "prNumber": None, "branch": None, "alert": [], "note": "", "total": None,
               "order": 10_000 + self.util_n}
        step = {"idx": 1, "label": label, "sub": script, "status": "running", "start": t, "end": None, "facts": [],
                "checks": [], "human": False}
        self.emit([{"type": "job", "job": copy.deepcopy(job)}, {"type": "step", "job": jid, "step": copy.deepcopy(step)},
                   {"type": "line", "job": jid, "step": 1, "n": 1, "t": t, "text": f"$ python {script}", "lvl": "cmd"}])
        self.utils[jid].update({"job": job, "step": step, "n": 1})
        threading.Thread(target=self._reader, args=(popen, jid), daemon=True).start()

    # ---------------------------------------------------------------- reading output
    def _log_session(self, kind, text, t=None):
        if self.session_file is None:
            return
        try:
            with open(self.session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": t or now_ms(), "p": kind, "text": text}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _reader(self, popen, who):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = popen.stdout.fileno()
        buf = ""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += dec.decode(chunk).replace("\r\n", "\n")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if "\r" in line:
                    parts = [p for p in line.split("\r") if p.strip()]
                    line = parts[-1] if parts else ""
                self._on_line(who, popen, line)
            if who == "watcher" and buf and RX_PROMPT.search(buf) and not popen.prompt_open:
                popen.prompt_open = True
                self._on_prompt(popen, buf)
        if buf:
            self._on_line(who, popen, buf)
        code = popen.wait()
        self._on_exit(who, popen, code)

    def _on_line(self, who, popen, line):
        line = redact(line, self.secrets)
        t = now_ms()
        if who == "watcher":
            self._log_session("watcher", line, t)
            popen.prompt_open = False if not RX_PROMPT.search(line) else popen.prompt_open
            before = self._active_key()
            evs = self.parser.feed(line, t)
            self.emit(evs)
            changed = self._active_key() != before
            with self.lock:
                if self.status == "starting":
                    self.status, changed = "running", True
            if changed or any(e["type"] in ("job", "prompt", "prompt_clear") for e in evs):
                self._push_state()
        else:
            u = self.utils.get(who)
            if not u:
                return
            u["n"] += 1
            evs = [{"type": "line", "job": who, "step": 1, "n": u["n"], "t": t, "text": line, "lvl": Parser._lvl(line)}]
            m = RX_CHECK.match(line)
            if m:
                u["step"]["checks"].append({"s": m.group(1), "name": re.sub(r"\s{2,}", " - ", m.group(2), count=1)})
                evs.append({"type": "step", "job": who, "step": copy.deepcopy(u["step"])})
            self.emit(evs)

    def _on_prompt(self, popen, text):
        t = now_ms()
        self._log_session("prompt", text, t)
        evs = self.parser.prompt(text, t)
        with self.lock:
            self.prompt = {"text": text.strip(), "job": self.parser.cur, "key": self._active_key()}
        self.emit(evs)
        self._push_state()

    def _on_exit(self, who, popen, code):
        t = now_ms()
        if who == "watcher":
            with self.lock:
                stopped = self.stopping
            self._log_session("exit", str(code), t)
            self.emit(self.parser.finish(code, t, stopped_by_user=stopped))
            with self.lock:
                active = [j for j in self.parser.jobs.values() if j["kind"] == "ticket" and j["status"] == "failed"
                          and j.get("note", "").startswith(("Stopped by you", "The watcher exited"))]
                if active:
                    keys = ", ".join(sorted({j["key"] for j in active}))
                    self.hint = (f"{keys} was in progress. Its Jira label is probably still ai-locked: set it back to ai-ready "
                                 "only to retry. The next start returns the repository to main by itself.")
                self.proc, self.status, self.prompt = None, "stopped", None
                self.exit_code = None if stopped else code      # a deliberate stop is not an error
                self.stopping = False
            self._push_state()
        else:
            u = self.utils.get(who)
            if not u:
                return
            step, job = u["step"], u["job"]
            step["status"] = "ok" if code == 0 and not any(c["s"] == "FAIL" for c in step["checks"]) else "failed"
            step["end"] = t
            job["status"], job["end"] = step["status"], t
            self.emit([{"type": "step", "job": who, "step": copy.deepcopy(step)}, {"type": "job", "job": copy.deepcopy(job)}])
            self._push_state()

    # ---------------------------------------------------------------- queue / sessions
    def queue(self):
        with self.lock:
            if self._q and time.time() - self._q[0] < 20:
                return self._q[1]
        try:
            if str(self.agent_dir) not in sys.path:
                sys.path.insert(0, str(self.agent_dir))
            import read_queue
            items = []
            for i in read_queue.fetch_queue():
                f = i.get("fields") or {}
                items.append({"key": i.get("key"), "summary": f.get("summary") or i.get("summary") or "",
                              "type": (f.get("issuetype") or {}).get("name", "")})
            res = {"ok": True, "items": items}
            for it in items:
                self.summaries[it["key"]] = it["summary"]
        except (Exception, SystemExit) as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}", "items": []}
        with self.lock:
            self._q = (time.time(), res)
        return res

    def sessions(self):
        out = []
        d = self.logs / "run-console"
        if d.exists():
            for f in sorted(d.glob("session-*.jsonl"), reverse=True)[:20]:
                try:
                    txt = f.read_text(encoding="utf-8")
                except OSError:
                    continue
                keys = sorted(set(re.findall(r'Claimed: ([A-Z]+-\d+)', txt)))
                first = txt.splitlines()[0] if txt else "{}"
                try:
                    started = json.loads(first).get("t")
                except ValueError:
                    started = None
                out.append({"name": f.name, "started": started, "tickets": keys, "size": f.stat().st_size})
        return out

    def replay(self, name):
        if not re.fullmatch(r"session-[0-9\-]+\.jsonl", name or ""):
            raise ValueError("Bad session name.")
        f = self.logs / "run-console" / name
        p = Parser(summary_of=lambda k: self.summaries.get(k, ""))
        evs, last_t = [], 0
        for raw in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            last_t = r.get("t", last_t)
            if r["p"] == "watcher":
                evs += p.feed(r["text"], r["t"])
            elif r["p"] == "meta":
                evs += p.begin(r["t"]) + p.raw_line("startup", 1, r["text"], r["t"], "cmd")
            elif r["p"] == "prompt":
                evs += p.prompt(r["text"], r["t"])
            elif r["p"] == "you":
                evs += p.answered(r["text"], r["t"])
            elif r["p"] == "exit":
                evs += p.finish(int(r["text"] or 0), r["t"], stopped_by_user=False)
        for i, e in enumerate(evs, 1):
            e["seq"] = i
        return evs

    def transcript(self):
        with self.lock:
            lines = [e for e in self.events if e["type"] == "line"]
        return "\n".join(f'{time.strftime("%H:%M:%S", time.localtime(e["t"] / 1000))}  [{e["job"]}/{e["step"]}]  {e["text"]}' for e in lines)


def kill_tree(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "RunConsole"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    @property
    def hub(self):
        return self.server.hub

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, need_token=True, post=False):
        host = self.headers.get("Host", "")
        if host not in self.server.allowed_hosts:
            self._send(403, {"error": "Host not allowed"})
            return False
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in self.server.allowed_hosts}:
                self._send(403, {"error": "Origin not allowed"})
                return False
        if need_token:
            q = parse_qs(urlparse(self.path).query)
            tok = self.headers.get("X-Console-Token") or (q.get("t") or [""])[0]
            if not secrets.compare_digest(tok.encode(), self.hub.token.encode()):
                self._send(401, {"error": "Missing or wrong token. Open the address printed by run_console.py."})
                return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                if not self._guard(need_token=False):
                    return
                html = (self.hub.agent_dir / "run_console.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy":
                                  "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'self'"})
            if not self._guard():
                return
            if u.path == "/api/state":
                return self._send(200, {"state": self.hub.snapshot(), "context": self.hub.context(), "seq": self.hub.seq})
            if u.path == "/api/stream":
                return self._stream(int((q.get("after") or ["0"])[0] or 0))
            if u.path == "/api/queue":
                return self._send(200, self.hub.queue())
            if u.path == "/api/sessions":
                return self._send(200, {"items": self.hub.sessions()})
            if u.path == "/api/session":
                return self._send(200, {"events": self.hub.replay((q.get("name") or [""])[0])})
            if u.path == "/api/transcript":
                return self._send(200, self.hub.transcript(), "text/plain; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="run-console-transcript.txt"'})
            if u.path == "/dashboard":
                f = self.hub.logs / "pipeline-dashboard.html"
                if not f.exists():
                    return self._send(404, {"error": "No dashboard yet. Use Rebuild dashboard."})
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return self._send(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._guard(post=True):
            return
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        try:
            if u.path == "/api/start":
                self.hub.start_watcher()
            elif u.path == "/api/stop":
                self.hub.stop()
            elif u.path == "/api/answer":
                self.hub.answer(body.get("answer", "n"))
            elif u.path == "/api/util":
                self.hub.run_util(body.get("name", ""))
            else:
                return self._send(404, {"error": "Not found"})
            return self._send(200, {"ok": True})
        except (RuntimeError, ValueError, FileNotFoundError) as e:
            return self._send(409, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _stream(self, after):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = after
        try:
            while not self.server.stopping:
                evs, last = self.hub.wait_events(last, timeout=10.0)
                if evs:
                    for ev in evs:
                        self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": hb\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, hub):
        self.hub = hub
        self.stopping = False
        self.allowed_hosts = {f"127.0.0.1:{addr[1]}", f"localhost:{addr[1]}"}
        super().__init__(addr, Handler)


def make_server(agent_dir, port=DEFAULT_PORT, token=None, tries=20):
    hub = Hub(agent_dir, token or secrets.token_urlsafe(16))
    for p in range(port, port + tries):
        try:
            srv = Server(("127.0.0.1", p), hub)
            return srv, hub
        except OSError:
            continue
    raise SystemExit(f"No free port between {port} and {port + tries - 1}.")


def main():
    ap = argparse.ArgumentParser(description="Local run console for the AI delivery pipeline")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--agent-dir", default=str(Path(__file__).resolve().parent))
    a = ap.parse_args()
    agent_dir = Path(a.agent_dir).resolve()
    if not (agent_dir / "run_console.html").exists():
        raise SystemExit("run_console.html must be in the same folder as run_console.py.")
    load_env(agent_dir.parent / ".env")
    srv, hub = make_server(agent_dir, a.port)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/#t={hub.token}"
    print("=" * 60)
    print("RUN CONSOLE  (local only; Ctrl+C here stops the console)")
    print("=" * 60)
    print(f"Open: {url}")
    print("The token in the address is private to this run. Stopping the console also stops the watcher it started.")
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping = True
        if hub.proc is not None:
            kill_tree(hub.proc.pid)
        srv.server_close()
        print("Run console stopped.")


if __name__ == "__main__":
    main()
