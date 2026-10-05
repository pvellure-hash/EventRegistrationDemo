"""agent/localizer.py - Phase 1: deterministic ticket localisation. No AI.

v2 (plain-language matching): a ticket written the way a real tester or
business user writes it - "email isn't validated on the registration form",
with NO class name, NO API name, NO __c suffix anywhere - now also matches,
via code_index's word_index (field LABELS + identifiers split into plain
words). This was the main gap flagged during Phase 1 testing: requiring a
reporter to already know Apex/field internals defeats the point of
automating defect triage. The system should read a ticket the way an
experienced developer on the team would, and infer the likely files from
ordinary language, the same way that developer would from memory of the
codebase - not require jargon it was never reasonable to expect.

Plain-word matches are scored lower per-word than an exact class/API/stack-
frame match (which remain the strongest signal when present), but multiple
distinct plain words corroborating the same file add up - e.g. a ticket
mentioning "email" AND "registration" AND "validate" pushes confidence
higher than any single generic word could alone. Purely generic words
(code_index.is_generic_word - "form", "controller", "record", etc.) are
capped so they can never carry a match on their own.
"""
import re
import code_index

API_NAME = re.compile(r"\b([A-Za-z][A-Za-z0-9]*__[cr])\b")
CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
FILE_LIKE = re.compile(r"\b([A-Za-z0-9_]+)\.(cls|trigger|js|cmp|page|flow)\b", re.IGNORECASE)
STACK_FRAME = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\.([a-zA-Z0-9_]+)\s*(?:\(|:|,)\s*line\s*(\d+)", re.IGNORECASE)
METHOD_CALL = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\.[a-zA-Z][A-Za-z0-9_]*\s*\(")
WORD_TOKEN = re.compile(r"[A-Za-z]{3,}")

# Plain English words that carry no signal about WHICH Salesforce feature
# a ticket is about (as opposed to code_index's GENERIC_WORDS, which are
# generic *identifier* fragments like "controller"/"service"). Filtering
# these out before word-matching avoids e.g. "is", "not", "the", "please"
# ever contributing to a file's score.
_ENGLISH_STOPWORDS = {
    "the", "and", "for", "are", "not", "but", "with", "this", "that", "from",
    "has", "have", "had", "was", "were", "been", "being", "will", "would",
    "should", "could", "can", "may", "might", "must", "shall", "does", "did",
    "please", "when", "what", "which", "who", "whom", "how", "why", "all",
    "any", "some", "its", "it's", "our", "your", "their", "user", "users",
    "issue", "bug", "error", "problem", "working", "broken", "fine", "fix",
    "fixed", "ticket", "functionality", "also", "then", "there", "here",
    "see", "need", "needs", "seems", "seem", "currently", "unable", "cannot",
    "shows", "showing", "shown", "allow", "allows", "allowed", "allowing",
}

SCORE_FILE_EXACT = 10
SCORE_CLASS_EXACT = 8
SCORE_STACK_FRAME = 9
SCORE_OBJECT_FIELD = 4
SCORE_DEPENDENT = 2
SCORE_WORD_SPECIFIC = 3      # a plain word that is NOT in code_index's generic list
SCORE_WORD_INHERITED = 1     # word only inherited from a field's parent object
                             # (e.g. "registration" on Email__c) - it says which
                             # feature AREA a file belongs to, not which file it is,
                             # so it must never let sibling fields tie with the
                             # field the ticket is actually about
TEST_CLASS_FACTOR = 0.5      # test classes are pulled in automatically alongside
                             # the class they test; they should not compete with
                             # real code for the top candidate slot
SCORE_WORD_GENERIC = 0.5     # a plain word that IS generic (e.g. "form", "controller")
SUMMARY_FACTOR = 2           # words in the ticket summary (first line) count double -
                             # the summary is where the reporter states the actual
                             # problem; the description adds context, often mentioning
                             # things that work fine ("guest count works correctly")
COMMON_WORD_SHARE = 0.5      # a word found in half or more of all indexed files (e.g.
                             # "event"/"registration" in an event-registration app)
                             # identifies the APP, not the file - score it as generic
MAX_WORD_SCORE_CONTRIBUTION = 9  # cap so word-matching alone can approach, but not
                                  # trivially exceed, a single exact identifier match


def extract_signals(text):
    text = text or ""
    return {
        "files": sorted({m.group(1) for m in FILE_LIKE.finditer(text)}),
        "stack_frames": sorted({m.group(1) for m in STACK_FRAME.finditer(text)}),
        "object_fields": sorted({m.group(1) for m in API_NAME.finditer(text)}),
        "camel_tokens": sorted({m.group(1) for m in CAMEL.finditer(text)}),
        "method_calls": sorted({m.group(1) for m in METHOD_CALL.finditer(text)}),
        "plain_words": sorted({w.lower() for w in WORD_TOKEN.findall(text)} - _ENGLISH_STOPWORDS),
    }


def localize(index, ticket_text, max_candidates=8):
    signals = extract_signals(ticket_text)
    if not index:
        return {"candidates": [], "confidence": 0.0, "signals": signals, "reason": "no code index available"}

    scores = {}

    def add(rel_path, api_name, points, reason):
        e = scores.setdefault(rel_path, {"score": 0.0, "api_name": api_name, "reasons": set(), "word_hits": 0})
        e["score"] += points
        e["reasons"].add(reason)

    # ---- strong signals: exact identifiers, stack frames, file mentions ----
    for frame_class, method, line in STACK_FRAME.findall(ticket_text or ""):
        for rel in code_index.find_symbol(index, frame_class):
            add(rel, frame_class, SCORE_STACK_FRAME, f"stack frame {frame_class}.{method}:{line}")
    for fname in signals["files"]:
        for rel in code_index.find_symbol(index, fname):
            add(rel, fname, SCORE_FILE_EXACT, f"file mentioned: {fname}")
    for cls in signals["method_calls"]:
        for rel in code_index.find_symbol(index, cls):
            add(rel, cls, SCORE_CLASS_EXACT, f"method call on {cls}")
    for tok in signals["camel_tokens"]:
        for rel in code_index.find_symbol(index, tok):
            add(rel, tok, SCORE_CLASS_EXACT, f"identifier mentioned: {tok}")
    for of in signals["object_fields"]:
        for rel in code_index.find_symbol(index, of):
            add(rel, of, SCORE_OBJECT_FIELD, f"object/field mentioned: {of}")
        for rel in code_index.dependents_of(index, of):
            add(rel, index["files"][rel]["api_name"], SCORE_DEPENDENT, f"touches {of}")

    # ---- plain-language signal: match ordinary words against field LABELS
    # and identifiers split into words (code_index.word_index) - this is
    # what lets a ticket like "email isn't validated on the registration
    # form" match EventRegistrationController and the Email field, with
    # NO class name, API name, or __c suffix present anywhere in the text.
    word_contribution = {}  # rel_path -> accumulated word score (for capping)
    lines = [l for l in (ticket_text or "").splitlines() if l.strip()]
    summary_words = ({w.lower() for w in WORD_TOKEN.findall(lines[0])} - _ENGLISH_STOPWORDS) if lines else set()
    n_files = max(1, index.get("file_count") or len(index.get("files", {})))
    for word in signals["plain_words"]:
        hits = code_index.find_by_word(index, word)
        if not hits:
            continue
        is_common = n_files >= 4 and len(set(hits)) / n_files >= COMMON_WORD_SHARE
        is_generic = code_index.is_generic_word(index, word) or is_common
        base_points = SCORE_WORD_GENERIC if is_generic else SCORE_WORD_SPECIFIC
        if word in summary_words and not is_generic:
            base_points *= SUMMARY_FACTOR
        for rel in hits:
            entry = index["files"].get(rel, {})
            api_name = entry.get("api_name", rel)
            own = set(entry.get("words_own", entry.get("words", [])))
            singular = word[:-1] if word.endswith("s") else word
            inherited = word not in own and singular not in own and (word + "s") not in own
            points = min(base_points, SCORE_WORD_INHERITED) if inherited else base_points
            if inherited and is_generic:
                points = min(points, SCORE_WORD_GENERIC)
            if entry.get("is_test"):
                points *= TEST_CLASS_FACTOR
            prior = word_contribution.get(rel, 0.0)
            if prior >= MAX_WORD_SCORE_CONTRIBUTION:
                continue  # cap reached for this file - ignore further word points
            award = min(points, MAX_WORD_SCORE_CONTRIBUTION - prior)
            word_contribution[rel] = prior + award
            tag = (" (common)" if is_common else " (generic)") if is_generic else \
                  (" (area)" if inherited else (" (summary)" if word in summary_words else ""))
            add(rel, api_name, award, f"mentions '{word}'" + tag)
            scores[rel]["word_hits"] += 1

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)[:max_candidates]
    candidates = [{"path": rel, "api_name": v["api_name"], "score": round(v["score"], 1),
                   "reasons": sorted(v["reasons"]), "word_hits": v["word_hits"]} for rel, v in ranked]

    total_signals = (sum(len(v) for k, v in signals.items() if k != "plain_words")
                     + len(signals["plain_words"]))
    if not candidates or total_signals == 0:
        confidence = 0.0
    else:
        top = candidates[0]["score"]
        # Files that are directly connected to the top candidate (a field and
        # the Apex that uses it, a class and its test) are not competitors -
        # they go into the same context pack. Only an UNRELATED file scoring
        # close to the top one means the ticket is genuinely ambiguous.
        top_c = candidates[0]
        def _object_folder(path):
            parts = path.replace("\\", "/").split("/")
            return parts[parts.index("objects") + 1] if "objects" in parts[:-1] else None

        def _related(c):
            # a field and its own parent object describe the same thing
            kinds = {index["files"].get(c["path"], {}).get("kind"),
                     index["files"].get(top_c["path"], {}).get("kind")}
            if "object" in kinds and _object_folder(c["path"]) and \
                    _object_folder(c["path"]) == _object_folder(top_c["path"]):
                return True
            if c["path"] in code_index.dependents_of(index, top_c["api_name"]):
                return True
            if top_c["path"] in code_index.dependents_of(index, c["api_name"]):
                return True
            return (c["path"] in code_index.tests_for(index, top_c["api_name"]) or
                    top_c["path"] in code_index.tests_for(index, c["api_name"]))
        unrelated = [c["score"] for c in candidates[1:] if not _related(c)]
        second = max(unrelated) if unrelated else 0
        separation = 1.0 if top == 0 else max(0.0, (top - second) / top)
        # corroboration now also counts distinct plain-word hits, not just
        # named reasons, so "email" + "registration" + "validate" all
        # matching the SAME file corroborates it even with zero exact
        # identifier matches present.
        n_reasons = len(candidates[0]["reasons"])
        corroboration = min(1.0, n_reasons / 3.0 if candidates[0]["word_hits"] >= 2 else n_reasons / 2.0)
        signal_strength = min(1.0, top / (SCORE_STACK_FRAME + SCORE_FILE_EXACT))
        confidence = round(0.40 * separation + 0.25 * corroboration + 0.35 * signal_strength, 2)
        # A ticket whose best match comes only from generic words ("form",
        # "controller") or feature-area words has no real signal about WHICH
        # file to change - it must never be confident enough to spend AI.
        if top < SCORE_WORD_SPECIFIC:
            confidence = min(confidence, 0.2)

    return {"candidates": candidates, "confidence": confidence, "signals": signals}


def expand_with_dependencies(index, candidates, include_tests=True, max_files=15):
    out, seen = [], set()
    # Only expand from STRONG candidates (>= half the top score). Weak matches
    # (a sibling field mentioned in passing) would otherwise drag their whole
    # dependency tree into the prompt and inflate cost for no benefit.
    top = max((c.get("score", 0) for c in candidates), default=0)
    candidates = [c for c in candidates if top and c.get("score", 0) >= 0.5 * top]
    for c in candidates:
        if c["path"] not in seen:
            out.append(c["path"]); seen.add(c["path"])
    for c in candidates:
        entry = index.get("files", {}).get(c["path"])
        if not entry:
            continue
        # A matched FIELD or OBJECT has no code of its own - the fix lives in
        # the Apex/LWC that uses it. Pull in every file that references it
        # (e.g. Email__c -> EventRegistrationController + its test), so the
        # context pack contains the code that actually needs changing.
        if entry.get("kind") in ("field", "object"):
            for rel in code_index.dependents_of(index, entry.get("api_name", "")):
                if rel not in seen and len(out) < max_files:
                    out.append(rel); seen.add(rel)
        for cls in entry.get("edges", {}).get("classes", []):
            for rel in code_index.find_symbol(index, cls):
                if rel not in seen and len(out) < max_files:
                    out.append(rel); seen.add(rel)
        if include_tests:
            for rel in code_index.tests_for(index, c["api_name"]):
                if rel not in seen and len(out) < max_files:
                    out.append(rel); seen.add(rel)
    return out[:max_files]
