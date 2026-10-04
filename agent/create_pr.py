"""Step 8: Push the validated fix branch and create a DRAFT pull request.
Respects AGENT_ENABLED and AGENT_DRY_RUN. Never merges. Never prints tokens.
Usage: python create_pr.py CLAUDE-11"""
import os
import re
import sys
import json
import subprocess
import requests
import jira_client as jc

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


def find_existing_pr(branch):
    owner = REPO.split("/")[0]
    r = requests.get(f"{API}/repos/{REPO}/pulls", headers=HEADERS, timeout=30,
                     params={"head": f"{owner}:{branch}", "state": "open"})
    r.raise_for_status()
    prs = r.json()
    return prs[0] if prs else None


def read_pr_body(key):
    path = os.path.join(REPO_ROOT, "docs", "ai-reports", f"{key}-pr.md")
    with open(path, encoding="utf-8") as f:
        body = f.read().strip()
    body += ("\n\n---\n"
             "_AI-assisted change. Draft PR created by agent. "
             "Requires human review and approval. No merge or deployment performed._")
    return body


def title_from(key):
    summary = jc.get_issue(key, fields="summary")["fields"].get("summary", "")
    return f"[{key}] {summary}"


def save_result(key, data):
    os.makedirs(os.path.join(REPO_ROOT, "logs"), exist_ok=True)
    with open(os.path.join(REPO_ROOT, "logs", f"{key}-pr.json"), "w",
              encoding="utf-8") as f:
        json.dump(data, f, indent=2)


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

    print("\nRe-running validation before push:")
    run_validation(key)

    existing = find_existing_pr(branch)
    title = title_from(key)
    body = read_pr_body(key)

    # Push (always to the fix branch, never to main)
    if jc.DRY_RUN:
        print(f"\n  [dry-run] would run: git push -u origin {branch}")
    else:
        git("push", "-u", "origin", branch)
        print(f"\n  done: pushed {branch}")
    jc.audit("push", key, branch, "dry-run" if jc.DRY_RUN else "ok")

    if existing:
        url = existing["html_url"]
        print(f"  PR already exists - reusing: {url}")
        jc.audit("create_pr", key, f"reused {url}")
    elif jc.DRY_RUN:
        url = "<dry-run: no PR created>"
        print(f"  [dry-run] would create DRAFT PR: {title}  ({branch} -> {BASE})")
        jc.audit("create_pr", key, title, "dry-run")
    else:
        r = requests.post(f"{API}/repos/{REPO}/pulls", headers=HEADERS, timeout=30,
                          json={"title": title, "head": branch, "base": BASE,
                                "body": body, "draft": True})
        if r.status_code >= 400:
            jc.audit("create_pr", key, f"HTTP {r.status_code}", "failed")
            sys.exit(f"FAIL GitHub PR: HTTP {r.status_code} - {r.text[:300]}")
        url = r.json()["html_url"]
        print(f"  done: DRAFT PR created: {url}")
        jc.audit("create_pr", key, url)

    save_result(key, {"ticket": key, "branch": branch, "base": BASE,
                      "title": title, "pr_url": url, "dry_run": jc.DRY_RUN})

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
