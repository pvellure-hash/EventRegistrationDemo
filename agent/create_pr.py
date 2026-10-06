"""Step 8: Push the validated fix branch and create a DRAFT pull request.
Respects AGENT_ENABLED and AGENT_DRY_RUN. Never merges. Never prints tokens.
STEP 3 (v7): if prepare_fix.py batched related tickets with this one (routing.read_batch),
the PR title lists every ticket key and the body gets a "Tickets in this PR" table, and
logs/<KEY>-pr.json records the members so update_jira.py updates each of them.
The branch check is unchanged: a batch branch is fix/<LEAD>-batch-..., which still
starts with fix/<LEAD>-.
Usage: python create_pr.py CLAUDE-11"""
import os
import re
import sys
import json
import time
import subprocess
import requests
import jira_client as jc
import routing

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE = os.getenv("GITHUB_BASE_BRANCH", "main").strip()
REPO = os.getenv("GITHUB_REPO", "").strip()
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitError(Exception):
    pass


def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def run_validation(key):
    """Re-run Step 7 right before pushing. Nothing is pushed unless it passes."""
    script = os.path.join(os.path.dirname(__file__), "validate_fix.py")
    r = subprocess.run([sys.executable, script, key], cwd=os.path.dirname(__file__))
    if r.returncode != 0:
        jc.audit("create_pr", key, "validation failed - push blocked", "failed")
        sys.exit("STOP: validation failed. Nothing was pushed.")


def find_existing_pr(branch, state="open"):
    """Issue G9 fix: originally only ever checked state='open'. If a PR for
    this branch had already been merged or closed by the time this ran
    (e.g. CLAUDE-15: PR #10 was merged out-of-band before this check ran
    again), an open-only search finds nothing, the script then tries to
    open a brand-new PR, and GitHub correctly rejects it with HTTP 422
    "No commits between main and fix/..." (because those commits are
    already on main via the merged PR) - which used to be reported as a
    bare FAIL even though the PR existed and had already merged
    successfully. state='all' catches open, closed and merged PRs for
    this branch so that case is recognised and reused/reported instead.
    """
    owner = REPO.split("/")[0]
    r = requests.get(f"{API}/repos/{REPO}/pulls", headers=HEADERS, timeout=30,
                     params={"head": f"{owner}:{branch}", "state": state})
    r.raise_for_status()
    prs = r.json()
    return prs[0] if prs else None


