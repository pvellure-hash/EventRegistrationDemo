"""agent/code_index.py - Phase 1: Code Intelligence Index.

Builds a symbol table and a dependency graph for a Salesforce DX repo
WITHOUT any AI call - pure parsing.

v2 (plain-language matching): in addition to exact API names, every file
now also gets a "word index" entry - the field LABEL (not just its API
name), and the class/trigger/component name split into plain words
(CamelCase -> individual words, e.g. EventRegistrationController ->
"event", "registration", "controller"). This lets a ticket written in
ordinary business language ("email isn't validated on the registration
form") match real files, without requiring the reporter to know any Apex
class name or API name - matching how an experienced developer on the team
would read the same ticket and already know where to look.
"""
import os
import re
import json
import hashlib
import datetime

INDEX_SCHEMA = 2
DEFAULT_PKG = "force-app/main/default"

CLASS_REF = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")
APEX_IMPORT = re.compile(r"@salesforce/apex/([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)")
OBJECT_FIELD_REF = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__c)\b")
SOBJECT_REF = re.compile(r"\bfrom\s+([A-Za-z][A-Za-z0-9_]*(?:__c)?)\b", re.IGNORECASE)
TRIGGER_DECL = re.compile(r"trigger\s+\w+\s+on\s+([A-Za-z0-9_]+)", re.IGNORECASE)
CLASS_DECL = re.compile(r"\bclass\s+([A-Za-z0-9_]+)", re.IGNORECASE)
ISTEST = re.compile(r"@IsTest", re.IGNORECASE)
TEST_TARGET_FROM_NAME = re.compile(r"^(.*?)(?:_?[Tt]est)$")
API_NAME_FROM_META = re.compile(r"<fullName>([^<]+)</fullName>")
LABEL_FROM_META = re.compile(r"<label>([^<]+)</label>")

_STOPWORDS = {
    "String", "Integer", "Boolean", "List", "Map", "Set", "Object", "Id",
    "Decimal", "Double", "Long", "Date", "DateTime", "Time", "Blob", "Void",
    "Public", "Private", "Protected", "Global", "Static", "Final", "Class",
    "Trigger", "Override", "Virtual", "Abstract", "Interface", "Enum",
    "Return", "If", "Else", "For", "While", "Try", "Catch", "Finally",
    "Throw", "New", "Null", "True", "False", "This", "Super", "Test",
    "System", "Database", "Schema", "SObject", "WithSharing",
    "WithoutSharing", "InheritedSharing", "Insert", "Update", "Delete",
    "Upsert", "Select", "From", "Where",
}

# Common words that appear in almost every class/field name in a Salesforce
# project (e.g. "Controller", "Service", "Record") - these are REAL words,
# unlike _STOPWORDS, but they are so generic that matching on them alone
# should never meaningfully raise confidence. Kept separate from
# _STOPWORDS so they still appear in the word index (useful alongside a
# more specific word), just scored at a steep discount on their own.
_GENERIC_WORDS = {
    "controller", "service", "helper", "handler", "trigger", "test",
    "record", "object", "field", "form", "component", "class", "util",
    "utility", "manager", "wrapper", "impl", "base", "custom", "app",
    "application", "data", "info", "item", "detail", "details", "item",
    "value", "status", "type", "name",
}

_WORD_SPLIT = re.compile(r"[^a-zA-Z0-9]+")


