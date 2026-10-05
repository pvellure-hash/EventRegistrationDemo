"""agent/routing.py - Phase 1: hand the model-routing decision from
prepare_fix.py to invoke_copilot.py.

Why a file and not an environment variable: watch_queue.py runs every step
as a SEPARATE subprocess. An os.environ value set inside prepare_fix.py
disappears when that process exits, so invoke_copilot.py (the next
subprocess) would never see it and would silently fall back to the default
model. Writing the decision to logs/routing/<KEY>.json is the simplest
reliable hand-off between steps, and it also leaves an audit record of
which tier/model each ticket was routed to.
"""
import os
import json
import datetime

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_AGENT_DIR, ".."))


def _path(key):
    d = os.getenv("AGENT_LOG_DIR") or os.path.join(_REPO_ROOT, "logs")
    d = os.path.join(d, "routing")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.json")


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
