"""agent/preflight.py — Automated pre-flight checks, run automatically by
watch_queue.py (not a manual checklist a human has to remember).

Covers every real hurdle hit while building this pipeline: wrong GitHub
token/repo, diverged or stale local branch, missing Copilot CLI folder
trust, wrong Salesforce org, kill switch off. See Part 7 / Part 10 of the
setup guide for the full narrative behind each check.

Two ways this is used by watch_queue.py:
  1. FULL run at startup (run_all()) — hard gate. If this fails, the
     watcher refuses to start polling at all.
  2. LIGHT re-check before each ticket (check_git_state() alone) — ensures
     the branch the agent is about to cut is built from the absolute
     latest origin/main, even if the watcher has been running for hours
     and someone merged a PR in the meantime. If this fails, that one
     ticket is skipped (not the whole watcher), and a notification fires.

Sync behaviour is deliberately conservative: it will only ever
fast-forward local main to match origin. It NEVER merges, rebases, or
resets --hard — if local and origin have genuinely diverged, this reports
a clear failure and leaves it for a human to reconcile (see Issue G2/G3),
rather than risk silently discarding or conflicting with someone's work.

Run standalone for a human-readable report:
    python preflight.py
"""
import os
import json
import subprocess

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main").strip()


def _run(cmd, cwd=REPO_ROOT, timeout=30):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out after %ss" % timeout
    except FileNotFoundError as e:
        return -1, "", str(e)


# ---------------------------------------------------------------- Group A
def check_git_state():
    """Repository state: clean tree, correct branch, and the local repo is
    synced to the absolute latest origin/<base> before any fix work starts."""
    results = []

    code, out, _ = _run(["git", "status", "--porcelain"])
    clean = (code == 0 and out.strip() == "")
    results.append(("A1 working tree clean", clean,
                     "clean" if clean else "uncommitted/untracked files present:\n" + out.strip()[:500]))

    code, out, _ = _run(["git", "branch", "--show-current"])
    branch = out.strip()
    on_base = (branch == BASE_BRANCH)
    results.append(("A2 on base branch (%s)" % BASE_BRANCH, on_base,
                     "on %s" % branch if on_base else
                     "currently on '%s' — switch to %s before starting the watcher" % (branch, BASE_BRANCH)))

    if not clean or not on_base:
        results.append(("A3 synced to latest origin/%s" % BASE_BRANCH, False,
                         "skipped — fix A1/A2 first"))
        return results

    _run(["git", "fetch", "origin"], timeout=30)
    _, local_head, _ = _run(["git", "rev-parse", "HEAD"])
    _, remote_head, _ = _run(["git", "rev-parse", "origin/%s" % BASE_BRANCH])
    local_head, remote_head = local_head.strip(), remote_head.strip()

    if local_head == remote_head:
        results.append(("A3 synced to latest origin/%s" % BASE_BRANCH, True,
                         "already up to date (%s)" % local_head[:8]))
        return results

    # Only fast-forward if local is a strict ancestor of origin (i.e.,
    # nothing local-only that would need merging). Never merge/rebase here.
    code, _, _ = _run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/%s" % BASE_BRANCH])
    if code == 0:
        code2, out2, err2 = _run(["git", "pull", "--ff-only", "origin", BASE_BRANCH], timeout=30)
        _, new_head, _ = _run(["git", "rev-parse", "HEAD"])
        updated = (code2 == 0 and new_head.strip() == remote_head)
        results.append(("A3 synced to latest origin/%s" % BASE_BRANCH, updated,
                         "fast-forwarded %s -> %s" % (local_head[:8], new_head.strip()[:8]) if updated
                         else "fast-forward pull failed: %s" % (err2.strip()[:300] or out2.strip()[:300])))
    else:
        results.append(("A3 synced to latest origin/%s" % BASE_BRANCH, False,
                         "local and origin/%s have DIVERGED (local has commits origin doesn't). "
                         "Will not auto-merge or reset — inspect with 'git log --oneline --graph --all' "
                         "and reconcile manually (see Issue G2/G3) before starting the watcher." % BASE_BRANCH))
    return results


