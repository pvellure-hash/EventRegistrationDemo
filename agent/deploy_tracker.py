"""deploy_tracker.py - v7.2: follow the Salesforce deploy after every pipeline PR merge, and
move the Jira ticket to Done only when the deploy has SUCCEEDED.

Why: a merge is not a release. salesforce-deploy.yml deploys main to the org, but only after
a person approves the salesforce-deploy-gate environment, and the deploy can still fail
(Apex tests, component errors). Before v7.2 a merged ticket was moved to Done straight away.

How it works (no AI, $0; called by watch_queue.track_prs() every poll):
  1. on_merged(ev)   - replaces the old "merged -> Done" step. Jira: comment, label ai-merged,
                       ai-locked removed, status UNCHANGED (stays In Review). The PR is added
                       to logs/deploy-state.json with its merge commit (sha) and merge time.
  2. track()         - reads the latest runs of the deploy workflow from the GitHub Actions
                       API and matches each tracked merge to the run for its merge commit.
                       Per tracked PR it decides ONE state (decide()):

     GitHub Actions run                         state              action (once)
     ------------------------------------------------------------------------------------------
     none yet (< DEPLOY_NO_RUN_HOURS)           pending            nothing
     run "waiting" (environment approval)       awaiting_approval  reminder after PR_REMIND_HOURS
     run queued / in progress                   deploying          nothing
     completed, conclusion success              deployed           Jira: comment + run link,
                                                                   label ai-deployed, -> JIRA_STATUS_DONE
     completed, failure / cancelled / timed_out failed             Jira: comment + run link,
                                                                   label ai-deploy-failed, alert
     own run failed or missing, but a LATER     deployed           same as deployed ("included in
       run on main succeeded after the merge                         a later deploy")
     no run after DEPLOY_NO_RUN_HOURS           no_run             alert once: check Actions
                                                                   (e.g. no force-app change)

     A failed deploy that is re-run and then succeeds moves to deployed (ai-deploy-failed is
     removed). deployed is final; anything else is re-checked for DEPLOY_TRACK_DAYS.
  3. For the timeline it also stores, once per completed run, the job's start/end time
     (GET /actions/runs/{id}/jobs): approval wait = job start - run created,
     deploy duration = job end - job start.

Files: logs/deploy-state.json (read by generate_pipeline_dashboard.py).
Token: needs Actions: Read-only on the fine-grained GITHUB_TOKEN. A 403/404 is reported once
per session and tracking fails OPEN (the watcher carries on).
.env:
  DEPLOY_TRACKING_ENABLED=true     false = old behaviour (Done on merge)
  DEPLOY_WORKFLOW=salesforce-deploy.yml
  DEPLOY_NO_RUN_HOURS=2
  DEPLOY_TRACK_DAYS=3
  JIRA_STATUS_DONE=Done            (shared with update_jira.py)
CLI (run from the agent folder):
  python deploy_tracker.py            table of tracked merges and their deploy state (read-only)
  python deploy_tracker.py --apply    run one tracking cycle now (Jira + state file)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "logs" / "deploy-state.json"


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
ENABLED = os.environ.get("DEPLOY_TRACKING_ENABLED", "true").strip().lower() == "true"
WORKFLOW = os.environ.get("DEPLOY_WORKFLOW", "salesforce-deploy.yml").strip() or "salesforce-deploy.yml"
BASE = (os.environ.get("GITHUB_BASE_BRANCH") or os.environ.get("BASE_BRANCH") or "main").strip()
NO_RUN_HOURS = float(os.environ.get("DEPLOY_NO_RUN_HOURS", "2") or 2)
TRACK_DAYS = float(os.environ.get("DEPLOY_TRACK_DAYS", "3") or 3)
REMIND_HOURS = float(os.environ.get("PR_REMIND_HOURS", "4") or 4)
STATUS_DONE = (os.environ.get("JIRA_STATUS_DONE") or "Done").strip()
FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure", "action_required"}
LABEL_MERGED, LABEL_DEPLOYED, LABEL_FAILED = "ai-merged", "ai-deployed", "ai-deploy-failed"
_warned = {"api": False}


# ------------------------------------------------------------------ helpers
def _t(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


def _secs(a, b):
    ta, tb = _t(a), _t(b)
    return round((tb - ta).total_seconds(), 1) if ta and tb else None


def _read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _gh(path: str):
    token, repo = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPO not set")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def list_runs() -> list[dict]:
    data = _gh(f"/actions/workflows/{WORKFLOW}/runs?branch={BASE}&per_page=30")
    return data.get("workflow_runs", [])


def job_times(run_id) -> dict:
    """{'job_started': iso, 'job_completed': iso} of the deploy job (first job)."""
    jobs = _gh(f"/actions/runs/{run_id}/jobs").get("jobs", [])
    if not jobs:
        return {}
    j = jobs[0]
    return {"job_started": j.get("started_at"), "job_completed": j.get("completed_at")}


# ------------------------------------------------------------------ decision (pure, tested)
def _run_state(run: dict) -> str:
    st = (run.get("status") or "").lower()
    if st == "completed":
        c = (run.get("conclusion") or "").lower()
        if c == "success":
            return "deployed"
        if c in FAILED_CONCLUSIONS:
            return "failed"
        return "failed" if c else "deploying"   # skipped / neutral / stale -> treat as failed
    if st == "waiting":
        return "awaiting_approval"
    return "deploying"                          # queued / requested / pending / in_progress


def decide(entry: dict, runs: list[dict], now: datetime) -> tuple[str, dict | None, str]:
    """Returns (state, run_used, note). Pure - no I/O."""
    sha, merged = entry.get("merge_sha"), _t(entry.get("merged_at"))
    own = [r for r in runs if sha and r.get("head_sha") == sha]
    own.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    run = own[0] if own else None
    later_ok = None
    if merged:
        ok = [r for r in runs if _run_state(r) == "deployed" and _t(r.get("created_at"))
              and _t(r.get("created_at")) >= merged and r is not run]
        ok.sort(key=lambda r: r.get("created_at") or "")
        later_ok = ok[0] if ok else None
    if run:
        st = _run_state(run)
        if st == "failed" and later_ok and _t(later_ok["created_at"]) > _t(run["created_at"]):
            return "deployed", later_ok, "included in a later successful deploy"
        return st, run, ""
    if later_ok:
        return "deployed", later_ok, "included in a later successful deploy"
    if merged and (now - merged).total_seconds() / 3600 >= NO_RUN_HOURS:
        return "no_run", None, f"no deploy run for {sha[:7] if sha else 'this merge'} after {NO_RUN_HOURS:g} h"
    return "pending", None, ""


# ------------------------------------------------------------------ Jira side (thin, uses jira_client)
def _jc():
    import jira_client
    return jira_client


def _notify(kind, key, title, detail="", url=""):
    try:
        import notify
        notify.notify_event(kind, key, title, detail, url)
    except Exception as e:
        print(f"  [deploy] WARNING notify: {e}")


def _labels(key):
    return _jc().get_issue(key, fields="labels")["fields"].get("labels", [])


def on_merged(ev: dict) -> None:
    """Replaces update_jira's MERGED handling when deploy tracking is on: no Done yet."""
    jc = _jc()
    n, url = ev["number"], ev["url"]
    state = _read()
    rec = state.setdefault(str(n), {})
    rec.update({"pr": n, "pr_url": url, "keys": ev["keys"], "merge_sha": ev.get("merge_sha"),
                "merged_at": ev.get("merged_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")})
    rec.setdefault("state", "pending")
    if not jc.DRY_RUN:
        _write(state)
    for key in ev["keys"]:
        try:
            if LABEL_MERGED in _labels(key):
                print(f"  {key}: already {LABEL_MERGED} - skipping")
                continue
            jc.add_comment(key, [
                f"Pull request #{n} was merged into main: {url}",
                "The Salesforce deploy now waits for approval in GitHub Actions "
                "(Review deployments -> salesforce-deploy-gate).",
                f"This ticket moves to {STATUS_DONE} automatically once the deploy succeeds."])
            jc.update_labels(key, add=[LABEL_MERGED], remove=["ai-locked"])
            _notify("pr_merged", key, f"PR #{n} merged - deploy awaiting approval", url=url)
            jc.audit("deploy_tracker", key, f"PR #{n} merged; waiting for deploy")
        except Exception as e:
            print(f"  [deploy] WARNING {key} PR #{n} merged: {e}")


