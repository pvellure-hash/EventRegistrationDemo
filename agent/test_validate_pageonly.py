"""Offline tests for validate_fix.py: page-only fixes (LWC / Aura) and the Apex dry-run (v10, Issue S14).
Builds throwaway git repos in a temp folder, runs validate_fix.main() against them and fakes the Salesforce CLI.
Never touches the real repo, Jira, GitHub or Salesforce.
Run from the agent folder:  python test_validate_pageonly.py"""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, types
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import jira_client  # noqa: F401
except Exception:                      # not importable here: a stub is enough, validate_fix only calls audit()
    stub = types.ModuleType("jira_client"); stub.audit = lambda *a, **k: None; sys.modules["jira_client"] = stub
import validate_fix as vf
vf.jc.audit = lambda *a, **k: None
vf.routing = None
CLS = "force-app/main/default/classes/"
LWC = "force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.html"
ok = 0
def chk(c, m):
    global ok
    if not c: raise AssertionError(m)
    ok += 1; print("  PASS", m)
def git(cwd, *a): subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)

def make_repo(fix_files, base_extra=None, subject="fix(CLAUDE-21): demo fix"):
    """A repo with main, plus a fix branch that changes/adds fix_files {path: text}."""
    tmp = Path(tempfile.mkdtemp(prefix="vf-test-"))
    def w(p, t): f = tmp / p; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(t, encoding="utf-8")
    git(tmp, "init", "-q"); git(tmp, "checkout", "-q", "-b", "main")
    w(CLS + "EventRegistrationController.cls", "public class EventRegistrationController {}")
    w(CLS + "EventRegistrationControllerTest.cls", "@IsTest class EventRegistrationControllerTest {}")
    w(LWC, "<td>{reg.Name}</td>\n")
    for p, t in (base_extra or {}).items(): w(p, t)
    git(tmp, "add", "-A"); git(tmp, "commit", "-q", "-m", "base")
    git(tmp, "checkout", "-q", "-b", "fix/CLAUDE-21-demo")
    for p, t in fix_files.items(): w(p, t)
    for n in ("CLAUDE-21.md", "CLAUDE-21-pr.md", "CLAUDE-21-jira.md"): w("docs/ai-reports/" + n, "report\n")
    git(tmp, "add", "-A"); git(tmp, "commit", "-q", "-m", subject)
    return tmp

