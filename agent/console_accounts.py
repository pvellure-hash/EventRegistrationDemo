"""console_accounts.py - user accounts, allowed email domains, email verification and approval (v15).

Requesting access, step by step:

    1. details   name, work email, username, password       email domain must be on the allowed list
    2. verify    a 6-digit code is e-mailed; it expires after CONSOLE_VERIFY_CODE_MINUTES (15)
    3. pending   only a verified request reaches the project administrators
    4. decision  a project administrator approves (and picks a role) or rejects
    5. active    the person can sign in, while their email domain stays on the allowed list

Rules built in
  * passwords are never stored: only a salted scrypt hash (hashlib, no extra package); the hash is made at step 1,
    so the password is not kept in memory while the code is outstanding
  * password policy: at least CONSOLE_MIN_PASSWORD_LENGTH (12) characters, not the username or email name,
    not a common password
  * verification codes are random, stored only as a salted hash, single use, 5 wrong tries end the request,
    a resend is allowed after 60 seconds (3 at most) and replaces the previous code
  * allowed domains: exact match ("deloitte.ca"), or "*.gov.bc.ca" for every sub-domain of gov.bc.ca;
    an empty list means nobody can request access and only administrators can sign in
  * sign-in checks the password first, then the status and domain, so nobody learns that an account exists without
    knowing its password; the unknown-user path costs the same time as a real one
  * lock-out after CONSOLE_LOCKOUT_ATTEMPTS (5) wrong passwords for CONSOLE_LOCKOUT_MINUTES (15)
  * one account per email address; an administrator can never approve their own request; the last administrator
    cannot be removed
  * files are written atomically and re-read when they change, so a disabled user or a removed domain loses access
    on their next request

Files (in CONSOLE_DATA_DIR, default %USERPROFILE%\\.ai-delivery; keep them out of git):
  accounts.json   accounts (password hashes, roles, status)
  domains.json    the allowed email domains (standalone mode; in platform mode they come from signed grants)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from pathlib import Path

ROLES = ("viewer", "operator", "approver")
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
DOMAIN = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
COMMON = {"password1234", "passwordpassword", "123456789012", "qwertyuiop12", "changeme1234", "welcome12345",
          "letmein12345", "administrator", "iloveyou1234", "abcdefghijkl", "p@ssw0rd1234", "password@123"}
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1        # tests lower SCRYPT_N for speed; the stored hash records its own


class AccountError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# --------------------------------------------------------------------------- passwords
def hash_password(password: str, n: int | None = None) -> str:
    n = n or SCRYPT_N
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=128 * 1024 * 1024)
    return "scrypt$%d$%d$%d$%s$%s" % (n, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            dklen=32, maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def password_problem(password: str, username: str = "", email: str = "", min_len: int = 12) -> str | None:
    if len(password) < min_len:
        return f"Use at least {min_len} characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    low = password.lower()
    if username and username.lower() in low:
        return "The password must not contain your username."
    local = email.split("@")[0].lower() if "@" in email else ""
    if len(local) >= 4 and local in low:
        return "The password must not contain your email name."
    if low in COMMON or len(set(password)) < 5:
        return "That password is too easy to guess. Use a longer phrase."
    return None


def email_domain(email: str) -> str:
    return (email or "").rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


def mask_email(email: str) -> str:
    local, _, dom = (email or "").partition("@")
    if not dom:
        return ""
    return (local[:1] + "\u2022" * max(2, min(6, len(local) - 1))) + "@" + dom


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "pw"}


REPLACE_TRIES = 8
REPLACE_SLEEP = time.sleep                  # tests swap this so they do not really wait


def replace_file(src, dst, tries: int | None = None):
    """os.replace that rides out the short lock Windows puts on a file while a virus scanner, the search indexer or
    another reader has it open (WinError 5 or 32). Waits 0.02 s, 0.04 s, 0.08 s ... (about 2.5 s in all), then gives
    up and raises the original error, so a real permission problem still shows. Other errors are never retried."""
    tries = tries or REPLACE_TRIES
    for n in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if n == tries - 1:
                raise
            REPLACE_SLEEP(0.02 * (2 ** n))


def read_text(path, encoding="utf-8-sig", tries: int | None = None) -> str:
    """Path.read_text that rides out the same short Windows lock while another process is replacing the file.
    FileNotFoundError is raised at once (a missing file is normal and callers handle it)."""
    tries = tries or REPLACE_TRIES
    for n in range(tries):
        try:
            return Path(path).read_text(encoding=encoding)
        except PermissionError:
            if n == tries - 1:
                raise
            REPLACE_SLEEP(0.02 * (2 ** n))


def _atomic_write(path: Path, data: dict):
    """Write JSON next to the target, then swap it in. The temporary name is unique per write, so two writers can
    never collide, and it is removed if anything fails, so a failed write leaves the old file and no litter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        replace_file(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _stamp(path: Path):
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)       # size too: two quick writes can share a timestamp tick


