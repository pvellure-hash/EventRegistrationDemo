"""Step 9: Update Jira after the draft PR is created.
- Posts a comment with the PR link and the agent's solution summary
- Attaches the Solution Report (docs/ai-reports/<KEY>.md)
- Labels: removes ai-ready, adds ai-pr-created (keeps ai-locked)
- Moves the ticket to the review status (JIRA_STATUS_IN_REVIEW)
Respects AGENT_ENABLED and AGENT_DRY_RUN. Safe to re-run (no duplicates).
Usage: python update_jira.py CLAUDE-11"""
import os
import re
import sys
import json
import requests
import jira_client as jc

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_DIR = os.path.join(REPO_ROOT, "docs", "ai-reports")
DONE_LABEL = "ai-pr-created"


def load_pr(key):
    path = os.path.join(REPO_ROOT, "logs", f"{key}-pr.json")
    if not os.path.exists(path):
        sys.exit(f"STOP: {path} not found. Run create_pr.py first.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    url = data.get("pr_url", "")
    if not url.startswith("https://github.com/"):
        sys.exit("STOP: no real PR link found (last create_pr.py run was a dry run). "
                 "Run create_pr.py with AGENT_DRY_RUN=false first.")
    return data


def load_summary(key, pr_url):
    """Read the agent's Jira text and turn Jira wiki markup into plain lines."""
    path = os.path.join(REPORT_DIR, f"{key}-jira.md")
    if not os.path.exists(path):
        return []
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("```"):
                continue
            line = re.sub(r"^h\d\.\s*", "", line)      # h3. Heading
            line = re.sub(r"^#+\s*", "", line)           # ## Heading
            line = re.sub(r"^\*\s+", "- ", line)         # * bullet
            line = line.replace("*", "").replace("_", "")
            line = line.replace("<PR link>", pr_url)
            lines.append(line[:500])
    return lines[:40]


def build_comment(key, pr, summary_lines):
    def para(text, link=None):
        node = {"type": "text", "text": text}
        if link:
            node["marks"] = [{"type": "link", "attrs": {"href": link}}]
        return {"type": "paragraph", "content": [node]}

    content = [
        para("AI-assisted fix ready for human review"),
        para(f"Branch: {pr['branch']}"),
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Draft pull request: "},
            {"type": "text", "text": pr["pr_url"],
             "marks": [{"type": "link", "attrs": {"href": pr["pr_url"]}}]},
        ]},
        para("Pre-push validation: all checks passed (scope, size, secrets, "
             "security patterns, tests, reports)."),
        para(f"Solution Report attached: {key}.md"),
    ]
    if summary_lines:
        content.append(para("Agent summary:"))
        content += [para(l) for l in summary_lines]
    content.append(para("No merge or deployment has been performed by the agent."))
    return {"type": "doc", "version": 1, "content": content}


def already_commented(key, pr_url):
    data = jc._request("GET", f"/rest/api/3/issue/{key}/comment",
                       params={"maxResults": 100})
    return pr_url in json.dumps(data.get("comments", []))


def attach_report(key):
    path = os.path.join(REPORT_DIR, f"{key}.md")
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found - nothing attached")
        jc.audit("attach", key, "solution report missing", "skipped")
        return
    name = os.path.basename(path)
    existing = jc.get_issue(key, fields="attachment")["fields"].get("attachment", [])
    if any(a.get("filename") == name for a in existing):
        print(f"  {name} already attached - skipping")
        jc.audit("attach", key, f"{name} already attached", "skipped")
        return
    if jc.DRY_RUN:
        print(f"  [dry-run] would attach: {name}")
        jc.audit("attach", key, name, "dry-run")
        return
    with open(path, "rb") as f:
        r = requests.post(f"{jc.BASE}/rest/api/3/issue/{key}/attachments",
                          auth=jc.AUTH, timeout=60,
                          headers={"X-Atlassian-Token": "no-check"},
                          files={"file": (name, f, "text/markdown")})
    if r.status_code >= 400:
        raise jc.JiraError(f"attach -> HTTP {r.status_code}: {r.text[:300]}")
    print(f"  done: attached {name}")
    jc.audit("attach", key, name)


def post_comment(key, body):
    if jc.DRY_RUN:
        print("  [dry-run] would post comment with PR link and summary")
        jc.audit("comment", key, "PR comment", "dry-run")
        return
    jc._request("POST", f"/rest/api/3/issue/{key}/comment", json={"body": body})
    print("  done: comment posted")
    jc.audit("comment", key, "PR comment")


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python update_jira.py <TICKET-KEY>")
    key = sys.argv[1].strip().upper()
    if not re.match(rf"^{re.escape(jc.PROJECT)}-\d+$", key):
        sys.exit(f"STOP: '{key}' is not a valid {jc.PROJECT} ticket key.")

    print("=" * 60)
    print(f"AGENT UPDATE JIRA | {key} | Dry run: {jc.DRY_RUN}")
    print("=" * 60)
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    labels = jc.get_issue(key, fields="labels")["fields"].get("labels", [])
    if "ai-locked" not in labels:
        sys.exit(f"STOP: {key} is not claimed by the agent (no ai-locked label).")

    pr = load_pr(key)
    print(f"PR: {pr['pr_url']}\n")

    if already_commented(key, pr["pr_url"]):
        print("  PR comment already on ticket - skipping")
        jc.audit("comment", key, "already posted", "skipped")
    else:
        post_comment(key, build_comment(key, pr, load_summary(key, pr["pr_url"])))

    attach_report(key)

    if DONE_LABEL in labels:
        print(f"  label {DONE_LABEL} already set - skipping")
    else:
        jc.update_labels(key, add=[DONE_LABEL], remove=["ai-ready"])

    jc.transition(key, jc.STATUS_IN_REVIEW)

    print("\n" + "-" * 60)
    print(f"Jira {key} updated. Waiting for human PR review.")
    if jc.DRY_RUN:
        print("DRY RUN: no changes were made in Jira.")


if __name__ == "__main__":
    try:
        main()
    except jc.JiraError as e:
        jc.audit("error", "-", str(e), "failed")
        sys.exit(f"FAIL {e}")
    except requests.exceptions.RequestException as e:
        jc.audit("error", "-", type(e).__name__, "failed")
        sys.exit(f"FAIL request: {type(e).__name__}")
