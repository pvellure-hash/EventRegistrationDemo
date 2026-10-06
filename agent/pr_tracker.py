"""pr_tracker.py - Step 2: follow every pipeline PR after it is opened, so the watcher
never waits for a person. Pure GitHub + local state; NO Jira calls and NO AI ($0).
Jira/notification side effects are done by update_jira.on_pr_event() / release_waiting(),
called from watch_queue.track_prs() once per poll cycle.

Per open or recently closed pipeline PR (head branch fix/<KEY>-... or batch/..., where
<KEY> belongs to JIRA_PROJECT_KEY) the tracker decides ONE action:

  GitHub state                              action      what happens (in update_jira)
  ----------------------------------------------------------------------------------------
  merged                                    MERGED      comment, label ai-merged, -> Done
  closed, not merged                        REJECTED    comment, label ai-rejected
  open, mergeable_state "dirty"             CONFLICT    comment + notification (once)
  open, mergeable_state "behind"            UPDATE      GitHub "Update branch" pressed here
                                                        (server-side; no local git touched;
                                                        Salesforce Validate re-runs)
  open for >= PR_REMIND_HOURS since         REMIND      notification only (no Jira spam)
    creation / last reminder
  otherwise                                 WAITING     nothing

Only NEW transitions are returned, so each one is reported once. State lives in
logs/pr-state.json. FIRST RUN (no state file yet): every PR that is already closed is
recorded silently as done, so old merged PRs never trigger Jira comments.

v7.1 (dashboard sync): every tracking cycle also writes logs/pr-status.json - a snapshot
of every ticket PR GitHub returned (number, ticket keys, branch, title, state, created /
merged / closed times, mergeable state, URL). generate_pipeline_dashboard.py reads it to
show merged / closed / open status and time-to-merge. It is a read-only copy of what
GitHub reported; it is rewritten each cycle and safe to delete.

Also owns logs/waiting.json - why a ticket carries the ai-waiting label:
  {"CLAUDE-30": {"kind": "pr", "prs": [18], "detail": "..."}}      overlap with open PR(s)
  {"CLAUDE-31": {"kind": "blocker", "blockers": ["CLAUDE-29"]}}    Jira "is blocked by"

.env:  PR_REMIND_HOURS=4   PR_AUTO_UPDATE_BRANCH=true
CLI (run from the agent folder):
  python pr_tracker.py              status table of pipeline PRs (read-only)
  python pr_tracker.py --snapshot   also write logs/pr-status.json now (no other changes)
  python pr_tracker.py --apply      run one tracking cycle (state file + Update branch + snapshot)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "logs" / "pr-state.json"
WAITING_FILE = REPO_ROOT / "logs" / "waiting.json"


def _load_env() -> None:
    f = REPO_ROOT / ".env"
    if not f.exists():
        return
    for raw in f.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
BASE = (os.environ.get("GITHUB_BASE_BRANCH") or os.environ.get("BASE_BRANCH") or "main").strip()
PROJECT = (os.environ.get("JIRA_PROJECT_KEY") or "CLAUDE").strip().upper()
KEY_RE = re.compile(rf"\b({re.escape(PROJECT)}-\d+)\b")
REMIND_HOURS = float(os.environ.get("PR_REMIND_HOURS", "4") or 4)
AUTO_UPDATE = os.environ.get("PR_AUTO_UPDATE_BRANCH", "true").strip().lower() == "true"
BRANCH_PREFIXES = ("fix/", "batch/")
TERMINAL = {"MERGED", "REJECTED"}


# ------------------------------------------------------------------ GitHub
def _gh(method: str, path: str, body: dict | None = None):
    token, repo = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPO not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else {}


def ticket_keys(pr: dict) -> list[str]:
    text = f"{pr['head']['ref']} {pr.get('title') or ''}".upper()
    return list(dict.fromkeys(KEY_RE.findall(text)))


def list_pipeline_prs() -> list[dict]:
    """Ticket PRs only (tooling branches like fix/g11-git-sync have no ticket key)."""
    prs = _gh("GET", f"/pulls?state=all&base={BASE}&per_page=50&sort=updated&direction=desc")
    return [p for p in prs if p["head"]["ref"].startswith(BRANCH_PREFIXES) and ticket_keys(p)]


def update_branch(number: int, head_sha: str) -> tuple[bool, str]:
    try:
        _gh("PUT", f"/pulls/{number}/update-branch", {"expected_head_sha": head_sha})
        return True, "Update branch requested (main merged in on GitHub; Validate re-runs)"
    except urllib.error.HTTPError as e:
        return False, f"Update branch failed HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
    except Exception as e:
        return False, f"Update branch failed: {e}"


# ------------------------------------------------------------------ decision (pure)
def _hours_since(iso: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600


def decide(pr: dict, prev: dict, now: datetime) -> str:
    if pr.get("merged_at"):
        return "MERGED"
    if pr["state"] == "closed":
        return "REJECTED"
    ms = pr.get("mergeable_state") or "unknown"
    if ms == "dirty":
        return "CONFLICT"
    if ms == "behind" and AUTO_UPDATE and prev.get("updated_for") != pr["base"]["sha"]:
        return "UPDATE"
    last = prev.get("reminded_at") or pr["created_at"]
    if _hours_since(last, now) >= REMIND_HOURS:
        return "REMIND"
    return "WAITING"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ snapshot (v7.1)
def snapshot_path() -> Path:
    return STATE_FILE.parent / "pr-status.json"


def build_snapshot(prs: list[dict], details: dict | None = None, now: datetime | None = None) -> dict:
    """prs = GitHub PR summaries; details = {number: detail json} for open PRs (mergeable state)."""
    details = details or {}
    now = now or datetime.now(timezone.utc)
    out = []
    for p in prs:
        d = details.get(p["number"], {})
        state = "merged" if p.get("merged_at") else p.get("state", "open")
        out.append({
            "number": p["number"], "keys": ticket_keys(p), "branch": p["head"]["ref"],
            "title": p.get("title") or "", "state": state, "url": p.get("html_url"),
            "created_at": p.get("created_at"), "merged_at": p.get("merged_at"),
            "closed_at": p.get("closed_at"), "draft": bool(p.get("draft")),
            "mergeable_state": d.get("mergeable_state") or (None if state != "open" else "unknown"),
            "merge_sha": p.get("merge_commit_sha") if state == "merged" else None,
        })
    return {"generated_at": now.isoformat(timespec="seconds"), "prs": out}


def write_snapshot(prs: list[dict], details: dict | None = None, now: datetime | None = None) -> Path:
    path = snapshot_path()
    _write(path, build_snapshot(prs, details, now))
    return path


def track_all(apply: bool = True, now: datetime | None = None,
              prs: list[dict] | None = None, detail_fn=None) -> tuple[list[dict], set[int]]:
    """Returns (events, open_pr_numbers). events = new transitions for the caller to act on.
    prs/detail_fn are injectable for tests."""
    now = now or datetime.now(timezone.utc)
    prs = list_pipeline_prs() if prs is None else prs
    detail_fn = detail_fn or (lambda n: _gh("GET", f"/pulls/{n}"))
    first_run = not STATE_FILE.exists()
    state = _read(STATE_FILE)
    events, open_numbers, details = [], set(), {}
    for summary in prs:
        n = str(summary["number"])
        prev = state.get(n, {})
        if summary["state"] == "open":
            open_numbers.add(summary["number"])
        if prev.get("last") in TERMINAL:
            continue
        if first_run and summary["state"] == "closed":
            state[n] = {"last": "MERGED" if summary.get("merged_at") else "REJECTED",
                        "keys": ticket_keys(summary), "baseline": True}
            continue
        pr = detail_fn(summary["number"]) if summary["state"] == "open" else summary
        if summary["state"] == "open":
            details[summary["number"]] = pr
        action = decide(pr, prev, now)
        ev = {"number": pr["number"], "url": pr["html_url"], "branch": pr["head"]["ref"],
              "keys": ticket_keys(pr), "action": action,
              "merge_sha": pr.get("merge_commit_sha"), "merged_at": pr.get("merged_at"),
              "age_h": round(_hours_since(pr["created_at"], now), 1), "detail": ""}
        if action == "UPDATE":
            if apply:
                ok, msg = update_branch(pr["number"], pr["head"]["sha"])
                ev["detail"] = msg
                if ok:
                    prev["updated_for"] = pr["base"]["sha"]
            else:
                ev["detail"] = "would press Update branch"
        if action == "REMIND":
            prev["reminded_at"] = now.isoformat()
        if action != prev.get("last") or action == "REMIND":
            if action != "WAITING":
                events.append(ev)
        prev["last"] = "WAITING" if action in ("REMIND", "UPDATE") else action
        prev["keys"] = ev["keys"]
        state[n] = prev
    if apply:
        _write(STATE_FILE, state)
        try:
            write_snapshot(prs, details, now)
        except Exception as e:  # the snapshot is for the dashboard only - never fail tracking
            print(f"  [pr-tracker] WARNING: could not write pr-status.json: {e}")
    return events, open_numbers


# ------------------------------------------------------------------ waiting state
def mark_waiting(key: str, kind: str, **info) -> None:
    w = _read(WAITING_FILE)
    w[key] = {"kind": kind, "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}
    _write(WAITING_FILE, w)


def waiting_info(key: str) -> dict:
    return _read(WAITING_FILE).get(key, {})


def clear_waiting(key: str) -> None:
    w = _read(WAITING_FILE)
    if w.pop(key, None) is not None:
        _write(WAITING_FILE, w)


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    snap = "--snapshot" in sys.argv
    now = datetime.now(timezone.utc)
    rows = list_pipeline_prs()
    details = {}
    print(f"{'PR':>4}  {'action':<9} {'merge':<9} {'age h':>6}  keys / branch")
    for s in rows:
        pr = _gh("GET", f"/pulls/{s['number']}") if s["state"] == "open" else s
        if s["state"] == "open":
            details[s["number"]] = pr
        print(f"{pr['number']:>4}  {decide(pr, {}, now):<9} {str(pr.get('mergeable_state') or '-'):<9} "
              f"{_hours_since(pr['created_at'], now):>6.1f}  {','.join(ticket_keys(pr))}  {pr['head']['ref']}")
    if not rows:
        print("  (no ticket PRs found)")
    if apply:
        evs, open_n = track_all(apply=True, prs=rows)
        print(f"\nopen ticket PRs: {sorted(open_n) or 'none'}")
        for e in evs:
            print(f"event: {e['action']:<8} PR #{e['number']} {','.join(e['keys'])} {e['detail']}")
        print(f"state saved: {STATE_FILE}")
        print(f"snapshot saved: {snapshot_path()}")
    elif snap:
        print(f"\nsnapshot saved: {write_snapshot(rows, details, now)}")
