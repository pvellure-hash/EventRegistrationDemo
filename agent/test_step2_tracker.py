import sys, types, json, os, tempfile, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
calls=[]
def mod(n,**k):
    m=types.ModuleType(n); m.__dict__.update(k); sys.modules[n]=m; return m
LABELS={}; LINKS={}
class JE(Exception): pass
def _req(method,path,**k):
    calls.append(("req",method,path))
    if "search/jql" in path:
        jql=k["params"]["jql"]
        return {"issues":[{"key":kk,"fields":{"labels":v,"issuelinks":LINKS.get(kk,[])}} for kk,v in LABELS.items() if "ai-waiting" in v]}
    return {}
def upd(key,add=(),remove=()):
    calls.append(("labels",key,tuple(add),tuple(remove)))
    s=set(LABELS.get(key,[])); s|=set(add); s-=set(remove); LABELS[key]=sorted(s)
jc=mod("jira_client",PROJECT="CLAUDE",ENABLED=True,DRY_RUN=False,STATUS_IN_PROGRESS="In Progress",STATUS_IN_REVIEW="In Review",
    LOG_FILE="x",JiraError=JE,_request=_req,audit=lambda *a,**k:None,update_labels=upd,
    add_comment=lambda key,lines:calls.append(("comment",key,lines[0][:50])),
    transition=lambda key,t:calls.append(("transition",key,t)) or True,
    get_issue=lambda key,fields="":{"fields":{"labels":LABELS.get(key,[]),"issuelinks":LINKS.get(key,[])}},
    assign=lambda *a:None,my_account_id=lambda:"me")
mod("redact",redact=lambda s:s)
notes=[]
mod("notify",notify_event=lambda kind,ticket,title,detail="",url="":notes.append((kind,ticket)),notify_failure=lambda **k:notes.append(("fail",k.get("ticket"))))
import pr_tracker as pt, update_jira as uj
tmp=Path(tempfile.mkdtemp()); pt.STATE_FILE=tmp/"pr-state.json"; pt.WAITING_FILE=tmp/"waiting.json"
ok=0
def chk(c,m):
    global ok
    if not c: raise AssertionError(m)
    ok+=1; print("  PASS", m)
now=datetime(2026,10,6,12,tzinfo=timezone.utc)
def pr(n,state="open",merged=None,ms="clean",ref=None,created="2026-10-06T11:00:00Z",title=""):
    return {"number":n,"state":state,"merged_at":merged,"mergeable_state":ms,"created_at":created,
            "html_url":f"https://github.com/x/pull/{n}","base":{"sha":"m1"},"head":{"ref":ref or f"fix/CLAUDE-{n}-x","sha":"h"},"title":title}
