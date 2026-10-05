"""Step 0: Long-running watcher.
Startup (hard gate - watcher refuses to start if any FAIL):
    preflight.run_all()          repo state, Copilot CLI, credentials, Salesforce
    security_checks.run_all()    PHASE 0: production-org guard, branch protection,
                                 GitHub token scope
Every poll cycle, BEFORE claiming a ticket:
    code_index.build_or_update() PHASE 1: incremental re-index (fast, no AI) so
                                 prepare_fix.py's localisation always sees the
                                 latest merged code, not a stale snapshot.
    guards.check_budget()        PHASE 0: monthly budget circuit-breaker
    guards.check_daily_cap()     PHASE 0: daily ticket cap
  If either guard blocks, nothing is claimed - tickets stay ai-ready in Jira,
  so no ticket is left half-processed. One alert per day per reason.
Per ticket:
    new run_id (shared with child steps via AGENT_RUN_ID)
    preflight.check_git_state() + production-org re-check
    prepare_fix -> invoke_copilot -> review_gate (human y/n) -> validate_fix
      -> create_pr -> update_jira
  Every step emits started/ok/failed run events with durations, so the
  funnel and cycle times are measured without changing the other scripts.
  The cost dashboard AND the full pipeline command-center dashboard
  (Phase 2 - generate_pipeline_dashboard.py) are both refreshed after the
  agent step and at ticket end, so either dashboard reflects every ticket
  run so far without any manual step.
.env: WATCH_POLL_INTERVAL_SECONDS=300, MONTHLY_BUDGET_USD, MAX_TICKETS_PER_DAY,
      TICKET_CREDIT_CEILING, SF_PACKAGE_DIR, NOTIFY_* (see notify.py)
"""
import os
import sys
import time
import datetime
import subprocess
import jira_client as jc
import notify
import preflight
import events
import guards
import redact
import security_checks
import code_index
try:
    import generate_cost_dashboard as dashboard
except Exception:
    dashboard = None
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
POLL_INTERVAL = int(os.getenv("WATCH_POLL_INTERVAL_SECONDS", "300"))
PIPELINE_STEPS = [
    ("prepare_fix.py", "prepare", "system"),
    ("invoke_copilot.py", "agent_step", "agent"),
    ("review_gate.py", "review", "human"),
    ("validate_fix.py", "validate", "system"),
    ("create_pr.py", "pr", "system"),
    ("update_jira.py", "jira", "system"),
]
OUTCOME_ON_FAIL = {"prepare": "blocked", "agent_step": "agent_failed", "review": "rejected_gate",
                   "validate": "failed_validation", "pr": "pr_failed", "jira": "jira_failed"}
_alerted = {}
def alert_once_per_day(reason_key, step, reason):
    today = datetime.date.today()
    if _alerted.get(reason_key) != today:
        _alerted[reason_key] = today
        notify.notify_failure(step=step, ticket="-", reason=redact.redact(reason))
def refresh_dashboard(key, when=""):
    if dashboard:
        try:
            stats = dashboard.generate_dashboard(quiet=True)
            if stats:
                print(f"  [dashboard] refreshed ({when}): {stats['n_tickets']} tickets, ${stats['total_cost']:.2f}")
        except Exception as e:
            print(f"  [dashboard] WARNING (non-blocking): {e}")
    # PHASE 2: full pipeline command-center dashboard, rebuilt from the
    # real run-events.jsonl (+ per-ticket usage/PR files) every time this
    # fires - same "never block the loop" principle as the cost dashboard
    # above, so a dashboard problem can never stop ticket processing.
    try:
        r = subprocess.run([sys.executable, os.path.join(AGENT_DIR, "generate_pipeline_dashboard.py")],
                           cwd=AGENT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            out = r.stdout.strip().splitlines()
            if out:
                print(f"  {out[-1]}")
        else:
            print(f"  [pipeline-dashboard] WARNING (non-blocking): exit {r.returncode}: {r.stderr.strip()[:200]}")
    except Exception as e:
        print(f"  [pipeline-dashboard] WARNING (non-blocking): {e}")
def refresh_index(when=""):
    """PHASE 1: incremental code-index rebuild. No AI cost - pure parsing.
    Wrapped so an indexing problem can NEVER block ticket intake; the
    localizer in prepare_fix.py already degrades to zero confidence (asks
    for more info) if the index is missing or stale, so failing open here
    is safe."""
    try:
        idx = code_index.build_or_update(REPO_ROOT)
        print(f"  [index] refreshed ({when}): {idx['file_count']} files, "
              f"{idx['reparsed_this_run']} re-parsed")
        return idx
    except Exception as e:
        print(f"  [index] WARNING (non-blocking): could not refresh index: {e}")
        return None
def run_step(script, step, actor, key):
    print(f"\n>>> {script} {key}")
    events.emit_event(key, step, "started", actor=actor)
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(AGENT_DIR, script), key], cwd=AGENT_DIR)
    ok = r.returncode == 0
    events.emit_event(key, step, "ok" if ok else "failed", actor=actor,
                      duration_s=round(time.time() - t0, 1),
                      reason_code=None if ok else f"exit_{r.returncode}")
    if script == "invoke_copilot.py":
        refresh_dashboard(key, "after agent")
    return ok, step
