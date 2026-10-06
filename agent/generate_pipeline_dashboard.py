"""generate_pipeline_dashboard.py
Rebuilds the AI Delivery Command Center dashboard from REAL pipeline telemetry:
  logs/run-events.jsonl         every step of every ticket run (append-only, events.py)
  logs/<KEY>-usage.json         Copilot usage: tokens, model, files/lines changed
  logs/<KEY>-pr.json            PR created by create_pr.py (url, title, batch members)
  logs/pr-status.json           v7.1: GitHub snapshot written by pr_tracker.py every poll
                                (merged / closed / open, created / merged / closed times)
  logs/waiting.json             v7.1: why ai-waiting tickets are waiting
It re-reads everything and rewrites logs/pipeline-dashboard.html from scratch on every run,
so it accumulates automatically as the logs grow. watch_queue.py calls it after the agent
step, at the end of every ticket, and after every poll that saw a PR change.

v7.1 additions
  - PR status per run: merged / closed without merge / open (from pr-status.json), with
    merged time, time to merge (draft PR -> merge) and lead time (claim -> merge).
  - Step timeline per run: start time, end time, duration and result of every step
    (claim, prepare, AI agent, review gate, validation, PR, Jira) plus PR merged, and the
    total end-to-end time (claim -> draft PR, from the watcher's own run event).
  - Change size and model from the Copilot usage file (codeChanges, currentModel).
  - Specific outcomes: waiting, agent_no_changes, agent_uncommitted, blocked (pre-check).
  - Review queue: open PRs (age, mergeable state) and ai-waiting tickets (reason).
  - Average time per step (where the time goes).
Fields that are still NOT captured anywhere are shown as "not logged", never invented:
tool-call counts, and the Salesforce deploy result after merge.

Usage (from the agent folder):  python generate_pipeline_dashboard.py
"""
import os
import re
import sys
import json
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
LOGS_DIR = os.path.join(REPO_ROOT, "logs")
EVENTS_PATH = os.path.join(LOGS_DIR, "run-events.jsonl")
TEMPLATE_PATH = os.path.join(HERE, "dashboard_template.html")
OUTPUT_PATH = os.path.join(LOGS_DIR, "pipeline-dashboard.html")
PR_STATUS_PATH = os.path.join(LOGS_DIR, "pr-status.json")
WAITING_PATH = os.path.join(LOGS_DIR, "waiting.json")

TIER_BY_COMPLEXITY = {"simple": "Economy", "moderate": "Standard", "complex": "Premium"}
# Steps the watcher runs, in order (watch_queue.PIPELINE_STEPS) and their display labels.
STEP_ORDER = ["prepare", "agent_step", "review", "validate", "pr", "jira"]
STEP_LABEL = {"prepare": "Localise & prepare (branch, prompt)", "agent_step": "AI agent (Copilot CLI)",
              "review": "Human review gate", "validate": "Validation (static + Apex tests)",
              "pr": "Push + draft pull request", "jira": "Jira update"}
STEP_FAILURE_OUTCOME = {"prepare": "blocked_triage", "localise": "blocked_triage", "agent": "agent_failed",
                        "agent_step": "agent_failed", "review": "rejected_gate", "validate": "failed_validation",
                        "pr": "pr_failed", "jira": "jira_failed", "precheck": "blocked"}
REASON_CODE_OUTCOME = {"locked": "locked", "budget": "budget_blocked", "ceiling": "ceiling_hit",
                       "prod_org_guard": "blocked_other", "mcp_allowlist": "blocked_other", "timeout": "timed_out",
                       "ceiling_overshoot": "ceiling_hit", "open_pr_overlap": "waiting",
                       "agent_no_changes": "agent_no_changes", "agent_uncommitted": "agent_uncommitted",
                       "precheck": "blocked"}
# watch_queue run-event outcomes that are already precise - kept as they are
RUN_OUTCOME_ALIASES = {"blocked": "blocked", "agent_failed": "agent_failed"}


