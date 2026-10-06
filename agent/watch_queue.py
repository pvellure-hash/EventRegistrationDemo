"""Step 0: Long-running watcher.
Startup (hard gate - watcher refuses to start if any FAIL):
    git_sync.sync_base()         G11: if the repo was left on a fix branch (e.g. the
                                 watcher was stopped mid-ticket), return to main and
                                 fast-forward BEFORE preflight runs. Never discards work:
                                 a dirty tree is left untouched and preflight reports it.
    preflight.run_all()          repo state, Copilot CLI, credentials, Salesforce
    security_checks.run_all()    PHASE 0: production-org guard, branch protection,
                                 GitHub token scope
Every poll cycle, BEFORE claiming a ticket:
    track_prs()                  STEP 2: pr_tracker.track_all() reads every open/closed
                                 ticket PR on GitHub (no AI, $0) and update_jira acts on
                                 new transitions: merged -> Done + ai-merged; closed ->
                                 ai-rejected; conflict -> comment + alert; behind ->
                                 GitHub "Update branch"; open too long -> reminder.
                                 Then release_waiting() returns ai-waiting tickets to
                                 ai-ready when their PR/blocker is gone. Fails open:
                                 a GitHub/Jira hiccup never stops the watcher.
                                 v7.1: pr_tracker also writes logs/pr-status.json and the
                                 pipeline dashboard is rebuilt right away, so a merge shows
                                 on the dashboard within one poll.
    code_index.build_or_update() PHASE 1: incremental re-index (fast, no AI)
    guards.check_budget()        PHASE 0: monthly budget circuit-breaker
    guards.check_daily_cap()     PHASE 0: daily ticket cap
  If either guard blocks, nothing is claimed - tickets stay ai-ready in Jira.
Per ticket:
    new run_id (shared with child steps via AGENT_RUN_ID)
    git_sync.sync_base() (G11 self-heal) + preflight.check_git_state()
      + production-org re-check
    prepare_fix -> invoke_copilot -> review_gate (human y/n) -> validate_fix
      -> create_pr -> update_jira
      STEP 2: prepare_fix exit code 3 = "waiting" (ticket overlaps an open PR,
      labelled ai-waiting, $0) - recorded as outcome "waiting", no failure alert.
      STEP 3: prepare_fix may batch related bugs into this run (batching.py). If the run
      does not end with a PR, batching.release_members() returns the other tickets to
      ai-ready before the repo goes back to main.
      v7.1: each step (except the human review gate) shows a live one-line status in this
      window - step N of 6, elapsed time against that step's typical time, and for the AI
      step the files edited / commits made so far (progress.py). WATCH_PROGRESS=false
      turns it off.
    finally: back to main + fast-forward (G11), dashboards, re-index.
STEP 2 continuous mode: after a ticket ends with a PR or "waiting", the watcher polls again
  after WATCH_CONTINUE_SECONDS (default 5) instead of the full interval.
.env: WATCH_POLL_INTERVAL_SECONDS=300, WATCH_CONTINUE_SECONDS=5, MONTHLY_BUDGET_USD,
      MAX_TICKETS_PER_DAY, TICKET_CREDIT_CEILING, SF_PACKAGE_DIR, NOTIFY_* (see notify.py),
      PR_REMIND_HOURS, PR_AUTO_UPDATE_BRANCH, OVERLAP_GUARD_ENABLED, JIRA_STATUS_DONE,
      WATCH_PROGRESS, WATCH_PROGRESS_INTERVAL, COPILOT_CLI_TIMEOUT_MINUTES
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
import git_sync
import pr_tracker
import update_jira
import batching
import progress
try:
    import generate_cost_dashboard as dashboard
except Exception:
    dashboard = None

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
POLL_INTERVAL = int(os.getenv("WATCH_POLL_INTERVAL_SECONDS", "300"))
CONTINUE_INTERVAL = int(os.getenv("WATCH_CONTINUE_SECONDS", "5"))
BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main").strip()
CLI_TIMEOUT_S = int(os.getenv("COPILOT_CLI_TIMEOUT_MINUTES", "15")) * 60
EXIT_WAITING = 3   # prepare_fix.py: ticket overlaps an open PR -> ai-waiting

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


def refresh_pipeline_dashboard(quiet=False):
    """PHASE 2: rebuild the command-center dashboard. Never blocks the loop."""
    try:
        r = subprocess.run([sys.executable, os.path.join(AGENT_DIR, "generate_pipeline_dashboard.py")],
                           cwd=AGENT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            out = r.stdout.strip().splitlines()
            if out and not quiet:
                print(f"  {out[-1]}")
        else:
            print(f"  [pipeline-dashboard] WARNING (non-blocking): exit {r.returncode}: {r.stderr.strip()[:200]}")
    except Exception as e:
        print(f"  [pipeline-dashboard] WARNING (non-blocking): {e}")


def refresh_dashboard(key, when=""):
    if dashboard:
        try:
            stats = dashboard.generate_dashboard(quiet=True)
            if stats:
                print(f"  [dashboard] refreshed ({when}): {stats['n_tickets']} tickets, ${stats['total_cost']:.2f}")
        except Exception as e:
            print(f"  [dashboard] WARNING (non-blocking): {e}")
    refresh_pipeline_dashboard()


def refresh_index(when=""):
    """PHASE 1: incremental code-index rebuild. No AI cost. Fails open."""
    try:
        idx = code_index.build_or_update(REPO_ROOT)
        print(f"  [index] refreshed ({when}): {idx['file_count']} files, "
              f"{idx['reparsed_this_run']} re-parsed")
        return idx
    except Exception as e:
        print(f"  [index] WARNING (non-blocking): could not refresh index: {e}")
        return None


def sync_to_base(key, when):
    """G11: put the repo back on main at the latest origin/main. Never discards work."""
    try:
        ok, msg = git_sync.sync_base(BASE_BRANCH)
    except Exception as e:
        ok, msg = False, f"git_sync raised: {e}"
    print(f"  [git-sync] {'OK  ' if ok else 'STOP'} ({when}) {msg}")
    events.emit_event(key, "git_sync", "ok" if ok else "failed", actor="system",
                      reason_code=None if ok else "sync_base", detail=redact.redact(msg))
    if not ok:
        jc.audit("watch_queue", key, f"git sync ({when}) did not run: {msg}", "failed")
        notify.notify_failure(step=f"git_sync ({when})", ticket=key, reason=redact.redact(msg))
    return ok


def track_prs():
    """STEP 2: follow open PRs and release waiting tickets. No AI, fails open.
    v7.1: rebuilds the dashboard after every poll (quietly unless something changed)."""
    try:
        evs, open_prs = pr_tracker.track_all(apply=not jc.DRY_RUN)
    except Exception as e:
        print(f"  [pr-tracker] WARNING (non-blocking): could not read PRs: {e}")
        return
    print(f"  [pr-tracker] open ticket PRs: {', '.join('#%s' % n for n in sorted(open_prs)) or 'none'}"
          f" | new updates: {len(evs)}")
    for ev in evs:
        print(f"  [pr-tracker] {ev['action']:<8} PR #{ev['number']} {','.join(ev['keys'])} {ev.get('detail', '')}")
        update_jira.on_pr_event(ev)
    released = []
    try:
        released = update_jira.release_waiting(open_prs)
        if released:
            print(f"  [pr-tracker] released to ai-ready: {', '.join(released)}")
    except Exception as e:
        print(f"  [pr-tracker] WARNING (non-blocking): release_waiting failed: {e}")
    refresh_pipeline_dashboard(quiet=not (evs or released))


def run_step(script, step, actor, key):
    names = [s for _, s, _ in PIPELINE_STEPS]
    index, total = (names.index(step) + 1 if step in names else 0), len(names)
    print(f"\n>>> [{index}/{total}] {script} {key}")
    events.emit_event(key, step, "started", actor=actor)
    t0 = time.time()
    cmd = [sys.executable, os.path.join(AGENT_DIR, script), key]
    if progress.ENABLED:
        rc = progress.run_with_progress(cmd, AGENT_DIR, index, total, step,
                                        interactive=(actor == "human"), repo_root=REPO_ROOT,
                                        base=BASE_BRANCH,
                                        timeout_s=CLI_TIMEOUT_S if step == "agent_step" else None)
    else:
        rc = subprocess.run(cmd, cwd=AGENT_DIR).returncode
    ok = rc == 0
    waiting = (script == "prepare_fix.py" and rc == EXIT_WAITING)
    events.emit_event(key, step, "ok" if ok else ("blocked" if waiting else "failed"), actor=actor,
                      duration_s=round(time.time() - t0, 1),
                      reason_code=None if ok else ("open_pr_overlap" if waiting else f"exit_{rc}"))
    if script == "invoke_copilot.py":
        refresh_dashboard(key, "after agent")
    return ok, step, rc


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
    sync_to_base(key, "before ticket")
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
    """Returns the outcome string: pr_open, waiting, blocked, or a failure outcome."""
    os.environ["AGENT_RUN_ID"] = events.new_run_id(key)
    print("\n" + "#" * 60 + f"\n# Processing {key}  (run {os.environ['AGENT_RUN_ID']})\n" + "#" * 60)
    events.emit_event(key, "run", "started", actor="system")
    t0 = time.time()
    outcome = "pr_open"
    try:
        if not resync_before_ticket(key):
            outcome = "blocked"
            return outcome
        for script, step, actor in PIPELINE_STEPS:
            ok, step, rc = run_step(script, step, actor, key)
            if ok:
                continue
            if step == "prepare" and rc == EXIT_WAITING:
                outcome = "waiting"
                print(f"[{key}] Waiting on an open PR (ai-waiting). No AI spend. Moving on.")
                jc.audit("watch_queue", key, "waiting on open PR", "skipped")
                return outcome
            outcome = OUTCOME_ON_FAIL.get(step, "failed")
            print(f"[{key}] Stopped at {script}.")
            jc.audit("watch_queue", key, f"stopped at {script}", "failed")
            notify.notify_failure(step=script, ticket=key, reason=f"{script} exited non-zero")
            return outcome
        print(f"[{key}] Completed in {progress.fmt_dur(time.time() - t0)}. "
              f"Draft PR awaiting human review/merge. Moving on to the next ticket.")
        jc.audit("watch_queue", key, "completed full pipeline", "ok")
        return outcome
    finally:
        events.emit_event(key, "run", "ok" if outcome in ("pr_open", "waiting") else "failed",
                          outcome=outcome, duration_s=round(time.time() - t0, 1))
        if outcome not in ("pr_open", "waiting"):
            try:
                released = batching.release_members(key, outcome)
                if released:
                    print(f"  [batch] released back to ai-ready: {', '.join(released)}")
            except Exception as e:
                print(f"  [batch] WARNING (non-blocking): could not release batch members: {e}")
        sync_to_base(key, "end of ticket")
        refresh_dashboard(key, "end of ticket")
        refresh_index(when="end of ticket")
        os.environ.pop("AGENT_RUN_ID", None)


def poll_loop():
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")
    sync_to_base("-", "startup")
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
    refresh_index(when="startup")
    print(f"\nWatching {jc.PROJECT} for ai-ready tickets every {POLL_INTERVAL}s (Ctrl+C to stop)...")
    print(f"Limits: budget ${guards.MONTHLY_BUDGET_USD:.0f}/month, {guards.MAX_TICKETS_PER_DAY} tickets/day, "
          f"{guards.TICKET_CREDIT_CEILING:.0f} credits/ticket")
    while True:
        sleep_for = POLL_INTERVAL
        try:
            track_prs()
            refresh_index(when="poll cycle")
            if spending_gates_open():
                key = claim_next()
                if key:
                    outcome = process_ticket(key)
                    if outcome in ("pr_open", "waiting"):
                        sleep_for = CONTINUE_INTERVAL
                else:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Nothing claimed. Sleeping {POLL_INTERVAL}s.")
        except Exception as e:
            print(f"Error during poll cycle: {e}")
            jc.audit("watch_queue", "-", redact.redact(str(e)), "failed")
            notify.notify_failure(step="poll_loop", ticket="-", reason=redact.redact(str(e)))
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")
