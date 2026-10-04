"""Step 6.5: Run GitHub Copilot CLI headlessly to implement the fix/feature
that Copilot Agent mode would otherwise do interactively in VS Code chat.

Reads docs/ai-reports/<KEY>-prompt.md (written by prepare_fix.py) and feeds
it to `copilot -p ...` in non-interactive mode. Respects AGENT_ENABLED and
AGENT_DRY_RUN exactly like every other step.

ONE-TIME SETUP, already done once interactively per machine/repo:
    Run `copilot` with no arguments from the repo root and accept the
    "trust this folder" prompt with the "remember" option. This is recorded
    in ~/.copilot/config.json and is NOT required again for this repo path.
    Without it, every file write silently fails even with --allow-tool set.

This does NOT weaken any guardrail: .github/copilot-instructions.md is
auto-discovered by the CLI from the repo root (same rules Agent mode in
VS Code already followed), and validate_fix.py (Step 7) still re-checks
everything afterward. This step only replaces the manual "paste into VS
Code chat and watch it work" action with a scripted equivalent.

.env additions needed:
    COPILOT_CLI_ENABLED=true          # false = refuses to run (safety default)
    COPILOT_CLI_MODEL=                # optional, blank = CLI default
    COPILOT_CLI_TIMEOUT_MINUTES=15

Usage: python invoke_copilot.py CLAUDE-11
"""
import os
import re
import sys
import shutil
import subprocess
import jira_client as jc

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_DIR = os.path.join(REPO_ROOT, "docs", "ai-reports")
LOG_DIR = os.path.join(REPO_ROOT, "logs")
KEY_PATTERN = re.compile(rf"^{re.escape(jc.PROJECT)}-\d+$")

CLI_ENABLED = os.getenv("COPILOT_CLI_ENABLED", "false").strip().lower() == "true"
CLI_MODEL = os.getenv("COPILOT_CLI_MODEL", "").strip()
CLI_TIMEOUT_MIN = int(os.getenv("COPILOT_CLI_TIMEOUT_MINUTES", "15"))

# What Copilot IS allowed to do without asking: read/write files, run git
# read/local-commit commands, run Salesforce CLI (local test runs only -
# path-level restrictions still live in copilot-instructions.md and are
# re-checked by validate_fix.py; this is belt-and-suspenders, not the only
# gate).
ALLOW_TOOLS = "read,write,shell(git:*),shell(sf:*)"

# Hard block at the CLI level, not just in prompt wording: Copilot must
# never push, never touch GitHub's `gh` CLI, never create a PR itself, and
# never deploy to Salesforce. create_pr.py (Step 8, run by this pipeline
# afterward, never by Copilot) is the only thing allowed to push and open
# the PR.
DENY_TOOLS = "shell(gh:*),shell(git push*),shell(sf * deploy*),shell(sf * org *)"


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python invoke_copilot.py <TICKET-KEY>")
    key = sys.argv[1].strip().upper()
    if not KEY_PATTERN.match(key):
        sys.exit(f"STOP: '{key}' is not a valid {jc.PROJECT} ticket key.")

    print("=" * 60)
    print(f"AGENT INVOKE COPILOT CLI | {key} | Dry run: {jc.DRY_RUN}")
    print("=" * 60)

    if not jc.ENABLED:
        sys.exit("Agent disabled (AGENT_ENABLED is not 'true'). Stopping.")

    prompt_path = os.path.join(REPORT_DIR, f"{key}-prompt.md")
    if not os.path.exists(prompt_path):
        sys.exit(f"STOP: {prompt_path} not found. Run prepare_fix.py first.")

    if jc.DRY_RUN:
        print(f"  [dry-run] would run: copilot -p <contents of {key}-prompt.md> "
              f"-s --no-ask-user --allow-tool {ALLOW_TOOLS} --deny-tool {DENY_TOOLS}")
        jc.audit("invoke_copilot", key, "dry-run - CLI not invoked", "dry-run")
        return

    if not CLI_ENABLED:
        sys.exit("STOP: COPILOT_CLI_ENABLED is not 'true' in .env. "
                  "Set it once `copilot login` has been done, or fall back "
                  "to the manual VS Code Agent-mode step for this ticket.")

    cli = shutil.which("copilot")
    if not cli:
        sys.exit("STOP: 'copilot' CLI not found on PATH. "
                  "Install with: npm install -g @github/copilot")

    with open(prompt_path, encoding="utf-8") as f:
        prompt_text = f.read()

    cmd = [cli, "-p", prompt_text, "-s", "--no-ask-user",
           "--allow-tool", ALLOW_TOOLS, "--deny-tool", DENY_TOOLS]
    if CLI_MODEL:
        cmd += ["--model", CLI_MODEL]

    # This repo's .env sets GITHUB_TOKEN to a fine-grained PAT scoped only
    # for create_pr.py (Contents/PRs/Metadata on one repo). That token does
    # NOT have the 'Copilot Requests' permission. Since jira_client already
    # called load_dotenv() at import time, GITHUB_TOKEN (and GH_TOKEN/
    # COPILOT_GITHUB_TOKEN, if ever set) are sitting in this process's
    # environment and would leak into the copilot subprocess, causing it to
    # try to authenticate with the wrong token instead of using your
    # `copilot login` session. Strip them here so the CLI always falls back
    # to its own stored login.
    cli_env = os.environ.copy()
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        cli_env.pop(var, None)

    print(f"  Running Copilot CLI headlessly (timeout {CLI_TIMEOUT_MIN} min)...")
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=cli_env,
                           timeout=CLI_TIMEOUT_MIN * 60)
    except subprocess.TimeoutExpired:
        jc.audit("invoke_copilot", key, f"timed out after {CLI_TIMEOUT_MIN} min", "failed")
        sys.exit("FAIL: Copilot CLI timed out.")

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{key}-copilot-cli.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"CMD: copilot -p <{len(prompt_text)} char prompt> -s --no-ask-user "
                 f"--allow-tool {ALLOW_TOOLS} --deny-tool {DENY_TOOLS}\n\n"
                 f"STDOUT:\n{r.stdout}\n\nSTDERR:\n{r.stderr}\n")

    if r.returncode != 0:
        jc.audit("invoke_copilot", key, f"exit {r.returncode} - see {log_path}", "failed")
        sys.exit(f"FAIL: Copilot CLI exited {r.returncode}. See {log_path}")

    jc.audit("invoke_copilot", key, f"completed - log: {os.path.relpath(log_path, REPO_ROOT)}", "ok")
    print(f"  done: Copilot CLI finished. Log: {os.path.relpath(log_path, REPO_ROOT)}")
    print("\n" + "-" * 60)
    print(f"NEXT: python review_gate.py {key}")


if __name__ == "__main__":
    try:
        main()
    except jc.JiraError as e:
        jc.audit("error", "-", str(e), "failed")
        sys.exit(f"FAIL {e}")
