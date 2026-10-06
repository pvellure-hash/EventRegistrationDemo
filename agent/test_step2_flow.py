import sys, types, tempfile
from pathlib import Path
exec(open("test_step2_tracker.py").read().split("# --- tracker")[0])   # reuse stubs
import pr_tracker as pt
tmp=Path(tempfile.mkdtemp()); pt.STATE_FILE=tmp/"s.json"; pt.WAITING_FILE=tmp/"w.json"
C="force-app/main/default/classes/EventRegistrationController.cls"
mod("code_index",load=lambda r:{"x":1},build_or_update=lambda r:{"file_count":1,"reparsed_this_run":0})
mod("localizer",localize=lambda i,t:{"confidence":0.7,"candidates":[{"score":9,"path":C,"reasons":["x"]}]},
    expand_with_dependencies=lambda i,c:[C])
mod("escalation",classify_complexity=lambda l,t:"simple",plan_attempt=lambda **k:{"action":"proceed","tier":"economy","model":"auto","reason":""})
mod("context_pack",build=lambda *a,**k:("pack",[C],[],100))
mod("injection_scan",scan_context_pack=lambda f:{"clean":True,"findings":[]})
mod("routing",write_routing=lambda *a:None)
mod("events",emit_event=lambda *a,**k:None,new_run_id=lambda k:"r",read_events=lambda:[])
mod("read_queue",fetch_queue=lambda:[],assess=lambda d:([],[]),adf_to_text=lambda d:d or "")
import git_sync, prepare_fix as pf
ok=0
def chk(c,m):
    global ok
    assert c,m; ok+=1; print("  PASS",m)
git_sync.overlapping_open_prs=lambda files,exclude_head=None,base=None:[{"number":30,"title":"t","head":"fix/CLAUDE-30-x","url":"u30","files":[C]}]
LABELS["CLAUDE-60"]=["ai-ready","ai-locked"]
jc.get_issue=lambda key,fields="":{"fields":{"labels":LABELS.get(key,[]),"summary":"Phone not validated","description":"d"}}
pf.check_repo_safe=lambda: (_ for _ in ()).throw(AssertionError("git touched!"))
sys.argv=["prepare_fix.py","CLAUDE-60"]
try: pf.main(); code=None
except SystemExit as e: code=e.code
chk(code==3, "overlap -> exit code 3 before any git change")
chk(LABELS["CLAUDE-60"]==["ai-waiting"], "labels: ai-waiting only (ai-ready, ai-locked removed)")
chk(pt.waiting_info("CLAUDE-60")["prs"]==[30], "waiting.json records PR #30")
git_sync.overlapping_open_prs=lambda files,exclude_head=None,base=None:[{"number":7,"title":"t","head":"fix/step3-batching","url":"u","files":[C]}]
d=pf.run_localisation("CLAUDE-61","s","d","fix/CLAUDE-61-x")
chk(d[0]=="proceed", "tooling PR overlap ignored")
def boom(*a,**k): raise RuntimeError("github down")
git_sync.overlapping_open_prs=boom
chk(pf.run_localisation("CLAUDE-62","s","d","b")[0]=="proceed", "GitHub error fails open")
# watch_queue outcome handling
mod("preflight",check_git_state=lambda:[]); mod("security_checks",check_salesforce_org=lambda:[]); mod("guards")
import watch_queue as w
w.refresh_dashboard=lambda *a:None; w.sync_to_base=lambda *a:True
class R: 
    def __init__(s,c): s.returncode=c
import subprocess as sp
w.subprocess.run=lambda *a,**k:R(3)
notes.clear()
chk(w.process_ticket("CLAUDE-60")=="waiting" and not any(n[0]=="fail" for n in notes), "watcher: exit 3 -> outcome waiting, no failure alert")
w.subprocess.run=lambda *a,**k:R(1)
chk(w.process_ticket("CLAUDE-63")=="blocked" and any(n[0]=="fail" for n in notes), "watcher: real prepare failure still alerts")
print(f"\n{ok}/{ok} passed")
