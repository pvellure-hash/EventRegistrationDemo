"""Step 10: End-to-end orchestrator for the Jira -> Copilot -> GitHub -> Jira flow.

Runs every step in order and pauses ONLY for the human-in-the-loop Copilot step.

Usage (from the agent folder):
  python run_agent.py                  # full run: claim next ticket ... update Jira
  python run_agent.py --resume CLAUDE-12   # continue a ticket after the Copilot step
  python run_agent.py --prepare-only   # claim + branch + prompt, then stop (for scheduler)

Requires AGENT_ENABLED=true. A full run requires AGENT_DRY_RUN=false;
with AGENT_DRY_RUN=true it previews claim + prepare and stops."""
import os
import re
import sys
import subprocess
import jira_client as jc

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
MAX_VALIDATION_ATTEMPTS = 3


def banner(text):
    print("\n" + "#" * 64)
    print(f"#  {text}")
    print("#" * 64)


def run_step(script, *args, capture=False):
    """Run one agent script. Returns (exit_code, output)."""
    cmd = [sys.executable, os.path.join(HERE, script), *args]
    if capture:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print(r.stdout, end="")
        if r.stderr:
            print(r.stderr, end="")
        return r.returncode, r.stdout + r.stderr
    r = subprocess.run(cmd, cwd=HERE)
    return r.returncode, ""


def fail(key, stage, reason):
    """Mark the ticket as needing a human and stop."""
    print(f"\nSTOPPED at {stage}: {reason}")
    jc.audit("orchestrator", key or "-", f"stopped at {stage}: {reason}", "failed")
    if key and not jc.DRY_RUN:
        try:
            jc.update_labels(key, add=["ai-failed"])
            jc.add_comment(key, [
                f"AI agent stopped at stage: {stage}.",
                f"Reason: {reason}",
                "A person needs to review this ticket. No merge or deployment was performed.",
            ])
        except jc.JiraError as e:
            print(f"  (could not update Jira: {e})")
    sys.exit(1)


def claim():
    banner("1/6  CLAIM next ticket from the queue")
    code, out = run_step("claim_ticket.py", capture=True)
    if code != 0:
        fail(None, "claim", "claim_ticket.py failed")
    m = re.search(r"Claimed:\s*([A-Z][A-Z0-9]+-\d+)", out)
    if not m:
        print("\nNo READY ticket in the queue. Nothing to do.")
        sys.exit(0)
    return m.group(1)


def prepare(key):
    banner(f"2/6  PREPARE branch + Copilot prompt for {key}")
    code, _ = run_step("prepare_fix.py", key)
    if code != 0:
        fail(key, "prepare", "prepare_fix.py failed")


def wait_for_copilot(key):
    banner(f"3/6  HUMAN-IN-THE-LOOP: Copilot Agent mode for {key}")
    print(f"""
  In VS Code:
    1. Open Copilot Chat, start a NEW chat, select Agent mode
    2. Send:  Follow #file:docs/ai-reports/{key}-prompt.md
    3. Review the fix plan, reply 'approved' only if it is correct
    4. Let it commit. Refuse any push / deploy / sf command.

  When Copilot has finished and committed, come back here.
""")
    while True:
        answer = input("  Type 'done' to continue, 'later' to pause, 'abort' to stop: ")
        answer = answer.strip().lower()
        if answer == "done":
            return
        if answer == "later":
            print(f"\n  Paused. Resume with:  python run_agent.py --resume {key}")
            jc.audit("orchestrator", key, "paused for Copilot step")
            sys.exit(0)
        if answer == "abort":
            fail(key, "copilot", "aborted by human reviewer")


def validate(key):
    banner(f"4/6  VALIDATE fix for {key} (static checks + Apex tests)")
    for attempt in range(1, MAX_VALIDATION_ATTEMPTS + 1):
        code, _ = run_step("validate_fix.py", key)
        if code == 0:
            return
        print(f"\n  Validation failed (attempt {attempt}/{MAX_VALIDATION_ATTEMPTS}).")
        if attempt == MAX_VALIDATION_ATTEMPTS:
            break
        print("  Ask Copilot to fix the failing check, let it commit, then retry.")
        answer = input("  Type 'retry' to validate again, or 'abort': ").strip().lower()
        if answer != "retry":
            break
    fail(key, "validate", "pre-push validation did not pass")


def create_pr(key):
    banner(f"5/6  PUSH branch + create DRAFT PR for {key}")
    code, _ = run_step("create_pr.py", key)
    if code != 0:
        fail(key, "create_pr", "create_pr.py failed")


def update_jira(key):
    banner(f"6/6  UPDATE Jira {key}")
    code, _ = run_step("update_jira.py", key)
    if code != 0:
        fail(key, "update_jira", "update_jira.py failed")


def main():
    args = sys.argv[1:]
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    print("=" * 64)
    print(f"AGENT ORCHESTRATOR | Project: {jc.PROJECT} | Dry run: {jc.DRY_RUN}")
    print("=" * 64)

    if "--resume" in args:
        i = args.index("--resume")
        if i + 1 >= len(args):
            sys.exit("Usage: python run_agent.py --resume <TICKET-KEY>")
        key = args[i + 1].strip().upper()
        if jc.DRY_RUN:
            sys.exit("Set AGENT_DRY_RUN=false to resume a real run.")
        jc.audit("orchestrator", key, "resumed")
        validate(key)
        create_pr(key)
        update_jira(key)
    else:
        key = claim()
        prepare(key)
        if jc.DRY_RUN:
            print("\nDRY RUN preview complete. Set AGENT_DRY_RUN=false for a real run.")
            return
        if "--prepare-only" in args:
            print(f"\nPrepared {key}. Run Copilot, then: python run_agent.py --resume {key}")
            return
        wait_for_copilot(key)
        validate(key)
        create_pr(key)
        update_jira(key)

    banner(f"DONE: {key} is In Review with a draft PR")
    print("  Next (human): review the PR, approve, merge.")
    print("  Merging to main triggers the Salesforce deploy workflow.")
    jc.audit("orchestrator", key, "completed end-to-end")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Resume later with --resume <KEY>.")
