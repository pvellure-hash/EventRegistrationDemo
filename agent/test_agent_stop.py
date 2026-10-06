"""Offline tests for the v7 agent-stop handling (C15): invoke_copilot.py and review_gate.py.
Uses a real temporary git repo; fakes Jira, events and the other agent modules.
Never touches the real repository, Jira, Copilot or Salesforce.
Run from the agent folder: python test_agent_stop.py"""
import os, sys, types, tempfile, subprocess, io, contextlib

def mod(n, **k):
    m = types.ModuleType(n); m.__dict__.update(k); sys.modules[n] = m; return m

comments, evs = [], []
class JE(Exception): pass
mod("jira_client", PROJECT="CLAUDE", ENABLED=True, DRY_RUN=False, JiraError=JE,
    audit=lambda *a, **k: None, add_comment=lambda key, lines: comments.append((key, lines)))
mod("events", emit_event=lambda *a, **k: evs.append((a, k)), current_run_id=lambda k: "r", log_dir=lambda: ".",
    CREDIT_TO_USD=0.01)
for n in ("guards", "security_checks", "tool_allowlist", "routing", "model_resolve"):
    mod(n)
mod("redact", redact=lambda s: s)
import invoke_copilot as ic

ok = 0
def chk(c, m):
    global ok
    if not c: raise AssertionError(m)
    ok += 1; print("  PASS", m)

print("stop reason")
claude18 = ("I'll first verify...\n\n**Plan before editing:** Update the controller.\n\n"
            "I can't proceed under the instruction to follow `.github/copilot-instructions.md` strictly without "
            "written approval to exceed the file limit. **No code was changed and no commit was made.** "
            "Awaiting approval; no push, PR, or deployment has been performed.\n\n")
r = ic.extract_stop_reason(claude18)
chk(r.startswith("I can't proceed") and "**" not in r and "`" not in r, "CLAUDE-18 output -> its last real paragraph, markdown removed")
chk(ic.extract_stop_reason("blah\n\nAGENT-STOP: needs a new custom field (Section 5)\n\nmore") ==
    "needs a new custom field (Section 5)", "AGENT-STOP line wins")
chk(ic.extract_stop_reason("Real reason here.\n\nAwaiting human review. No merge or deployment has been performed.")
    == "Real reason here.", "standard closing line skipped")
chk(ic.extract_stop_reason("") == "Copilot produced no output.", "empty output handled")

print("branch checks (real git)")
repo = tempfile.mkdtemp()
g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
g("init", "-q", "-b", "main"); g("config", "user.email", "a@b"); g("config", "user.name", "a")
open(os.path.join(repo, "a.cls"), "w").write("x\n"); g("add", "."); g("commit", "-qm", "init")
ic.REPO_ROOT = repo; ic.BASE = "main"
g("checkout", "-qb", "fix/CLAUDE-18-edit")
def run_check(stdout):
    comments.clear(); evs.clear()
    try:
        with contextlib.redirect_stdout(io.StringIO()): ic.check_agent_output("CLAUDE-18", stdout, 0.4577)
        return None
    except SystemExit as e:
        return str(e.code)
code = run_check(claude18)
br = g("branch", "--show-current").stdout.strip()
chk(code and code.startswith("FAIL: agent_no_changes"), "no commits -> exits with agent_no_changes")
chk(br == "main" and "fix/CLAUDE-18-edit" not in g("branch").stdout, "empty branch deleted, back on main")
chk(comments and "0.46 AI credits" in comments[0][1][0] and "I can't proceed" in comments[0][1][1],
    "Jira comment has cost and the agent's reason")
chk(any(k.get("outcome") == "agent_no_changes" for _, k in evs), "run event outcome agent_no_changes")

g("checkout", "-qb", "fix/CLAUDE-19-x"); open(os.path.join(repo, "a.cls"), "a").write("wip\n")
code = run_check("did stuff")
chk(code.startswith("FAIL: agent_uncommitted") and g("branch", "--show-current").stdout.strip() == "fix/CLAUDE-19-x"
    and "wip" in open(os.path.join(repo, "a.cls")).read(), "uncommitted edits -> agent_uncommitted, nothing touched")
g("commit", "-qam", "feat(CLAUDE-19): x")
chk(run_check("done") is None, "with a commit -> continues to the review gate")

print("review gate")
mod("validate_fix", ALLOWED_PREFIXES=("force-app/", "docs/ai-reports/"), BLOCKED_PATTERNS=(".github/",),
    CLASSES_DIR="force-app/main/default/classes/")
import review_gate as rg
rg.REPO_ROOT = repo; rg.BASE = "main"
g("checkout", "-q", "main"); g("checkout", "-qb", "fix/CLAUDE-20-empty")
sys.argv = ["review_gate.py", "CLAUDE-20"]
asked = []
import builtins; builtins.input = lambda *a: asked.append(1) or "y"
try:
    with contextlib.redirect_stdout(io.StringIO()) as out: rg.main(); code = 0
except SystemExit as e: code = e.code
chk(code == 1 and not asked and "NOTHING TO REVIEW" in out.getvalue(), "empty branch auto-rejected without asking")
print(f"\n{ok}/{ok} passed")
