"""Step 0: Long-running watcher. Polls Jira every WATCH_POLL_INTERVAL_SECONDS
and, whenever claim_ticket.py's own triage logic claims a ticket, runs the
rest of the pipeline automatically for it:

    claim_ticket -> prepare_fix -> invoke_copilot (headless CLI fix)
        -> review_gate (BLOCKS here for your y/n)
        -> validate_fix -> create_pr -> update_jira

Start once and leave running:  python watch_queue.py
Stop any time with Ctrl+C - it finishes the step currently running, then
exits (no state is lost; a ticket stuck mid-pipeline just gets picked back
up where validate/create_pr/update_jira's existing idempotency already
handles reruns, exactly as it does today when you run steps by hand).

Each step is invoked as its own subprocess, same as create_pr.py already
does for validate_fix.py - no step's internal logic is duplicated here.
Processes ONE ticket fully (success, rejection, or failure) before polling
again, since claim_ticket.py itself only ever claims one ticket per run.

Respects AGENT_ENABLED / AGENT_DRY_RUN like every other step. With
AGENT_DRY_RUN=true this is safe to leave running to watch what it WOULD do.

NEW: on ANY step failure (non-zero exit) or an unhandled poll-level error,
calls notify.notify_failure(...) - this shows a Windows toast, appends a
structured record to logs/notifications.jsonl, and (if you've completed the
optional email setup in notify.py) emails you with exactly which step
failed and what to check next. See notify.py for setup.

.env additions needed:
    WATCH_POLL_INTERVAL_SECONDS=300
    NOTIFY_ENABLED=true
    NOTIFY_TOAST_ENABLED=true
    NOTIFY_EMAIL_ENABLED=false   (true once optional email setup is done)
    NOTIFY_EMAIL_TO=you@yourcompany.com
"""
import os
import sys
import time
import subprocess
import jira_client as jc
import notify

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_INTERVAL = int(os.getenv("WATCH_POLL_INTERVAL_SECONDS", "300"))

# Steps run, in order, after claim_ticket.py has already claimed a ticket.
# review_gate.py is interactive (blocks on input()) - it is NOT given
# capture_output=True so your terminal's stdin/stdout pass straight through.
PIPELINE_STEPS = [
    "prepare_fix.py",
    "invoke_copilot.py",
    "review_gate.py",
    "validate_fix.py",
    "create_pr.py",
    "update_jira.py",
]


def run_step(script, key):
    path = os.path.join(AGENT_DIR, script)
    print(f"\n>>> {script} {key}")
    r = subprocess.run([sys.executable, path, key], cwd=AGENT_DIR)
    return r.returncode == 0


def claim_next():
    """Runs claim_ticket.py exactly as you would by hand. It already does
    the full triage loop (needs-info / blocked / claim-one) itself; we just
    parse its final 'Claimed: <KEY or none>' line to know what to do next."""
    r = subprocess.run([sys.executable, os.path.join(AGENT_DIR, "claim_ticket.py")],
                       cwd=AGENT_DIR, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(r.stdout)
    if r.stderr.strip():
        print(r.stderr, file=sys.stderr)
    for line in r.stdout.splitlines():
        if line.strip().startswith("Claimed:"):
            val = line.split("Claimed:", 1)[1].strip()
            return None if val == "none" else val
    return None


def process_ticket(key):
    print("\n" + "#" * 60)
    print(f"# Processing {key}")
    print("#" * 60)
    for script in PIPELINE_STEPS:
        ok = run_step(script, key)
        if not ok:
            print(f"[{key}] Stopped at {script} (failed, auto-blocked, or you rejected it).")
            jc.audit("watch_queue", key, f"stopped at {script}", "failed")
            notify.notify_failure(step=script, ticket=key,
                                   reason=f"{script} exited non-zero")
            return False
    print(f"[{key}] Completed through Jira update. Draft PR awaiting human review/merge.")
    jc.audit("watch_queue", key, "completed full pipeline", "ok")
    return True


def poll_loop():
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")
    print(f"Watching project {jc.PROJECT} for ai-ready tickets every "
          f"{POLL_INTERVAL}s (Ctrl+C to stop)...")
    while True:
        try:
            key = claim_next()
            if key:
                process_ticket(key)
            else:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"Nothing claimed this cycle. Sleeping {POLL_INTERVAL}s.")
        except Exception as e:
            print(f"Error during poll cycle: {e}")
            jc.audit("watch_queue", "-", str(e), "failed")
            notify.notify_failure(step="poll_loop", ticket="-", reason=str(e))
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")
