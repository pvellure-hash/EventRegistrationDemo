"""Step 0: Long-running watcher. NEW: runs automated pre-flight checks
(preflight.py) once at startup as a hard gate, and a lightweight re-sync
check before every ticket, so the agent never starts fixing code against a
stale or diverged local repo.

Startup sequence:
    preflight.run_all() — Repository state (incl. fast-forward sync to the
    latest origin/main), Copilot CLI, credentials, Salesforce. If ANY check
    fails, the watcher notifies and REFUSES to start polling at all.

Per-ticket sequence (after claim_ticket.py claims one):
    preflight.check_git_state() — a lighter re-check (clean tree, correct
    branch, fast-forward-sync to latest origin/main) run immediately before
    prepare_fix.py cuts a new branch. This matters because the watcher may
    run for hours; someone could merge a PR in the meantime. If this fails,
    only THIS ticket is skipped (with a notification) - not the whole
    watcher - since the next ticket may well be fine.

    claim_ticket -> [re-sync check] -> prepare_fix -> invoke_copilot
        (headless CLI fix) -> review_gate (BLOCKS for y/n)
        -> validate_fix -> create_pr -> update_jira

Sequential, one ticket at a time, start to finish - no overlap by design
(blocking subprocess calls). Respects AGENT_ENABLED / AGENT_DRY_RUN.

Notifications: any step failure, a failed pre-flight start, or a
skipped-ticket re-sync failure calls notify.notify_failure(...), which
shows a Windows toast, logs to logs/notifications.jsonl, and (if enabled)
emails everyone in agent/recipients.json subscribed to that category.

.env additions needed:
    WATCH_POLL_INTERVAL_SECONDS=300
    NOTIFY_ENABLED=true / NOTIFY_TOAST_ENABLED=true / NOTIFY_EMAIL_ENABLED=...
See preflight.py and notify.py for full detail.
"""
import os
import sys
import time
import subprocess
import jira_client as jc
import notify
import preflight

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_INTERVAL = int(os.getenv("WATCH_POLL_INTERVAL_SECONDS", "300"))

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


def resync_before_ticket(key):
    """Lightweight re-check, immediately before cutting a fix branch: is the
    local repo clean, on the base branch, and fast-forwarded to the exact
    latest origin/main? If not, skip this ticket (not the whole watcher)
    and notify - the next poll cycle will retry it."""
    print(f"\n--- Re-sync check before {key} ---")
    results = preflight.check_git_state()
    ok = all(passed for _, passed, _ in results)
    for name, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}  {'' if passed else detail}")
    if not ok:
        reason = preflight.failure_summary(results)
        print(f"[{key}] Skipping this cycle - repo not in a safe state to branch from.")
        jc.audit("watch_queue", key, f"skipped - resync check failed: {reason}", "failed")
        notify.notify_failure(step="preflight", ticket=key, reason=reason)
    return ok


def process_ticket(key):
    print("\n" + "#" * 60)
    print(f"# Processing {key}")
    print("#" * 60)

    if not resync_before_ticket(key):
        return False

    for script in PIPELINE_STEPS:
        ok = run_step(script, key)
        if not ok:
            print(f"[{key}] Stopped at {script} (failed, auto-blocked, or you rejected it).")
            jc.audit("watch_queue", key, f"stopped at {script}", "failed")
            notify.notify_failure(step=script, ticket=key, reason=f"{script} exited non-zero")
            return False
    print(f"[{key}] Completed through Jira update. Draft PR awaiting human review/merge.")
    jc.audit("watch_queue", key, "completed full pipeline", "ok")
    return True


def poll_loop():
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    print("Running pre-flight checks before starting the watcher...\n")
    ok, results = preflight.run_all(verbose=True)
    if not ok:
        reason = preflight.failure_summary(results)
        jc.audit("watch_queue", "-", f"pre-flight failed at startup: {reason}", "failed")
        notify.notify_failure(step="preflight", ticket="-", reason=reason)
        sys.exit("\nPre-flight checks failed. Fix the items above, then restart watch_queue.py.")

    print(f"\nWatching project {jc.PROJECT} for ai-ready tickets every "
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
