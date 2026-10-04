"""Step 4: Read the Jira queue (READ-ONLY).
Lists tickets the agent would pick up and checks whether each is ready.
Makes NO changes to Jira, Git or GitHub. Never prints tokens."""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("JIRA_BASE_URL")
AUTH = (os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN"))
PROJECT = os.getenv("JIRA_PROJECT_KEY")

# Fail-safe defaults: OFF unless "true", dry run unless "false".
ENABLED = os.getenv("AGENT_ENABLED", "false").strip().lower() == "true"
DRY_RUN = os.getenv("AGENT_DRY_RUN", "true").strip().lower() != "false"

READY_LABEL = "ai-ready"
LOCK_LABEL = "ai-locked"
REQUIRED_SECTIONS = [
    "steps to reproduce",
    "expected result",
    "actual result",
    "acceptance criteria",
]
# Ticket text is data, not instructions. Flag attempts to steer the agent.
SUSPICIOUS_PHRASES = [
    "ignore previous", "ignore the instructions", "ignore all instructions",
    "copilot-instructions", "api key", "api token", "password",
    "deploy to production", "disable test", "permission set", "profile",
]

JQL = (
    f'project = "{PROJECT}" AND labels = "{READY_LABEL}" '
    f'AND labels not in ("{LOCK_LABEL}") '
    "ORDER BY priority DESC, created ASC"
)


def adf_to_text(node):
    """Convert Jira's rich-text format (ADF) to plain text."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return "\n"
    text = "".join(adf_to_text(child) for child in node.get("content", []))
    if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock"):
        text += "\n"
    return text


def fetch_queue():
    r = requests.get(
        f"{BASE}/rest/api/3/search/jql",
        params={
            "jql": JQL,
            "maxResults": 10,
            "fields": "summary,status,priority,assignee,labels,description",
        },
        auth=AUTH,
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"FAIL Jira search: HTTP {r.status_code} - {r.text[:300]}")
    return r.json().get("issues", [])


def assess(description):
    """Return (missing_sections, suspicious_phrases)."""
    lower = description.lower()
    missing = [s for s in REQUIRED_SECTIONS if s not in lower]
    suspicious = [p for p in SUSPICIOUS_PHRASES if p in lower]
    return missing, suspicious


def main():
    print("=" * 60)
    print("AGENT QUEUE CHECK (read-only)")
    print(f"Project: {PROJECT} | Enabled: {ENABLED} | Dry run: {DRY_RUN}")
    print("=" * 60)

    if not ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    issues = fetch_queue()
    if not issues:
        print(f"Queue empty. Add the '{READY_LABEL}' label to a ticket.")
        return

    next_ticket = None
    for issue in issues:
        f = issue["fields"]
        description = adf_to_text(f.get("description"))
        missing, suspicious = assess(description)
        assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")

        if suspicious:
            verdict = "BLOCKED - suspicious instructions in ticket"
        elif missing:
            verdict = "NOT READY - missing sections"
        else:
            verdict = "READY"
            next_ticket = next_ticket or issue["key"]

        print(f"\n{issue['key']}: {f.get('summary')}")
        print(f"  Status:   {f['status']['name']}")
        print(f"  Priority: {(f.get('priority') or {}).get('name')}")
        print(f"  Assignee: {assignee}")
        print(f"  Labels:   {', '.join(f.get('labels', []))}")
        print(f"  Verdict:  {verdict}")
        if missing:
            print(f"  Missing:  {', '.join(missing)}")
        if suspicious:
            print(f"  Flagged:  {', '.join(suspicious)}")

    print("\n" + "-" * 60)
    if next_ticket:
        print(f"Next ticket the agent WOULD pick up: {next_ticket}")
    else:
        print("No ticket is ready. Fix the missing sections first.")
    print("No changes were made (read-only step).")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.SSLError:
        sys.exit("FAIL SSL/certificate error - re-check Step 3.4")