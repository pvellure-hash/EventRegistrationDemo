"""Offline tests for v7.1: PR status on the dashboard, step timings, and the PowerShell
progress line. Uses temporary folders only - never touches the real logs, Jira or GitHub.
Run from the agent folder: python test_dashboard_v71.py"""
import os, sys, io, json, types, tempfile, datetime as D
from pathlib import Path
TMP = tempfile.mkdtemp(); LOGS = os.path.join(TMP, "logs"); os.makedirs(LOGS)
os.environ["AGENT_LOG_DIR"] = LOGS
if "events" not in sys.modules:
    m = types.ModuleType("events"); m.read_events = lambda: []; sys.modules["events"] = m
import generate_pipeline_dashboard as g
import progress
import pr_tracker as pt
for k, v in dict(LOGS_DIR=LOGS, EVENTS_PATH=os.path.join(LOGS, "run-events.jsonl"),
                 OUTPUT_PATH=os.path.join(LOGS, "out.html"), PR_STATUS_PATH=os.path.join(LOGS, "pr-status.json"),
                 WAITING_PATH=os.path.join(LOGS, "waiting.json")).items():
    setattr(g, k, v)
pt.STATE_FILE = Path(LOGS) / "pr-state.json"; pt.WAITING_FILE = Path(LOGS) / "waiting.json"
ok = 0
def chk(c, m):
    global ok
    if not c: raise AssertionError(m)
    ok += 1; print("  PASS", m)

tz = D.timezone(D.timedelta(hours=-7)); E = []
def ev(run, tk, step, st, t, **k):
    E.append({"run_id": run, "ticket": tk, "project": "P", "step": step, "status": st, "time": t.isoformat(), **k})
def run(tk, rid, start, steps, outcome, credits=1.0, fail=None, agent_outcome=None):
    t = start; ev(rid, tk, "run", "started", t)
    ev(rid, tk, "localise", "ok", t, reason_code="moderate", detail="confidence=0.77")
    for st, d in steps:
        ev(rid, tk, st, "started", t)
        if st == "agent_step":
            ev(rid, tk, "agent", "started", t); ev(rid, tk, "agent", "ok", t, ai_credits=credits, duration_s=d)
            if agent_outcome: ev(rid, tk, "agent", "failed", t, outcome=agent_outcome, reason_code=agent_outcome)
        t += D.timedelta(seconds=d); ev(rid, tk, st, "failed" if st == fail else "ok", t, duration_s=d)
        if st == fail: break
    ev(rid, tk, "run", "failed" if fail else "ok", t, outcome=outcome, duration_s=(t - start).total_seconds())
full = [("prepare", 10), ("agent_step", 100), ("review", 5), ("validate", 20), ("pr", 10), ("jira", 5)]
t0 = D.datetime(2026, 10, 5, 21, 0, tzinfo=tz)
run("CLAUDE-16", "a", t0, full, "pr_open")
run("CLAUDE-18", "b", t0 + D.timedelta(minutes=10), [("prepare", 10), ("agent_step", 30)], "agent_failed",
    credits=0.46, fail="agent_step", agent_outcome="agent_no_changes")
run("CLAUDE-18", "c", t0 + D.timedelta(minutes=30), full, "pr_open")
run("CLAUDE-19", "d", t0 + D.timedelta(minutes=40), [("prepare", 9)], "waiting", fail=None)
run("CLAUDE-20", "e", t0 + D.timedelta(minutes=50), full, "pr_open")
open(g.EVENTS_PATH, "w").write("\n".join(json.dumps(e) for e in E))
pr_done_16 = t0 + D.timedelta(seconds=150)
json.dump({"generated_at": "x", "prs": [
    {"number": 16, "keys": ["CLAUDE-16"], "state": "merged", "url": "u16", "created_at": pr_done_16.isoformat(),
     "merged_at": (pr_done_16 + D.timedelta(minutes=30)).isoformat()},
    {"number": 24, "keys": ["CLAUDE-18"], "state": "open", "url": "u24",
     "created_at": (t0 + D.timedelta(minutes=33)).isoformat(), "mergeable_state": "clean"},
    {"number": 25, "keys": ["CLAUDE-20"], "state": "closed", "url": "u25",
     "created_at": (t0 + D.timedelta(minutes=53)).isoformat(), "closed_at": (t0 + D.timedelta(hours=2)).isoformat()}]},
    open(g.PR_STATUS_PATH, "w"))
