"""Step 9: Update Jira after the draft PR is created.
- Posts a comment with the PR link and the agent's solution summary
- Attaches the Solution Report (docs/ai-reports/<KEY>.md)
- Labels: removes ai-ready, adds ai-pr-created (keeps ai-locked)
- Moves the ticket to the review status (JIRA_STATUS_IN_REVIEW)
Respects AGENT_ENABLED and AGENT_DRY_RUN. Safe to re-run (no duplicates).
Usage: python update_jira.py CLAUDE-11

STEP 2 (v7) - importable handlers used by watch_queue.track_prs() every poll cycle
(main() above is unchanged):
  on_pr_event(ev)        acts on one pr_tracker event:
      MERGED   -> comment, label ai-merged (remove ai-locked), move to JIRA_STATUS_DONE
      REJECTED -> comment, label ai-rejected (remove ai-locked)
      CONFLICT -> comment with the two ways forward + notification
      UPDATE   -> notification only (GitHub Update branch pressed, Validate re-runs)
      REMIND   -> notification only (no Jira comment spam)
    Every Jira write is idempotent (label already present -> skipped).
  release_waiting(open_prs)   moves ai-waiting tickets back to ai-ready when what they
      waited for is gone: kind "pr" -> none of its PRs is still open; kind "blocker" ->
      every Jira "is blocked by" ticket is Done. A ticket without a record in
      logs/waiting.json is treated as "blocker" (checked against its Jira links).
STEP 3 (v7): for a batched PR (logs/<LEAD>-pr.json "members"), main() also updates every
  member ticket: comment with the shared PR link, label ai-pr-created (ai-ready removed),
  move to In Review. MERGED/REJECTED/CONFLICT events already reach every member because
  pr_tracker reads all keys from the batch branch name.
.env: JIRA_STATUS_DONE=Done (if your workflow has no such transition, a WARNING is printed
      and the status is left as is - labels and comments still apply)."""
import os
import re
import sys
import json
import requests
import jira_client as jc

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_DIR = os.path.join(REPO_ROOT, "docs", "ai-reports")
DONE_LABEL = "ai-pr-created"
MERGED_LABEL = "ai-merged"
REJECTED_LABEL = "ai-rejected"
WAITING_LABEL = "ai-waiting"
STATUS_DONE = os.getenv("JIRA_STATUS_DONE", "Done").strip() or "Done"


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


# ---------------------------------------------------------------- STEP 2: PR lifecycle
def _labels(key):
    return jc.get_issue(key, fields="labels")["fields"].get("labels", [])


def _notify(kind, key, title, detail="", url=""):
    try:
        import notify
        notify.notify_event(kind, key, title, detail, url)
    except Exception as e:
        print(f"  [notify] WARNING (non-blocking): {e}")