def _apply(rec: dict, new: str, run: dict | None, note: str, now: datetime) -> list[str]:
    """Jira + notifications for a state change. Returns printable lines."""
    jc = _jc()
    out, n = [], rec["pr"]
    link = (run or {}).get("html_url", "")
    took = ""
    if rec.get("job_started") and rec.get("job_completed"):
        took = f" Approval wait {_fmt(rec.get('approval_wait_s'))}, deploy {_fmt(rec.get('deploy_s'))}."
    for key in rec.get("keys", []):
        try:
            labels = _labels(key)
            if new == "deployed":
                if LABEL_DEPLOYED in labels:
                    continue
                jc.add_comment(key, [
                    f"Salesforce deploy SUCCEEDED for PR #{n}" + (f" ({note})" if note else "") + f": {link}",
                    ("Deployed to the org." + took).strip(),
                    f"Ticket moved to {STATUS_DONE} by the pipeline."])
                jc.update_labels(key, add=[LABEL_DEPLOYED], remove=[LABEL_FAILED])
                jc.transition(key, STATUS_DONE)
                _notify("deploy_succeeded", key, f"PR #{n} deployed to Salesforce", note, link)
                out.append(f"{key}: deployed -> {STATUS_DONE}")
            elif new == "failed":
                jc.add_comment(key, [
                    f"Salesforce deploy FAILED for PR #{n}: {link}",
                    "Nothing from this deploy is live (Salesforce rolls the whole deploy back). Open the run "
                    "summary (component errors / Apex test failures) or Setup -> Deployment Status.",
                    "Fix forward with a new ticket, or re-run the job in GitHub Actions if it was a "
                    "platform problem. The ticket stays out of Done until a deploy succeeds."])
                jc.update_labels(key, add=[LABEL_FAILED])
                _notify("deploy_failed", key, f"PR #{n} deploy FAILED", (run or {}).get("conclusion", ""), link)
                out.append(f"{key}: deploy failed")
            elif new == "no_run":
                _notify("deploy_no_run", key, f"PR #{n}: no deploy run found", note, rec.get("pr_url", ""))
                out.append(f"{key}: no deploy run")
            jc.audit("deploy_tracker", key, f"PR #{n} deploy {new} {note}".strip())
        except Exception as e:
            print(f"  [deploy] WARNING {key} PR #{n} {new}: {e}")
    return out


