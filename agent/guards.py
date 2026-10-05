"""agent/guards.py - Phase 0: spending and concurrency guardrails.

  - Monthly budget circuit-breaker   (MONTHLY_BUDGET_USD)
  - Daily ticket cap                 (MAX_TICKETS_PER_DAY)
  - Per-ticket credit ceiling        (TICKET_CREDIT_CEILING)
  - Per-ticket run lock              (logs/locks/<KEY>.lock)

Each check returns (allowed: bool, reason: str) and never raises, so callers
stay simple. All limits are read from .env with conservative defaults.

About the per-ticket ceiling: Copilot CLI reports AI credits only at the END
of a run, so a ceiling cannot be enforced mid-run from credit data. The live
hard limit is COPILOT_CLI_TIMEOUT_MINUTES. The ceiling is enforced (a) BEFORE
every attempt, using credits already spent on this ticket, so retries and
manual re-runs cannot keep spending, and (b) AFTER a run, by flagging any
single run that overshot so it is visible and alerted.
"""
import os
import json
import time
import socket
import datetime

import events

MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "25"))
BUDGET_WARN_PCT = float(os.getenv("BUDGET_WARN_PCT", "80"))
MAX_TICKETS_PER_DAY = int(os.getenv("MAX_TICKETS_PER_DAY", "10"))
TICKET_CREDIT_CEILING = float(os.getenv("TICKET_CREDIT_CEILING", "60"))
LOCK_STALE_MINUTES = int(os.getenv("COPILOT_CLI_TIMEOUT_MINUTES", "15")) + 10


def check_budget(evs=None):
    spent = events.spend_this_month_usd(evs)
    pct = (spent / MONTHLY_BUDGET_USD * 100) if MONTHLY_BUDGET_USD > 0 else 100
    if spent >= MONTHLY_BUDGET_USD:
        return False, f"monthly budget reached: ${spent:.2f} of ${MONTHLY_BUDGET_USD:.2f}"
    if pct >= BUDGET_WARN_PCT:
        return True, f"WARN budget {pct:.0f}% used (${spent:.2f} of ${MONTHLY_BUDGET_USD:.2f})"
    return True, f"budget ok: ${spent:.2f} of ${MONTHLY_BUDGET_USD:.2f} ({pct:.0f}%)"


def check_daily_cap(evs=None):
    n = events.tickets_started_today(evs)
    if n >= MAX_TICKETS_PER_DAY:
        return False, f"daily ticket cap reached: {n} of {MAX_TICKETS_PER_DAY}"
    return True, f"daily cap ok: {n} of {MAX_TICKETS_PER_DAY}"


def check_ticket_ceiling(ticket, evs=None):
    spent = events.credits_for_ticket(ticket, evs)
    if spent >= TICKET_CREDIT_CEILING:
        return False, f"{ticket} already used {spent:.2f} credits (ceiling {TICKET_CREDIT_CEILING:.0f})"
    return True, f"{ticket} ceiling ok: {spent:.2f} of {TICKET_CREDIT_CEILING:.0f} credits"


# ---------------- run lock ----------------
def _lock_path(ticket):
    d = os.path.join(events.log_dir(), "locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{ticket}.lock")


def acquire_lock(ticket):
    """Atomic create (O_EXCL). A lock older than LOCK_STALE_MINUTES is treated
    as abandoned (crashed run) and replaced. Returns (acquired, reason)."""
    path = _lock_path(ticket)
    info = {"pid": os.getpid(), "host": socket.gethostname(),
            "since": datetime.datetime.now().isoformat(timespec="seconds"),
            "run_id": os.getenv("AGENT_RUN_ID", "")}
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(info, f)
            return True, "lock acquired"
        except FileExistsError:
            age_min = (time.time() - os.path.getmtime(path)) / 60
            if age_min > LOCK_STALE_MINUTES and attempt == 0:
                try:
                    os.remove(path)
                    continue
                except OSError:
                    pass
            try:
                with open(path, encoding="utf-8") as f:
                    holder = json.load(f)
            except Exception:
                holder = {}
            return False, (f"{ticket} is already being processed "
                           f"(pid {holder.get('pid','?')} on {holder.get('host','?')} since {holder.get('since','?')})")
        except Exception as e:
            return False, f"could not create lock: {e}"
    return False, "could not acquire lock"


def release_lock(ticket):
    try:
        os.remove(_lock_path(ticket))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [guards] WARNING: could not release lock for {ticket}: {e}")
