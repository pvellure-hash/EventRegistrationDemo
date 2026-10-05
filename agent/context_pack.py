"""agent/context_pack.py - Phase 1: scoped context pack builder.

Turns a ranked file list (from localizer.py) into:
  1. A context pack markdown file the agent's prompt can include directly,
     so it does not need to grep/glob to find these files itself.
  2. A sparse-checkout path list, so the agent's workspace physically only
     contains these files (cost control AND security control - see
     Blueprint v4 Section 5.2 "scoped workspace").

Token budgeting is approximate (chars / 4) - good enough to decide whether
to include a file in full, truncated, or as a one-line summary only.
"""
import os

CHARS_PER_TOKEN = 4  # rough heuristic, consistent across this module


def estimate_tokens(text):
    return max(1, len(text) // CHARS_PER_TOKEN)


def build(repo_root, index, file_paths, ticket_text, max_tokens=12000):
    """Reads each file (in priority order) and includes as much as the
    token budget allows: full content while budget remains, then a
    one-line summary (from the index, if present) for the rest.
    Returns (markdown_str, included_paths, summarized_paths, total_tokens)."""
    parts = [f"# Context pack for this defect\n",
             "The files below were selected automatically because they match "
             "identifiers, object/field names, or stack frames mentioned in the "
             "ticket. Start here before searching further.\n"]
    budget = max_tokens
    included, summarized = [], []

    for rel in file_paths:
        abspath = os.path.join(repo_root, rel)
        if not os.path.exists(abspath):
            continue
        try:
            with open(abspath, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        tok = estimate_tokens(content)
        entry = index.get("files", {}).get(rel, {}) if index else {}
        if tok <= budget:
            parts.append(f"\n## `{rel}`\n```\n{content}\n```\n")
            budget -= tok
            included.append(rel)
        else:
            label = entry.get("api_name", os.path.basename(rel))
            parts.append(f"\n## `{rel}` (not included in full - token budget reached)\n"
                         f"Symbol: `{label}`. Open this file directly if the fix needs it.\n")
            summarized.append(rel)

    md = "".join(parts)
    return md, included, summarized, estimate_tokens(md)


def sparse_checkout_paths(file_paths, always_include=("force-app/main/default/classes/*.cls-meta.xml",)):
    """Paths suitable for `git sparse-checkout set` - directories of each
    included file, so related meta files resolve too, plus any defaults."""
    dirs = sorted({os.path.dirname(p) for p in file_paths if p})
    return dirs + list(always_include)