def load_events():
    if not os.path.exists(EVENTS_PATH):
        print(f"No events file found at {EVENTS_PATH} - nothing to build yet.")
        return []
    out = []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def load_json_if_exists(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def parse_time(s):
    if not s:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.astimezone()
    except Exception:
        return None


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def secs(a, b):
    return round((b - a).total_seconds(), 1) if a and b else None


def confidence_from_detail(detail):
    m = re.search(r"confidence=([\d.]+)", detail or "")
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


# ---------------------------------------------------------------- PR status (v7.1)
def load_pr_index():
    """{ticket key: [pr dicts, newest first]} from pr-status.json (all keys of a batch PR)."""
    snap = load_json_if_exists(PR_STATUS_PATH) or {}
    idx = {}
    for p in snap.get("prs", []):
        for k in p.get("keys", []):
            idx.setdefault(k, []).append(p)
    for k in idx:
        idx[k].sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return idx, snap


def match_pr(ticket, pr_url, run_end, pr_index):
    """The PR this run produced: same URL if known, else the newest PR for the ticket created
    at or after the run started (so an older PR for the same key is not misattributed)."""
    cands = pr_index.get(ticket, [])
    if pr_url:
        for p in cands:
            if p.get("url") == pr_url:
                return p
    for p in cands:
        c = parse_time(p.get("created_at"))
        if run_end is None or (c and c >= run_end - datetime.timedelta(hours=12)):
            return p
    return None


# ---------------------------------------------------------------- per-run records
def build_steps(evs):
    """Ordered step list with start, end, duration and status, from started/ok/failed pairs."""
    steps, open_ = {}, {}
    for e in evs:
        st = e.get("step")
        if st not in STEP_ORDER:
            continue
        t = parse_time(e.get("time"))
        if e.get("status") == "started":
            open_[st] = t
            steps.setdefault(st, {"step": st, "label": STEP_LABEL[st], "start": None, "end": None,
                                  "dur_s": None, "status": "running"})
            steps[st]["start"] = iso(t)
        elif e.get("status") in ("ok", "failed", "blocked", "skipped"):
            s = steps.setdefault(st, {"step": st, "label": STEP_LABEL[st], "start": None, "end": None,
                                      "dur_s": None, "status": None})
            s["end"] = iso(t)
            d = e.get("duration_s")
            s["dur_s"] = d if isinstance(d, (int, float)) else secs(open_.get(st), t)
            if not s["start"] and t and isinstance(d, (int, float)):
                s["start"] = iso(t - datetime.timedelta(seconds=d))
            s["status"] = e.get("status")
    return [steps[s] for s in STEP_ORDER if s in steps]


def run_outcome(evs):
    """Precise final outcome of one run."""
    specific = None
    for e in evs:   # invoke_copilot's own no-change outcomes beat the generic agent_failed
        if e.get("outcome") in ("agent_no_changes", "agent_uncommitted"):
            specific = e["outcome"]
    run_end = [e for e in evs if e.get("step") == "run" and e.get("status") in ("ok", "failed")]
    if run_end:
        o = run_end[-1].get("outcome") or ("pr_open" if run_end[-1].get("status") == "ok" else None)
        if specific and o in (None, "agent_failed", "failed"):
            return specific
        if o:
            return o
    if specific:
        return specific
    for e in reversed(evs):
        if e.get("status") in ("blocked", "failed"):
            rc = e.get("reason_code") or ""
            return REASON_CODE_OUTCOME.get(rc) or STEP_FAILURE_OUTCOME.get(e.get("step"), "blocked_other")
    return "unknown"


def build_ticket_records(events, pr_index=None):
    pr_index = pr_index if pr_index is not None else load_pr_index()[0]
    groups, order = {}, []
    for e in events:
        run_id, ticket = e.get("run_id"), e.get("ticket")
        if not run_id or run_id == "-" or not ticket or ticket == "-":
            continue
        if run_id not in groups:
            groups[run_id] = []
            order.append(run_id)
        groups[run_id].append(e)
    # Copilot writes ONE usage file per ticket key (latest attempt), so only the latest run of
    # a ticket that reached the agent may use it - an earlier attempt must not show its numbers.
    latest_agent_run = {}
    for run_id in order:
        if any(e.get("step") == "agent" for e in groups[run_id]):
            latest_agent_run[groups[run_id][0].get("ticket")] = run_id
    records = []
    for run_id in order:
        evs = sorted(groups[run_id], key=lambda e: e.get("time", ""))
        ticket = evs[0].get("ticket")
        project = evs[0].get("project") or "unknown"
        run_start_ev = next((e for e in evs if e.get("step") == "run" and e.get("status") == "started"), evs[0])
        run_end_ev = next((e for e in reversed(evs) if e.get("step") == "run" and e.get("status") != "started"), None)
        t_start = parse_time(run_start_ev.get("time"))
        t_end = parse_time(run_end_ev.get("time")) if run_end_ev else None
        e2e = run_end_ev.get("duration_s") if run_end_ev and isinstance(run_end_ev.get("duration_s"), (int, float)) \
            else secs(t_start, t_end)

        complexity, confidence = "unknown", None
        for e in evs:
            if e.get("step") == "localise" and e.get("status") == "ok":
                rc = e.get("reason_code")
                if rc in TIER_BY_COMPLEXITY or (rc or "").startswith("batch-"):
                    complexity = rc if rc in TIER_BY_COMPLEXITY else complexity
                c = confidence_from_detail(e.get("detail"))
                if c is not None:
                    confidence = c
        tier = TIER_BY_COMPLEXITY.get(complexity, "Unknown")

        agent_events = [e for e in evs if e.get("step") == "agent"]
        runs = len([e for e in agent_events if e.get("status") == "started"])
        credits, any_credit, agent_dur = 0.0, False, 0.0
        for e in agent_events:
            c = e.get("ai_credits")
            if isinstance(c, (int, float)):
                credits += c
                any_credit = True
            d = e.get("duration_s")
            if isinstance(d, (int, float)):
                agent_dur = d

        steps = build_steps(evs)
        dur = {s["step"]: s["dur_s"] or 0 for s in steps}
        outcome = run_outcome(evs)

        usage = load_json_if_exists(os.path.join(LOGS_DIR, f"{ticket}-usage.json")) \
            if latest_agent_run.get(ticket) == run_id else None
        tokens, files, lines, model = {"fresh": 0, "cached": 0, "out": 0}, 0, 0, None
        if usage:
            td = usage.get("tokenDetails") or {}
            tokens = {"fresh": (td.get("input") or {}).get("tokenCount", 0) or 0,
                      "cached": (td.get("cache_read") or {}).get("tokenCount", 0) or 0,
                      "out": (td.get("output") or {}).get("tokenCount", 0) or 0}
            cc = usage.get("codeChanges") or {}
            files = cc.get("filesModifiedCount") or 0
            lines = (cc.get("linesAdded") or 0) + (cc.get("linesRemoved") or 0)
            model = usage.get("currentModel")

        pr_info = load_json_if_exists(os.path.join(LOGS_DIR, f"{ticket}-pr.json")) or {}
        title = pr_info.get("title") or ticket
        pr_url = pr_info.get("pr_url")
        if pr_url and pr_url.startswith("<"):
            pr_url = None
        members = pr_info.get("members") or []
        if run_outcome(evs) != "pr_open":   # the <KEY>-pr.json belongs to a later, successful run
            pr_url, members = None, []

        pr_state = pr_number = merged_at = closed_at = pr_created = mergeable = None
        if outcome == "pr_open":
            p = match_pr(ticket, pr_url, t_end, pr_index)
            if p:
                pr_state, pr_number, pr_url = p.get("state"), p.get("number"), p.get("url") or pr_url
                merged_at, closed_at, pr_created = p.get("merged_at"), p.get("closed_at"), p.get("created_at")
                mergeable = p.get("mergeable_state")
                title = title if title != ticket else (p.get("title") or ticket)
                if pr_state == "merged":
                    outcome = "merged"
                elif pr_state == "closed":
                    outcome = "pr_rejected"
            else:
                pr_state = "open"
        t_merged = parse_time(merged_at)
        pr_ready = parse_time(next((s["end"] for s in steps if s["step"] == "pr" and s["status"] == "ok"), None)) \
            or parse_time(pr_created)

        records.append({
            "key": ticket, "runId": run_id, "project": project, "type": "Bug", "title": title,
            "complexity": complexity, "tier": tier, "confidence": confidence,
            "routed": complexity != "unknown", "outcome": outcome, "runs": runs or (1 if agent_events else 0),
            "toolCalls": 0, "tokens": tokens, "credits": round(credits, 4) if any_credit else 0,
            "files": files, "lines": lines, "model": model,
            "time": iso(t_start) or evs[0].get("time"), "endTime": iso(t_end), "e2e_s": e2e,
            "durations": {"copilot": round(agent_dur / 60, 1) if agent_dur else 0,
                          "gate": round(dur.get("review", 0) / 60, 1),
                          "validate": dur.get("validate", 0)},
            "steps": steps, "apex": {"run": 0}, "prUrl": pr_url, "prNumber": pr_number, "prState": pr_state,
            "prCreated": pr_created, "mergedAt": merged_at, "closedAt": closed_at, "mergeable": mergeable,
            "timeToMerge_s": secs(pr_ready, t_merged), "leadTime_s": secs(t_start, t_merged),
            "members": members,
        })
    return records


def build_queue(pr_snap):
    """Open PRs and waiting tickets for the Review queue card."""
    now = datetime.datetime.now().astimezone()
    open_prs = []
    for p in (pr_snap or {}).get("prs", []):
        if p.get("state") == "open":
            c = parse_time(p.get("created_at"))
            open_prs.append({"number": p.get("number"), "keys": p.get("keys", []), "title": p.get("title"),
                             "url": p.get("url"), "created": p.get("created_at"),
                             "age_h": max(0.0, round((now - c).total_seconds() / 3600, 1)) if c else None,
                             "mergeable": p.get("mergeable_state")})
    waiting = []
    for k, w in (load_json_if_exists(WAITING_PATH) or {}).items():
        if w.get("kind") == "pr":
            why = "Waiting on PR " + ", ".join(f"#{n}" for n in w.get("prs", []))
        else:
            why = "Blocked by " + ", ".join(w.get("blockers", []))
        waiting.append({"key": k, "why": why, "since": w.get("since")})
    return {"openPrs": sorted(open_prs, key=lambda p: -(p["age_h"] or 0)), "waiting": waiting,
            "snapshotAt": (pr_snap or {}).get("generated_at")}


def render(records, queue):
    if not os.path.exists(TEMPLATE_PATH):
        sys.exit(f"STOP: template not found at {TEMPLATE_PATH}. Copy dashboard_template.html into the agent/ folder first.")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    banner_display = "none" if records else "flex"
    banner_text = "" if records else ("<b>No tickets yet</b> — this dashboard will populate automatically "
                                      "the first time watch_queue.py completes a ticket.")
    html = html.replace("__BANNER_DISPLAY__", banner_display)
    html = html.replace("__BANNER_TEXT__", banner_text)
    html = html.replace("__PIPELINE_DATA_JSON__", json.dumps(records, ensure_ascii=False))
    html = html.replace("__QUEUE_JSON__", json.dumps(queue, ensure_ascii=False))
    html = html.replace("__MONTHLY_BUDGET__", os.getenv("MONTHLY_BUDGET_USD", "25"))
    html = html.replace("__TICKET_CEILING__", os.getenv("TICKET_CREDIT_CEILING", "60"))
    html = html.replace("__NOW_ISO__", json.dumps(datetime.datetime.now().astimezone().isoformat(timespec="seconds")))
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return OUTPUT_PATH


def main():
    pr_index, snap = load_pr_index()
    records = build_ticket_records(load_events(), pr_index)
    out = render(records, build_queue(snap))
    merged = sum(1 for r in records if r["outcome"] == "merged")
    print(f"[dashboard] rebuilt from {len(records)} ticket run(s), {merged} merged -> {out}")


if __name__ == "__main__":
    main()