def _hash(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def split_words(identifier):
    """CamelCase / snake_case / __c suffix -> lowercase plain words.
    'EventRegistrationController' -> ['event','registration','controller']
    'Number_of_Guests__c' -> ['number','of','guests']
    'eventRegistrationForm' -> ['event','registration','form']"""
    if not identifier:
        return []
    name = identifier[:-3] if identifier.endswith("__c") else identifier
    name = name[:-3] if name.endswith("__r") else name
    # insert boundaries: lower->Upper, letter->digit, acronym->Word
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    name = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", name)
    words = [w.lower() for w in _WORD_SPLIT.split(name) if w]
    return [w for w in words if len(w) > 1]


def _iter_files(pkg_dir):
    specs = [
        ("class", os.path.join(pkg_dir, "classes"), (".cls",)),
        ("trigger", os.path.join(pkg_dir, "triggers"), (".trigger",)),
        ("lwc", os.path.join(pkg_dir, "lwc"), (".js",)),
        ("aura", os.path.join(pkg_dir, "aura"), (".cmp",)),
        ("object", os.path.join(pkg_dir, "objects"), (".object-meta.xml",)),
        ("field", os.path.join(pkg_dir, "objects"), (".field-meta.xml",)),
    ]
    for kind, base, exts in specs:
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for fn in files:
                if fn.endswith(exts):
                    yield kind, os.path.join(root, fn)


def _api_name(kind, path, text):
    if kind in ("object", "field"):
        m = API_NAME_FROM_META.search(text)
        if m:
            return m.group(1)
        if kind == "object":
            return os.path.basename(os.path.dirname(path))
        return os.path.splitext(os.path.basename(path))[0]
    if kind in ("lwc", "aura"):
        return os.path.basename(os.path.dirname(path))
    m = CLASS_DECL.search(text) or TRIGGER_DECL.search(text)
    return m.group(1) if m else os.path.splitext(os.path.basename(path))[0]


def _label(kind, text):
    """Human-readable label, if the metadata defines one (fields/objects
    always should via <label>; classes/triggers never do - None for those)."""
    if kind in ("object", "field"):
        m = LABEL_FROM_META.search(text)
        if m:
            return m.group(1)
    return None


def _parse_edges(kind, text):
    edges = {"classes": set(), "objects_fields": set(), "apex_methods": set()}
    if kind in ("class", "trigger"):
        for m in CLASS_REF.finditer(text):
            name = m.group(1)
            if name not in _STOPWORDS and len(name) > 2:
                edges["classes"].add(name)
        for m in OBJECT_FIELD_REF.finditer(text):
            edges["objects_fields"].add(m.group(1))
        for m in SOBJECT_REF.finditer(text):
            edges["objects_fields"].add(m.group(1))
    elif kind in ("lwc", "aura"):
        for m in APEX_IMPORT.finditer(text):
            edges["classes"].add(m.group(1))
            edges["apex_methods"].add(f"{m.group(1)}.{m.group(2)}")
    return {k: sorted(v) for k, v in edges.items()}


def _is_test(kind, text):
    return kind == "class" and bool(ISTEST.search(text))


def _test_target_guess(api_name):
    m = TEST_TARGET_FROM_NAME.match(api_name)
    return m.group(1) if m and m.group(1) != api_name else None


def build_or_update(repo_root, pkg_dir=None, force=False, changed_paths=None):
    pkg_dir_rel = pkg_dir or DEFAULT_PKG
    pkg_abs = os.path.join(repo_root, pkg_dir_rel)
    idx_path = os.path.join(repo_root, "logs", "code-index.json")
    existing = {}
    if not force and os.path.exists(idx_path):
        try:
            with open(idx_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    files_idx = {} if force else dict(existing.get("files", {}))
    changed_set = set(os.path.normpath(p) for p in changed_paths) if changed_paths is not None else None

    seen_paths = set()
    reparsed = 0
    for kind, abspath in _iter_files(pkg_abs):
        rel = os.path.normpath(os.path.relpath(abspath, repo_root))
        seen_paths.add(rel)
        if changed_set is not None and rel not in changed_set and rel in files_idx:
            continue
        try:
            with open(abspath, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        h = _hash(text)
        if rel in files_idx and files_idx[rel].get("hash") == h:
            continue
        api = _api_name(kind, abspath, text)
        label = _label(kind, text)
        words_own = set(split_words(api))
        if label:
            words_own |= set(split_words(label))
        entry = {
            "kind": kind, "path": rel, "api_name": api, "label": label, "hash": h,
            "lines": text.count("\n") + 1,
            "is_test": _is_test(kind, text),
            "test_target_guess": _test_target_guess(api) if _is_test(kind, text) else None,
            "edges": _parse_edges(kind, text),
            "words_own": sorted(words_own),
            "indexed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        files_idx[rel] = entry
        reparsed += 1

    for rel in list(files_idx):
        if rel not in seen_paths:
            del files_idx[rel]

    # Build a lookup of each custom object's own words, keyed by its folder
    # name (e.g. "Event_Registration__c"), so fields under that object can
    # inherit them. Recomputed on every build (cheap, pure set operations)
    # so it never drifts even for files that were skipped as unchanged.
    object_words_by_folder = {}
    for rel, e in files_idx.items():
        if e["kind"] == "object":
            folder = os.path.basename(os.path.dirname(rel))
            object_words_by_folder[folder] = set(e.get("words_own", []))

    for rel, e in files_idx.items():
        own = set(e.get("words_own", []))
        if e["kind"] == "field":
            # .../objects/<ObjectFolder>/fields/<Field>.field-meta.xml
            parts = rel.replace("\\", "/").split("/")
            folder = None
            if "fields" in parts:
                fi = parts.index("fields")
                if fi >= 1:
                    folder = parts[fi - 1]
            parent_words = object_words_by_folder.get(folder, set())
            e["words"] = sorted(own | parent_words)
        else:
            e["words"] = sorted(own)

    symbols = {}
    word_index = {}  # word -> list of rel paths (reverse index for fast lookup)
    for rel, e in files_idx.items():
        symbols.setdefault(e["api_name"], []).append(rel)
        for w in e.get("words", []):
            word_index.setdefault(w, []).append(rel)

    index = {
        "schema": INDEX_SCHEMA,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "package_dir": pkg_dir_rel,
        "file_count": len(files_idx),
        "reparsed_this_run": reparsed,
        "files": files_idx,
        "symbols": symbols,
        "word_index": word_index,
        "generic_words": sorted(_GENERIC_WORDS),
    }
    os.makedirs(os.path.dirname(idx_path), exist_ok=True)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    return index


def load(repo_root):
    idx_path = os.path.join(repo_root, "logs", "code-index.json")
    if not os.path.exists(idx_path):
        return None
    with open(idx_path, encoding="utf-8") as f:
        return json.load(f)


def find_symbol(index, name):
    if not index:
        return []
    if name in index["symbols"]:
        return list(index["symbols"][name])
    low = name.lower()
    for sym, paths in index["symbols"].items():
        if sym.lower() == low:
            return list(paths)
    hits = []
    for sym, paths in index["symbols"].items():
        if low in sym.lower() or sym.lower() in low:
            hits.extend(paths)
    return hits


def find_by_word(index, word):
    """Plain-language lookup: returns rel paths whose class/field name or
    field LABEL contains this word, e.g. find_by_word(idx, 'email') finds
    EventRegistrationController (mentions 'email' in code) AND the
    Email__c field (label 'Email Address' contains 'email').

    Also tries a simple plural/singular variant (word with/without a
    trailing 's') so a ticket saying "registrations" still matches an
    indexed word "registration", without needing a full stemmer."""
    if not index:
        return []
    word_index = index.get("word_index", {})
    low = word.lower()
    hits = list(word_index.get(low, []))
    if hits:
        return hits
    variant = low[:-1] if low.endswith("s") and len(low) > 3 else low + "s"
    return list(word_index.get(variant, []))


def is_generic_word(index, word):
    generic = set(index.get("generic_words", [])) if index else _GENERIC_WORDS
    return word.lower() in generic


def dependents_of(index, api_name, max_hits=25):
    out = []
    for rel, e in index.get("files", {}).items():
        edges = e.get("edges", {})
        if (api_name in edges.get("classes", []) or
                api_name in edges.get("objects_fields", [])):
            out.append(rel)
            if len(out) >= max_hits:
                break
    return out


def tests_for(index, api_name):
    out = []
    for rel, e in index.get("files", {}).items():
        if not e.get("is_test"):
            continue
        if e.get("test_target_guess") == api_name or api_name in e.get("edges", {}).get("classes", []):
            out.append(rel)
    return out