# ---------------------------------------------------------------- Group B
def check_copilot_cli():
    results = []
    cli_enabled = os.getenv("COPILOT_CLI_ENABLED", "false").strip().lower() == "true"
    if not cli_enabled:
        results.append(("B0 Copilot CLI enabled", True,
                         "COPILOT_CLI_ENABLED=false — headless step disabled, skipping CLI checks"))
        return results

    code, out, err = _run(["copilot", "-p", "say hello", "-s", "--no-ask-user"], timeout=45)
    logged_in = (code == 0 and "Authentication failed" not in (out + err))
    results.append(("B1 Copilot CLI logged in", logged_in,
                     "responded OK" if logged_in else
                     "run: copilot login --device-code (%s)" % err.strip()[:300]))

    code, out, err = _run(["copilot", "-p", "run: git status --short --branch", "-s", "--no-ask-user"], timeout=45)
    denied = "Permission denied" in (out + err)
    trusted = (code == 0 and not denied)
    results.append(("B2 folder trusted for shell/write", trusted,
                     "verified headless" if trusted else
                     "Permission denied — this folder's approvals are missing/incomplete in "
                     "%USERPROFILE%\\.copilot\\permissions-config.json (see Appendix D / Issue C1-C2)"))
    return results


# ---------------------------------------------------------------- Group C
def check_credentials():
    results = []
    code, out, _ = _run(["python", os.path.join(AGENT_DIR, "check_connections.py")], timeout=30)
    all_ok = (code == 0) and ("FAIL" not in out)
    results.append(("C1 Jira + GitHub reachable", all_ok,
                     "all OK" if all_ok else out.strip()[:400]))

    github_repo_env = os.getenv("GITHUB_REPO", "").strip()
    _, remote_out, _ = _run(["git", "remote", "get-url", "origin"])
    remote_out = remote_out.strip()
    repo_match = bool(github_repo_env) and (github_repo_env in remote_out)
    results.append(("C2 GITHUB_REPO matches git remote", repo_match,
                     "matches" if repo_match else
                     ".env GITHUB_REPO='%s' vs git remote='%s' — these must point at the same repo (Issue G6)" % (github_repo_env, remote_out)))

    sf_org = os.getenv("SF_TARGET_ORG", "").strip()
    results.append(("C3 SF_TARGET_ORG set", bool(sf_org),
                     "set to '%s'" % sf_org if sf_org else "SF_TARGET_ORG is empty in .env"))

    agent_enabled = os.getenv("AGENT_ENABLED", "false").strip().lower() == "true"
    results.append(("C4 AGENT_ENABLED=true", agent_enabled,
                     "true" if agent_enabled else "false — kill switch is off, nothing will run anyway"))
    return results


# ---------------------------------------------------------------- Group D
def check_salesforce():
    results = []
    sf_org = os.getenv("SF_TARGET_ORG", "").strip()
    if not sf_org:
        results.append(("D1 org authenticated", False, "SF_TARGET_ORG not set in .env"))
        return results
    code, out, err = _run(["sf", "org", "display", "--target-org", sf_org, "--json"], timeout=30)
    ok, detail = False, (err.strip()[:300] or out.strip()[:300])
    if code == 0:
        try:
            data = json.loads(out)
            ok = data.get("status") == 0
            if ok:
                detail = "connected (%s)" % data.get("result", {}).get("username", sf_org)
        except Exception:
            detail = "could not parse sf output"
    results.append(("D1 org '%s' authenticated" % sf_org, ok, detail))
    return results


# ---------------------------------------------------------------- Runner
def run_all(verbose=True):
    groups = [
        ("Repository state (incl. sync to latest origin/%s)" % BASE_BRANCH, check_git_state),
        ("Copilot CLI", check_copilot_cli),
        ("Credentials and config", check_credentials),
        ("Salesforce", check_salesforce),
    ]
    all_results, all_passed = [], True
    if verbose:
        print("=" * 60); print("PRE-FLIGHT CHECKS"); print("=" * 60)
    for label, fn in groups:
        if verbose:
            print("\n%s" % label)
        try:
            results = fn()
        except Exception as e:
            results = [("%s (error)" % label, False, "check raised an exception: %s" % e)]
        for name, passed, detail in results:
            all_results.append((name, passed, detail))
            all_passed = all_passed and passed
            if verbose:
                print("  %-4s %-45s %s" % ("PASS" if passed else "FAIL", name, "" if passed else detail))
    if verbose:
        print("\n" + "-" * 60)
        print("ALL PRE-FLIGHT CHECKS PASSED - safe to start the watcher." if all_passed
              else "PRE-FLIGHT CHECKS FAILED - fix the items above before starting the watcher.")
    return all_passed, all_results


def failure_summary(results):
    failed = [(n, d) for n, p, d in results if not p]
    return "No failures." if not failed else "\n".join("- %s: %s" % (n, d) for n, d in failed)


if __name__ == "__main__":
    ok, _ = run_all(verbose=True)
    raise SystemExit(0 if ok else 1)
