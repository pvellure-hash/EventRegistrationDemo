"""agent/injection_scan.py - Phase 1: prompt-injection scanning.

Phase 0 already scans ticket TEXT (see prepare_fix.py / preflight.py in the
existing pipeline). This module extends that to CODE the agent is about to
read as part of its context pack - closing the "planted instruction in a
code comment" gap from Blueprint v4 Section 5.1/5.2: a prior, legitimate-
looking change could leave behind a comment that tries to influence a
LATER ticket's agent session when it reads that file.

Detection is phrase/pattern based (same philosophy as the existing ticket
scanner) - not a classifier. It is intentionally broad: a false positive
means a file is flagged for a human glance; a false negative means an
injection reaches the model.
"""
import re

_PHRASES = [
    r"ignore (all |your )?(previous|above|prior) instructions",
    r"disregard (all |your )?(previous|above|prior) (instructions|rules)",
    r"you are now",
    r"new instructions?:",
    r"system prompt",
    r"do not (tell|inform|notify) (the )?(user|developer|reviewer)",
    r"act as (if|an?) ",
    r"\bAI[,:]? (please|must|should) (run|execute|push|deploy|merge)",
    r"push (this |these )?(changes?|commits?) (directly|straight) to (main|master|production)",
    r"skip (the )?(review|validation|tests?|approval)",
    r"exfiltrat",
    r"send (this|the) (data|credentials?|secrets?) to",
    r"curl\s+https?://(?!.*salesforce|.*github)",
    r"base64\s*-d",
    r"eval\s*\(",
]
_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _PHRASES]

# Flag content hidden where a human reviewer is unlikely to look.
_SUSPICIOUS_STRUCTURE = [
    (re.compile(r"/\*[\s\S]{0,400}?(ignore|disregard|new instructions?|AI[,:])"
                r"[\s\S]{0,400}?\*/", re.IGNORECASE), "suspicious block comment"),
    (re.compile(r"[\u200b\u200c\u200d\ufeff]"), "zero-width/invisible unicode character"),
]


def scan_text(text, source_label=""):
    """Returns a list of finding dicts: {"pattern","match","source"}."""
    if not text:
        return []
    findings = []
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            findings.append({"pattern": pat.pattern, "match": m.group(0)[:120], "source": source_label})
    for pat, label in _SUSPICIOUS_STRUCTURE:
        m = pat.search(text)
        if m:
            findings.append({"pattern": label, "match": m.group(0)[:120], "source": source_label})
    return findings


def scan_context_pack(file_contents):
    """file_contents: dict of rel_path -> text (what's about to be sent to
    the model). Returns {"clean": bool, "findings": [...]}."""
    all_findings = []
    for path, text in file_contents.items():
        all_findings.extend(scan_text(text, source_label=path))
    return {"clean": not all_findings, "findings": all_findings}
