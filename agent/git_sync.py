"""
git_sync.py - keep every ticket on a fresh, merged base branch (fixes G11).

Self-contained (standard library only). Safe by design:
  * never discards work: a dirty tree => STOP, nothing touched
  * never force-updates: only `git pull --ff-only`; diverged local main => STOP
  * never merges or resets anything

Layers provided:
  1/2  sync_base()              -> checkout base + fast-forward (start AND end of every ticket)
  3    overlapping_open_prs()   -> open PRs already changing the files this ticket will touch
  4    unresolved_blockers()    -> Jira "is blocked by" links that are not Done

CLI (run from the agent folder):
  python git_sync.py status
  python git_sync.py sync
  python git_sync.py overlap force-app/main/default/classes/EventRegistrationController.cls
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- env
def _load_env() -> None:
    """Load ..\\.env without requiring python-dotenv; existing env vars win."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()
BASE_BRANCH = os.environ.get("BASE_BRANCH") or os.environ.get("GITHUB_BASE_BRANCH") or "main"


# --------------------------------------------------------------------------- git
def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd or REPO_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def current_branch(cwd: Path | None = None) -> str:
    return _git("branch", "--show-current", cwd=cwd).stdout.strip()


def dirty_files(cwd: Path | None = None) -> list[str]:
    out = _git("status", "--porcelain", cwd=cwd).stdout
    return [l for l in out.splitlines() if l.strip()]


def sync_base(base: str | None = None, cwd: Path | None = None) -> tuple[bool, str]:
    """
    Put the repo on <base> at the latest origin/<base>.
    Returns (ok, message). ok=False means NOTHING was changed - caller must notify and skip.
    """
    base = base or BASE_BRANCH
    dirty = dirty_files(cwd)
    if dirty:
        return False, (f"Working tree not clean ({len(dirty)} change(s)); left untouched on "
                       f"'{current_branch(cwd)}'. First: {dirty[0]}")

    r = _git("fetch", "origin", base, "--prune", cwd=cwd)
    if r.returncode != 0:
        return False, f"git fetch failed: {r.stderr.strip()}"

    start = current_branch(cwd)
    if start != base:
        r = _git("checkout", base, cwd=cwd)
        if r.returncode != 0:
            return False, f"git checkout {base} failed: {r.stderr.strip()}"

    ahead = _git("rev-list", "--count", f"origin/{base}..{base}", cwd=cwd).stdout.strip()
    if ahead not in ("", "0"):
        return False, (f"Local {base} has {ahead} commit(s) not on origin/{base} "
                       f"(diverged). Not touching it - fix by hand.")

    r = _git("merge", "--ff-only", f"origin/{base}", cwd=cwd)
    if r.returncode != 0:
        return False, f"fast-forward failed: {r.stderr.strip()}"

    head = _git("rev-parse", "--short", "HEAD", cwd=cwd).stdout.strip()
    moved = f" (was on '{start}')" if start != base else ""
    return True, f"On {base} @ {head}, up to date with origin{moved}"


# --------------------------------------------------------------------------- GitHub overlap
def _gh_get(path: str):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPO not set")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    root = str(REPO_ROOT).replace("\\", "/").rstrip("/") + "/"
    if p.lower().startswith(root.lower()):
        p = p[len(root):]
    return p.lstrip("./").lower()


def overlapping_open_prs(files: list[str], exclude_head: str | None = None,
                         base: str | None = None) -> list[dict]:
    """
    Open PRs into <base> that change any of `files`.
    exclude_head: this ticket's own branch (so a resumed ticket doesn't block itself).
    Raises on API error - caller decides fail-open (warn) or fail-closed.
    """
    base = base or BASE_BRANCH
    wanted = {_norm(f) for f in files if f}
    if not wanted:
        return []
    hits = []
    for pr in _gh_get(f"/pulls?state=open&base={base}&per_page=100"):
        head = pr["head"]["ref"]
        if exclude_head and head == exclude_head:
            continue
        pr_files = _gh_get(f"/pulls/{pr['number']}/files?per_page=100")
        common = sorted({f["filename"] for f in pr_files if _norm(f["filename"]) in wanted})
        if common:
            hits.append({"number": pr["number"], "title": pr["title"], "head": head,
                         "url": pr["html_url"], "files": common})
    return hits


# --------------------------------------------------------------------------- Jira blockers
def unresolved_blockers(issue: dict) -> list[str]:
    """
    Given a raw Jira issue JSON (REST /issue/<key>), return keys of issues that
    block it and are not Done. Pure function - no Jira call here.
    """
    out = []
    for link in (issue.get("fields") or {}).get("issuelinks") or []:
        ltype = (link.get("type") or {})
        other = link.get("inwardIssue")
        if not other or "block" not in (ltype.get("inward") or "").lower():
            continue
        cat = (((other.get("fields") or {}).get("status") or {})
               .get("statusCategory") or {}).get("key", "")
        if cat != "done":
            out.append(other.get("key", "?"))
    return out


# --------------------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        d = dirty_files()
        print(f"branch : {current_branch()}\nbase   : {BASE_BRANCH}\nclean  : {not d}")
        for l in d[:10]:
            print(f"   {l}")
        return 0
    if cmd == "sync":
        ok, msg = sync_base()
        print(("OK   " if ok else "STOP ") + msg)
        return 0 if ok else 1
    if cmd == "overlap":
        hits = overlapping_open_prs(argv[2:])
        if not hits:
            print("No open PR touches those files.")
        for h in hits:
            print(f"PR #{h['number']} ({h['head']}): {', '.join(h['files'])}\n   {h['url']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