json.dump({"pr_url": "u24", "title": "[CLAUDE-18] Edit", "members": []}, open(os.path.join(LOGS, "CLAUDE-18-pr.json"), "w"))
json.dump({"codeChanges": {"linesAdded": 140, "linesRemoved": 8, "filesModifiedCount": 4}, "currentModel": "m"},
          open(os.path.join(LOGS, "CLAUDE-18-usage.json"), "w"))
json.dump({"CLAUDE-19": {"kind": "pr", "prs": [24]}}, open(g.WAITING_PATH, "w"))

print("dashboard data")
idx, snap = g.load_pr_index()
R = {r["runId"]: r for r in g.build_ticket_records(g.load_events(), idx)}
chk(R["a"]["outcome"] == "merged" and R["a"]["prNumber"] == 16, "merged PR -> outcome merged (#16)")
chk(R["a"]["timeToMerge_s"] == 1805 and R["a"]["leadTime_s"] == 1950, "time to merge (draft PR step end -> merged) and lead time (claim -> merged)")
chk(R["c"]["outcome"] == "pr_open" and R["c"]["prState"] == "open" and R["c"]["mergeable"] == "clean", "open PR -> pr_open + mergeable")
chk(R["e"]["outcome"] == "pr_rejected", "closed without merge -> pr_rejected")
chk(R["b"]["outcome"] == "agent_no_changes" and R["b"]["prUrl"] is None, "no-change run: precise outcome, no PR link from later run")
chk(R["b"]["files"] == 0 and R["c"]["files"] == 4 and R["c"]["lines"] == 148, "usage file only on the latest run of a ticket")
chk(R["d"]["outcome"] == "waiting", "waiting outcome kept")
st = R["a"]["steps"]
chk([s["step"] for s in st] == ["prepare", "agent_step", "review", "validate", "pr", "jira"], "six steps in order")
chk(st[1]["dur_s"] == 100 and st[1]["start"] and st[1]["end"] and st[1]["status"] == "ok", "step has start, end, duration, status")
chk(R["a"]["e2e_s"] == 150 and R["b"]["steps"][-1]["status"] == "failed", "end-to-end time; failed step marked")
q = g.build_queue(snap)
chk([p["number"] for p in q["openPrs"]] == [24] and q["waiting"][0]["why"] == "Waiting on PR #24", "review queue: open PRs + waiting")
g.TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(g.__file__)), "dashboard_template.html")
g.render(list(R.values()), q)
html = open(g.OUTPUT_PATH, encoding="utf-8").read()
chk("__QUEUE_JSON__" not in html and "__PIPELINE_DATA_JSON__" not in html and "renderQueue" in html,
    "template filled (requires the v7.1 dashboard_template.html)")

print("PR snapshot")
pr = lambda n, **k: {"number": n, "state": "open", "merged_at": None, "created_at": "2026-10-06T10:00:00Z",
                     "html_url": f"h{n}", "head": {"ref": f"fix/CLAUDE-{n}-x", "sha": "s"}, "base": {"sha": "m"}, "title": "", **k}
pt.track_all(apply=True, now=D.datetime(2026, 10, 6, 11, tzinfo=D.timezone.utc),
             prs=[pr(30), pr(31, state="closed", merged_at="2026-10-06T10:30:00Z")],
             detail_fn=lambda n: pr(n, mergeable_state="behind"))
s = json.load(open(os.path.join(LOGS, "pr-status.json")))
by = {p["number"]: p for p in s["prs"]}
chk(by[31]["state"] == "merged" and by[31]["merged_at"] and by[30]["mergeable_state"] == "behind",
    "pr-status.json written each cycle with state, times and mergeable state")

print("progress line")
chk(progress.typical_seconds("agent_step", [{"step": "agent_step", "status": "ok", "duration_s": d} for d in (100, 200, 300)]) == 200,
    "typical = median of past successful runs")
chk(progress.typical_seconds("validate", []) == 90, "default typical until there is history")
l1 = progress.status_line(2, 6, "agent_step", 60, 120, "1 file edited, 0 commits", 900)
chk("[2/6]" in l1 and " 50%" in l1 and "1m 00s / ~2m 00s typical" in l1 and "timeout 15m" in l1, "bar shows step, %, elapsed vs typical")
l2 = progress.status_line(2, 6, "agent_step", 300, 120)
chk("longer than usual" in l2 and "100%" not in l2, "never claims 100%; says 'longer than usual'")
out = io.StringIO()
rc = progress.run_with_progress([sys.executable, "-c", "print('hello'); import sys; sys.exit(4)"], TMP, 1, 6, "prepare", out=out)
txt = out.getvalue()
chk(rc == 4 and "hello" in txt and "[1/6] prepare exit 4" in txt, "child output relayed, exit code returned")
print(f"\n{ok}/{ok} passed")
