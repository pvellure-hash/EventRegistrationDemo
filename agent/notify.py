"""agent/notify.py — Failure notification layer, now with configurable
multi-recipient alerts (recipients.json) and a "preflight" category
distinct from per-ticket "failure" alerts.
Three kinds of alerts:
  - category="preflight": the watcher refused to start at all, or skipped
    a ticket, because an automated pre-flight check failed (see preflight.py).
  - category="failure": a ticket's pipeline run failed partway through.
  - category="pr" (Step 2): pull-request lifecycle updates from pr_tracker -
    merged, closed without merge, merge conflict, waiting-for-review reminder,
    ticket waiting / released. Sent with notify_event().
Who gets emailed is controlled entirely by agent/recipients.json — add or
remove people there without touching any code. Each person's "notify_on"
list can be ["preflight"], ["failure"], ["pr"], or ["all"].
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
        "%USERPROFILE%\\\\.copilot\\\\permissions-config.json; (2) GITHUB_TOKEN leaking into the "
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
    # Step 1 / Step 2 additions
    "git_sync":
        "The repo could not be returned to main safely (uncommitted changes, or local main "
        "has commits that are not on origin). Nothing was changed. Run 'git status' and "
        "'python git_sync.py status' in the agent folder, commit or move the work, then "
        "'python git_sync.py sync'.",
    "pr_merged":
        "No action needed. Approve the Salesforce deploy in GitHub Actions "
        "(Review deployments -> salesforce-deploy-gate) when ready.",
    "pr_rejected":
        "The PR was closed without merging; the ticket is labelled ai-rejected. To retry, "
        "delete the branch, update the ticket if needed, and set the labels back to ai-ready only.",
    "pr_conflict":
        "The PR conflicts with main. Either resolve it on GitHub, or close the PR, delete the "
        "branch and set the ticket labels back to ai-ready only so the agent redoes it from "
        "the latest main.",
    "pr_reminder":
        "A pipeline PR is still waiting for review. Review and merge (or close) it on GitHub; "
        "tickets that touch the same files stay ai-waiting until then.",
    "pr_update":
        "The PR was behind main, so GitHub's Update branch was pressed. Salesforce Validate "
        "re-runs on the updated branch; no action needed unless it fails.",
    "ticket_waiting":
        "No action needed. The ticket is labelled ai-waiting and is released automatically "
        "when the PR or Jira ticket it depends on is merged/closed or Done.",
    "ticket_released":
        "No action needed. The ticket is back to ai-ready and will be picked up next poll.",
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


def _toast_safe(text):
    # The toast script embeds text inside a PowerShell double-quoted string.
    return str(text).replace('"', "'").replace("`", "'").replace("$", "S")


def _show_toast(title, message):
    title, message = _toast_safe(title), _toast_safe(message)
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


def _email_all(category, title, body_html):
    recipients = _recipients_for(category)
    if not recipients:
        print(f"  [notify] No recipients.json entries subscribed to '{category}' - no email sent.")
    for r in recipients:
        ok = _send_email(r["email"], f"[Agent Pipeline] {title}", body_html)
        print(f"  [notify] email to {r['name']} <{r['email']}>: {'sent' if ok else 'FAILED'}")


def notify_failure(step: str, ticket: str, reason: str = ""):
    """Call whenever a pipeline step fails, is rejected, or pre-flight
    checks block a start. step='preflight' is treated as its own category
    so recipients can subscribe to it separately from per-ticket failures."""
    reason = redact.redact(reason)
    if not NOTIFY_ENABLED:
        return
    category = "preflight" if step == "preflight" else "failure"
    guidance = STEP_GUIDANCE.get(step.split(" ")[0], "Check the terminal output and the relevant log file for details.")
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
        body_html = f"""
        <p><b>Category:</b> {category}</p>
        <p><b>Ticket:</b> {ticket}</p>
        <p><b>Failed at step:</b> {step}</p>
        <p><b>Time (UTC):</b> {timestamp}</p>
        <p><b>Detail:</b><br>{(reason or '(see terminal/log output)').replace(chr(10), '<br>')}</p>
        <p><b>What to do next:</b><br>{guidance}</p>
        <p>Full detail is in logs/agent-audit.jsonl and logs/notifications.jsonl.</p>
        """
        _email_all(category, title, body_html)


def notify_event(kind: str, ticket: str, title: str, detail: str = "", url: str = ""):
    """Step 2: non-failure pipeline updates (category "pr"). kind is a STEP_GUIDANCE key:
    pr_merged, pr_rejected, pr_conflict, pr_reminder, pr_update, ticket_waiting,
    ticket_released. Same channels as notify_failure (log + toast + optional email)."""
    detail = redact.redact(detail)
    if not NOTIFY_ENABLED:
        return
    category = "pr"
    guidance = STEP_GUIDANCE.get(kind, "")
    timestamp = _now()
    _append_log({"time": timestamp, "category": category, "kind": kind, "ticket": ticket,
                 "title": title, "detail": detail, "url": url, "guidance": guidance})
    print(f"  [notify:{kind}] {ticket} - {title}" + (f" | {detail}" if detail else ""))
    if TOAST_ENABLED:
        _show_toast(f"{ticket}: {title}", (detail or guidance)[:150])
    if EMAIL_ENABLED:
        link = f'<p><b>Link:</b> <a href="{url}">{url}</a></p>' if url else ""
        body_html = f"""
        <p><b>Ticket:</b> {ticket}</p>
        <p><b>Update:</b> {title}</p>
        <p><b>Time (UTC):</b> {timestamp}</p>
        <p><b>Detail:</b><br>{(detail or '-').replace(chr(10), '<br>')}</p>
        {link}
        <p><b>What to do next:</b><br>{guidance}</p>
        """
        _email_all(category, f"{ticket}: {title}", body_html)


if __name__ == "__main__":
    notify_failure("validate_fix.py", "TEST-0", "manual test of notify.py")
    notify_event("pr_reminder", "TEST-0", "PR #0 waiting 4.0 h for review", "manual test")
    print("Test notifications sent (check for toasts + logs/notifications.jsonl).")
    print("Current recipients.json entries:", _load_recipients())
