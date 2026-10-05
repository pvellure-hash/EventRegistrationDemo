"""generate_pipeline_dashboard.py

Rebuilds the AI Delivery Command Center dashboard from REAL pipeline
telemetry in logs/run-events.jsonl (plus, when present, each ticket's
logs/<KEY>-usage.json and logs/<KEY>-pr.json).

This is the Phase 2 piece referenced in PROJECT-CONTEXT.md Section 13/15:
"wiring it to run-events.jsonl". It replaces the earlier sample-data
prototype (ai-pipeline-command-center.html) with a generator that reads
your own files and writes a real HTML dashboard.

HOW IT ACCUMULATES AUTOMATICALLY
---------------------------------
This script re-reads the ENTIRE run-events.jsonl every time it runs and
rebuilds the output file from scratch. It does not append or remember
state between runs on its own - the accumulation comes from the fact
that run-events.jsonl itself is append-only (events.py never truncates
it). So as you fix more defects, that file grows, and the next time this
script runs it will naturally include everything that came before.

HOW TO RUN IT AUTOMATICALLY AFTER EVERY TICKET
------------------------------------------------
Add one call to this script in watch_queue.py, at the exact point it
already calls generate_cost_dashboard.py (per PROJECT-CONTEXT.md Section
4: "generate_cost_dashboard.py rebuilds logs/cost-dashboard.html after
each ticket"). See the bottom of this file for the exact snippet.

WHAT IS AND ISN'T AVAILABLE FROM REAL DATA (read before trusting a number)
---------------------------------------------------------------------------
Available directly from run-events.jsonl: ticket, project, step timings/
durations, localisation confidence + complexity tier, AI credits (once
Issue C12's totalNanoAiu fix is in place), and the step-level outcome.

NOT currently captured anywhere in this pipeline, so these fields are
always 0 / "not logged" in the dashboard rather than invented:
  - tool-call counts (invoke_copilot.py does not log how many tool calls
    Copilot made per run)
  - files/lines changed (not written to run-events.jsonl; only present,
    informally, inside docs/ai-reports/<KEY>-pr.md if you want to parse
    that separately later)
  - confirmed GitHub merge status - a ticket's final locally-known state
    is "pr_open" (create_pr.py opened a draft PR) because the pipeline
    has no step that observes a human clicking Merge on GitHub, or the
    deploy-approval click in the salesforce-deploy-gate environment (R8).
    If you want "merged" to show accurately, the honest fix is to poll
    GitHub's PR API for merged_at (a small follow-up, not done here).

Usage:
    python generate_pipeline_dashboard.py
    (reads ../logs/run-events.jsonl relative to this file's folder,
     i.e. run it from the agent/ folder like the other pipeline scripts)
"""
import os
import re
import sys
import json
import glob
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
LOGS_DIR = os.path.join(REPO_ROOT, "logs")
EVENTS_PATH = os.path.join(LOGS_DIR, "run-events.jsonl")
TEMPLATE_PATH = os.path.join(HERE, "dashboard_template.html")
OUTPUT_PATH = os.path.join(LOGS_DIR, "pipeline-dashboard.html")

# Mirrors escalation.py's documented thresholds (PROJECT-CONTEXT.md Section 9):
# simple -> economy, moderate -> standard, else premium.
TIER_BY_COMPLEXITY = {"simple": "Economy", "moderate": "Standard", "complex": "Premium"}

# Mirrors watch_queue.py's own documented outcome mapping (PROJECT-CONTEXT.md
# Section 4/8): prepare->blocked, agent_step->agent_failed, review->rejected_gate,
# validate->failed_validation, pr->pr_failed, jira->jira_failed.
STEP_FAILURE_OUTCOME = {
    "prepare": "blocked_triage",
    "localise": "blocked_triage",
    "agent": "agent_failed",
    "agent_step": "agent_failed",
    "review": "rejected_gate",
    "validate": "failed_validation",
    "pr": "pr_failed",
    "jira": "jira_failed",
}
REASON_CODE_OUTCOME = {
    "locked": "locked",
    "budget": "budget_blocked",
    "ceiling": "ceiling_hit",
    "prod_org_guard": "blocked_other",
    "mcp_allowlist": "blocked_other",
    "timeout": "timed_out",
    "ceiling_overshoot": "ceiling_hit",
}


