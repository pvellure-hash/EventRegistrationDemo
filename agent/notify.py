"""agent/notify.py — Failure notification layer, now with configurable
multi-recipient alerts (recipients.json) and a "preflight" category
distinct from per-ticket "failure" alerts.

Two kinds of alerts:
  - category="preflight": the watcher refused to start at all, or skipped
    a ticket, because an automated pre-flight check failed (see preflight.py).
  - category="failure": a ticket's pipeline run failed partway through.

Who gets emailed is controlled entirely by agent/recipients.json — add or
remove people there without touching any code. Each person's "notify_on"
list can be ["preflight"], ["failure"], or ["all"].

ALWAYS happens (zero setup):
  1. Appends a structured record to logs/notifications.jsonl
  2. Shows a Windows toast notification (native WinRT via PowerShell)

OPTIONAL (one-time Azure AD app registration + MSAL device-code sign-in,
see setup comment below): emails every matching recipient in
recipients.json via Microsoft Graph (me/sendMail).

.env additions:
    NOTIFY_ENABLED=true
    NOTIFY_TOAST_ENABLED=true
    NOTIFY_EMAIL_ENABLED=false        # true once Azure app + sign-in done
    AZURE_CLIENT_ID=                  # from self-service app registration
    AZURE_AUTHORITY=https://login.microsoftonline.com/organizations

ONE-TIME SETUP FOR EMAIL:
  1. portal.azure.com -> Microsoft Entra ID -> App registrations -> New
     registration. Single tenant; no redirect URI needed.
  2. Authentication -> Advanced settings -> Allow public client flows -> Yes.
  3. API permissions -> Add -> Microsoft Graph -> Delegated -> Mail.Send.
     (May need admin consent depending on tenant policy.)
  4. Copy the Application (client) ID into AZURE_CLIENT_ID in .env.
  5. pip install msal
  6. Set NOTIFY_EMAIL_ENABLED=true. First run prints a device code once;
     sign in in a browser. Token cache refreshes silently after that.
"""
import json
import os
import subprocess
import time
import redact

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
LOG_DIR = os.path.join(REPO_ROOT, "logs")
NOTIFICATIONS_LOG = os.path.join(LOG_DIR, "notifications.jsonl")
MSAL_CACHE_PATH = os.path.join(AGENT_DIR, ".msal_cache.bin")
RECIPIENTS_PATH = os.path.join(AGENT_DIR, "recipients.json")

NOTIFY_ENABLED = os.getenv("NOTIFY_ENABLED", "true").strip().lower() == "true"
TOAST_ENABLED = os.getenv("NOTIFY_TOAST_ENABLED", "true").strip().lower() == "true"
EMAIL_ENABLED = os.getenv("NOTIFY_EMAIL_ENABLED", "false").strip().lower() == "true"
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "").strip()
AZURE_AUTHORITY = os.getenv("AZURE_AUTHORITY", "https://login.microsoftonline.com/organizations").strip()
GRAPH_SCOPES = ["Mail.Send"]