def batch_section(key, batch):
    """STEP 3: markdown table of every ticket in a batched PR ('' for a single ticket)."""
    if not batch:
        return ""
    jira = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    rows = [(key, jc.get_issue(key, fields="summary")["fields"].get("summary", ""))]
    rows += [(m["key"], m.get("summary", "")) for m in batch["members"]]
    lines = ["", "### Tickets in this PR", "",
             "This PR fixes related tickets together because they change the same code.", "",
             "| Ticket | Summary |", "|---|---|"]
    for k, s in rows:
        link = f"[{k}]({jira}/browse/{k})" if jira else k
        lines.append(f"| {link} | {s.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def read_pr_body(key, batch=None):
    path = os.path.join(REPO_ROOT, "docs", "ai-reports", f"{key}-pr.md")
    with open(path, encoding="utf-8") as f:
        body = f.read().strip()
    body += batch_section(key, batch)
    body += ("\n\n---\n"
             "_AI-assisted change. Draft PR created by agent. "
             "Requires human review and approval. No merge or deployment performed._")
    return body


def title_from(key, batch=None):
    summary = jc.get_issue(key, fields="summary")["fields"].get("summary", "")
    if batch:
        keys = [key] + [m["key"] for m in batch["members"]]
        return f"[{', '.join(keys)}] {summary} (+{len(keys) - 1} related)"
    return f"[{key}] {summary}"


def save_result(key, data):
    os.makedirs(os.path.join(REPO_ROOT, "logs"), exist_ok=True)
    with open(os.path.join(REPO_ROOT, "logs", f"{key}-pr.json"), "w",
              encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def describe_pr_state(pr):
    if pr.get("merged_at"):
        return f"already MERGED at {pr['merged_at']}"
    if pr.get("state") == "closed":
        return "already CLOSED (not merged)"
    return "already OPEN"


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python create_pr.py <TICKET-KEY>")
    key = sys.argv[1].strip().upper()
    if not re.match(rf"^{re.escape(jc.PROJECT)}-\d+$", key):
        sys.exit(f"STOP: '{key}' is not a valid {jc.PROJECT} ticket key.")
    print("=" * 60)
    print(f"AGENT CREATE PR | {key} | Dry run: {jc.DRY_RUN}")
    print("=" * 60)
    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")
    if not REPO or not TOKEN:
        sys.exit("STOP: GITHUB_REPO or GITHUB_TOKEN missing in .env")
    branch = git("branch", "--show-current")
    if not branch.startswith(f"fix/{key}-"):
        sys.exit(f"STOP: current branch '{branch}' is not the fix branch for {key}.")
    batch = routing.read_batch(key)
    if batch and batch.get("branch") != branch:
        print(f"  NOTE: batch record is for {batch.get('branch')}, not {branch} - treating as single ticket")
        batch = None
    members = [m["key"] for m in batch["members"]] if batch else []
    if members:
        print(f"  batch  : {key} + {', '.join(members)}")
    print("\nRe-running validation before push:")
    run_validation(key)
    # Issue G9 fix: check all states up front, not just open, so an
    # already-merged/closed PR for this branch is recognised immediately.
    existing = find_existing_pr(branch, state="all")
    title = title_from(key, batch)
    body = read_pr_body(key, batch)
    # Push (always to the fix branch, never to main)
    if jc.DRY_RUN:
        print(f"\n  [dry-run] would run: git push -u origin {branch}")
    else:
        git("push", "-u", "origin", branch)
        print(f"\n  done: pushed {branch}")
    jc.audit("push", key, branch, "dry-run" if jc.DRY_RUN else "ok")
    result = {"ticket": key, "branch": branch, "base": BASE, "title": title,
              "members": members, "dry_run": jc.DRY_RUN}
    if existing:
        url = existing["html_url"]
        print(f"  PR {describe_pr_state(existing)} - reusing: {url}")
        jc.audit("create_pr", key, f"reused {url} ({describe_pr_state(existing)})")
    elif jc.DRY_RUN:
        url = "<dry-run: no PR created>"
        print(f"  [dry-run] would create DRAFT PR: {title}  ({branch} -> {BASE})")
        jc.audit("create_pr", key, title, "dry-run")
    else:
        r = requests.post(f"{API}/repos/{REPO}/pulls", headers=HEADERS, timeout=30,
                          json={"title": title, "head": branch, "base": BASE,
                                "body": body, "draft": True})
        if r.status_code >= 400:
            # Issue G9 fix: a 422 here can mean "no commits between main and
            # this branch" because a PR for this branch already merged in
            # the time between our first check and this POST (GitHub's
            # compare API can also lag a few seconds right after a push).
            # Re-check for an existing PR (any state) before giving up.
            if r.status_code == 422:
                print(f"  HTTP 422 on create - re-checking for an existing PR before failing...")
                time.sleep(2)
                existing = find_existing_pr(branch, state="all")
                if existing:
                    url = existing["html_url"]
                    print(f"  PR {describe_pr_state(existing)} - reusing: {url}")
                    jc.audit("create_pr", key, f"reused after 422: {url} ({describe_pr_state(existing)})")
                    save_result(key, {**result, "pr_url": url})
                    print("\n" + "-" * 60)
                    print(f"PR: {url}")
                    print(f"Saved to logs/{key}-pr.json (used by Step 9 Jira update)")
                    print("Agent stops here. A human must review and merge the PR.")
                    return
            jc.audit("create_pr", key, f"HTTP {r.status_code}", "failed")
            sys.exit(f"FAIL GitHub PR: HTTP {r.status_code} - {r.text[:300]}")
        url = r.json()["html_url"]
        print(f"  done: DRAFT PR created: {url}")
        jc.audit("create_pr", key, url)
    save_result(key, {**result, "pr_url": url})
    print("\n" + "-" * 60)
    print(f"PR: {url}")
    print(f"Saved to logs/{key}-pr.json (used by Step 9 Jira update)")
    print("Agent stops here. A human must review and merge the PR.")


if __name__ == "__main__":
    try:
        main()
    except (GitError, jc.JiraError) as e:
        jc.audit("error", "-", str(e), "failed")
        sys.exit(f"FAIL {e}")
    except requests.exceptions.RequestException as e:
        jc.audit("error", "-", type(e).__name__, "failed")
        sys.exit(f"FAIL GitHub request: {type(e).__name__}")