def _fmt(s):
    if s is None:
        return "n/a"
    s = int(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def track(apply: bool = True, now: datetime | None = None, runs: list[dict] | None = None,
          jobs_fn=None) -> list[str]:
    """One tracking cycle. Returns printable change lines. Fails open."""
    if not ENABLED:
        return []
    now = now or datetime.now(timezone.utc)
    state = _read()
    active = {k: v for k, v in state.items() if v.get("state") != "deployed"
              and _t(v.get("merged_at")) and (now - _t(v["merged_at"])).days < TRACK_DAYS}
    if not active:
        return []
    if runs is None:
        try:
            runs = list_runs()
        except urllib.error.HTTPError as e:
            if not _warned["api"]:
                print(f"  [deploy] WARNING: cannot read GitHub Actions runs (HTTP {e.code}). "
                      f"The fine-grained token needs 'Actions: Read-only'. Tracking skipped.")
                _warned["api"] = True
            return []
        except Exception as e:
            print(f"  [deploy] WARNING (non-blocking): {e}")
            return []
    jobs_fn = jobs_fn or job_times
    lines = []
    for k, rec in active.items():
        new, run, note = decide(rec, runs, now)
        if run:
            rec.update({"run_id": run.get("id"), "run_url": run.get("html_url"),
                        "run_created": run.get("created_at"), "run_status": run.get("status"),
                        "run_conclusion": run.get("conclusion"), "run_attempt": run.get("run_attempt")})
            if new in ("deployed", "failed") and not rec.get("job_completed"):
                try:
                    rec.update(jobs_fn(run["id"]))
                except Exception:
                    pass
            if new == "deploying" and not rec.get("job_started"):
                try:
                    rec.update(jobs_fn(run["id"]))
                except Exception:
                    pass
            rec["approval_wait_s"] = _secs(rec.get("run_created"), rec.get("job_started"))
            rec["deploy_s"] = _secs(rec.get("job_started"), rec.get("job_completed"))
        rec["note"] = note
        if new == "awaiting_approval":
            rec.setdefault("waiting_since", run.get("created_at") if run else now.isoformat())
            last = _t(rec.get("reminded_at") or rec.get("waiting_since"))
            if last and (now - last).total_seconds() / 3600 >= REMIND_HOURS:
                rec["reminded_at"] = now.isoformat(timespec="seconds")
                if apply:
                    for key in rec.get("keys", []):
                        _notify("deploy_reminder", key, f"PR #{rec['pr']} deploy waiting for approval",
                                url=rec.get("run_url", ""))
                lines.append(f"PR #{rec['pr']}: deploy still awaiting approval (reminder sent)")
        old = rec.get("state")
        if new != old:
            lines.append(f"PR #{rec['pr']} {','.join(rec.get('keys', []))}: deploy {old} -> {new}"
                         + (f" ({note})" if note else ""))
            if apply and new in ("deployed", "failed", "no_run"):
                lines += ["  " + x for x in _apply(rec, new, run, note, now)]
            rec["state"] = new
            rec["state_at"] = now.isoformat(timespec="seconds")
            if new == "deployed":
                rec["deployed_at"] = (run or {}).get("updated_at") or rec.get("job_completed") \
                    or now.isoformat(timespec="seconds")
    if apply:
        state.update(active)
        _write(state)
    return lines


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    st = _read()
    if not st:
        print("No merges tracked yet (logs/deploy-state.json is empty). Merges are added by the watcher.")
    if apply:
        for line in track(apply=True):
            print(line)
        st = _read()
    print(f"{'PR':>4}  {'state':<18} {'merged':<20} keys / run")
    for k, v in sorted(st.items(), key=lambda kv: int(kv[0])):
        print(f"{v.get('pr', k):>4}  {v.get('state', '?'):<18} {str(v.get('merged_at', ''))[:19]:<20} "
              f"{','.join(v.get('keys', []))}  {v.get('run_url') or ''}")
