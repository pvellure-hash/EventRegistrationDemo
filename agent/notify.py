"""agent/notify.py - NEW: Failure notification layer.

Call notify_failure(step, ticket, reason) whenever a pipeline step fails.
This ALWAYS does two things (zero setup required):
  1. Appends a structured record to logs/notifications.jsonl
  2. Shows a Windows toast notification (native WinRT via PowerShell,
     no extra modules to install)

It OPTIONALLY also emails you via Microsoft Graph (me/sendMail), if you've
completed the one-time Azure AD app registration + device-code sign-in
described in the setup comment below. Until then, NOTIFY_EMAIL_ENABLED
should stay 'false' and you'll still get the toast + log, just not email.

Per-step guidance text is centralized here so the message always tells you
exactly what to check, matching the troubleshooting table in the setup
guide.

.env additions:
    NOTIFY_ENABLED=true
    NOTIFY_TOAST_ENABLED=true
    NOTIFY_EMAIL_ENABLED=false        # true once Azure app + sign-in done
    NOTIFY_EMAIL_TO=pvellure@deloitte.ca
    AZURE_CLIENT_ID=                  # from self-service app registration, see below
    AZURE_AUTHORITY=https://login.microsoftonline.com/organizations

ONE-TIME SETUP FOR EMAIL (optional - skip if toast + log is enough for now):
  1. Go to portal.azure.com -> Microsoft Entra ID -> App registrations -> New registration
     - Name: anything, e.g. "EventRegistrationPipelineNotifier"
     - Supported account types: single tenant (your org only)
     - Redirect URI: leave blank (not needed for device code flow)
  2. Open the new app -> Authentication -> Advanced settings ->
     "Allow public client flows" -> Yes -> Save
  3. API permissions -> Add a permission -> Microsoft Graph -> Delegated ->
     Mail.Send -> Add. If your tenant requires admin consent for this
     (depends on your org's policy), ask your M365 admin to grant it - this
     is a narrow, read-nothing, send-only permission acting as you.
  4. Copy the "Application (client) ID" from the Overview page into
     AZURE_CLIENT_ID in .env.
  5. pip install msal
  6. Set NOTIFY_EMAIL_ENABLED=true, run any pipeline step once - on first
     run you'll see a device code + URL printed; sign in once in a browser.
     A token cache is saved locally (.msal_cache.bin, gitignored) and
     silently refreshes after that - no need to sign in again.
"""
import json
import os
import subprocess
import time

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
LOG_DIR = os.path.join(REPO_ROOT, "logs")
NOTIFICATIONS_LOG = os.path.join(LOG_DIR, "notifications.jsonl")
MSAL_CACHE_PATH = os.path.join(AGENT_DIR, ".msal_cache.bin")

NOTIFY_ENABLED = os.getenv("NOTIFY_ENABLED", "true").strip().lower() == "true"
TOAST_ENABLED = os.getenv("NOTIFY_TOAST_ENABLED", "true").strip().lower() == "true"
EMAIL_ENABLED = os.getenv("NOTIFY_EMAIL_ENABLED", "false").strip().lower() == "true"
EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "").strip()
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "").strip()
AZURE_AUTHORITY = os.getenv("AZURE_AUTHORITY", "https://login.microsoftonline.com/organizations").strip()
GRAPH_SCOPES = ["Mail.Send"]

# Centralized per-step guidance - keep aligned with the troubleshooting
# table in the setup guide so the message you get always matches what the
# doc says to do.
STEP_GUIDANCE = {
    "claim_ticket.py":
        "The ticket likely failed triage (missing one of the four required "
        "Description headings, or flagged by the prompt-injection filter). "
        "Run 'python claim_ticket.py' manually to see the exact reason, or "
        "check the ticket's labels in Jira (ai-needs-info / ai-blocked).",
    "prepare_fix.py":
        "Usually an unclean working tree or a git branch divergence. Run "
        "'git status' in the repo root; if diverged from origin/main, "
        "reconcile with 'git merge' or 'git cherry-pick' rather than "
        "'git reset --hard' to avoid losing local commits.",
    "invoke_copilot.py":
        "Check logs/<KEY>-copilot-cli.log for the actual error. Common "
        "causes: (1) this repo folder's write/shell approvals are missing "
        "from %USERPROFILE%\\.copilot\\permissions-config.json - compare "
        "against a working repo's entry and add the missing tool_approvals; "
        "(2) GITHUB_TOKEN leaking into the subprocess and conflicting with "
        "the CLI's own login; (3) a 15-minute timeout - rerun or raise "
        "COPILOT_CLI_TIMEOUT_MINUTES in .env.",
    "review_gate.py":
        "Either you answered 'n', or a restricted file path was touched and "
        "auto-rejected. Check the diff summary printed just before this. If "
        "rejected in error, resume the Copilot session or rerun "
        "invoke_copilot.py, then review_gate.py again.",
    "validate_fix.py":
        "Check which specific check(s) printed FAIL - common ones: no "
        "commit yet (Copilot edited files but didn't commit - commit them "
        "yourself), missing docs/ai-reports/<KEY>-pr.md or -jira.md (ask "
        "Copilot to finish writing just those two files), or a real Apex "
        "test failure (read the stack trace and decide if the fix or the "
        "test needs correcting).",
    "create_pr.py":
        "Usually a GitHub token problem. Run 'python check_connections.py' "
        "- a 404 means GITHUB_TOKEN isn't scoped to this exact repo (check "
        "the fine-grained PAT's repository access); a 422 on PR creation "
        "usually means the branch wasn't actually pushed to the repo this "
        "token points to.",
    "update_jira.py":
        "Check JIRA_API_TOKEN validity, and confirm JIRA_STATUS_IN_REVIEW "
        "in .env exactly matches (case and spacing) a real status name in "
        "this Jira project's workflow.",
    "poll_loop":
        "An unhandled exception occurred while polling Jira for tickets, "
        "outside of any single ticket's pipeline run. Check network/Jira "
        "connectivity and review the full traceback printed in the "
        "terminal.",
}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_log(record):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(NOTIFICATIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _show_toast(title, message):
    # Native WinRT toast via PowerShell - no extra modules needed, works on
    # any Windows 10/11 machine out of the box.
    ps_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($template.CreateTextNode("{title}")) > $null
$textNodes.Item(1).AppendChild($template.CreateTextNode("{message}")) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Agent Pipeline").Show($toast)
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script],
                       capture_output=True, timeout=10)
    except Exception:
        pass  # toast is best-effort; never let it break the pipeline


