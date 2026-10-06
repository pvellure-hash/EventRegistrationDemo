"""Step 7: Validate the agent's fix before anything is pushed.
Static checks + Apex tests in the dev org (validate-only, rolled back).
Exit code 0 = all checks passed, 1 = blocked.
STEP 3 (v7): batch-aware. If prepare_fix.py batched related tickets with this one
(logs/routing/<KEY>.batch.json, and the current branch is that batch's branch):
  - size limits scale with the number of tickets, capped:
      files = MAX_FILES + BATCH_EXTRA_FILES x (tickets - 1), at most BATCH_MAX_FILES_CAP
      lines = MAX_LINES + BATCH_EXTRA_LINES x (tickets - 1), at most BATCH_MAX_LINES_CAP
    defaults: 1 ticket 5/200, 2 tickets 8/350, 3 tickets 10/450 (caps 10/450).
    Set BATCH_EXTRA_FILES=0 and BATCH_EXTRA_LINES=0 to keep the single-ticket limits.
  - a commit message may reference the lead OR any member key (e.g. test(CLAUDE-21): ...).
A single-ticket run is checked exactly as before (5 files / 200 lines, lead key only).
Usage: python validate_fix.py CLAUDE-11"""
import os
import re
import sys
import json
import shutil
import subprocess
import jira_client as jc
try:
    import routing
except Exception:  # routing is optional for a manual single-ticket run
    routing = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE = os.getenv("GITHUB_BASE_BRANCH", "main").strip()
RUN_APEX_TESTS = os.getenv("AGENT_RUN_APEX_TESTS", "true").strip().lower() == "true"
SF_TARGET_ORG = os.getenv("SF_TARGET_ORG", "").strip()
APEX_TIMEOUT_MIN = 30
MAX_FILES = 5
MAX_LINES = 200


def _int_env(name, default):
    try:
        return max(0, int(os.getenv(name, str(default)).strip() or default))
    except ValueError:
        return default


BATCH_EXTRA_FILES = _int_env("BATCH_EXTRA_FILES", 3)
BATCH_EXTRA_LINES = _int_env("BATCH_EXTRA_LINES", 150)
BATCH_MAX_FILES_CAP = _int_env("BATCH_MAX_FILES_CAP", 10)
BATCH_MAX_LINES_CAP = _int_env("BATCH_MAX_LINES_CAP", 450)
CLASSES_DIR = "force-app/main/default/classes/"
ALLOWED_PREFIXES = (
    CLASSES_DIR,
    "force-app/main/default/triggers/",
    "force-app/main/default/lwc/",
    "force-app/main/default/aura/",
    "docs/ai-reports/",
)
BLOCKED_PATTERNS = (
    "profiles/", "permissionsets/", "permissionsetgroups/", "namedCredentials/",
    "connectedApps/", "remoteSiteSettings/", "authproviders/",
    ".github/", "agent/", ".env", ".forceignore", ".gitignore", "sfdx-project.json",
)
SECRET_PATTERNS = {
    "Atlassian token": r"ATATT[0-9A-Za-z_\-=]{20,}",
    "GitHub token": r"(ghp_|gho_|github_pat_)[0-9A-Za-z_]{20,}",
    "Salesforce auth URL": r"force://[^\s'\"]+",
    "Private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "AWS key": r"AKIA[0-9A-Z]{16}",
    "Hard-coded password": r"(?i)password\s*[:=]\s*['\"][^'\"]{4,}['\"]",
}


def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


results = []


def check(name, passed, detail=""):
    results.append({"check": name, "passed": passed, "detail": detail})
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))


def warn(name, detail=""):
    results.append({"check": name, "passed": True, "warning": True, "detail": detail})
    print(f"  WARN  {name}" + (f" - {detail}" if detail else ""))


# ---------------- STEP 3: batch awareness ----------------
def batch_members(key, branch):
    """Member keys of the batch this run belongs to, or [] for a single-ticket run.
    The batch record only counts if it was written for THIS branch."""
    if routing is None:
        return []
    try:
        b = routing.read_batch(key)
    except Exception:
        return []
    if not b or b.get("branch") != branch:
        return []
    return [m["key"] for m in b.get("members", []) if m.get("key")]


def size_limits(n_tickets):
    """(max_files, max_lines) for a run fixing n_tickets tickets. 1 ticket = unchanged."""
    extra = max(0, n_tickets - 1)
    if extra == 0:
        return MAX_FILES, MAX_LINES
    files = min(MAX_FILES + BATCH_EXTRA_FILES * extra, max(MAX_FILES, BATCH_MAX_FILES_CAP))
    lines = min(MAX_LINES + BATCH_EXTRA_LINES * extra, max(MAX_LINES, BATCH_MAX_LINES_CAP))
    return files, lines