# --------------------------------------------------------------------------- allowed email domains
def domain_matches(entry: str, email: str) -> bool:
    """entry "deloitte.ca" matches exactly; "*.gov.bc.ca" matches any sub-domain of gov.bc.ca (not gov.bc.ca itself)."""
    dom = email_domain(email)
    if not dom or not re.fullmatch(r"[a-z0-9.-]+", dom):
        return False
    if entry.startswith("*."):
        return dom.endswith(entry[1:]) and dom != entry[2:]
    return dom == entry


def normalise_domain(value: str) -> str:
    d = (value or "").strip().lower().lstrip("@").rstrip(".")
    if not DOMAIN.match(d):
        raise AccountError("bad_domain", "Enter a domain like deloitte.ca, or *.gov.bc.ca for all of its sub-domains.")
    return d


class DomainPolicy:
    """domains.json: {"domains": ["deloitte.ca", "*.gov.bc.ca"]}. Re-read when it changes. Used in standalone mode.
    A missing file means an empty list. An unreadable file allows nobody (fail closed) and is never overwritten."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._stamp = None
        self._domains: list[str] = []
        self._error = None

    def _load(self):
        try:
            st = _stamp(self.path)
        except OSError:
            self._domains, self._stamp, self._error = [], None, None
            return
        if st == self._stamp:
            return
        try:
            raw = json.loads(read_text(self.path)).get("domains") or []
            out = []
            for d in raw:
                try:
                    n = normalise_domain(str(d))
                except AccountError:
                    continue
                if n not in out:
                    out.append(n)
            self._domains, self._stamp, self._error = out, st, None
        except (ValueError, AttributeError) as exc:
            self._domains, self._stamp, self._error = [], st, f"domains file unreadable: {exc}"

    @property
    def error(self):
        with self.lock:
            self._load()
            return self._error

    def list(self) -> list[str]:
        with self.lock:
            self._load()
            return list(self._domains)

    def allows(self, email: str) -> bool:
        return any(domain_matches(entry, email) for entry in self.list())

    def rows(self, count_users) -> list[dict]:
        return [{"domain": d, "active_users": count_users(d), "state": "allowed"} for d in self.list()]

    def add(self, value: str) -> str:
        d = normalise_domain(value)
        with self.lock:
            self._load()
            if self._error:
                raise AccountError("store_unreadable", "The domains file is unreadable; refusing to overwrite it.")
            if d not in self._domains:
                self._domains.append(d)
                _atomic_write(self.path, {"domains": self._domains})
                self._stamp = _stamp(self.path)
            return d

    def remove(self, value: str) -> bool:
        d = (value or "").strip().lower().lstrip("@")
        with self.lock:
            self._load()
            if self._error:
                raise AccountError("store_unreadable", "The domains file is unreadable; refusing to overwrite it.")
            if d not in self._domains:
                return False
            self._domains.remove(d)
            _atomic_write(self.path, {"domains": self._domains})
            self._stamp = _stamp(self.path)
            return True

    def restore(self, value: str) -> bool:
        raise AccountError("not_supported", "Domains are managed directly in standalone mode; use add.")


# --------------------------------------------------------------------------- the account store
class AccountStore:
    def __init__(self, path, clock=time.time, min_len=12, max_fail=5, lock_s=900, max_pending=100, reg_per_hour=30):
        self.path = Path(path)
        self.clock, self.min_len, self.max_fail, self.lock_s = clock, min_len, max_fail, lock_s
        self.max_pending, self.reg_per_hour = max_pending, reg_per_hour
        self.lock = threading.RLock()
        self._mtime = None
        self._users: dict[str, dict] = {}
        self._error = None
        self._fail: dict[str, dict] = {}
        self._reg_times: list[float] = []
        self._dummy = None

    # ---- file
    def _load(self):
        try:
            m = _stamp(self.path)
        except OSError:
            self._users, self._mtime, self._error = {}, None, None       # no file yet = no accounts
            return
        if m == self._mtime:
            return
        try:
            data = json.loads(read_text(self.path))
            self._users = {str(k).lower(): v for k, v in (data.get("users") or {}).items() if isinstance(v, dict)}
            self._mtime, self._error = m, None
        except (ValueError, AttributeError) as exc:
            self._users, self._mtime, self._error = {}, m, f"accounts file unreadable: {exc}"

    def _save(self):
        if self._error:
            raise AccountError("store_unreadable", "The accounts file is unreadable; refusing to overwrite it.")
        _atomic_write(self.path, {"users": self._users})
        self._mtime = _stamp(self.path)

    @property
    def error(self):
        with self.lock:
            self._load()
            return self._error

    # ---- reads
    def get(self, username: str) -> dict | None:
        with self.lock:
            self._load()
            rec = self._users.get((username or "").strip().lower())
            return _public(rec) if rec else None

    def list_users(self) -> list[dict]:
        with self.lock:
            self._load()
            return sorted((_public(r) for r in self._users.values()), key=lambda r: r["username"].lower())

    def pending(self) -> list[dict]:
        return sorted((r for r in self.list_users() if r["status"] == "pending"), key=lambda r: r.get("requested", 0))

    def active_admins(self) -> list[dict]:
        return [r for r in self.list_users() if r["status"] == "active" and r.get("admin")]

    def count_active_in_domain(self, entry: str) -> int:
        """Active non-administrator accounts whose email matches this allowed-domain entry."""
        return sum(1 for r in self.list_users()
                   if r["status"] == "active" and not r.get("admin") and domain_matches(entry, r.get("email", "")))

    # ---- sign in
    def _dummy_hash(self):
        if self._dummy is None:
            self._dummy = hash_password("not-a-real-password-for-timing")
        return self._dummy

    def verify(self, username: str, password: str):
        """Returns (code, record-or-None, retry_after_seconds).
        code: ok | bad | locked | pending | rejected | disabled.  The password is checked BEFORE the status."""
        key = (username or "").strip().lower()
        now = self.clock()
        with self.lock:
            f = self._fail.get(key)
            if f and f["until"] > now:
                return "locked", None, int(f["until"] - now) + 1
            self._load()
            rec = self._users.get(key)
            stored = rec["pw"] if rec and rec.get("pw") else self._dummy_hash()
            ok = len(password or "") <= 256 and check_password(password or "", stored) and rec is not None
            if not ok:
                f = self._fail.setdefault(key, {"n": 0, "until": 0})
                f["n"] += 1
                if f["n"] >= self.max_fail:
                    f["n"], f["until"] = 0, now + self.lock_s
                if len(self._fail) > 2000:
                    self._fail = {k: v for k, v in self._fail.items() if v["until"] > now}
                return "bad", None, 0
            self._fail.pop(key, None)
            return (rec["status"] if rec["status"] != "active" else "ok"), _public(rec), 0

    # ---- requests and decisions
    def validate_request(self, username, name, email, password=None, pw_hash=None):
        """Check a request without saving it. Returns the cleaned (username, name, email)."""
        username, name, email = (username or "").strip(), " ".join((name or "").split()), (email or "").strip()
        if not USERNAME.match(username):
            raise AccountError("bad_username", "Username: 3-32 letters, digits, dot, dash or underscore, starting with a letter or digit.")
        if not (2 <= len(name) <= 80):
            raise AccountError("bad_name", "Enter your full name.")
        if not EMAIL.match(email):
            raise AccountError("bad_email", "Enter a valid work email address.")
        if pw_hash is None:
            problem = password_problem(password or "", username, email, self.min_len)
            if problem:
                raise AccountError("weak_password", problem)
        with self.lock:
            self._load()
            old = self._users.get(username.lower())
            if old and old["status"] != "rejected":
                raise AccountError("username_taken", "That username is not available.")
            for r in self._users.values():
                if r["status"] != "rejected" and r.get("email", "").lower() == email.lower() and r["username"].lower() != username.lower():
                    raise AccountError("email_taken", "That email address already has an account or a request.")
        return username, name, email

    def register(self, username, name, email, password=None, reason="", pw_hash=None) -> dict:
        username, name, email = self.validate_request(username, name, email, password, pw_hash)
        reason = (reason or "").strip()[:500]
        pw = pw_hash or hash_password(password)
        now = self.clock()
        with self.lock:
            self._load()
            self._reg_times = [t for t in self._reg_times if now - t < 3600]
            if len(self._reg_times) >= self.reg_per_hour:
                raise AccountError("rate_limited", "Too many requests right now. Try again later.")
            self.validate_request(username, name, email, pw_hash=pw)        # again, under the lock
            if sum(1 for r in self._users.values() if r["status"] == "pending") >= self.max_pending:
                raise AccountError("too_many", "Too many requests are waiting for approval.")
            rec = {"username": username, "name": name, "email": email, "role": None, "admin": False,
                   "status": "pending", "pw": pw, "requested": now, "reason": reason}
            self._users[username.lower()] = rec
            self._reg_times.append(now)
            self._save()
            return _public(rec)

    def decide(self, username: str, decision: str, by: str, role: str | None = None) -> dict:
        key = (username or "").strip().lower()
        with self.lock:
            self._load()
            rec = self._users.get(key)
            if not rec or rec["status"] != "pending":
                raise AccountError("not_pending", "There is no pending request for that user.")
            if (by or "").strip().lower() == key:
                raise AccountError("self_approval", "You cannot decide your own request.")
            if decision == "approve":
                if role not in ROLES:
                    raise AccountError("bad_role", "Choose a role: " + ", ".join(ROLES) + ".")
                rec.update(status="active", role=role)
            elif decision == "reject":
                rec.update(status="rejected", role=None)
            else:
                raise AccountError("bad_decision", "Decision must be approve or reject.")
            rec.update(decided_by=by, decided_at=self.clock())
            self._save()
            return _public(rec)

    def set_user(self, username: str, by: str, role=None, disabled=None, admin=None) -> dict:
        key = (username or "").strip().lower()
        with self.lock:
            self._load()
            rec = self._users.get(key)
            if not rec or rec["status"] not in ("active", "disabled"):
                raise AccountError("not_found", "No such active or disabled user.")
            new = dict(rec)
            if role is not None:
                if role not in ROLES:
                    raise AccountError("bad_role", "Choose a role: " + ", ".join(ROLES) + ".")
                new["role"] = role
            if disabled is not None:
                new["status"] = "disabled" if disabled else "active"
            if admin is not None:
                new["admin"] = bool(admin)
            others = [r for k, r in self._users.items() if k != key and r["status"] == "active" and r.get("admin")]
            if not others and not (new["status"] == "active" and new.get("admin")):
                raise AccountError("last_admin", "At least one active project administrator must remain.")
            rec.update(new)
            rec.update(changed_by=by, changed_at=self.clock())
            self._save()
            return _public(rec)

    # ---- project administrators
    def bootstrap_admin(self, username, name, email, password) -> dict:
        """Standalone mode only (the console refuses this when a platform key is configured)."""
        username, email = (username or "").strip(), (email or "").strip()
        if not USERNAME.match(username):
            raise AccountError("bad_username", "Username: 3-32 letters, digits, dot, dash or underscore.")
        if not EMAIL.match(email):
            raise AccountError("bad_email", "Enter a valid email address.")
        problem = password_problem(password or "", username, email, self.min_len)
        if problem:
            raise AccountError("weak_password", problem)
        return self.create_admin_with_hash(username, name, email, hash_password(password), source="bootstrap")

    def create_admin_with_hash(self, username, name, email, pw_hash, source="invitation") -> dict:
        """Create an active project administrator whose password was already hashed (the invitation flow)."""
        username, email = (username or "").strip(), (email or "").strip()
        if not USERNAME.match(username):
            raise AccountError("bad_username", "Username: 3-32 letters, digits, dot, dash or underscore.")
        if not EMAIL.match(email):
            raise AccountError("bad_email", "Enter a valid email address.")
        with self.lock:
            self._load()
            if username.lower() in self._users:
                raise AccountError("username_taken", "That user already exists. Use reset-password, or make-admin.")
            rec = {"username": username, "name": " ".join((name or username).split()), "email": email, "role": "approver",
                   "admin": True, "status": "active", "pw": pw_hash, "requested": self.clock(), "reason": source,
                   "decided_by": source, "decided_at": self.clock()}
            self._users[username.lower()] = rec
            self._save()
            return _public(rec)

    def make_admin(self, username: str) -> dict:
        """CLI only: turn an existing account into an active project administrator with the approver role."""
        key = (username or "").strip().lower()
        with self.lock:
            self._load()
            rec = self._users.get(key)
            if not rec:
                raise AccountError("not_found", "No such user.")
            rec.update(status="active", admin=True, role="approver", changed_by="cli", changed_at=self.clock())
            self._save()
            return _public(rec)

    make_manager = make_admin                  # the old name still works

    def set_password(self, username: str, password: str):
        key = (username or "").strip().lower()
        with self.lock:
            self._load()
            rec = self._users.get(key)
            if not rec:
                raise AccountError("not_found", "No such user.")
            problem = password_problem(password or "", rec["username"], rec.get("email", ""), self.min_len)
            if problem:
                raise AccountError("weak_password", problem)
            rec["pw"] = hash_password(password)
            self._fail.pop(key, None)
            self._save()


# --------------------------------------------------------------------------- email verification codes
class VerificationStore:
    """Short-lived, single-use codes that prove a person controls the email address they gave.
    Kept in memory only: restarting the console cancels requests that are still waiting for their code."""

    def __init__(self, clock=time.time, ttl_s=900, max_attempts=5, resend_after_s=60, max_sends=4,
                 starts_per_email_per_hour=5, max_open=500):
        self.clock, self.ttl_s, self.max_attempts = clock, ttl_s, max_attempts
        self.resend_after_s, self.max_sends = resend_after_s, max_sends
        self.starts_per_email_per_hour, self.max_open = starts_per_email_per_hour, max_open
        self.lock = threading.Lock()
        self._open: dict[str, dict] = {}
        self._starts: dict[str, list[float]] = {}

    @staticmethod
    def _digest(salt: bytes, code: str) -> str:
        return hashlib.sha256(salt + code.encode("ascii")).hexdigest()

    @staticmethod
    def new_code() -> str:
        return f"{secrets.randbelow(10 ** 6):06d}"

    def _purge(self, now):
        self._open = {k: v for k, v in self._open.items() if v["expires"] > now and not v.get("done")}

    def start(self, details: dict) -> tuple[str, str, dict]:
        """details: username, name, email, pw (hash), reason. Returns (request id, code, record)."""
        now = self.clock()
        email = details["email"].lower()
        with self.lock:
            self._purge(now)
            times = [t for t in self._starts.get(email, []) if now - t < 3600]
            if len(times) >= self.starts_per_email_per_hour:
                raise AccountError("rate_limited", "Too many codes were requested for this email. Try again in an hour.")
            if len(self._open) >= self.max_open:
                raise AccountError("rate_limited", "Too many requests right now. Try again later.")
            # a new start replaces any open request for the same email or username
            for k in [k for k, v in self._open.items()
                      if v["details"]["email"].lower() == email or v["details"]["username"].lower() == details["username"].lower()]:
                del self._open[k]
            times.append(now)
            self._starts[email] = times
            vid, code, salt = secrets.token_urlsafe(24), self.new_code(), secrets.token_bytes(16)
            rec = {"details": dict(details), "salt": salt, "digest": self._digest(salt, code), "created": now,
                   "expires": now + self.ttl_s, "attempts": 0, "sends": 1, "last_sent": now}
            self._open[vid] = rec
            return vid, code, rec

    def resend(self, vid: str) -> tuple[str, dict]:
        now = self.clock()
        with self.lock:
            rec = self._open.get(vid or "")
            if not rec or rec.get("done"):
                raise AccountError("not_found", "This request has ended. Start again.")
            wait = int(rec["last_sent"] + self.resend_after_s - now)
            if wait > 0:
                raise AccountError("too_soon", f"Wait {wait} seconds before asking for another code.")
            if rec["sends"] >= self.max_sends:
                raise AccountError("too_many_codes", "No more codes can be sent for this request. Start again later.")
            code, salt = self.new_code(), secrets.token_bytes(16)
            rec.update(salt=salt, digest=self._digest(salt, code), expires=now + self.ttl_s, attempts=0,
                       sends=rec["sends"] + 1, last_sent=now)
            return code, rec

    def check(self, vid: str, code: str) -> dict:
        """The request details if the code is right; raises AccountError otherwise. Single use."""
        now = self.clock()
        code = re.sub(r"\s+", "", str(code or ""))
        with self.lock:
            rec = self._open.get(vid or "")
            if not rec or rec.get("done"):
                raise AccountError("not_found", "This request has ended. Start again.")
            if rec["expires"] <= now:
                raise AccountError("expired", "The code has expired. Send a new code.")
            if not re.fullmatch(r"\d{6}", code):
                raise AccountError("invalid_code", "Enter the 6-digit code from the email.")
            rec["attempts"] += 1
            if not hmac.compare_digest(self._digest(rec["salt"], code), rec["digest"]):
                left = self.max_attempts - rec["attempts"]
                if left <= 0:
                    del self._open[vid]
                    raise AccountError("too_many_attempts", "Too many wrong codes. Start again.")
                raise AccountError("invalid_code", f"That code is not right. {left} attempt{'s' if left != 1 else ''} left.")
            rec["done"] = True
            del self._open[vid]
            return dict(rec["details"])

    def cancel(self, vid: str):
        with self.lock:
            self._open.pop(vid or "", None)

    def info(self, vid: str) -> dict | None:
        with self.lock:
            rec = self._open.get(vid or "")
            return None if not rec else {"expires": rec["expires"], "last_sent": rec["last_sent"], "sends": rec["sends"]}


# --------------------------------------------------------------------------- e-mail
def _clean(s: str, n: int = 200) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(s or "")).strip()[:n]


def _secret_from_store(name: str):
    try:
        import secret_store
        return secret_store.get(name)
    except Exception:  # noqa: BLE001
        return None


class Notifier:
    """E-mail: verification codes, new requests (to project administrators) and decisions (to the requester).
    Off unless CONSOLE_SMTP_HOST and CONSOLE_SMTP_FROM are set. Recipients for new requests: CONSOLE_MANAGER_EMAILS,
    else every active administrator. The SMTP password comes from the secret store (SMTP_PASSWORD; add it to
    SECRETS_EXTRA). TLS is always used. Notices never block a request; a verification code that cannot be sent is
    reported."""

    def __init__(self, store, env=None, sender=None, on_result=None, secret=None, threaded=True):
        e = os.environ if env is None else env
        self.store, self.threaded = store, threaded
        self.host = (e.get("CONSOLE_SMTP_HOST") or "").strip()
        try:
            self.port = int(e.get("CONSOLE_SMTP_PORT") or 587)
        except ValueError:
            self.port = 587
        self.user = (e.get("CONSOLE_SMTP_USER") or "").strip()
        self.from_addr = (e.get("CONSOLE_SMTP_FROM") or self.user).strip()
        self.managers = [x.strip() for x in (e.get("CONSOLE_MANAGER_EMAILS") or "").split(",") if EMAIL.match(x.strip())]
        self.base_url = (e.get("CONSOLE_BASE_URL") or "").rstrip("/")
        self._sender = sender or self._smtp_send
        self._on_result = on_result or (lambda kind, ok, detail: None)
        self._secret = secret or _secret_from_store

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.from_addr)

    def recipients(self) -> list[str]:
        if self.managers:
            return list(self.managers)
        return [r["email"] for r in self.store.active_admins() if r.get("email")] if self.store else []

    def _build(self, to: list[str], subject: str, body: str) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.from_addr, ", ".join(to), _clean(subject, 150)
        m.set_content(body)
        return m

    def send_plain(self, to: str, subject: str, body: str):
        """Synchronous. Raises if e-mail is off or the send fails. Used for platform invitations."""
        if not self.enabled:
            raise RuntimeError("e-mail is not configured")
        if not EMAIL.match(to or ""):
            raise RuntimeError("not a valid address")
        self._sender(self._build([to], subject, body))

    def send_code(self, email: str, name: str, code: str, minutes: int):
        """Synchronous, so the person is told at once if the code could not be sent. Raises on failure."""
        if not self.enabled:
            raise RuntimeError("e-mail is not configured")
        body = (f"Hello {_clean(name, 80)},\n\nYour verification code for the AI Delivery Console is:\n\n    {code}\n\n"
                f"It expires in {minutes} minutes and can be used once.\n\n"
                "If you did not ask for access, you can ignore this email; nothing happens without the code.\n")
        self._sender(self._build([email], "Your AI Delivery Console verification code", body))

    def request_submitted(self, rec: dict) -> bool:
        to = self.recipients()
        if not (self.enabled and to):
            return False
        link = f"\nReview it: {self.base_url}/auth/admin\n" if self.base_url else "\nReview it in the console: Access requests.\n"
        body = (f"{_clean(rec['name'], 80)} ({_clean(rec['email'], 120)}) asked for access to the AI Delivery Console.\n"
                f"Username: {_clean(rec['username'], 40)}\nReason: {_clean(rec.get('reason'), 500) or '(none given)'}\n"
                f"The email address was verified with a code.\n{link}"
                "Nothing happens until a project administrator approves the request.\n")
        return self._dispatch("request", self._build(to, f"Access request: {_clean(rec['name'], 60)}", body))

    def decided(self, rec: dict, decision: str, by: str) -> bool:
        if not (self.enabled and rec.get("email")):
            return False
        if decision == "approve":
            body = (f"Hello {_clean(rec['name'], 80)},\n\nYour access to the AI Delivery Console was approved "
                    f"(role: {rec.get('role')}). Sign in with your username ({rec['username']}) and the password you chose.\n")
            subject = "Your access was approved"
        else:
            body = f"Hello {_clean(rec['name'], 80)},\n\nYour request for access to the AI Delivery Console was not approved.\n"
            subject = "Your access request was not approved"
        return self._dispatch("decision", self._build([rec["email"]], subject, body))

    def _dispatch(self, kind: str, msg: EmailMessage) -> bool:
        def run():
            try:
                self._sender(msg)
                self._on_result(kind, True, "")
            except Exception as exc:  # noqa: BLE001
                self._on_result(kind, False, type(exc).__name__)
        if self.threaded:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return True

    def _smtp_send(self, msg: EmailMessage):
        pw = self._secret("SMTP_PASSWORD") if self.user else None
        ctx = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ctx) as s:
                if self.user:
                    s.login(self.user, pw or "")
                s.send_message(msg)
            return
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls(context=ctx)
            if self.user:
                s.login(self.user, pw or "")
            s.send_message(msg)