def _get_graph_token():
    try:
        import msal
    except ImportError:
        print("  [notify] msal not installed - run: pip install msal")
        return None

    cache = msal.SerializableTokenCache()
    if os.path.exists(MSAL_CACHE_PATH):
        with open(MSAL_CACHE_PATH, "r", encoding="utf-8") as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        AZURE_CLIENT_ID, authority=AZURE_AUTHORITY, token_cache=cache
    )
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            print(f"  [notify] Could not start device flow: {flow}")
            return None
        print(f"  [notify] One-time sign-in needed: {flow['message']}")
        result = app.acquire_token_by_device_flow(flow)

    if cache.has_state_changed:
        with open(MSAL_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())

    return result.get("access_token") if result else None


def _send_email(subject, body_html):
    if not AZURE_CLIENT_ID or not EMAIL_TO:
        print("  [notify] AZURE_CLIENT_ID or NOTIFY_EMAIL_TO not set - skipping email.")
        return False
    try:
        import requests
    except ImportError:
        print("  [notify] requests not installed - skipping email.")
        return False

    token = _get_graph_token()
    if not token:
        print("  [notify] Could not acquire Graph token - skipping email.")
        return False

    r = requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": EMAIL_TO}}],
            },
            "saveToSentItems": "true",
        },
        timeout=30,
    )
    if r.status_code == 202:
        return True
    print(f"  [notify] Graph sendMail failed: HTTP {r.status_code} {r.text[:300]}")
    return False


def notify_failure(step: str, ticket: str, reason: str = ""):
    """Call this whenever a pipeline step fails or is rejected.
    step: the script name that failed, e.g. 'validate_fix.py', or 'poll_loop'.
    ticket: the Jira ticket key, or '-' if not ticket-specific.
    reason: optional short machine-readable detail (e.g. exit code)."""
    if not NOTIFY_ENABLED:
        return

    guidance = STEP_GUIDANCE.get(step, "Check the terminal output and the relevant log file for details.")
    timestamp = _now()

    record = {
        "time": timestamp, "ticket": ticket, "failed_step": step,
        "reason": reason, "guidance": guidance,
    }
    _append_log(record)

    title = f"Pipeline failed: {ticket}"
    short_msg = f"Stopped at {step}. {guidance[:120]}"
    print(f"\n{'!'*60}\n! NOTIFICATION: {title}\n!   Step: {step}\n!   Guidance: {guidance}\n{'!'*60}\n")

    if TOAST_ENABLED:
        _show_toast(title, short_msg)

    if EMAIL_ENABLED:
        body_html = f"""
        <p><b>Ticket:</b> {ticket}</p>
        <p><b>Failed at step:</b> {step}</p>
        <p><b>Time (UTC):</b> {timestamp}</p>
        <p><b>Detail:</b> {reason or '(see terminal/log output)'}</p>
        <p><b>What to do next:</b><br>{guidance}</p>
        <p>Full detail is in logs/agent-audit.jsonl and logs/notifications.jsonl
        on the machine running the pipeline.</p>
        """
        _send_email(f"[Agent Pipeline] {ticket} failed at {step}", body_html)


if __name__ == "__main__":
    # Quick manual test: python notify.py
    notify_failure("validate_fix.py", "TEST-0", "manual test of notify.py")
    print("Test notification sent (check for toast + logs/notifications.jsonl).")
