"""Offline tests for v7.2 Salesforce deploy tracking (deploy_tracker.py) and its dashboard view.
Fakes Jira, notifications and the GitHub Actions API; uses temporary folders only.
Run from the agent folder: python test_deploy_v72.py"""
import os, sys, io, json, types, tempfile, contextlib, datetime as D
from pathlib import Path
TMP = tempfile.mkdtemp(); LOGS = os.path.join(TMP, "logs"); os.makedirs(LOGS)
os.environ.update(DEPLOY_TRACKING_ENABLED="true", DEPLOY_NO_RUN_HOURS="2", PR_REMIND_HOURS="4",
                  JIRA_STATUS_DONE="Done")

def mod(n, **k):
    m = types.ModuleType(n); m.__dict__.update(k); sys.modules[n] = m; return m
LABELS, comments, trans, notes = {}, [], [], []
def upd(key, add=(), remove=()):
    s = set(LABELS.get(key, [])); s |= set(add); s -= set(remove); LABELS[key] = sorted(s)
mod("jira_client", DRY_RUN=False, audit=lambda *a, **k: None, update_labels=upd,
    add_comment=lambda key, lines: comments.append((key, " ".join(lines))),
    transition=lambda key, t: trans.append((key, t)) or True,
    get_issue=lambda key, fields="": {"fields": {"labels": LABELS.get(key, [])}})
mod("notify", notify_event=lambda kind, ticket, title, detail="", url="": notes.append((kind, ticket)))
if "events" not in sys.modules:
    mod("events", read_events=lambda: [])
import deploy_tracker as dt
dt.STATE_FILE = Path(LOGS) / "deploy-state.json"
ok = 0
def chk(c, m):
    global ok
    if not c: raise AssertionError(m)
    ok += 1; print("  PASS", m)
Z = D.timezone.utc
T0 = D.datetime(2026, 10, 7, 16, 0, tzinfo=Z)
iso = lambda t: t.isoformat().replace("+00:00", "Z")
def run(i, sha, status, conclusion=None, created=T0 + D.timedelta(minutes=1), updated=None):
    return {"id": i, "head_sha": sha, "status": status, "conclusion": conclusion, "created_at": iso(created),
            "updated_at": iso(updated or created + D.timedelta(minutes=5)), "html_url": f"https://gh/run/{i}", "run_attempt": 1}
entry = {"merge_sha": "aaa111", "merged_at": iso(T0)}

print("decide()")
chk(dt.decide(entry, [], T0 + D.timedelta(minutes=10))[0] == "pending", "no run yet -> pending")
chk(dt.decide(entry, [run(1, "aaa111", "waiting")], T0)[0] == "awaiting_approval", "environment gate -> awaiting_approval")
chk(dt.decide(entry, [run(1, "aaa111", "in_progress")], T0)[0] == "deploying", "in progress -> deploying")
chk(dt.decide(entry, [run(1, "aaa111", "completed", "success")], T0)[0] == "deployed", "success -> deployed")
chk(dt.decide(entry, [run(1, "aaa111", "completed", "failure")], T0)[0] == "failed", "failure -> failed")
st, r, note = dt.decide(entry, [run(1, "aaa111", "completed", "failure"),
                               run(2, "bbb222", "completed", "success", created=T0 + D.timedelta(minutes=30))], T0)
chk(st == "deployed" and r["id"] == 2 and "later" in note, "own run failed but a later run succeeded -> deployed (later deploy)")
chk(dt.decide(entry, [run(9, "zzz", "completed", "success", created=T0 - D.timedelta(hours=1))], T0 + D.timedelta(hours=3))[0] == "no_run",
    "no own run, only an older success, after 2 h -> no_run")
chk(dt.decide(entry, [run(2, "bbb", "completed", "success", created=T0 + D.timedelta(minutes=5))], T0)[0] == "deployed",
    "no own run (no force-app change) but a later main deploy succeeded -> deployed")

print("lifecycle with Jira")
LABELS["CLAUDE-20"] = ["ai-locked", "ai-pr-created"]; LABELS["CLAUDE-21"] = ["ai-locked", "ai-pr-created"]
with contextlib.redirect_stdout(io.StringIO()):
    dt.on_merged({"number": 30, "url": "https://gh/pr/30", "keys": ["CLAUDE-20", "CLAUDE-21"],
                  "merge_sha": "aaa111", "merged_at": iso(T0)})
chk(all("ai-merged" in LABELS[k] and "ai-locked" not in LABELS[k] for k in ("CLAUDE-20", "CLAUDE-21")) and not trans,
    "merge: both batch tickets ai-merged, NOT moved to Done")