def load_events():
    if not os.path.exists(EVENTS_PATH):
        print(f"No events file found at {EVENTS_PATH} - nothing to build yet.")
        return []
    events = []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip any malformed/partial line rather than crash
    return events


def load_json_if_exists(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def confidence_from_detail(detail):
    if not detail:
        return None
    m = re.search(r"confidence=([\d.]+)", detail)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def build_ticket_records(events):
    # Group by run_id (one group = one ticket attempt). run_id "-" means a
    # startup/system event with no associated ticket - skip those.
    groups = {}
    order = []
    for e in events:
        run_id = e.get("run_id")
        ticket = e.get("ticket")
        if not run_id or run_id == "-" or not ticket or ticket == "-":
            continue
        if run_id not in groups:
            groups[run_id] = []
            order.append(run_id)
        groups[run_id].append(e)

    records = []
    for run_id in order:
        evs = sorted(groups[run_id], key=lambda e: e.get("time", ""))
        ticket = evs[0].get("ticket")
        project = evs[0].get("project") or "unknown"
        start_time = evs[0].get("time")

        complexity = "unknown"
        confidence = None
        for e in evs:
            if e.get("step") == "localise" and e.get("status") == "ok":
                rc = e.get("reason_code")
                if rc in TIER_BY_COMPLEXITY:
                    complexity = rc
                conf = confidence_from_detail(e.get("detail"))
                if conf is not None:
                    confidence = conf
        tier = TIER_BY_COMPLEXITY.get(complexity, "Unknown")

        agent_events = [e for e in evs if e.get("step") in ("agent",)]
        runs = len([e for e in agent_events if e.get("status") == "started"])
        total_credits = 0.0
        any_credit_found = False
        last_agent_duration = 0.0
        for e in agent_events:
            c = e.get("ai_credits")
            if isinstance(c, (int, float)):
                total_credits += c
                any_credit_found = True
            d = e.get("duration_s")
            if isinstance(d, (int, float)):
                last_agent_duration = d  # keep the most recent attempt's duration

        def duration_for(step_name):
            vals = [e.get("duration_s") for e in evs
                    if e.get("step") == step_name and isinstance(e.get("duration_s"), (int, float))]
            return vals[-1] if vals else 0

        gate_min = round(duration_for("review") / 60, 1) if duration_for("review") else 0
        validate_s = duration_for("validate")
        copilot_min = round(last_agent_duration / 60, 1) if last_agent_duration else 0

        # Determine outcome: prefer the terminal "run" step's own outcome field
        # (this is what watch_queue.py itself records as the ticket's result).
        outcome = None
        for e in evs:
            if e.get("step") == "run" and e.get("status") == "ok":
                outcome = e.get("outcome") or "pr_open"
        if outcome is None:
            # No clean terminal "run ok" - look for the last blocked/failed event
            # and translate it through the documented outcome mapping.
            for e in reversed(evs):
                if e.get("status") in ("blocked", "failed"):
                    rc = e.get("reason_code") or ""
                    if rc in REASON_CODE_OUTCOME:
                        outcome = REASON_CODE_OUTCOME[rc]
                    else:
                        outcome = STEP_FAILURE_OUTCOME.get(e.get("step"), "blocked_other")
                    break
        if outcome is None:
            outcome = "unknown"  # run is likely still in progress / log ends mid-run

        # Token breakdown: only available if logs/<KEY>-usage.json exists for
        # THIS specific attempt. Multiple attempts for the same ticket key will
        # all point at the same filename today (invoke_copilot.py names it
        # <KEY>-usage.json, not per-run_id) so this reflects the most recent
        # successful Copilot CLI call for that ticket, not necessarily this
        # exact run_id if the ticket was retried.
        usage = load_json_if_exists(os.path.join(LOGS_DIR, f"{ticket}-usage.json"))
        tokens = {"fresh": 0, "cached": 0, "out": 0}
        if usage and isinstance(usage.get("tokenDetails"), dict):
            td = usage["tokenDetails"]
            tokens["fresh"] = (td.get("input") or {}).get("tokenCount", 0) or 0
            tokens["cached"] = (td.get("cache_read") or {}).get("tokenCount", 0) or 0
            tokens["out"] = (td.get("output") or {}).get("tokenCount", 0) or 0

        pr_info = load_json_if_exists(os.path.join(LOGS_DIR, f"{ticket}-pr.json"))
        title = (pr_info or {}).get("title") or ticket
        pr_url = (pr_info or {}).get("pr_url")
        if pr_url and pr_url.startswith("<"):
            pr_url = None

        records.append({
            "key": ticket,
            "project": project,
            "type": "Bug",
            "title": title,
            "complexity": complexity,
            "tier": tier,
            "confidence": confidence,
            "routed": complexity != "unknown",
            "outcome": outcome,
            "runs": runs or (1 if agent_events else 0),
            "toolCalls": 0,  # not logged anywhere today - see module docstring
            "tokens": tokens,
            "credits": round(total_credits, 4) if any_credit_found else 0,
            "files": 0,       # not logged anywhere today - see module docstring
            "lines": 0,       # not logged anywhere today - see module docstring
            "time": start_time,
            "durations": {"copilot": copilot_min, "gate": gate_min, "validate": validate_s},
            "apex": {"run": 0},
            "prUrl": pr_url,
        })
    return records


def render(records):
    if not os.path.exists(TEMPLATE_PATH):
        sys.exit(f"STOP: template not found at {TEMPLATE_PATH}. "
                 f"Copy dashboard_template.html into the agent/ folder first.")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()

    if records:
        banner_display = "none"
        banner_text = ""
    else:
        banner_display = "flex"
        banner_text = ("<b>No tickets yet</b> — this dashboard will populate automatically "
                        "the first time watch_queue.py completes a ticket.")

    data_json = json.dumps(records, ensure_ascii=False)
    now_iso = datetime.datetime.now().isoformat(timespec="seconds")

    # Budget/ceiling constants mirror .env (MONTHLY_BUDGET_USD, TICKET_CREDIT_CEILING)
    # per PROJECT-CONTEXT.md Section 6; read from the real environment if set.
    monthly_budget = os.getenv("MONTHLY_BUDGET_USD", "25")
    ticket_ceiling = os.getenv("TICKET_CREDIT_CEILING", "60")

    html = html.replace("__BANNER_DISPLAY__", banner_display)
    html = html.replace("__BANNER_TEXT__", banner_text)
    html = html.replace("__PIPELINE_DATA_JSON__", data_json)
    html = html.replace("__MONTHLY_BUDGET__", monthly_budget)
    html = html.replace("__TICKET_CEILING__", ticket_ceiling)
    html = html.replace("__NOW_ISO__", json.dumps(now_iso))

    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return OUTPUT_PATH


def main():
    events = load_events()
    records = build_ticket_records(events)
    out_path = render(records)
    print(f"[dashboard] rebuilt from {len(records)} ticket run(s) -> {out_path}")


if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------
# WIRING: add this to watch_queue.py, at the same point it already calls
# generate_cost_dashboard.py (end of each ticket / after the agent step):
#
#   import subprocess, sys as _sys
#   subprocess.run([_sys.executable, "generate_pipeline_dashboard.py"],
#                  cwd=os.path.dirname(os.path.abspath(__file__)))
#
# This keeps the same "never block the loop" principle as the rest of
# Phase 0: wrap it in try/except if you want a dashboard failure to never
# stop ticket processing, exactly like generate_cost_dashboard.py today.
# -----------------------------------------------------------------------
