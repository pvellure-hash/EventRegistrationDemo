"""Step 6.6: Human review gate. Shows a diff-based summary of exactly what
the headless Copilot CLI fix changed, then BLOCKS for an explicit y/n
before the pipeline is allowed to continue to Step 7 (validate_fix.py).

The summary is built entirely from `git diff` against the base branch -
never from anything the agent claims about itself in its Solution Report -
so it can't be spoofed by a confused agent or a prompt-injected ticket.
Reuses the exact same ALLOWED_PREFIXES / BLOCKED_PATTERNS constants that
validate_fix.py enforces, so a restricted-path touch is auto-rejected here
too, before you even get asked.

Usage: python review_gate.py CLAUDE-11
Exit code 0 = approved, 1 = rejected or auto-blocked.
"""
import os
import re
import sys
import subprocess
import jira_client as jc
from validate_fix import ALLOWED_PREFIXES, BLOCKED_PATTERNS, CLASSES_DIR

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE = os.getenv("GITHUB_BASE_BRANCH", "main").strip()
REPORT_DIR = os.path.join(REPO_ROOT, "docs", "ai-reports")
KEY_PATTERN = re.compile(rf"^{re.escape(jc.PROJECT)}-\d+$")

BUCKET_LABELS = [
    (CLASSES_DIR, "Apex Classes"),
    ("force-app/main/default/triggers/", "Triggers"),
    ("force-app/main/default/lwc/", "Lightning Web Components"),
    ("force-app/main/default/aura/", "Aura Components"),
    ("force-app/main/default/objects/", "Object Metadata"),
    ("docs/ai-reports/", "AI Reports / Docs"),
]


def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def bucket(files):
    out = {}
    for prefix, label in BUCKET_LABELS:
        matched = [f for f in files if f.startswith(prefix)]
        if matched:
            out[label] = matched
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python review_gate.py <TICKET-KEY>")
    key = sys.argv[1].strip().upper()
    if not KEY_PATTERN.match(key):
        sys.exit(f"STOP: '{key}' is not a valid {jc.PROJECT} ticket key.")

    print("=" * 60)
    print(f"REVIEW GATE | {key}")
    print("=" * 60)

    branch = git("branch", "--show-current")
    if not branch.startswith(f"fix/{key}-"):
        sys.exit(f"STOP: current branch '{branch}' is not the fix branch for {key}.")

    files = [f for f in git("diff", "--name-only", f"{BASE}...HEAD").splitlines() if f]
    diffstat = git("diff", "--stat", f"{BASE}...HEAD")
    blocked = [f for f in files if any(p in f for p in BLOCKED_PATTERNS)]
    outside = [f for f in files if not f.startswith(ALLOWED_PREFIXES)]

    print(f"\nBranch: {branch}")
    print(f"Files changed: {len(files)}")

    buckets = bucket(files)
    if buckets:
        print("\nObjects/components impacted:")
        for label, fs in buckets.items():
            print(f"  {label}:")
            for f in fs:
                print(f"    - {f}")

    print("\nDiff stat:")
    print("  " + diffstat.replace("\n", "\n  "))

    report_path = os.path.join(REPORT_DIR, f"{key}.md")
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as f:
            excerpt = "".join(f.readlines()[:25])
        print("\nSolution Report excerpt (root cause / fix description):")
        print("  " + excerpt.replace("\n", "\n  "))

    if blocked or outside:
        bad = sorted(set(blocked + outside))
        print("\nRESTRICTED PATHS TOUCHED - auto-rejecting, this should not happen:")
        for f in bad:
            print(f"  - {f}")
        jc.audit("review_gate", key, "restricted path touched: " + ", ".join(bad), "failed")
        sys.exit(1)

    print("\n" + "-" * 60)
    answer = input("Proceed to validation + draft PR + Jira update? [y/N]: ").strip().lower()
    approved = answer in ("y", "yes")
    jc.audit("review_gate", key, f"{len(files)} files changed", "ok" if approved else "rejected")

    if not approved:
        print(f"\nRejected. Branch {branch} left as-is for inspection.")
        jc.add_comment(key, [
            "AI agent: fix was generated but rejected at human review gate.",
            "Branch left in place for inspection. No PR was created.",
        ])
        sys.exit(1)

    print("\nApproved. Proceeding to Step 7 (validate_fix.py).")


if __name__ == "__main__":
    try:
        main()
    except jc.JiraError as e:
        jc.audit("error", "-", str(e), "failed")
        sys.exit(f"FAIL {e}")
