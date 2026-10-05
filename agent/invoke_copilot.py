"""Step 6.5: Run GitHub Copilot CLI headlessly to implement the fix.

Reads docs/ai-reports/<KEY>-prompt.md (written by prepare_fix.py) and feeds
it to `copilot -p ...` non-interactively. Respects AGENT_ENABLED and
AGENT_DRY_RUN like every other step.

PHASE 0 additions (Blueprint v4):
  Before any AI spend:
    - run lock (no two runs on the same ticket)         guards.acquire_lock
    - monthly budget circuit-breaker                    guards.check_budget
    - per-ticket credit ceiling (incl. earlier attempts) guards.check_ticket_ceiling
    - production-org guard                              security_checks.check_salesforce_org
  After the run:
    - AI credits parsed and written as a run event      events.emit_event
    - single-run overshoot of the ceiling is flagged and alerted
    - CLI log is redacted before it is written to disk
  The lock is always released (finally), even on crash or timeout.

ONE-TIME SETUP (already done per machine/repo): run `copilot` once from the
repo root and accept "trust this folder" with "remember".

.env:
    COPILOT_CLI_ENABLED=true
    COPILOT_CLI_MODEL=
    COPILOT_CLI_TIMEOUT_MINUTES=15
    TICKET_CREDIT_CEILING=60
    MONTHLY_BUDGET_USD=25

Usage: python invoke_copilot.py CLAUDE-11
"""
import os
import re
import sys
import json
import time
import shutil
import subprocess
import datetime

import jira_client as jc
import events
import guards
import redact
import security_checks

try:
    import notify
except Exception:  # notify is optional for this step
    notify = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_DIR = os.path.join(REPO_ROOT, "docs", "ai-reports")
KEY_PATTERN = re.compile(rf"^{re.escape(jc.PROJECT)}-\d+$")

CLI_ENABLED = os.getenv("COPILOT_CLI_ENABLED", "false").strip().lower() == "true"
CLI_MODEL = os.getenv("COPILOT_CLI_MODEL", "").strip()
CLI_TIMEOUT_MIN = int(os.getenv("COPILOT_CLI_TIMEOUT_MINUTES", "15"))

ALLOW_TOOLS = "read,write,shell(git:*),shell(sf:*)"
DENY_TOOLS = "shell(gh:*),shell(git push*),shell(sf * deploy*),shell(sf * org *)"

AI_CREDITS_PATTERNS = [
    re.compile(r"AI Credits\s*[:\-]?\s*([\d.]+)", re.IGNORECASE),
    re.compile(r"Credits used\s*[:\-]?\s*([\d.]+)", re.IGNORECASE),
    re.compile(r"Total credits\s*[:\-]?\s*([\d.]+)", re.IGNORECASE),
]


