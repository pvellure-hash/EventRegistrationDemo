"""Step 3 offline test for validate_fix.py batch limits (real git in a temp repo; no org, no Jira).
Run from the agent folder: python test_step3_validate.py"""
import os, sys, types, json, tempfile, subprocess, io, contextlib
TMP = tempfile.mkdtemp(); os.environ["AGENT_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["AGENT_RUN_APEX_TESTS"] = "false"
for k in ("BATCH_EXTRA_FILES", "BATCH_EXTRA_LINES", "BATCH_MAX_FILES_CAP", "BATCH_MAX_LINES_CAP"):
    os.environ.pop(k, None)
m = types.ModuleType("jira_client"); m.audit = lambda *a, **k: None; sys.modules["jira_client"] = m
import routing, validate_fix as v
ok = 0
def chk(c, msg):
    global ok
    if not c: raise AssertionError(msg)
    ok += 1; print("  PASS", msg)
chk(v.size_limits(1) == (5, 200), "single ticket: unchanged 5 files / 200 lines")
chk(v.size_limits(2) == (8, 350), "2 tickets: 8 / 350")
chk(v.size_limits(3) == (10, 450) and v.size_limits(6) == (10, 450), "3+ tickets: capped 10 / 450")
chk(v.bad_commits(["fix(CLAUDE-20): a", "test(CLAUDE-21): b", "Merge x"], ["CLAUDE-20", "CLAUDE-21"]) == [],
    "batch: member-key commit accepted")
chk(v.bad_commits(["test(CLAUDE-21): b"], ["CLAUDE-20"]) == ["test(CLAUDE-21): b"], "single: other key rejected")

# real git: 2-ticket batch with 300 changed lines in 2 classes
repo = os.path.join(TMP, "repo"); os.makedirs(repo)
g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout
g("init", "-q", "-b", "main"); g("config", "user.email", "a@b"); g("config", "user.name", "a")
cls = os.path.join(repo, "force-app/main/default/classes"); rep = os.path.join(repo, "docs/ai-reports")
os.makedirs(cls); os.makedirs(rep)
open(os.path.join(repo, ".gitignore"), "w").write("logs/\n"); open(os.path.join(cls, "A.cls"), "w").write("x\n"); g("add", "."); g("commit", "-qm", "init")
branch = "fix/CLAUDE-20-batch-claude-21"; g("checkout", "-qb", branch)
open(os.path.join(cls, "A.cls"), "w").write("".join(f"a{i}\n" for i in range(150)))
open(os.path.join(cls, "ATest.cls"), "w").write("".join(f"t{i}\n" for i in range(150)))
for s in ("", "-pr", "-jira"):
    open(os.path.join(rep, f"CLAUDE-20{s}.md"), "w").write("r")
g("add", "."); g("commit", "-qm", "fix(CLAUDE-20): both"); 
open(os.path.join(cls, "ATest.cls"), "a").write("z\n"); g("commit", "-qam", "test(CLAUDE-21): name trim")
v.REPO_ROOT = repo
def run():
    v.results.clear(); sys.argv = ["validate_fix.py", "CLAUDE-20"]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out: v.main(); code = 0
    except SystemExit as e: code = e.code
    return code, json.load(open(os.path.join(repo, "logs", "CLAUDE-20-validation.json")))
os.makedirs(os.path.join(repo, "logs"), exist_ok=True)
code, res = run()
chk(code == 1 and any(r["check"].startswith("Code lines changed <= 200") and not r["passed"] for r in res["results"]),
    "no batch record: 301 lines fails the normal 200 limit (unchanged behaviour)")
routing.write_batch("CLAUDE-20", [{"key": "CLAUDE-21", "summary": "s", "files": []}], "standard", [], branch)
code, res = run()
chk(code == 0 and res["limits"] == {"files": 8, "lines": 350} and res["batch_members"] == ["CLAUDE-21"],
    "batch record for this branch: 301 lines passes the 350 limit; member commit accepted")
routing.write_batch("CLAUDE-20", [{"key": "CLAUDE-21", "summary": "s", "files": []}], "standard", [], "fix/CLAUDE-20-other")
code, res = run()
chk(code == 1 and res["batch_members"] == [], "stale batch record for another branch is ignored")
print(f"\n{ok}/{ok} passed")