def bad_commits(commits, keys):
    """Commit subjects that reference none of the run's ticket keys (merges allowed)."""
    return [c for c in commits
            if not any(f"({k})" in c for k in keys) and not c.lower().startswith("merge")]


# ---------------- Apex tests ----------------
def find_test_classes(files):
    """Test classes to run: changed *Test.cls files, plus <Class>Test for changed classes."""
    tests = set()
    for f in files:
        if not (f.startswith(CLASSES_DIR) and f.endswith(".cls")):
            continue
        name = os.path.basename(f)[:-4]
        if name.endswith("Test"):
            tests.add(name)
        elif os.path.exists(os.path.join(REPO_ROOT, CLASSES_DIR, f"{name}Test.cls")):
            tests.add(f"{name}Test")
    return sorted(tests)


def run_apex_tests(key, files):
    if not RUN_APEX_TESTS:
        warn("Apex tests in org", "skipped (AGENT_RUN_APEX_TESTS=false)")
        return
    if not SF_TARGET_ORG:
        check("Apex tests in org", False, "SF_TARGET_ORG missing in .env")
        return
    sf = shutil.which("sf")
    if not sf:
        check("Apex tests in org", False, "Salesforce CLI (sf) not found on PATH")
        return
    tests = find_test_classes(files)
    if not tests:
        check("Apex tests in org", False, "no test class found for changed Apex")
        return
    print(f"  ....  Running Apex tests in '{SF_TARGET_ORG}' (validate-only, rolled back): "
          f"{', '.join(tests)}")
    # Issue S9 fix (Oct 2026): `deploy start --dry-run --test-level
    # RunSpecifiedTests` has two confirmed upstream Salesforce CLI bugs -
    # tests are sometimes silently not read (forcedotcom/cli#2117) and the
    # run can return a bare "Fatal Error" with 0 tests executed
    # (forcedotcom/cli#2648). Reproduced locally on CLAUDE-15: a direct
    # `sf apex run test` against the same 10 tests passed cleanly twice,
    # while `deploy start --dry-run` failed both times with
    # numberTestsTotal=0 and a 0% coverage error, even though nothing in
    # the code or test changed between runs. `project deploy validate` is
    # the purpose-built validate-only command (confirmed by a Salesforce
    # CLI maintainer as the right tool when you need reliable test
    # execution without deploying) and does not share that code path.
    # Note: `deploy validate` has no --dry-run flag - it is inherently
    # validate-only, so that flag is simply omitted here.
    cmd = [sf, "project", "deploy", "validate",
           "--source-dir", "force-app",
           "--target-org", SF_TARGET_ORG,
           "--test-level", "RunSpecifiedTests",
           "--wait", str(APEX_TIMEOUT_MIN),
           "--json"]
    for t in tests:
        cmd += ["--tests", t]
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=(APEX_TIMEOUT_MIN + 5) * 60)
    except subprocess.TimeoutExpired:
        check("Apex tests in org", False, "timed out")
        return
    try:
        out = json.loads(r.stdout[r.stdout.find("{"):])
    except ValueError:
        check("Apex tests in org", False, "could not read sf output")
        return
    res = out.get("result") or out.get("data") or {}
    details = res.get("details") or {}
    run = details.get("runTestResult") or {}
    failures = run.get("failures") or []
    if isinstance(failures, dict):
        failures = [failures]
    comp_errors = details.get("componentFailures") or []
    if isinstance(comp_errors, dict):
        comp_errors = [comp_errors]
    total = int(res.get("numberTestsTotal") or run.get("numTestsRun") or 0)
    errors = int(res.get("numberTestErrors") or len(failures))
    status = res.get("status") or out.get("name") or "Unknown"
    summary = {
        "ticket": key, "org": SF_TARGET_ORG, "tests": tests, "status": status,
        "tests_run": total, "test_failures": errors,
        "failures": [{"class": f.get("name"), "method": f.get("methodName"),
                      "message": (f.get("message") or "")[:300]} for f in failures],
        "component_errors": [{"component": c.get("fullName"),
                              "problem": (c.get("problem") or "")[:300]}
                             for c in comp_errors],
    }
    with open(os.path.join(REPO_ROOT, "logs", f"{key}-apex-tests.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    passed = r.returncode == 0 and status == "Succeeded" and errors == 0 and total > 0
    detail = f"{total} run, {errors} failed, status {status}"
    if summary["failures"]:
        detail += " | " + "; ".join(f"{f['class']}.{f['method']}"
                                    for f in summary["failures"])
    if summary["component_errors"]:
        detail += " | compile: " + "; ".join(c["component"] or "?"
                                             for c in summary["component_errors"])
    check("Apex tests in org", passed, detail)


# ---------------- Main ----------------
def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python validate_fix.py <TICKET-KEY>")
    key = sys.argv[1].strip().upper()
    os.makedirs(os.path.join(REPO_ROOT, "logs"), exist_ok=True)
    print("=" * 60)
    print(f"AGENT VALIDATION | {key} | base: {BASE}")
    print("=" * 60)
    branch = git("branch", "--show-current")
    members = batch_members(key, branch)
    keys = [key] + members
    max_files, max_lines = size_limits(len(keys))
    if members:
        print(f"  batch : {key} + {', '.join(members)} -> limits {max_files} files / {max_lines} lines")
    check("On the ticket's fix branch", branch.startswith(f"fix/{key}-"), branch)
    check("Working tree clean", git("status", "--porcelain") == "",
          "commit or restore changes first")
    commits = [c for c in git("log", "--format=%s", f"{BASE}..HEAD").splitlines() if c]
    check("Has at least one commit", len(commits) > 0, f"{len(commits)} commit(s)")
    bad = bad_commits(commits, keys)
    check("Commit messages reference ticket" + ("s" if members else ""), not bad, "; ".join(bad))
    files = [f for f in git("diff", "--name-only", f"{BASE}...HEAD").splitlines() if f]
    code_files = [f for f in files if not f.startswith("docs/ai-reports/")]
    check(f"Code files changed <= {max_files}", len(code_files) <= max_files,
          f"{len(code_files)} file(s)")
    outside = [f for f in files if not f.startswith(ALLOWED_PREFIXES)]
    check("Only allowed paths changed", not outside, ", ".join(outside))
    blocked = [f for f in files if any(p in f for p in BLOCKED_PATTERNS)]
    check("No restricted files changed", not blocked, ", ".join(blocked))
    numstat = git("diff", "--numstat", f"{BASE}...HEAD", "--", "force-app")
    lines = sum(int(a) + int(d) for a, d, _ in
                (row.split("\t") for row in numstat.splitlines() if row)
                if a.isdigit() and d.isdigit())
    check(f"Code lines changed <= {max_lines}", lines <= max_lines, f"{lines} line(s)")
    added = [l[1:] for l in git("diff", "-U0", f"{BASE}...HEAD").splitlines()
             if l.startswith("+") and not l.startswith("+++")]
    found = sorted({name for name, pat in SECRET_PATTERNS.items()
                    for l in added if re.search(pat, l)})
    check("No secrets in added lines", not found, ", ".join(found))
    added_text = "\n".join(added).lower()
    check("No SeeAllData=true", "seealldata=true" not in added_text.replace(" ", ""))
    check("No 'without sharing' introduced", "without sharing" not in added_text)
    if any(f.endswith("Test.cls") for f in files):
        check("Apex test updated or added", True)
    else:
        warn("No *Test.cls changed", "reviewer must confirm existing tests cover the fix")
    report = f"docs/ai-reports/{key}.md"
    check("Solution Report committed", report in files, report)
    for suffix in ("-pr.md", "-jira.md"):
        path = os.path.join(REPO_ROOT, "docs", "ai-reports", f"{key}{suffix}")
        check(f"{key}{suffix} exists", os.path.exists(path))
    # Run the slow org test only if all static checks passed
    if all(r["passed"] for r in results):
        run_apex_tests(key, files)
    else:
        warn("Apex tests in org", "skipped because earlier checks failed")
    failed = [r for r in results if not r["passed"]]
    with open(os.path.join(REPO_ROOT, "logs", f"{key}-validation.json"), "w",
              encoding="utf-8") as f:
        json.dump({"ticket": key, "branch": branch, "batch_members": members,
                   "limits": {"files": max_files, "lines": max_lines},
                   "passed": not failed, "results": results}, f, indent=2)
    jc.audit("validate", key, f"{len(results) - len(failed)}/{len(results)} passed",
             "ok" if not failed else "failed")
    print("-" * 60)
    if failed:
        print(f"BLOCKED: {len(failed)} check(s) failed. Do not push.")
        sys.exit(1)
    print("ALL CHECKS PASSED. Safe to proceed to Step 8 (push + draft PR).")


if __name__ == "__main__":
    main()
