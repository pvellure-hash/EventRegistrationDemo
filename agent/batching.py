"""agent/batching.py - Step 3: fix related bugs together in ONE branch, ONE Copilot run, ONE PR.

When the watcher has claimed a ticket (the LEAD) and prepare_fix.py has localised it, this
module looks at the other ai-ready tickets and picks the ones that would change the SAME
files. Those become "members": they are claimed too, fixed in the same Copilot run, and
share the lead's PR. Everything here is deterministic - NO AI call and $0 until the single
batched Copilot run.

A ticket joins the lead's batch only if ALL of these hold:
  - BATCH_ENABLED=true, and both lead and member are an issue type in BATCH_KINDS (default Bug)
  - it is ai-ready, not ai-locked / ai-waiting, has the four headings, no injection text
  - no unresolved Jira "is blocked by" link
  - localisation is confident enough (same threshold as a single ticket)
  - its strong files share at least one file with the lead's files
  - its extra files (not in the lead's set) are not already changed by an open ticket PR
  - it has not already failed in a batch with this lead (logs/batch-failed.json)
  - the batch stays within BATCH_MAX_TICKETS (lead included, default 3)

Why the lead "owns" the run: every existing step (invoke_copilot, review_gate, validate_fix,
create_pr) is keyed on ONE ticket key and checks the branch starts with fix/<KEY>-. The batch
branch is fix/<LEAD>-batch-<member keys>, the prompt and reports use the lead key, so those
steps run unchanged. pr_tracker reads every key from the branch name, so merge/close/conflict
updates reach every member automatically.

If the batch run fails (agent, review gate, validation, PR), watch_queue calls
release_members(): members go back to ai-ready with a comment, and the pair is recorded so
they are retried ALONE next time instead of looping in the same batch.

.env: BATCH_ENABLED=true  BATCH_MAX_TICKETS=3  BATCH_KINDS=Bug
"""
import os
import json
import datetime

import jira_client as jc
import routing

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(AGENT_DIR, ".."))
FAILED_FILE = os.path.join(REPO_ROOT, "logs", "batch-failed.json")
ENABLED = os.getenv("BATCH_ENABLED", "true").strip().lower() == "true"
MAX_TICKETS = max(1, int(os.getenv("BATCH_MAX_TICKETS", "3") or 3))
KINDS = {k.strip().lower() for k in os.getenv("BATCH_KINDS", "Bug").split(",") if k.strip()}
TIER_ORDER = ["economy", "standard", "premium"]
SKIP_LABELS = {"ai-locked", "ai-waiting", "ai-needs-info", "ai-blocked", "ai-pr-created"}


# ---------------------------------------------------------------- pure helpers (tested)
def norm(p):
    return (p or "").replace("\\", "/").strip().lstrip("./").lower()


def issue_kind(fields):
    return ((fields or {}).get("issuetype") or {}).get("name", "") or ""


def is_batchable_kind(name):
    return (name or "").strip().lower() in KINDS


def bump_tier(tiers):
    """One tier above the highest member tier (one run does more work), capped at premium."""
    idx = max((TIER_ORDER.index(t) for t in tiers if t in TIER_ORDER), default=0)
    return TIER_ORDER[min(idx + 1, len(TIER_ORDER) - 1)]


def batch_branch(lead, member_keys):
    """fix/CLAUDE-20-batch-claude-21-claude-22 - still starts with fix/<LEAD>- (create_pr check)
    and pr_tracker.ticket_keys() finds every key in it."""
    return f"fix/{lead}-batch-" + "-".join(k.lower() for k in member_keys)


def select_members(lead_key, lead_files, candidates, excluded=(), max_total=None):
    """candidates: list of dicts {key, kind, files, ok, reason}. ok=False means it already failed
    a per-ticket check (reason says why). Returns (chosen, skipped{key: reason}). Pure."""
    max_total = max_total or MAX_TICKETS
    lead_set = {norm(f) for f in lead_files}
    chosen, skipped = [], {}
    for c in sorted(candidates, key=lambda c: _key_num(c["key"])):
        if c["key"] == lead_key:
            continue
        if not c.get("ok", True):
            skipped[c["key"]] = c.get("reason", "not eligible")
            continue
        if not is_batchable_kind(c.get("kind")):
            skipped[c["key"]] = f"issue type '{c.get('kind') or '?'}' not in BATCH_KINDS"
            continue
        if c["key"] in excluded:
            skipped[c["key"]] = f"already failed in a batch with {lead_key} - will run alone"
            continue
        shared = sorted(lead_set & {norm(f) for f in c.get("files", [])})
        if not shared:
            skipped[c["key"]] = "no shared files"
            continue
        if len(chosen) + 1 >= max_total:
            skipped[c["key"]] = f"batch full (BATCH_MAX_TICKETS={max_total}) - next cycle"
            continue
        chosen.append({**c, "shared": shared})
    return chosen, skipped