def on_pr_event(ev):
    """Act on one pr_tracker event. Raises nothing for a single bad ticket - logs and continues."""
    n, url, action = ev["number"], ev["url"], ev["action"]
    for key in ev["keys"]:
        try:
            if action == "MERGED":
                if MERGED_LABEL in _labels(key):
                    print(f"  {key}: already {MERGED_LABEL} - skipping")
                    continue
                jc.add_comment(key, [
                    f"Pull request #{n} was merged into main: {url}",
                    "The Salesforce deploy now waits for approval in GitHub Actions "
                    "(Review deployments -> salesforce-deploy-gate).",
                    "Tickets waiting on this change will be released automatically."])
                jc.update_labels(key, add=[MERGED_LABEL], remove=["ai-locked"])
                jc.transition(key, STATUS_DONE)
                _notify("pr_merged", key, f"PR #{n} merged", url=url)
            elif action == "REJECTED":
                if REJECTED_LABEL in _labels(key):
                    print(f"  {key}: already {REJECTED_LABEL} - skipping")
                    continue
                jc.add_comment(key, [
                    f"Pull request #{n} was closed without merging: {url}",
                    "No change reached main or Salesforce. The branch was kept for reference.",
                    "To retry: delete the branch, update the ticket if needed, then set the "
                    "labels back to ai-ready only."])
                jc.update_labels(key, add=[REJECTED_LABEL], remove=["ai-locked"])
                _notify("pr_rejected", key, f"PR #{n} closed without merge", url=url)
            elif action == "CONFLICT":
                jc.add_comment(key, [
                    f"Pull request #{n} now conflicts with main (another change to the same "
                    f"lines was merged first): {url}",
                    "Option 1: resolve the conflict on GitHub and let Salesforce Validate re-run.",
                    "Option 2: close the PR, delete the branch and set the labels back to "
                    "ai-ready only - the agent redoes the ticket from the latest main."])
                _notify("pr_conflict", key, f"PR #{n} has a merge conflict", url=url)
            elif action == "UPDATE":
                _notify("pr_update", key, f"PR #{n} was behind main - branch updated",
                        ev.get("detail", ""), url)
            elif action == "REMIND":
                _notify("pr_reminder", key, f"PR #{n} waiting {ev.get('age_h', '?')} h for review",
                        url=url)
            jc.audit("pr_tracker", key, f"PR #{n} {action}")
        except Exception as e:
            print(f"  [pr-tracker] WARNING {key} PR #{n} {action}: {e}")
            jc.audit("pr_tracker", key, f"PR #{n} {action} handling failed: {e}", "failed")


def _search(jql, fields):
    data = jc._request("GET", "/rest/api/3/search/jql",
                       params={"jql": jql, "maxResults": 50, "fields": fields})
    return data.get("issues", [])


def release_waiting(open_prs):
    """open_prs: set of open ticket-PR numbers from pr_tracker.track_all().
    Returns the list of released keys."""
    import pr_tracker
    import git_sync
    released = []
    jql = f'project = "{jc.PROJECT}" AND labels = "{WAITING_LABEL}" ORDER BY created ASC'
    for issue in _search(jql, "labels,issuelinks"):
        key = issue["key"]
        info = pr_tracker.waiting_info(key) or {"kind": "blocker"}
        if info.get("kind") == "pr":
            still_open = [p for p in info.get("prs", []) if p in open_prs]
            if still_open:
                continue
            why = f"PR(s) {', '.join('#%s' % p for p in info.get('prs', []))} no longer open"
        else:
            blockers = git_sync.unresolved_blockers(issue)
            if blockers:
                continue
            why = "all blocking tickets are Done"
        jc.update_labels(key, add=["ai-ready"], remove=[WAITING_LABEL])
        jc.add_comment(key, [f"AI agent: released back to the queue ({why}).",
                             "It will be picked up from the latest main on the next poll."])
        if not jc.DRY_RUN:
            pr_tracker.clear_waiting(key)
        _notify("ticket_released", key, "Released to ai-ready", why)
        jc.audit("release_waiting", key, why)
        released.append(key)
    return released


def update_member(member, lead, pr):
    """STEP 3: give a batch member the same PR link and status as the lead. Idempotent."""
    url = pr["pr_url"]
    print(f"\n  member {member} (fixed together with {lead})")
    labels = jc.get_issue(member, fields="labels")["fields"].get("labels", [])
    if already_commented(member, url):
        print("    PR comment already on ticket - skipping")
    else:
        jc.add_comment(member, [
            f"AI-assisted fix ready for human review - fixed together with {lead} in one pull request.",
            f"Draft pull request: {url}",
            f"Branch: {pr['branch']}",
            f"Details for every ticket are in the Solution Report attached to {lead}.",
            "No merge or deployment has been performed by the agent."])
    if DONE_LABEL in labels:
        print(f"    label {DONE_LABEL} already set - skipping")
    else:
        jc.update_labels(member, add=[DONE_LABEL], remove=["ai-ready"])
    jc.transition(member, jc.STATUS_IN_REVIEW)


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
    for member in pr.get("members") or []:
        update_member(member, key, pr)
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
