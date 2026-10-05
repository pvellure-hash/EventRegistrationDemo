"""agent/events.py - Phase 0: the run-event store.

One append-only JSON line per pipeline step in logs/run-events.jsonl. This
replaces "one cost line per ticket" with a record of every step, so cost per
delivered fix, wasted spend, funnel drop-off and durations can all be derived
later (Phase 2 dashboard) from a single source of truth.

Design rules:
  - emit_event() NEVER raises. Telemetry must not be able to fail a ticket.
  - Every event is redacted before it is written (redact.redact_obj).
  - A run_id ties all steps for one ticket attempt together. watch_queue.py
    creates it and passes it to child steps via the AGENT_RUN_ID env var, so
    no other script needs a new command-line argument.
  - Writes are single os.write() calls of one line on an O_APPEND handle, so
    concurrent writers do not interleave lines.

Override the log folder with AGENT_LOG_DIR (used by tests).
"""
import os
import json
import uuid
import socket
import datetime

import redact

SCHEMA_VERSION = 1
PIPELINE_VERSION = "4.0-phase0"
CREDIT_TO_USD = float(os.getenv("CREDIT_TO_USD", "0.01"))

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_AGENT_DIR, ".."))


def log_dir():
    d = os.getenv("AGENT_LOG_DIR") or os.path.join(_REPO_ROOT, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def events_path():
    return os.path.join(log_dir(), "run-events.jsonl")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id(ticket):
    return f"{ticket}-{datetime.datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"


def current_run_id(ticket):
    """Run id from the watcher if present, else a fresh one (manual runs)."""
    rid = os.getenv("AGENT_RUN_ID", "").strip()
    if not rid or not rid.startswith(ticket):
        rid = new_run_id(ticket)
        os.environ["AGENT_RUN_ID"] = rid
    return rid


def emit_event(ticket, step, status, **fields):
    """Append one event. status: started | ok | failed | blocked | skipped.
    Extra fields (all optional): duration_s, actor, model, tier, attempt_no,
    ai_credits, input_tokens_fresh, input_tokens_cached, output_tokens,
    tool_calls, files_changed, lines_changed, outcome, reason_code, detail."""
    try:
        ev = {
            "schema": SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "time": now_iso(),
            "run_id": current_run_id(ticket) if ticket and ticket != "-" else os.getenv("AGENT_RUN_ID", "-"),
            "ticket": ticket,
            "project": os.getenv("AGENT_PROJECT_NAME") or os.path.basename(_REPO_ROOT),
            "environment": os.getenv("AGENT_ENVIRONMENT", "local"),
            "host": socket.gethostname(),
            "pipeline_version": PIPELINE_VERSION,
            "step": step,
            "status": status,
            "actor": fields.pop("actor", "system"),
        }
        if fields.get("ai_credits") is not None:
            fields.setdefault("est_cost_usd", round(fields["ai_credits"] * CREDIT_TO_USD, 4))
        ev.update(fields)
        ev = redact.redact_obj(ev)
        ev["redacted"] = True
        line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(events_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return ev
    except Exception as e:  # never break the pipeline for telemetry
        print(f"  [events] WARNING: could not write event ({step}/{status}): {e}")
        return None


def read_events():
    path = events_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def _parse_time(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def credits_for_ticket(ticket, events=None):
    evs = read_events() if events is None else events
    return sum(e.get("ai_credits") or 0 for e in evs if e.get("ticket") == ticket)


def spend_this_month_usd(events=None, now=None):
    now = now or datetime.datetime.now().astimezone()
    evs = read_events() if events is None else events
    total = 0.0
    for e in evs:
        t = _parse_time(e.get("time", ""))
        if t and t.year == now.year and t.month == now.month:
            total += (e.get("ai_credits") or 0) * CREDIT_TO_USD
    return round(total, 4)


def tickets_started_today(events=None, now=None):
    now = now or datetime.datetime.now().astimezone()
    evs = read_events() if events is None else events
    seen = set()
    for e in evs:
        t = _parse_time(e.get("time", ""))
        if e.get("step") == "run" and e.get("status") == "started" and t and t.date() == now.date():
            seen.add(e.get("ticket"))
    return len(seen)