def _key_num(key):
    try:
        return int(key.rsplit("-", 1)[1])
    except Exception:
        return 0


# ---------------------------------------------------------------- failure memory
def _read_failed():
    try:
        with open(FAILED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def excluded_for(lead):
    return set(_read_failed().get(lead, []))


def record_failure(lead, member_keys):
    data = _read_failed()
    data[lead] = sorted(set(data.get(lead, [])) | set(member_keys))
    for m in member_keys:                         # symmetric: never re-pair either way
        data[m] = sorted(set(data.get(m, [])) | {lead})
    os.makedirs(os.path.dirname(FAILED_FILE), exist_ok=True)
    with open(FAILED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


# ---------------------------------------------------------------- Jira-facing steps
def gather_candidates(lead_key, localise_fn):
    """Reads the ai-ready queue and evaluates each other ticket WITHOUT any AI.
    localise_fn(summary, description) -> (confidence, complexity, plan, files)."""
    from read_queue import fetch_queue, assess, adf_to_text
    import git_sync
    out = []
    for issue in fetch_queue():
        key = issue["key"]
        if key == lead_key:
            continue
        try:
            f = jc.get_issue(key, fields="summary,description,labels,issuetype,issuelinks")["fields"]
        except Exception as e:
            out.append({"key": key, "ok": False, "reason": f"could not read: {e}"})
            continue
        labels = set(f.get("labels") or [])
        cand = {"key": key, "kind": issue_kind(f), "summary": f.get("summary", ""), "files": [],
                "ok": True, "reason": ""}
        if "ai-ready" not in labels or labels & SKIP_LABELS:
            cand.update(ok=False, reason="not ai-ready / already handled")
        else:
            description = adf_to_text(f.get("description"))
            missing, flagged = assess(description)
            blockers = git_sync.unresolved_blockers({"fields": f})
            if flagged:
                cand.update(ok=False, reason="flagged text - left for normal triage")
            elif missing:
                cand.update(ok=False, reason="missing headings - left for normal triage")
            elif blockers:
                cand.update(ok=False, reason=f"blocked by {', '.join(blockers)}")
            else:
                conf, complexity, plan, files = localise_fn(f.get("summary", ""), description)
                if plan.get("action") == "ask_for_info":
                    cand.update(ok=False, reason=f"low localisation confidence ({conf})")
                else:
                    cand.update(files=files, tier=plan.get("tier"), confidence=conf,
                                description=description)
        out.append(cand)
    return out


def drop_open_pr_overlaps(chosen, lead_files, branch):
    """A member's EXTRA files (outside the lead's set) must not be in an open ticket PR."""
    import git_sync
    import pr_tracker
    lead_set = {norm(f) for f in lead_files}
    kept, dropped = [], {}
    for m in chosen:
        extra = [f for f in m["files"] if norm(f) not in lead_set]
        hits = []
        if extra:
            try:
                hits = [h for h in git_sync.overlapping_open_prs(extra, exclude_head=branch)
                        if pr_tracker.KEY_RE.search(h["head"].upper())]
            except Exception as e:
                print(f"  [batch] WARNING: open-PR check failed for {m['key']} ({e}) - left out to be safe")
                dropped[m["key"]] = "open-PR check failed"
                continue
        if hits:
            dropped[m["key"]] = "extra files overlap open PR " + ", ".join(f"#{h['number']}" for h in hits)
        else:
            kept.append(m)
    return kept, dropped


def claim_members(lead, chosen):
    """Claims each member with the normal claim (lock, assign, In Progress, comment)."""
    import claim_ticket
    account_id = jc.my_account_id()
    claimed = []
    for m in chosen:
        if claim_ticket.claim(m["key"], account_id):
            jc.add_comment(m["key"], [
                f"AI agent: this ticket is being fixed together with {lead} in one change, "
                f"because both affect: {', '.join(m['shared'])}.",
                "One pull request will cover both tickets; it will be linked here for review."])
            claimed.append(m)
    return claimed


def release_members(lead, outcome):
    """Called by watch_queue when a batch run did not end with a PR. Returns released keys."""
    batch = routing.read_batch(lead)
    if not batch:
        return []
    released = []
    for m in batch["members"]:
        key = m["key"]
        try:
            labels = jc.get_issue(key, fields="labels")["fields"].get("labels", [])
            if "ai-pr-created" in labels or "ai-locked" not in labels:
                continue
            jc.update_labels(key, add=["ai-ready"], remove=["ai-locked"])
            jc.add_comment(key, [
                f"AI agent: the combined fix with {lead} did not complete ({outcome}).",
                "This ticket is back in the queue and will be fixed on its own next time. "
                "No code from the failed attempt reached main."])
            released.append(key)
        except Exception as e:
            print(f"  [batch] WARNING: could not release {key}: {e}")
    record_failure(lead, [m["key"] for m in batch["members"]])
    routing.clear_batch(lead)
    jc.audit("batch", lead, f"released {released} after {outcome}")
    return released