def parse_ai_credits(cli_output):
    for pattern in AI_CREDITS_PATTERNS:
        m = pattern.search(cli_output or "")
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def legacy_cost_log(key, credits, timed_out):
    """Keeps logs/cost-tracking.jsonl alive for generate_cost_dashboard.py
    until the Phase 2 dashboard reads run-events.jsonl instead."""
    try:
        entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "ticket": key,
                 "model": CLI_MODEL or "default", "ai_credits": credits,
                 "estimated_cost_usd": round(credits * events.CREDIT_TO_USD, 4) if credits is not None else None,
                 "timed_out": timed_out}
        with open(os.path.join(events.log_dir(), "cost-tracking.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"  [cost] WARNING: legacy cost log failed: {e}")


def stop(key, step_reason, outcome, message, notify_too=True):
    """Record a blocked run and exit non-zero, before any AI spend."""
    events.emit_event(key, "agent", "blocked", outcome=outcome, reason_code=step_reason, detail=message)
    jc.audit("invoke_copilot", key, message, "blocked")
    if notify_too and notify:
        try:
            notify.notify_failure(step="invoke_copilot.py", ticket=key, reason=redact.redact(message))
        except Exception:
            pass
    sys.exit(f"STOP: {message}")


def record_run(key, r_stdout, r_stderr, duration, timed_out, exit_code):
    credits = parse_ai_credits((r_stdout or "") + (r_stderr or ""))
    status = "failed" if (timed_out or exit_code != 0) else "ok"
    events.emit_event(key, "agent", status, actor="agent", model=CLI_MODEL or "default",
                      ai_credits=credits, duration_s=round(duration, 1),
                      outcome="timed_out" if timed_out else None,
                      reason_code=("timeout" if timed_out else (f"exit_{exit_code}" if exit_code else None)),
                      credits_parsed=credits is not None)
    legacy_cost_log(key, credits, timed_out)
    if credits is None:
        print("  cost: WARNING - 'AI Credits' line not found in CLI output; recorded as unknown.")
    else:
        print(f"  cost: {credits:.2f} AI credits (~${credits * events.CREDIT_TO_USD:.2f})")
        if credits >= guards.TICKET_CREDIT_CEILING:
            msg = (f"single run used {credits:.2f} credits, above the per-ticket ceiling "
                   f"({guards.TICKET_CREDIT_CEILING:.0f}). Further attempts on {key} are blocked.")
            print(f"  WARNING: {msg}")
            events.emit_event(key, "agent", "failed", outcome=None, reason_code="ceiling_overshoot", detail=msg)
            if notify:
                try:
                    notify.notify_failure(step="cost_ceiling", ticket=key, reason=msg)
                except Exception:
                    pass
    return credits


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python invoke_copilot.py <TICKET-KEY>")
    key = sys.argv[1].strip().upper()
    if not KEY_PATTERN.match(key):
        sys.exit(f"STOP: '{key}' is not a valid {jc.PROJECT} ticket key.")
    events.current_run_id(key)

    print("=" * 60)
    print(f"AGENT INVOKE COPILOT CLI | {key} | Dry run: {jc.DRY_RUN}")
    print("=" * 60)

    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    prompt_path = os.path.join(REPORT_DIR, f"{key}-prompt.md")
    if not os.path.exists(prompt_path):
        sys.exit(f"STOP: {prompt_path} not found. Run prepare_fix.py first.")

    if jc.DRY_RUN:
        print("  [dry-run] Copilot CLI NOT invoked (no AI cost). Guards evaluated for information:")
        for label, (ok, why) in (("budget", guards.check_budget()), ("ceiling", guards.check_ticket_ceiling(key))):
            print(f"    {label}: {'ok' if ok else 'WOULD BLOCK'} - {why}")
        events.emit_event(key, "agent", "skipped", reason_code="dry_run")
        jc.audit("invoke_copilot", key, "dry-run - CLI not invoked", "dry-run")
        return

    if not CLI_ENABLED:
        sys.exit("STOP: COPILOT_CLI_ENABLED is not 'true' in .env.")
    cli = shutil.which("copilot")
    if not cli:
        sys.exit("STOP: 'copilot' CLI not found on PATH. Install with: npm install -g @github/copilot")

    # ---------------- Phase 0 gates: before ANY AI spend ----------------
    got, why = guards.acquire_lock(key)
    if not got:
        stop(key, "locked", None, why, notify_too=False)
    try:
        ok, why = guards.check_budget()
        print(f"  budget : {why}")
        if not ok:
            stop(key, "budget", "budget_blocked", why)
        ok, why = guards.check_ticket_ceiling(key)
        print(f"  ceiling: {why}")
        if not ok:
            stop(key, "ceiling", "ceiling_hit", why)
        [(_, ok, why)] = security_checks.check_salesforce_org()
        print(f"  org    : {why}")
        if not ok:
            stop(key, "prod_org_guard", "blocked", why)

        with open(prompt_path, encoding="utf-8") as f:
            prompt_text = f.read()
        cmd = [cli, "-p", prompt_text, "-s", "--no-ask-user",
               "--allow-tool", ALLOW_TOOLS, "--deny-tool", DENY_TOOLS]
        if CLI_MODEL:
            cmd += ["--model", CLI_MODEL]

        # The .env GITHUB_TOKEN is a PR-only PAT without Copilot permission;
        # strip it so the CLI uses its own `copilot login` session.
        cli_env = os.environ.copy()
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
            cli_env.pop(var, None)

        events.emit_event(key, "agent", "started", actor="agent", model=CLI_MODEL or "default")
        print(f"  Running Copilot CLI headlessly (timeout {CLI_TIMEOUT_MIN} min)...")
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=cli_env,
                               timeout=CLI_TIMEOUT_MIN * 60)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            record_run(key, out, err, time.time() - t0, timed_out=True, exit_code=124)
            jc.audit("invoke_copilot", key, f"timed out after {CLI_TIMEOUT_MIN} min", "failed")
            sys.exit("FAIL: Copilot CLI timed out.")

        log_path = os.path.join(events.log_dir(), f"{key}-copilot-cli.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"CMD: copilot -p <{len(prompt_text)} char prompt> -s --no-ask-user "
                    f"--allow-tool {ALLOW_TOOLS} --deny-tool {DENY_TOOLS}\n\n"
                    f"STDOUT:\n{redact.redact(r.stdout)}\n\nSTDERR:\n{redact.redact(r.stderr)}\n")

        record_run(key, r.stdout, r.stderr, time.time() - t0, timed_out=False, exit_code=r.returncode)

        if r.returncode != 0:
            jc.audit("invoke_copilot", key, f"exit {r.returncode} - see {log_path}", "failed")
            sys.exit(f"FAIL: Copilot CLI exited {r.returncode}. See {log_path}")

        jc.audit("invoke_copilot", key, f"completed - log: {os.path.relpath(log_path, REPO_ROOT)}", "ok")
        print(f"  done. Log: {os.path.relpath(log_path, REPO_ROOT)}")
        print("\n" + "-" * 60)
        print(f"NEXT: python review_gate.py {key}")
    finally:
        guards.release_lock(key)


if __name__ == "__main__":
    try:
        main()
    except jc.JiraError as e:
        jc.audit("error", "-", str(e), "failed")
        sys.exit(f"FAIL {e}")
