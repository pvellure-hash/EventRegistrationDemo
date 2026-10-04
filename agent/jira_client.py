"""Shared Jira helpers for the agent. Never prints or logs tokens."""
import os
import json
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("JIRA_BASE_URL")
AUTH = (os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN"))
PROJECT = os.getenv("JIRA_PROJECT_KEY")
ENABLED = os.getenv("AGENT_ENABLED", "false").strip().lower() == "true"
DRY_RUN = os.getenv("AGENT_DRY_RUN", "true").strip().lower() != "false"
STATUS_IN_PROGRESS = os.getenv("JIRA_STATUS_IN_PROGRESS", "In Progress")
STATUS_IN_REVIEW = os.getenv("JIRA_STATUS_IN_REVIEW", "In Review")

LOG_FILE = os.path.join("logs", "agent-audit.jsonl")
TIMEOUT = 30


class JiraError(Exception):
    pass


def audit(action, key, detail="", result="ok"):
    """Append one line to the audit log (JSON lines format)."""
    os.makedirs("logs", exist_ok=True)
    entry = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "ticket": key,
        "dry_run": DRY_RUN,
        "result": result,
        "detail": detail,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _request(method, path, **kwargs):
    r = requests.request(method, BASE + path, auth=AUTH, timeout=TIMEOUT, **kwargs)
    if r.status_code >= 400:
        raise JiraError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


def my_account_id():
    return _request("GET", "/rest/api/3/myself")["accountId"]


def get_issue(key, fields="summary,status,labels,assignee,description"):
    return _request("GET", f"/rest/api/3/issue/{key}", params={"fields": fields})


def adf_paragraphs(lines):
    """Build a Jira rich-text (ADF) comment from a list of plain-text lines."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in lines if line
        ],
    }


# ---------- Write actions: all respect DRY_RUN ----------

def _write(action, key, detail, func):
    if DRY_RUN:
        print(f"  [dry-run] would {action}: {detail}")
        audit(action, key, detail, result="dry-run")
        return
    func()
    print(f"  done: {action}: {detail}")
    audit(action, key, detail)


def update_labels(key, add=(), remove=()):
    ops = [{"add": l} for l in add] + [{"remove": l} for l in remove]
    detail = f"add={list(add)} remove={list(remove)}"
    _write("update labels", key, detail, lambda: _request(
        "PUT", f"/rest/api/3/issue/{key}", json={"update": {"labels": ops}}))


def assign(key, account_id):
    _write("assign", key, "agent user", lambda: _request(
        "PUT", f"/rest/api/3/issue/{key}/assignee", json={"accountId": account_id}))


def add_comment(key, lines):
    _write("comment", key, lines[0] if lines else "", lambda: _request(
        "POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf_paragraphs(lines)}))


def transition(key, target_name):
    data = _request("GET", f"/rest/api/3/issue/{key}/transitions")
    options = data.get("transitions", [])
    match = next((t for t in options
                  if t["to"]["name"].lower() == target_name.lower()
                  or t["name"].lower() == target_name.lower()), None)
    if not match:
        names = ", ".join(sorted({t["to"]["name"] for t in options})) or "none"
        print(f"  WARNING: no transition to '{target_name}'. Available: {names}")
        audit("transition", key, f"'{target_name}' not available ({names})", "skipped")
        return False
    _write("transition", key, f"-> {match['to']['name']}", lambda: _request(
        "POST", f"/rest/api/3/issue/{key}/transitions",
        json={"transition": {"id": match["id"]}}))
    return True