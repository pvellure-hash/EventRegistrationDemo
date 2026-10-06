"""agent/routing.py - Phase 1: hand the model-routing decision from
prepare_fix.py to invoke_copilot.py.
Why a file and not an environment variable: watch_queue.py runs every step
as a SEPARATE subprocess. An os.environ value set inside prepare_fix.py
disappears when that process exits, so invoke_copilot.py (the next
subprocess) would never see it and would silently fall back to the default
model. Writing the decision to logs/routing/<KEY>.json is the simplest
reliable hand-off between steps, and it also leaves an audit record of
which tier/model each ticket was routed to.

STEP 3 (v7): the same hand-off is used for BATCHES. When prepare_fix.py fixes
several related bugs in one run, the lead ticket (lowest key) owns the run and
logs/routing/<LEAD>.batch.json lists the other tickets ("members"). create_pr.py,
update_jira.py and watch_queue.py read it so every member gets the PR link and
Jira update, and members are released again if the batch run fails.
"""
import os
import json
import datetime

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_AGENT_DIR, ".."))


def _dir():
    d = os.getenv("AGENT_LOG_DIR") or os.path.join(_REPO_ROOT, "logs")
    d = os.path.join(d, "routing")
    os.makedirs(d, exist_ok=True)
    return d


def _path(key):
    return os.path.join(_dir(), f"{key}.json")


def write_routing(key, tier, model, confidence, complexity):
    data = {"ticket": key, "tier": tier, "model": model,
            "confidence": confidence, "complexity": complexity,
            "decided_at": datetime.datetime.now().isoformat(timespec="seconds")}
    with open(_path(key), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return data


def read_routing(key):
    """Returns the routing dict for this ticket, or None if prepare_fix.py
    has not routed it (in which case invoke_copilot.py keeps its default)."""
    try:
        with open(_path(key), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- STEP 3: batches
def _batch_path(lead):
    return os.path.join(_dir(), f"{lead}.batch.json")


def write_batch(lead, members, tier, files, branch):
    """members: list of {"key", "summary", "files"} for the NON-lead tickets."""
    data = {"lead": lead, "members": members, "tier": tier, "files": files, "branch": branch,
            "decided_at": datetime.datetime.now().isoformat(timespec="seconds")}
    with open(_batch_path(lead), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return data


def read_batch(lead):
    """The batch this lead ticket owns, or None for a normal single-ticket run."""
    try:
        with open(_batch_path(lead), encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("members") else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def member_keys(lead):
    b = read_batch(lead)
    return [m["key"] for m in b["members"]] if b else []


def clear_batch(lead):
    try:
        os.remove(_batch_path(lead))
    except FileNotFoundError:
        pass
