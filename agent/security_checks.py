"""agent/security_checks.py - Phase 0: security pre-flight checks.

  1. Production-org guard   - refuse unless SF_TARGET_ORG is a sandbox or a
                              Developer Edition org (or an explicitly
                              allow-listed org id).
  2. Branch protection      - confirm the base branch is protected and
                              requires a pull request before merging.
  3. GitHub token scope     - refuse classic (broad) tokens; warn if a
                              fine-grained token can reach more than one repo.

Results use the same (name, passed, detail) tuple shape as preflight.py, so
they print and summarise the same way. A detail starting with "WARN" passed
but should be looked at.

Run on its own:  python security_checks.py
"""
import os
import re
import sys
import json
import shutil
import subprocess
import urllib.request
import urllib.error

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
ALLOWED_ORG_TYPES = {"Developer Edition"}
GITHUB_API = os.getenv("GITHUB_API_URL", "https://api.github.com")


# ---------------- helpers ----------------
def _cli(args, timeout=90):
    """Run a CLI. On Windows, npm shims (sf.cmd) go through cmd /c - the same
    workaround the rest of the pipeline uses."""
    exe = shutil.which(args[0])
    if not exe:
        return 127, "", f"'{args[0]}' not found on PATH"
    cmd = [exe] + args[1:]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"{args[0]} timed out after {timeout}s"


def _repo_slug():
    slug = os.getenv("GITHUB_REPOSITORY", "").strip()
    if slug:
        return slug
    rc, out, _ = _cli(["git", "remote", "get-url", "origin"], timeout=15)
    m = re.search(r"github\.com[:/]([^/]+/[^/.\s]+?)(?:\.git)?\s*$", out.strip())
    return m.group(1) if (rc == 0 and m) else None


def _gh(path, token):
    req = urllib.request.Request(GITHUB_API + path, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agent-preflight"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"null")
        except Exception:
            body = None
        return e.code, body, dict(e.headers or {})
    except Exception as e:
        return 0, {"message": str(e)}, {}


# ---------------- 1. production-org guard ----------------
def check_salesforce_org():
    name = "Salesforce org is non-production"
    org = os.getenv("SF_TARGET_ORG", "").strip()
    if not org:
        return [(name, False, "SF_TARGET_ORG is not set in .env")]
    rc, out, err = _cli(["sf", "data", "query", "--query",
                         "SELECT Id, Name, IsSandbox, OrganizationType FROM Organization",
                         "--target-org", org, "--json"])
    try:
        rec = json.loads(out)["result"]["records"][0]
    except Exception:
        msg = (err or out or "").strip().splitlines()[-1:] or ["no output"]
        return [(name, False, f"could not query org '{org}': {msg[0][:200]}")]
    org_id = rec.get("Id", "")
    allow_ids = {x.strip()[:15] for x in os.getenv("ALLOWED_ORG_IDS", "").split(",") if x.strip()}
    if allow_ids and org_id[:15] not in allow_ids:
        return [(name, False, f"org {org_id} is not in ALLOWED_ORG_IDS")]
    if rec.get("IsSandbox"):
        return [(name, True, f"sandbox '{rec.get('Name')}' ({org_id})")]
    if rec.get("OrganizationType") in ALLOWED_ORG_TYPES:
        return [(name, True, f"{rec.get('OrganizationType')} '{rec.get('Name')}' ({org_id})")]
    return [(name, False, f"REFUSED: '{org}' is a PRODUCTION org "
                          f"({rec.get('OrganizationType')}, {org_id}). The agent never runs against production.")]


# ---------------- 2. branch protection ----------------
def check_branch_protection():
    name = "Base branch requires a reviewed PR"
    token = os.getenv("GITHUB_TOKEN", "").strip()
    branch = os.getenv("BASE_BRANCH", "main").strip()
    slug = _repo_slug()
    if not token or not slug:
        return [(name, False, "GITHUB_TOKEN or repository (origin remote / GITHUB_REPOSITORY) not available")]
    st, body, _ = _gh(f"/repos/{slug}/branches/{branch}", token)
    if st != 200:
        return [(name, False, f"cannot read branch {slug}@{branch} (HTTP {st})")]
    if not body.get("protected"):
        return [(name, False, f"{branch} is NOT protected on {slug} - anyone with write access could push or merge")]
    # Rulesets (readable with normal repo read access)
    st2, rules, _ = _gh(f"/repos/{slug}/rules/branches/{branch}", token)
    rule_types = {r.get("type") for r in rules} if (st2 == 200 and isinstance(rules, list)) else set()
    if "pull_request" in rule_types:
        return [(name, True, f"{branch} protected; ruleset requires pull request")]
    # Classic protection (needs admin read; often 403/404 for a least-privilege token - that is fine)
    st3, prot, _ = _gh(f"/repos/{slug}/branches/{branch}/protection", token)
    if st3 == 200 and isinstance(prot, dict):
        rv = prot.get("required_pull_request_reviews") or {}
        n = rv.get("required_approving_review_count", 0)
        if rv and n >= 1:
            return [(name, True, f"{branch} protected; {n} approving review(s) required")]
        return [(name, False, f"{branch} is protected but does not require an approving review")]
    return [(name, True, f"WARN {branch} is protected, but review rules could not be read with this "
                         f"least-privilege token. Confirm 'Require a pull request before merging' in repo settings.")]


# ---------------- 3. token scope ----------------
def check_github_token_scope():
    name = "GitHub token is least-privilege"
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return [(name, False, "GITHUB_TOKEN not set")]
    if token.startswith(("ghp_", "gho_")):
        st, _, hdr = _gh("/user", token)
        scopes = hdr.get("X-OAuth-Scopes") or hdr.get("x-oauth-scopes") or "unknown"
        return [(name, False, f"classic token in use (scopes: {scopes}). Replace with a fine-grained token "
                              f"limited to this one repo: Contents RW, Pull requests RW, Metadata R.")]
    st, repos, _ = _gh("/user/repos?per_page=100&affiliation=owner,collaborator,organization_member", token)
    if st != 200 or not isinstance(repos, list):
        return [(name, False, f"token check failed (HTTP {st})")]
    slug = (_repo_slug() or "").lower()
    names = [r.get("full_name", "") for r in repos]
    if slug and slug not in [n.lower() for n in names]:
        return [(name, False, f"token cannot see {slug}")]
    admin = [r["full_name"] for r in repos if (r.get("permissions") or {}).get("admin")]
    if len(names) > 1:
        return [(name, True, f"WARN token can reach {len(names)} repos; scope it to {slug or 'this repo'} only")]
    if admin:
        return [(name, True, f"WARN account has admin on {', '.join(admin)}; the token itself should not grant Administration")]
    return [(name, True, f"fine-grained token scoped to {names[0] if names else slug}")]


def run_all(verbose=True):
    results = []
    for fn in (check_salesforce_org, check_branch_protection, check_github_token_scope):
        try:
            results += fn()
        except Exception as e:
            results.append((fn.__name__, False, f"check crashed: {e}"))
    if verbose:
        print("Security checks:")
        for n, ok, d in results:
            tag = "WARN" if ok and str(d).startswith("WARN") else ("PASS" if ok else "FAIL")
            print(f"  {tag:4}  {n}  - {d}")
    return all(ok for _, ok, _ in results), results


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO_ROOT, ".env"))
    except ImportError:
        pass
    ok, _ = run_all()
    sys.exit(0 if ok else 1)
