"""agent/redact.py - Phase 0: scrub secrets and personal data before anything
is written to logs, events, or notifications.

Pure functions, no dependencies, safe to import from any step. Patterns
cover the same secret types validate_fix.py already scans diffs for, plus
common personal-data shapes. Redaction is deliberately aggressive: a false
positive costs a slightly less readable log line; a false negative leaks a
credential into an email.
"""
import re

_PATTERNS = [
    # --- credentials ---
    ("ATLASSIAN_TOKEN", re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}")),
    ("GITHUB_TOKEN",    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("SF_AUTH_URL",     re.compile(r"force://[^\s'\"]+")),
    ("SF_SESSION_ID",   re.compile(r"\b00D[A-Za-z0-9]{12,15}![A-Za-z0-9_.]{20,}")),
    ("PRIVATE_KEY",     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("AWS_KEY",         re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("BEARER",          re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
    ("BASIC_AUTH_URL",  re.compile(r"(?i)\b(https?://)[^\s:/@]+:[^\s@/]+@")),
    ("KEY_VALUE",       re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b(\s*[:=]\s*)(['\"]?)[^\s'\",;]{4,}")),
    # --- personal data ---
    ("EMAIL",           re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("CARD",            re.compile(r"\b(?:\d[ \-]?){13,16}\b")),
    ("SIN_SSN",         re.compile(r"\b\d{3}[- ]\d{3}[- ]\d{3}\b|\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE",           re.compile(r"(?<!\d)(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?!\d)")),
]


def redact(text, keep_emails=False):
    """Return text with secrets/PII replaced by [REDACTED:<TYPE>].
    Non-strings are returned unchanged. keep_emails=True leaves addresses
    intact (used only for notification routing headers, never bodies)."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for name, pat in _PATTERNS:
        if keep_emails and name == "EMAIL":
            continue
        if name == "KEY_VALUE":
            out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:SECRET]", out)
        elif name == "BASIC_AUTH_URL":
            out = pat.sub(lambda m: f"{m.group(1)}[REDACTED:CREDENTIALS]@", out)
        else:
            out = pat.sub(f"[REDACTED:{name}]", out)
    return out


def redact_obj(obj):
    """Recursively redact every string inside dicts/lists (for JSON events)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def contains_secret(text):
    """True if any credential pattern (not PII) matches - useful as a gate."""
    if not isinstance(text, str):
        return False
    return any(p.search(text) for n, p in _PATTERNS[:9])