def run(tmp, run_apex=True, org="eventreg-dev"):
    """Run validate_fix.main() in tmp. Returns (exit code, printed text, list of sf commands it tried to run)."""
    sf_calls, real_run, real_which = [], subprocess.run, shutil.which
    def fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith("sf"):
            sf_calls.append(list(cmd))
            out = json.dumps({"status": 0, "result": {"status": "Succeeded", "numberTestsTotal": 3, "numberTestErrors": 0, "details": {"runTestResult": {"numTestsRun": 3}}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return real_run(cmd, *a, **k)
    saved = (vf.REPO_ROOT, vf.BASE, vf.SF_TARGET_ORG, vf.RUN_APEX_TESTS, sys.argv)
    vf.REPO_ROOT, vf.BASE, vf.SF_TARGET_ORG, vf.RUN_APEX_TESTS = str(tmp), "main", org, run_apex
    vf.results.clear(); sys.argv = ["validate_fix.py", "CLAUDE-21"]
    subprocess.run, shutil.which = fake_run, (lambda n, *a, **k: "/usr/bin/sf" if n == "sf" else real_which(n, *a, **k))
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf):
            try: vf.main()
            except SystemExit as e: code = e.code or 0
    finally:
        subprocess.run, shutil.which = real_run, real_which
        vf.REPO_ROOT, vf.BASE, vf.SF_TARGET_ORG, vf.RUN_APEX_TESTS, sys.argv = saved
        shutil.rmtree(tmp, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))    # git object files are read-only on Windows
    return code, buf.getvalue(), sf_calls

print("apex_changed()")
chk(not vf.apex_changed([LWC, "docs/ai-reports/CLAUDE-21.md"]), "an LWC change plus reports is not an Apex change")
chk(not vf.apex_changed(["force-app/main/default/aura/Foo/Foo.cmp"]), "an Aura change is not an Apex change")
chk(vf.apex_changed([CLS + "Foo.cls"]) and vf.apex_changed([CLS + "Foo.cls-meta.xml"]), "a class (or its meta file) is an Apex change")
chk(vf.apex_changed(["force-app/main/default/triggers/Foo.trigger"]), "a trigger is an Apex change")

print("page-only fix (the CLAUDE-21 case)")
code, out, sf = run(make_repo({LWC: "<td>{reg.Attendee_Name__c}</td>\n"}))
chk(code == 0 and "ALL CHECKS PASSED" in out, "a page-only fix passes validation (it used to stop at the Apex dry-run)")
chk("skipped: no Apex changed (page-only fix)" in out and "FAIL" not in out, "it shows an amber warning that names the reason, and no failure")
chk(sf == [], "no Salesforce CLI call is made for a page-only fix")
chk(out.count("  PASS  ") == 14, "all 14 static checks that apply still ran and passed")

print("static checks still protect a page-only fix")
code, out, sf = run(make_repo({LWC: "<td>x</td>\n"}, subject="fix: no ticket key"))
chk(code == 1 and "FAIL  Commit messages reference ticket" in out, "a commit that does not name the ticket still blocks it")
chk(sf == [] and "skipped because earlier checks failed" in out, "the Apex step is skipped when a static check failed")
code, out, sf = run(make_repo({".env": "X=1\n"}))
chk(code == 1 and ("FAIL  Only allowed paths changed" in out or "FAIL  No restricted files changed" in out), "a restricted or disallowed file still blocks it")

print("Apex fixes behave exactly as before")
code, out, sf = run(make_repo({CLS + "EventRegistrationController.cls": "public class EventRegistrationController { Integer x; }"}))
chk(code == 0 and len(sf) == 1, "a changed class with its test class runs the specified tests")
cmd = sf[0]
chk("RunSpecifiedTests" in cmd and cmd[cmd.index("--tests") + 1] == "EventRegistrationControllerTest", "it runs EventRegistrationControllerTest at RunSpecifiedTests")
code, out, sf = run(make_repo({CLS + "BrandNew.cls": "public class BrandNew {}"}))
chk(code == 1 and "FAIL  Apex tests in org - no test class found for changed Apex" in out and sf == [], "a changed class with NO test class still fails (that is a real gap)")
code, out, sf = run(make_repo({"force-app/main/default/triggers/Foo.trigger": "trigger Foo on Account (before insert) {}"}))
chk(code == 1 and "no test class found for changed Apex" in out, "a changed trigger with no test class still fails, as before")
code, out, sf = run(make_repo({LWC: "<td>{reg.Attendee_Name__c}</td>\n", CLS + "EventRegistrationController.cls": "public class EventRegistrationController { Integer x; }"}))
chk(code == 0 and len(sf) == 1, "a mixed change (page + Apex) still runs the Apex tests")
code, out, sf = run(make_repo({CLS + "EventRegistrationController.cls": "public class EventRegistrationController { Integer x; }"}), org="")
chk(code == 1 and "SF_TARGET_ORG missing" in out, "an Apex fix with no SF_TARGET_ORG still fails")

print("the switch still works")
code, out, sf = run(make_repo({CLS + "EventRegistrationController.cls": "public class EventRegistrationController { Integer x; }"}), run_apex=False)
chk(code == 0 and "skipped (AGENT_RUN_APEX_TESTS=false)" in out and sf == [], "AGENT_RUN_APEX_TESTS=false still skips the dry-run, with its own message")
print(f"\n{ok}/{ok} passed")