def claim_next():
    r = subprocess.run([sys.executable, os.path.join(AGENT_DIR, "claim_ticket.py")],
                       cwd=AGENT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout)
    if r.stderr.strip():
        print(r.stderr, file=sys.stderr)
    for line in r.stdout.splitlines():
        if line.strip().startswith("Claimed:"):
            val = line.split("Claimed:", 1)[1].strip()
            return None if val == "none" else val
    return None
def spending_gates_open():
    evs = events.read_events()
    for name, (ok, why) in (("budget", guards.check_budget(evs)), ("daily_cap", guards.check_daily_cap(evs))):
        if not ok:
            print(f"[{time.strftime('%H:%M:%S')}] Not claiming: {why}")
            jc.audit("watch_queue", "-", f"paused - {why}", "blocked")
            alert_once_per_day(name, "spending_guard", f"Watcher paused, no tickets claimed: {why}")
            return False
        if why.startswith("WARN"):
            print(f"  {why}")
            alert_once_per_day(name + "_warn", "budget_warning", why)
    return True
def resync_before_ticket(key):
    print(f"\n--- Re-checks before {key} ---")
    results = preflight.check_git_state() + security_checks.check_salesforce_org()
    for name, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}  {'' if passed else detail}")
    if all(p for _, p, _ in results):
        return True
    reason = "; ".join(f"{n}: {d}" for n, p, d in results if not p)
    events.emit_event(key, "precheck", "failed", outcome="blocked", reason_code="precheck", detail=reason)
    jc.audit("watch_queue", key, f"skipped - re-check failed: {reason}", "failed")
    notify.notify_failure(step="preflight", ticket=key, reason=redact.redact(reason))
    return False
def process_ticket(key):
    os.environ["AGENT_RUN_ID"] = events.new_run_id(key)
    print("\n" + "#" * 60 + f"\n# Processing {key}  (run {os.environ['AGENT_RUN_ID']})\n" + "#" * 60)
    events.emit_event(key, "run", "started", actor="system")
    t0 = time.time()
    outcome = "pr_open"
    try:
        if not resync_before_ticket(key):
            outcome = "blocked"
            return False
        for script, step, actor in PIPELINE_STEPS:
            ok, step = run_step(script, step, actor, key)
            if not ok:
                outcome = OUTCOME_ON_FAIL.get(step, "failed")
                print(f"[{key}] Stopped at {script}.")
                jc.audit("watch_queue", key, f"stopped at {script}", "failed")
                notify.notify_failure(step=script, ticket=key, reason=f"{script} exited non-zero")
                return False
        print(f"[{key}] Completed. Draft PR awaiting human review/merge.")
        jc.audit("watch_queue", key, "completed full pipeline", "ok")
        return True
    finally:
        events.emit_event(key, "run", "ok" if outcome == "pr_open" else "failed",
                          outcome=outcome, duration_s=round(time.time() - t0, 1))
        refresh_dashboard(key, "end of ticket")
        # PHASE 1: the PR for this ticket isn't merged yet (that's a human
        # action later), but re-indexing here is still cheap and harmless -
        # it picks up the fix branch's own committed files for free and
        # costs nothing if nothing changed. The authoritative refresh is
        # still the one at the top of each poll cycle below, which runs
        # against main after merges actually land.
        refresh_index(when="end of ticket")
        os.environ.pop("AGENT_RUN_ID", None)
def poll_loop():
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")
    print("Running pre-flight and security checks...\n")
    ok1, res1 = preflight.run_all(verbose=True)
    print()
    ok2, res2 = security_checks.run_all(verbose=True)
    if not (ok1 and ok2):
        reason = "; ".join(f"{n}: {d}" for n, p, d in res1 + res2 if not p)
        events.emit_event("-", "startup", "failed", reason_code="preflight", detail=reason)
        jc.audit("watch_queue", "-", f"startup checks failed: {reason}", "failed")
        notify.notify_failure(step="preflight", ticket="-", reason=redact.redact(reason))
        sys.exit("\nChecks failed. Fix the items above, then restart watch_queue.py.")
    events.emit_event("-", "startup", "ok")
    # PHASE 1: build the index once at startup so the very first ticket
    # claimed already has it available, rather than waiting for the first
    # poll-cycle refresh below.
    refresh_index(when="startup")
    print(f"\nWatching {jc.PROJECT} for ai-ready tickets every {POLL_INTERVAL}s (Ctrl+C to stop)...")
    print(f"Limits: budget ${guards.MONTHLY_BUDGET_USD:.0f}/month, {guards.MAX_TICKETS_PER_DAY} tickets/day, "
          f"{guards.TICKET_CREDIT_CEILING:.0f} credits/ticket")
    while True:
        try:
            refresh_index(when="poll cycle")  # PHASE 1: pick up anything merged since last cycle
            if spending_gates_open():
                key = claim_next()
                if key:
                    process_ticket(key)
                else:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Nothing claimed. Sleeping {POLL_INTERVAL}s.")
        except Exception as e:
            print(f"Error during poll cycle: {e}")
            jc.audit("watch_queue", "-", redact.redact(str(e)), "failed")
            notify.notify_failure(step="poll_loop", ticket="-", reason=redact.redact(str(e)))
        time.sleep(POLL_INTERVAL)
if __name__ == "__main__":
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")
