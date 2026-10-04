"""Step 5: Claim the next READY ticket and triage the rest.
Respects AGENT_ENABLED (kill switch) and AGENT_DRY_RUN."""
import sys
import jira_client as jc
from read_queue import fetch_queue, assess, adf_to_text

READY = "ai-ready"
LOCKED = "ai-locked"
NEEDS_INFO = "ai-needs-info"
BLOCKED = "ai-blocked"


def triage_not_ready(key, missing):
    print(f"\n{key}: NOT READY - asking for more information")
    jc.update_labels(key, add=[NEEDS_INFO], remove=[READY])
    jc.add_comment(key, [
        "AI agent: this ticket cannot be processed yet.",
        "Please add these sections to the description: " + ", ".join(missing) + ".",
        "Then remove the 'ai-needs-info' label and add 'ai-ready' again.",
        "No code has been changed.",
    ])


def triage_blocked(key, flagged):
    print(f"\n{key}: BLOCKED - suspicious instructions found")
    jc.update_labels(key, add=[BLOCKED], remove=[READY])
    jc.add_comment(key, [
        "AI agent: this ticket was not processed because its text contains "
        "instructions the agent is not allowed to follow.",
        "A person must review the description before it re-enters the queue.",
        "No code has been changed.",
    ])
    jc.audit("blocked", key, "flagged: " + ", ".join(flagged))


def claim(key, account_id):
    print(f"\n{key}: READY - claiming")

    # Re-check right before claiming, in case someone else took it.
    labels = jc.get_issue(key, fields="labels")["fields"].get("labels", [])
    if LOCKED in labels:
        print("  Already locked by another run. Skipping.")
        jc.audit("claim", key, "already locked", "skipped")
        return None

    jc.update_labels(key, add=[LOCKED])

    # Confirm the lock actually stuck before doing anything else.
    if not jc.DRY_RUN:
        labels = jc.get_issue(key, fields="labels")["fields"].get("labels", [])
        if LOCKED not in labels:
            jc.audit("claim", key, "lock not confirmed", "failed")
            sys.exit("Lock could not be confirmed. Stopping.")

    jc.assign(key, account_id)
    jc.transition(key, jc.STATUS_IN_PROGRESS)
    jc.add_comment(key, [
        "AI agent has claimed this ticket and started work.",
        "A draft pull request will be linked here for human review.",
        "No merge or deployment will be performed by the agent.",
    ])
    return key


def main():
    print("=" * 60)
    print(f"AGENT CLAIM | Project: {jc.PROJECT} | Dry run: {jc.DRY_RUN}")
    print("=" * 60)

    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    issues = fetch_queue()
    if not issues:
        print("Queue empty.")
        return

    account_id = jc.my_account_id()
    claimed = None

    for issue in issues:
        key = issue["key"]
        missing, flagged = assess(adf_to_text(issue["fields"].get("description")))
        if flagged:
            triage_blocked(key, flagged)
        elif missing:
            triage_not_ready(key, missing)
        elif claimed is None:
            claimed = claim(key, account_id)
        else:
            print(f"\n{key}: READY - left in queue (one ticket per run)")

    print("\n" + "-" * 60)
    print(f"Claimed: {claimed or 'none'}")
    print(f"Audit log: {jc.LOG_FILE}")
    if jc.DRY_RUN:
        print("DRY RUN: no changes were made in Jira.")


if __name__ == "__main__":
    try:
        main()
    except jc.JiraError as e:
        jc.audit("error", "-", str(e), "failed")
        sys.exit(f"FAIL {e}")