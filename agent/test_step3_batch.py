"""Step 3 offline tests: batching of related bugs. Fakes Jira, GitHub, localisation and git;
never touches the real repo, Jira or Copilot. Run from the agent folder: python test_step3_batch.py"""
import os, sys, types, json, tempfile, io, contextlib
TMP = tempfile.mkdtemp()
os.environ["AGENT_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ.update(BATCH_ENABLED="true", BATCH_MAX_TICKETS="3", BATCH_KINDS="Bug")

def mod(n, **k):
    m = types.ModuleType(n); m.__dict__.update(k); sys.modules[n] = m; return m

C = "force-app/main/default/classes/EventRegistrationController.cls"
T = "force-app/main/default/classes/EventRegistrationControllerTest.cls"
L = "force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.js"
DB = {}      # key -> fields
def ticket(key, summary, kind="Bug", labels=("ai-ready",), links=()):
    DB[key] = {"summary": summary, "description": f"{summary} body", "labels": list(labels),
               "issuetype": {"name": kind}, "issuelinks": list(links)}
calls = []
class JE(Exception): pass
def get_issue(key, fields=""):
    return {"key": key, "fields": DB[key]}
def upd(key, add=(), remove=()):
    s = set(DB[key]["labels"]); s |= set(add); s -= set(remove); DB[key]["labels"] = sorted(s)
    calls.append(("labels", key, tuple(add), tuple(remove)))
comments = {}
def add_comment(key, lines):
    comments.setdefault(key, []).append(" ".join(lines))
jc = mod("jira_client", PROJECT="CLAUDE", ENABLED=True, DRY_RUN=False, STATUS_IN_PROGRESS="In Progress",
         STATUS_IN_REVIEW="In Review", LOG_FILE="x", JiraError=JE, BASE="https://jira", AUTH=None,
         _request=lambda m, p, **k: {"comments": [{"body": c} for c in comments.get(p.split("/")[4], [])]} if "comment" in p else {},
         audit=lambda *a, **k: None, update_labels=upd, add_comment=add_comment,
         transition=lambda key, t: calls.append(("transition", key, t)) or True,
         get_issue=get_issue, assign=lambda *a: None, my_account_id=lambda: "me")
mod("redact", redact=lambda s: s)
mod("notify", notify_event=lambda *a, **k: None, notify_failure=lambda **k: calls.append(("notify_fail", k.get("ticket"))))
mod("read_queue", fetch_queue=lambda: [{"key": k, "fields": DB[k]} for k in sorted(DB) if "ai-ready" in DB[k]["labels"]],
    assess=lambda d: ([], []), adf_to_text=lambda d: d or "")
# localisation fake: files depend on words in the ticket text
def files_for(text):
    t = text.lower(); out = []
    if "phone" in t or "name" in t or "guest" in t: out += [C, T]
    if "button" in t: out += [L]
    return out
mod("code_index", load=lambda r: {"files": {}}, build_or_update=lambda r: {"file_count": 1, "reparsed_this_run": 0})
mod("localizer", localize=lambda idx, text: {"confidence": 0.7 if files_for(text) else 0.1,
                                            "candidates": [{"score": 9, "path": p, "reasons": ["x"]} for p in files_for(text)]},
    expand_with_dependencies=lambda idx, cands: [c["path"] for c in cands])
mod("escalation", classify_complexity=lambda loc, text: "simple",
    plan_attempt=lambda attempt_no, complexity, confidence: {"action": "proceed" if confidence >= 0.35 else "ask_for_info",
                                                             "tier": "economy", "model": "auto", "reason": "x"})
mod("context_pack", build=lambda root, idx, files, text, max_tokens: (f"PACK {len(files)} files", files, [], 500))
mod("injection_scan", scan_context_pack=lambda f: {"clean": True, "findings": []})
mod("events", emit_event=lambda *a, **k: None, new_run_id=lambda k: "r1", read_events=lambda: [])

import git_sync, pr_tracker, routing, batching, prepare_fix as pf, update_jira as uj
git_sync.overlapping_open_prs = lambda files, exclude_head=None, base=None: []
pf.REPORT_DIR = os.path.join(TMP, "reports")
pf.check_repo_safe = lambda: None
branches = []
pf.prepare_branch = lambda b: branches.append(b)
batching.FAILED_FILE = os.path.join(TMP, "logs", "batch-failed.json")
pr_tracker.STATE_FILE = __import__("pathlib").Path(TMP, "s.json"); pr_tracker.WAITING_FILE = __import__("pathlib").Path(TMP, "w.json")

ok = 0
def chk(c, m):
    global ok
    if not c: raise AssertionError(m)
    ok += 1; print("  PASS", m)

print("pure rules")
cands = [{"key": "CLAUDE-21", "kind": "Bug", "files": [C], "ok": True},
         {"key": "CLAUDE-22", "kind": "Story", "files": [C], "ok": True},
         {"key": "CLAUDE-23", "kind": "Bug", "files": [L], "ok": True},
         {"key": "CLAUDE-24", "kind": "Bug", "files": [C.replace("/", "\\")], "ok": True},
         {"key": "CLAUDE-25", "kind": "Bug", "files": [C], "ok": True},
         {"key": "CLAUDE-26", "kind": "Bug", "files": [C], "ok": False, "reason": "blocked by CLAUDE-9"}]
ch, sk = batching.select_members("CLAUDE-20", [C, T], cands, max_total=3)
chk([c["key"] for c in ch] == ["CLAUDE-21", "CLAUDE-24"], "shared files -> members; Windows path matched; cap 3 incl. lead")
chk("BATCH_KINDS" in sk["CLAUDE-22"] and sk["CLAUDE-23"] == "no shared files" and "full" in sk["CLAUDE-25"]
    and "blocked" in sk["CLAUDE-26"], "story / unrelated / overflow / blocked skipped with reasons")
ch, sk = batching.select_members("CLAUDE-20", [C], cands, excluded={"CLAUDE-21"}, max_total=3)
chk("CLAUDE-21" not in [c["key"] for c in ch] and "alone" in sk["CLAUDE-21"], "pair that failed before is not re-batched")
chk(batching.bump_tier(["economy", "economy"]) == "standard" and batching.bump_tier(["premium"]) == "premium", "tier bump capped at premium")
b = batching.batch_branch("CLAUDE-20", ["CLAUDE-21", "CLAUDE-24"])
chk(b.startswith("fix/CLAUDE-20-") and pr_tracker.ticket_keys({"head": {"ref": b}, "title": ""}) == ["CLAUDE-20", "CLAUDE-21", "CLAUDE-24"],
    "batch branch passes create_pr check and pr_tracker sees every key")

print("prepare_fix end-to-end (fake Jira/git)")
ticket("CLAUDE-20", "Phone number format is not validated", labels=("ai-ready", "ai-locked"))
ticket("CLAUDE-21", "Attendee name saved with spaces")
ticket("CLAUDE-22", "Edit guest count on registration", kind="Story")
ticket("CLAUDE-23", "Submit button colour wrong")
ticket("CLAUDE-24", "Guest count of 11 accepted", links=[{"type": {"inward": "is blocked by"},
       "inwardIssue": {"key": "CLAUDE-9", "fields": {"status": {"statusCategory": {"key": "new"}}}}}])
sys.argv = ["prepare_fix.py", "CLAUDE-20"]
with contextlib.redirect_stdout(io.StringIO()) as out:
    pf.main()
log = out.getvalue()
batch = routing.read_batch("CLAUDE-20")
chk(batch and [m["key"] for m in batch["members"]] == ["CLAUDE-21"], "only the related, unblocked Bug joined (21)")
chk(branches == ["fix/CLAUDE-20-batch-claude-21"], "one branch: fix/CLAUDE-20-batch-claude-21")
chk("ai-locked" in DB["CLAUDE-21"]["labels"] and any("together with CLAUDE-20" in c for c in comments["CLAUDE-21"]),
    "member claimed (ai-locked) and told it is fixed with CLAUDE-20")
chk(all("ai-locked" not in DB[k]["labels"] for k in ("CLAUDE-22", "CLAUDE-23", "CLAUDE-24")), "non-members untouched")
r = routing.read_routing("CLAUDE-20")
chk(r["tier"] == "standard" and r["complexity"] == "batch-2", "routing: tier economy -> standard, complexity batch-2")
prompt = open(os.path.join(pf.REPORT_DIR, "CLAUDE-20-prompt.md"), encoding="utf-8").read()
chk(prompt.count("<<<TICKET_START") == 2 and "Key: CLAUDE-21" in prompt and "fix(CLAUDE-20)" in prompt
    and "Also fixes: CLAUDE-21" in prompt, "one prompt, both tickets in separate untrusted blocks, lead-key commits")

print("PR + Jira for every ticket")
import create_pr as cp
cp.jc = jc
chk(cp.title_from("CLAUDE-20", batch).startswith("[CLAUDE-20, CLAUDE-21] Phone number"), "PR title lists both keys")
chk("| CLAUDE-21" in cp.batch_section("CLAUDE-20", batch).replace("[", "| ").replace("| | ", "| ") or "CLAUDE-21" in cp.batch_section("CLAUDE-20", batch),
    "PR body has 'Tickets in this PR' table")
pr = {"pr_url": "https://github.com/x/pull/30", "branch": batch["branch"], "members": ["CLAUDE-21"]}
uj.already_commented = lambda key, url: any(url in c for c in comments.get(key, []))
with contextlib.redirect_stdout(io.StringIO()):
    uj.update_member("CLAUDE-21", "CLAUDE-20", pr)
    n = len(comments["CLAUDE-21"]); uj.update_member("CLAUDE-21", "CLAUDE-20", pr)
chk("ai-pr-created" in DB["CLAUDE-21"]["labels"] and ("transition", "CLAUDE-21", "In Review") in calls, "member: ai-pr-created + In Review")
chk(len(comments["CLAUDE-21"]) == n, "member update idempotent (no duplicate comment)")

print("failure path")
DB["CLAUDE-21"]["labels"] = ["ai-locked", "ai-ready"]          # batch failed before a PR
with contextlib.redirect_stdout(io.StringIO()):
    rel = batching.release_members("CLAUDE-20", "failed_validation")
chk(rel == ["CLAUDE-21"] and DB["CLAUDE-21"]["labels"] == ["ai-ready"], "failed batch -> member back to ai-ready")
chk(routing.read_batch("CLAUDE-20") is None and "CLAUDE-21" in batching.excluded_for("CLAUDE-20")
    and "CLAUDE-20" in batching.excluded_for("CLAUDE-21"), "batch record cleared; pair remembered both ways")
DB["CLAUDE-20"]["labels"] = ["ai-ready", "ai-locked"]; branches.clear()
with contextlib.redirect_stdout(io.StringIO()):
    pf.main()
chk(routing.read_batch("CLAUDE-20") is None and branches[0].startswith("fix/CLAUDE-20-phone"), "retry runs CLAUDE-20 alone (normal branch)")

print("watcher releases members when the batch run fails")
mod("preflight", check_git_state=lambda: []); mod("security_checks", check_salesforce_org=lambda: []); mod("guards")
import watch_queue as w
w.refresh_dashboard = lambda *a: None; w.sync_to_base = lambda *a: True; w.refresh_index = lambda **k: None
routing.write_batch("CLAUDE-30", [{"key": "CLAUDE-31", "summary": "s", "files": [C]}], "standard", [C], "fix/CLAUDE-30-batch-claude-31")
ticket("CLAUDE-31", "x", labels=("ai-ready", "ai-locked"))
w.run_step = lambda script, step, actor, key: (step != "validate", step, 1)
with contextlib.redirect_stdout(io.StringIO()):
    outc = w.process_ticket("CLAUDE-30")
chk(outc == "failed_validation" and DB["CLAUDE-31"]["labels"] == ["ai-ready"], "validate fails -> outcome failed_validation, member released")
print(f"\n{ok}/{ok} passed")