# --- tracker
chk(pt.ticket_keys(pr(1,ref="fix/g11-git-sync"))==[], "tooling branch has no ticket key")
old=[pr(10,"closed",merged="2026-10-01"),pr(11,"closed"),pr(30)]
evs,open_n=pt.track_all(apply=True,now=now,prs=old,detail_fn=lambda n:pr(n))
chk(evs==[] and open_n=={30}, "first run: old closed PRs baselined silently, no events")
pt.update_branch=lambda n,s:(True,"updated")
evs,_=pt.track_all(apply=True,now=now,prs=[pr(30,ms="behind")],detail_fn=lambda n:pr(30,ms="behind"))
chk([e["action"] for e in evs]==["UPDATE"], "behind -> UPDATE (Update branch pressed)")
evs,_=pt.track_all(apply=True,now=now,prs=[pr(30,ms="behind")],detail_fn=lambda n:pr(30,ms="behind"))
chk(not any(e["action"]=="UPDATE" for e in evs), "no second Update branch for same main sha")
evs,_=pt.track_all(apply=True,now=now,prs=[pr(30,ms="dirty")],detail_fn=lambda n:pr(30,ms="dirty"))
chk([e["action"] for e in evs]==["CONFLICT"], "conflict reported")
evs,_=pt.track_all(apply=True,now=now,prs=[pr(30,ms="dirty")],detail_fn=lambda n:pr(30,ms="dirty"))
chk(evs==[], "conflict reported only once")
later=now+timedelta(hours=5)
evs,_=pt.track_all(apply=True,now=later,prs=[pr(31)],detail_fn=lambda n:pr(31))
chk([e["action"] for e in evs]==["REMIND"], "reminder after PR_REMIND_HOURS")
evs,_=pt.track_all(apply=True,now=later+timedelta(hours=1),prs=[pr(31)],detail_fn=lambda n:pr(31))
chk(evs==[], "no reminder spam within interval")
evs,open_n=pt.track_all(apply=True,now=later,prs=[pr(30,"closed",merged="x"),pr(31)],detail_fn=lambda n:pr(31))
chk([e["action"] for e in evs]==["MERGED"] and open_n=={31}, "merge detected, open set updated")
evs,_=pt.track_all(apply=True,now=later,prs=[pr(30,"closed",merged="x")],detail_fn=None)
chk(evs==[], "merge reported only once")
# --- Jira handlers
LABELS["CLAUDE-30"]=["ai-locked","ai-pr-created"]
calls.clear(); uj.on_pr_event({"number":30,"url":"u","action":"MERGED","keys":["CLAUDE-30"]})
chk("ai-merged" in LABELS["CLAUDE-30"] and "ai-locked" not in LABELS["CLAUDE-30"] and ("transition","CLAUDE-30","Done") in calls, "MERGED -> ai-merged, unlocked, Done")
calls.clear(); uj.on_pr_event({"number":30,"url":"u","action":"MERGED","keys":["CLAUDE-30"]})
chk(not any(c[0]=="comment" for c in calls), "MERGED idempotent (no duplicate comment)")
LABELS["CLAUDE-32"]=["ai-locked"]; uj.on_pr_event({"number":32,"url":"u","action":"REJECTED","keys":["CLAUDE-32"]})
chk("ai-rejected" in LABELS["CLAUDE-32"], "REJECTED -> ai-rejected")
notes.clear(); uj.on_pr_event({"number":31,"url":"u","action":"REMIND","keys":["CLAUDE-31"],"age_h":5})
chk(notes==[("pr_reminder","CLAUDE-31")], "REMIND -> notification only")
# --- release
LABELS["CLAUDE-40"]=["ai-waiting"]; pt.mark_waiting("CLAUDE-40","pr",prs=[31])
LABELS["CLAUDE-41"]=["ai-waiting"]; pt.mark_waiting("CLAUDE-41","pr",prs=[30])
LABELS["CLAUDE-42"]=["ai-waiting"]; pt.mark_waiting("CLAUDE-42","blocker",blockers=["CLAUDE-39"])
LINKS["CLAUDE-42"]=[{"type":{"inward":"is blocked by"},"inwardIssue":{"key":"CLAUDE-39","fields":{"status":{"statusCategory":{"key":"indeterminate"}}}}}]
LABELS["CLAUDE-43"]=["ai-waiting"]   # manual label, no record, no links
rel=uj.release_waiting({31})
chk(sorted(rel)==["CLAUDE-41","CLAUDE-43"], "release: PR gone + no-blocker released; open PR & unresolved blocker kept")
chk("ai-ready" in LABELS["CLAUDE-41"] and "ai-waiting" not in LABELS["CLAUDE-41"] and "CLAUDE-41" not in json.loads(pt.WAITING_FILE.read_text()), "released labels + state cleared")
LINKS["CLAUDE-42"][0]["inwardIssue"]["fields"]["status"]["statusCategory"]["key"]="done"
chk(uj.release_waiting({31})==["CLAUDE-42"], "blocker Done -> released")
# --- claim_ticket blocker path
Q=[]
mod("read_queue",fetch_queue=lambda:Q,assess=lambda d:([],[]),adf_to_text=lambda d:d or "")
import claim_ticket as ct
LABELS.update({"CLAUDE-50":["ai-ready"],"CLAUDE-51":["ai-ready"]})
LINKS["CLAUDE-50"]=[{"type":{"inward":"is blocked by"},"inwardIssue":{"key":"CLAUDE-49","fields":{"status":{"statusCategory":{"key":"new"}}}}}]
Q[:]=[{"key":"CLAUDE-50","fields":{"labels":["ai-ready"]}},{"key":"CLAUDE-51","fields":{"labels":["ai-ready"]}}]
import io,contextlib
buf=io.StringIO()
with contextlib.redirect_stdout(buf): ct.main()
out=buf.getvalue()
chk("Claimed: CLAUDE-51" in out and "ai-waiting" in LABELS["CLAUDE-50"] and "ai-ready" not in LABELS["CLAUDE-50"], "claim: blocked ticket waits, next ticket claimed same run")
print(f"\n{ok}/{ok} passed")