STEP_GUIDANCE = {
    "preflight":
        "The watcher's automated pre-flight checks failed before it would start polling. "
        "Run 'python preflight.py' directly in the agent folder to see exactly which check "
        "failed and why - each one maps to a specific fix in Part 7/10 of the setup guide.",
    "claim_ticket.py":
        "The ticket likely failed triage (missing one of the four required Description "
        "headings, or flagged by the prompt-injection filter). Run 'python claim_ticket.py' "
        "manually to see the exact reason, or check the ticket's labels in Jira.",
    "prepare_fix.py":
        "Usually an unclean working tree or a git branch divergence. Run 'git status'; if "
        "diverged from origin/main, reconcile with 'git merge' or 'git cherry-pick' rather "
        "than 'git reset --hard' to avoid losing local commits.",
    "invoke_copilot.py":
        "Check logs/<KEY>-copilot-cli.log for the actual error. Common causes: (1) this "
        "repo folder's write/shell approvals are missing from "
        "%USERPROFILE%\\.copilot\\permissions-config.json; (2) GITHUB_TOKEN leaking into the "
        "subprocess; (3) a timeout - rerun or raise COPILOT_CLI_TIMEOUT_MINUTES.",
    "review_gate.py":
        "Either you answered 'n', or a restricted file path was touched and auto-rejected. "
        "Check the diff summary printed just before this.",
    "validate_fix.py":
        "Check which specific check(s) printed FAIL - common ones: no commit yet, missing "
        "docs/ai-reports/<KEY>-pr.md or -jira.md, or a real Apex test failure.",
    "create_pr.py":
        "Usually a GitHub token problem. Run 'python check_connections.py' - a 404 means "
        "GITHUB_TOKEN isn't scoped to this exact repo.",
    "update_jira.py":
        "Check JIRA_API_TOKEN validity, and confirm JIRA_STATUS_IN_REVIEW in .env exactly "
        "matches a real status name in this Jira project's workflow.",
    "poll_loop":
        "An unhandled exception occurred while polling Jira, outside any single ticket's "
        "pipeline run. Check network/Jira connectivity and the full traceback printed.",
}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_log(record):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(NOTIFICATIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _load_recipients():
    """Returns a list of {"name":..., "email":..., "notify_on":[...]} dicts.
    Falls back to an empty list (toast/log still work) if the file is
    missing or malformed - this must never crash the pipeline."""
    if not os.path.exists(RECIPIENTS_PATH):
        return []
    try:
        with open(RECIPIENTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        recipients = data.get("recipients", [])
        valid = []
        for r in recipients:
            email = (r.get("email") or "").strip()
            notify_on = r.get("notify_on") or []
            if email and notify_on:
                valid.append({"name": r.get("name", email), "email": email, "notify_on": notify_on})
        return valid
    except Exception as e:
        print(f"  [notify] Could not read recipients.json ({e}) - no email recipients loaded.")
        return []


def _recipients_for(category):
    return [r for r in _load_recipients() if category in r["notify_on"] or "all" in r["notify_on"]]


def _show_toast(title, message):
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
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, timeout=10)
    except Exception:
        pass


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
    app = msal.PublicClientApplication(AZURE_CLIENT_ID, authority=AZURE_AUTHORITY, token_cache=cache)
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


def _send_email(to_email, subject, body_html):
    if not AZURE_CLIENT_ID:
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
        json={"message": {"subject": subject, "body": {"contentType": "HTML", "content": body_html},
                          "toRecipients": [{"emailAddress": {"address": to_email}}]},
              "saveToSentItems": "true"},
        timeout=30,
    )
    if r.status_code == 202:
        return True
    print(f"  [notify] Graph sendMail to {to_email} failed: HTTP {r.status_code} {r.text[:200]}")
    return False


def notify_failure(step: str, ticket: str, reason: str = ""):
    """Call whenever a pipeline step fails, is rejected, or pre-flight
    checks block a start. step='preflight' is treated as its own category
    so recipients can subscribe to it separately from per-ticket failures."""
    reason = redact.redact(reason)
    if not NOTIFY_ENABLED:
        return

    category = "preflight" if step == "preflight" else "failure"
    guidance = STEP_GUIDANCE.get(step, "Check the terminal output and the relevant log file for details.")
    timestamp = _now()

    record = {"time": timestamp, "category": category, "ticket": ticket,
              "failed_step": step, "reason": reason, "guidance": guidance}
    _append_log(record)

    title = f"Pipeline pre-flight failed" if category == "preflight" else f"Pipeline failed: {ticket}"
    short_msg = f"{step}: {guidance[:120]}"
    print(f"\n{'!'*60}\n! NOTIFICATION [{category}]: {title}\n!   Step: {step}\n!   Guidance: {guidance}\n{'!'*60}\n")

    if TOAST_ENABLED:
        _show_toast(title, short_msg)

    if EMAIL_ENABLED:
        recipients = _recipients_for(category)
        if not recipients:
            print(f"  [notify] No recipients.json entries subscribed to '{category}' - no email sent.")
        body_html = f"""
        <p><b>Category:</b> {category}</p>
        <p><b>Ticket:</b> {ticket}</p>
        <p><b>Failed at step:</b> {step}</p>
        <p><b>Time (UTC):</b> {timestamp}</p>
        <p><b>Detail:</b><br>{(reason or '(see terminal/log output)').replace(chr(10), '<br>')}</p>
        <p><b>What to do next:</b><br>{guidance}</p>
        <p>Full detail is in logs/agent-audit.jsonl and logs/notifications.jsonl.</p>
        """
        for r in recipients:
            ok = _send_email(r["email"], f"[Agent Pipeline] {title}", body_html)
            print(f"  [notify] email to {r['name']} <{r['email']}>: {'sent' if ok else 'FAILED'}")


if __name__ == "__main__":
    notify_failure("validate_fix.py", "TEST-0", "manual test of notify.py")
    print("Test notification sent (check for toast + logs/notifications.jsonl).")
    print("Current recipients.json entries:", _load_recipients())