chk(any("moves to Done automatically once the deploy succeeds" in c for _, c in comments), "merge comment explains Done waits for deploy")
jobs = lambda rid: {"job_started": iso(T0 + D.timedelta(minutes=40)), "job_completed": iso(T0 + D.timedelta(minutes=44))}
lines = dt.track(apply=True, now=T0 + D.timedelta(minutes=5), runs=[run(1, "aaa111", "waiting")], jobs_fn=jobs)
chk(any("pending -> awaiting_approval" in l for l in lines) and not trans, "awaiting approval recorded, still not Done")
notes.clear()
lines = dt.track(apply=True, now=T0 + D.timedelta(hours=5), runs=[run(1, "aaa111", "waiting")], jobs_fn=jobs)
chk(("deploy_reminder", "CLAUDE-20") in notes, "approval reminder after PR_REMIND_HOURS")
lines = dt.track(apply=True, now=T0 + D.timedelta(hours=5, minutes=10),
                 runs=[run(1, "aaa111", "completed", "failure", updated=T0 + D.timedelta(minutes=44))], jobs_fn=jobs)
chk("ai-deploy-failed" in LABELS["CLAUDE-20"] and not trans and ("deploy_failed", "CLAUDE-20") in notes,
    "deploy failed: ai-deploy-failed + alert, not Done")
lines = dt.track(apply=True, now=T0 + D.timedelta(hours=6),
                 runs=[run(1, "aaa111", "completed", "success", updated=T0 + D.timedelta(hours=5, minutes=50))], jobs_fn=jobs)
chk(("CLAUDE-20", "Done") in trans and ("CLAUDE-21", "Done") in trans and "ai-deployed" in LABELS["CLAUDE-21"]
    and "ai-deploy-failed" not in LABELS["CLAUDE-20"], "re-run succeeded: both tickets ai-deployed and moved to Done")
s = json.load(open(dt.STATE_FILE))["30"]
chk(s["state"] == "deployed" and s["approval_wait_s"] == 2340 and s["deploy_s"] == 240, "approval wait 39m and deploy 4m recorded")
n = len(trans)
dt.track(apply=True, now=T0 + D.timedelta(hours=7), runs=[run(1, "aaa111", "completed", "success")], jobs_fn=jobs)
chk(len(trans) == n, "deployed is final: no second Done / comment")
dt._read = lambda: {}  # nothing tracked -> no API call
chk(dt.track(apply=True, runs=None) == [], "nothing tracked -> no GitHub call")

print("dashboard")
dt._read = lambda: json.loads(dt.STATE_FILE.read_text())
import generate_pipeline_dashboard as g
for k, v in dict(LOGS_DIR=LOGS, EVENTS_PATH=os.path.join(LOGS, "run-events.jsonl"), OUTPUT_PATH=os.path.join(LOGS, "out.html"),
                 PR_STATUS_PATH=os.path.join(LOGS, "pr-status.json"), WAITING_PATH=os.path.join(LOGS, "waiting.json"),
                 DEPLOY_PATH=str(dt.STATE_FILE)).items():
    setattr(g, k, v)
tz = D.timezone(D.timedelta(hours=-7)); t = D.datetime(2026, 10, 7, 8, 0, tzinfo=tz); E = []
def ev(step, st, tt, **k): E.append({"run_id": "r1", "ticket": "CLAUDE-20", "project": "P", "step": step, "status": st, "time": tt.isoformat(), **k})
ev("run", "started", t)
for i, (stp, d) in enumerate([("prepare", 10), ("agent_step", 100), ("review", 5), ("validate", 20), ("pr", 10), ("jira", 5)]):
    ev(stp, "started", t); t += D.timedelta(seconds=d); ev(stp, "ok", t, duration_s=d)
ev("run", "ok", t, outcome="pr_open", duration_s=150)
open(g.EVENTS_PATH, "w").write("\n".join(json.dumps(e) for e in E))
json.dump({"generated_at": "x", "prs": [{"number": 30, "keys": ["CLAUDE-20", "CLAUDE-21"], "state": "merged", "url": "https://gh/pr/30",
           "created_at": iso(T0 - D.timedelta(minutes=20)), "merged_at": iso(T0)}]}, open(g.PR_STATUS_PATH, "w"))
recs = g.build_ticket_records(g.load_events())
r = recs[0]
chk(r["outcome"] == "deployed" and r["deploy"]["state"] == "deployed" and r["mergeToDeploy_s"] is not None
    and r["leadTimeDeployed_s"] > r["leadTime_s"], "record: outcome deployed, merge->deploy and lead-time-to-live computed")
g.TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(g.__file__)), "dashboard_template.html")
g.render(recs, g.build_queue(json.load(open(g.PR_STATUS_PATH))))
h = open(g.OUTPUT_PATH, encoding="utf-8").read()
chk("Deployed to Salesforce" in h and "depPill" in h and "__QUEUE_JSON__" not in h, "template has deploy states (needs v7.2 template)")
print(f"\n{ok}/{ok} passed")
