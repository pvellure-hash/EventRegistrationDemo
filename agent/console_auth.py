"""console_auth.py - sign-in, roles, allowed domains, invitations, email verification, approval and audit (v15).

Choose how people sign in with CONSOLE_AUTH_MODE in .env (there is no "off"):

  windows   The Windows Security window opens (PIN, fingerprint, face or password) and Windows checks it.
            For a laptop. Needs an access list entry for your Windows account.
  accounts  Username + password accounts. A person asks for access with a work email on an ALLOWED DOMAIN, proves
            the address with an e-mailed code (15 minutes), and a PROJECT ADMINISTRATOR approves and picks a role.
            For a shared or hosted console (Docker, a server).
  entra     Microsoft Entra ID sign-in (company SSO, MFA).
  local     No prompt: trusts the Windows account running the service. Single-user fallback.

Two ways to run accounts mode
  standalone  (default)  one team runs its own console: it adds its own email domains and creates its first
                         administrator with `bootstrap-admin`.
  platform    (CONSOLE_PLATFORM_PUBLIC_KEY set)  the console belongs to a project that the Deloitte platform team
                         onboarded. The allowed email domains come ONLY from grants the platform team signed; a
                         project administrator can pause or resume a domain but never add one. The first
                         administrator is the person the platform team invited: they claim a single-use invitation
                         at /auth/claim (invitation code + a code e-mailed to the invited address) and choose their
                         own password. Nobody sets it for them.

Roles (each includes the ones before it):
  viewer    see runs, logs, dashboard
  operator  + start/stop the watcher, pre-flight, rebuild dashboard, service settings
  approver  + approve / reject at the review gate
A PROJECT ADMINISTRATOR (accounts mode) is a separate power: approving who may use the console and which allowed
domains are paused does not by itself allow approving a code change. Administrators can always sign in.

Enforced on every request, on the server (the page only mirrors it):
  * sessions: random 256-bit id in an HttpOnly, SameSite=Strict cookie; 30 min idle, 8 h maximum
  * Approve / Reject and administrator actions need a sign-in within the last CONSOLE_APPROVE_REAUTH_MIN (60) minutes
  * accounts, access list, allowed domains and grants are re-read on every request: disable someone, pause a domain
    or revoke a grant and access ends at once
  * unknown routes are deny-by-default; every sign-in, denial, action and decision is audited by name
    (logs\\run-console\\auth-audit.jsonl; passwords, codes, invitation codes, tokens and cookies are never logged)

Settings (.env, no comments after values):
  CONSOLE_AUTH_MODE=windows|accounts|entra|local
  CONSOLE_PLATFORM_PUBLIC_KEY=<base64>             platform mode: the platform team's PUBLIC key (not a secret)
  CONSOLE_WINDOWS_VERIFY=auto|hello|password       windows mode (default auto: Hello first, then password window)
  CONSOLE_DATA_DIR=<folder>                        accounts, domains and grants (default %USERPROFILE%\\.ai-delivery)
  CONSOLE_ALLOW_REGISTRATION=true                  accounts mode: let people request access
  CONSOLE_VERIFY_CODE_MINUTES=15                   how long an emailed code is valid (5-60)
  CONSOLE_VERIFY_DELIVERY=email|log                log = write codes to logs\\run-console\\verification-codes.log
                                                   instead of sending them (local testing only)
  CONSOLE_MIN_PASSWORD_LENGTH=12  CONSOLE_LOCKOUT_ATTEMPTS=5  CONSOLE_LOCKOUT_MINUTES=15
  CONSOLE_MANAGER_EMAILS=a@x.com,b@x.com           optional: who is e-mailed about new requests
  CONSOLE_SMTP_HOST / _PORT / _USER / _FROM        e-mail (password: secret store, name SMTP_PASSWORD)
  CONSOLE_BASE_URL=https://console.example.com     optional: link in the e-mail
  CONSOLE_COOKIE_SECURE=true                       set when the console is served over HTTPS
  ENTRA_TENANT_ID / ENTRA_CLIENT_ID                entra mode;  CONSOLE_AUTH_ALLOW_LOCAL=true for local mode
  CONSOLE_REQUIRE_MFA, CONSOLE_SESSION_IDLE_MIN, CONSOLE_SESSION_MAX_HOURS, CONSOLE_APPROVE_REAUTH_MIN

Command line (run from agent\\):
  python console_auth.py project                            this console's project, domains and invitation status
  python console_auth.py grant apply FILE                   apply a grant file from the platform team (platform mode)
  python console_auth.py domains                            list allowed email domains
  python console_auth.py domains add deloitte.ca            standalone: allow a domain   (*.gov.bc.ca = all sub-domains)
  python console_auth.py domains remove|restore gov.bc.ca   platform: pause or resume a domain; standalone: remove
  python console_auth.py windows-test [--method auto|hello|password]   try the Windows sign-in prompt
  python console_auth.py init [--upn you@company.com]       access list with you as approver (windows/local/entra)
  python console_auth.py add|remove|list ...                manage the access list
  python console_auth.py bootstrap-admin --user pavan --email p@x.com  standalone only: first administrator
  python console_auth.py make-admin --user pavan            turn an existing account into a project administrator
  python console_auth.py accounts | requests                list accounts / pending requests
  python console_auth.py approve --user u --role viewer     | reject --user u | disable --user u | enable --user u
  python console_auth.py reset-password --user u
  python console_auth.py check                              validate settings and secrets
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import console_accounts as accts
import platform_trust as pt

ROLES = ("viewer", "operator", "approver")
RANK = {r: i for i, r in enumerate(ROLES)}
MODES = ("entra", "windows", "accounts", "local")
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Exact routes of run_console.py / console_service.py. Anything not listed is deny-by-default.
ROUTES = {
    ("POST", "/api/answer"): "approver",                 # Approve / Reject at the review gate
    ("POST", "/api/start"): "operator",
    ("POST", "/api/stop"): "operator",
    ("POST", "/api/util"): "operator",                   # pre-flight, rebuild dashboard
    ("POST", "/api/service/settings"): "operator",
    ("POST", "/api/service/kill-orphan"): "operator",
    ("POST", "/api/service/shutdown"): "operator",
}
# The launcher (python console_service.py --quit / --status) may use ONLY these, with the control token.
CONTROL_ROUTES = {("GET", "/api/state"), ("POST", "/api/service/shutdown")}


class AuthConfigError(RuntimeError):
    pass


def _env_int(env, name, default):
    try:
        return int(str(env.get(name, default)).strip())
    except ValueError:
        return default


def _flag(env, name, default="false"):
    return str(env.get(name) or default).strip().lower() == "true"


def local_identity() -> str:
    dom = os.getenv("USERDOMAIN", "")
    return f"{dom}\\{getpass.getuser()}" if dom else getpass.getuser()


def default_access_file(env=None) -> Path:
    e = os.environ if env is None else env
    home = Path(e.get("USERPROFILE") or e.get("HOME") or str(Path.home()))
    return Path(e.get("CONSOLE_ACCESS_FILE") or home / ".ai-delivery" / "access.json")


def data_dir(env=None) -> Path:
    e = os.environ if env is None else env
    return Path(e["CONSOLE_DATA_DIR"]) if e.get("CONSOLE_DATA_DIR") else default_access_file(e).parent


class AuthConfig:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.mode = (e.get("CONSOLE_AUTH_MODE") or "entra").strip().lower()
        self.tenant_id = (e.get("ENTRA_TENANT_ID") or "").strip()
        self.client_id = (e.get("ENTRA_CLIENT_ID") or e.get("AZURE_CLIENT_ID") or "").strip()
        self.access_file = default_access_file(e)
        d = data_dir(e)
        self.data_dir = d
        self.accounts_file = d / "accounts.json"
        self.domains_file = d / "domains.json"
        self.platform_key = (e.get("CONSOLE_PLATFORM_PUBLIC_KEY") or "").strip()
        self.windows_verify = (e.get("CONSOLE_WINDOWS_VERIFY") or "auto").strip().lower()
        self.allow_registration = _flag(e, "CONSOLE_ALLOW_REGISTRATION", "true")
        self.verify_minutes = _env_int(e, "CONSOLE_VERIFY_CODE_MINUTES", 15)
        self.verify_delivery = (e.get("CONSOLE_VERIFY_DELIVERY") or "email").strip().lower()
        self.cookie_secure = _flag(e, "CONSOLE_COOKIE_SECURE")
        self.min_password = _env_int(e, "CONSOLE_MIN_PASSWORD_LENGTH", 12)
        self.lock_attempts = _env_int(e, "CONSOLE_LOCKOUT_ATTEMPTS", 5)
        self.lock_s = _env_int(e, "CONSOLE_LOCKOUT_MINUTES", 15) * 60
        self.idle_s = _env_int(e, "CONSOLE_SESSION_IDLE_MIN", 30) * 60
        self.max_s = _env_int(e, "CONSOLE_SESSION_MAX_HOURS", 8) * 3600
        self.reauth_s = _env_int(e, "CONSOLE_APPROVE_REAUTH_MIN", 60) * 60
        self.require_mfa = _flag(e, "CONSOLE_REQUIRE_MFA")
        self.allow_local = _flag(e, "CONSOLE_AUTH_ALLOW_LOCAL")
        self.env = dict(e)
        self.validate()

    def validate(self):
        if self.mode not in MODES:
            raise AuthConfigError("CONSOLE_AUTH_MODE must be one of: " + ", ".join(MODES) + " (there is no 'off')")
        if self.windows_verify not in ("auto", "hello", "password"):
            raise AuthConfigError("CONSOLE_WINDOWS_VERIFY must be auto, hello or password")
        if self.min_password < 8:
            raise AuthConfigError("CONSOLE_MIN_PASSWORD_LENGTH must be at least 8")
        if not (5 <= self.verify_minutes <= 60):
            raise AuthConfigError("CONSOLE_VERIFY_CODE_MINUTES must be between 5 and 60")
        if self.verify_delivery not in ("email", "log"):
            raise AuthConfigError("CONSOLE_VERIFY_DELIVERY must be email or log")
        if self.platform_key:
            try:
                pt.check_public_key(self.platform_key)
            except pt.TrustError:
                raise AuthConfigError("CONSOLE_PLATFORM_PUBLIC_KEY is not a valid public key. Copy it exactly from "
                                      "the platform team (python platform_admin.py public-key).")
        if self.mode == "entra":
            if not GUID.match(self.tenant_id):
                raise AuthConfigError("ENTRA_TENANT_ID must be your tenant's GUID "
                                      "('common' or 'organizations' would let any company sign in)")
            if not GUID.match(self.client_id):
                raise AuthConfigError("ENTRA_CLIENT_ID (or AZURE_CLIENT_ID) must be the app registration's GUID")
        if self.mode == "local" and not self.allow_local:
            raise AuthConfigError("CONSOLE_AUTH_MODE=local also needs CONSOLE_AUTH_ALLOW_LOCAL=true "
                                  "(single-user fallback, not for shared use)")


# --------------------------------------------------------------------------- access list (windows / entra / local)
class AccessList:
    """access.json:
    {"users": [
       {"upn": "pvellure@company.com", "windows": "CORP\\\\pvellure", "role": "approver", "name": "Pavan Vellure"},
       {"upn": "someone@company.com", "oid": "<optional object id>", "role": "viewer"}
    ]}
    Re-read when the file changes. A missing or broken file denies everyone."""

    def __init__(self, path):
        self.path = Path(path)
        self._mtime = None
        self._users: list[dict] = []
        self._error = None

    def _load(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._users, self._mtime, self._error = [], None, f"access list not found: {self.path}"
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(accts.read_text(self.path))
            self._users = [u for u in data.get("users", []) if u.get("role") in RANK]
            self._mtime, self._error = mtime, None
        except (ValueError, AttributeError) as exc:
            self._users, self._mtime, self._error = [], mtime, f"access list unreadable: {exc}"

    @property
    def error(self):
        self._load()
        return self._error

    def users(self) -> list[dict]:
        self._load()
        return list(self._users)

    def role_for(self, upn: str = "", oid: str = "", windows: str = "") -> str | None:
        self._load()
        for u in self._users:
            if windows:
                if u.get("windows", "").lower() == windows.lower():
                    return u["role"]
                continue
            if not upn or u.get("upn", "").lower() != upn.lower():
                continue
            if u.get("oid") and u["oid"].lower() != (oid or "").lower():
                return None          # same name, different person: deny
            return u["role"]
        return None


def _write_access(path: Path, users: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps({"users": users}, indent=2) + "\n", encoding="utf-8")
        accts.replace_file(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_users(path: Path) -> list[dict]:
    try:
        return list(json.loads(accts.read_text(path)).get("users", []))
    except (OSError, ValueError):
        return []


def msal_interactive(cfg: AuthConfig) -> dict:
    """System browser Microsoft sign-in (authorization code + PKCE, loopback redirect); returns ID-token claims."""
    import msal  # pip install msal
    app = msal.PublicClientApplication(cfg.client_id, authority=f"https://login.microsoftonline.com/{cfg.tenant_id}")
    result = app.acquire_token_interactive(scopes=["User.Read"], prompt="select_account", timeout=180)
    if "id_token_claims" not in result:
        raise PermissionError(result.get("error_description") or result.get("error") or "sign-in failed")
    return result["id_token_claims"]


# --------------------------------------------------------------------------- the gate
class AuthGate:
    def __init__(self, cfg: AuthConfig, audit_path, port="", signer=None, clock=time.time,
                 access: AccessList | None = None, console_token: str = "", accounts=None, verifier=None,
                 notifier=None, domains=None, verifications=None, trust=None):
        self.cfg = cfg
        self.access = access or AccessList(cfg.access_file)
        self.audit_path = Path(audit_path)
        self.cookie = f"aidc_session_{port}" if port else "aidc_session"
        self.pending_cookie = self.cookie + "_pending"
        self.signer = signer or (lambda: msal_interactive(cfg))
        self.clock = clock
        self.console_token = console_token
        self.control_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, dict] = {}
        self.attempts: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.claim_lock = threading.Lock()
        self._claim_fails: list[float] = []
        self.login_running = False
        self.accounts = accounts
        if cfg.mode == "accounts" and self.accounts is None:
            self.accounts = accts.AccountStore(cfg.accounts_file, clock=clock, min_len=cfg.min_password,
                                               max_fail=cfg.lock_attempts, lock_s=cfg.lock_s)
        self.trust = trust
        if self.trust is None and cfg.mode == "accounts" and cfg.platform_key:
            self.trust = pt.InstanceTrust(cfg.data_dir, cfg.platform_key, clock=clock)
        self.domains = domains or (pt.PlatformDomains(self.trust, cfg.data_dir / "domain-pauses.json") if self.trust
                                   else accts.DomainPolicy(cfg.domains_file))
        self.verifications = verifications or accts.VerificationStore(clock=clock, ttl_s=cfg.verify_minutes * 60)
        self.verifier = verifier
        if cfg.mode == "windows" and self.verifier is None:
            import windows_login
            self.verifier = windows_login.WindowsVerifier(cfg.windows_verify)
        self.notifier = notifier or accts.Notifier(self.accounts, cfg.env, on_result=self._mail_result)

    @property
    def domain_control(self) -> str:
        return "platform" if self.trust else "local"

    # ---- audit
    def audit(self, event, **fields):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.clock())), "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def _mail_result(self, kind, ok, detail):
        self.audit("mail", kind=kind, ok=ok, detail=detail or None)

    # ---- helpers
    @staticmethod
    def required_role(method: str, path: str) -> str:
        key = (method.upper(), urlsplit(path).path)
        if key in ROUTES:
            return ROUTES[key]
        return "viewer" if key[0] in ("GET", "HEAD") else "approver"

    @staticmethod
    def _cookies(h) -> dict:
        out = {}
        for part in (h.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _cookie_header(self, name, value, max_age):
        return (f"{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(max_age)}"
                + ("; Secure" if self.cfg.cookie_secure else ""))

    @staticmethod
    def _drain(h):
        """Read and discard a request body the gate never used, so a keep-alive connection stays in step."""
        if getattr(h, "_auth_body_for", None) is h.headers:      # this request's body was already read
            return
        rfile = getattr(h, "rfile", None)
        try:
            n = int(h.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if rfile is None or n <= 0:
            return
        h._auth_body_for = h.headers
        if n > 1_000_000:
            h.close_connection = True          # too large to bother reading: close instead
            return
        try:
            rfile.read(n)
        except OSError:
            h.close_connection = True

    def _json(self, h, limit=16384) -> dict:
        """The request body as a dict ({} if empty or invalid). Marks the body as consumed."""
        h._auth_body_for = h.headers     # a kept-alive connection reuses the handler object: key on THIS request
        try:
            n = int(h.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        if n > limit:
            h.close_connection = True
            return {}
        try:
            data = json.loads(h.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _send(self, h, code, body=b"", ctype="application/json", headers=()):
        self._drain(h)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        h.send_response(code)
        h.send_header("Content-Type", ctype + "; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("Referrer-Policy", "no-referrer")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _token_ok(self, h) -> bool:
        tok = h.headers.get("X-Console-Token") or ""
        return bool(self.console_token) and secrets.compare_digest(tok.encode(), self.console_token.encode())

    def _control_ok(self, h) -> bool:
        ctl = h.headers.get("X-Console-Control") or ""
        return bool(ctl) and secrets.compare_digest(ctl.encode(), self.control_token.encode()) \
            and (h.command, urlsplit(h.path).path) in CONTROL_ROUTES

    def _role_for_ident(self, ident: str):
        return self.access.role_for(upn=ident) if "@" in ident else self.access.role_for(windows=ident)

    def _account_allowed(self, rec) -> bool:
        """Administrators can always sign in; everyone else needs an email domain that is still allowed."""
        return bool(rec.get("admin")) or self.domains.allows(rec.get("email", ""))

    def _lookup(self, s):
        """(role, is_admin) for a live session, or (None, False) if the person's access has ended."""
        src = s.get("src")
        if src == "account":
            rec = self.accounts.get(s["user"]) if self.accounts else None
            if not rec or rec["status"] != "active" or not self._account_allowed(rec):
                return None, False
            return rec["role"], bool(rec.get("admin"))
        if src == "entra":
            return self.access.role_for(upn=s["user"], oid=s.get("oid", "")), False
        return self._role_for_ident(s["user"]), False

    def _session(self, h):
        sid = self._cookies(h).get(self.cookie)
        if not sid:
            return None, None
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return None, None
            now = self.clock()
            if now - s["last_seen"] > self.cfg.idle_s or now - s["created"] > self.cfg.max_s:
                del self.sessions[sid]
                self.audit("session_expired", user=s["user"])
                return None, None
            return sid, s

    def current_user(self, h):
        sid, s = self._session(h)
        if not s:
            return None
        role, admin = self._lookup(s)
        if role is None:
            with self.lock:
                self.sessions.pop(sid, None)
            self.audit("access_revoked", user=s["user"])
            return None
        s["role"], s["admin"] = role, admin
        return s

    def _new_session(self, a: dict) -> str:
        sid, now = secrets.token_urlsafe(32), self.clock()
        with self.lock:
            self.sessions[sid] = {"user": a["user"], "name": a["name"], "oid": a.get("oid", ""), "role": a["role"],
                                  "src": a.get("src", "local"), "admin": bool(a.get("admin")),
                                  "created": now, "last_seen": now, "auth_time": now}
        return sid

    # ---- called from the console's _guard()
    def require(self, h, role: str | None = None):
        """The session dict if the request is allowed; otherwise the response is sent and None returned."""
        path = urlsplit(h.path).path
        if self._control_ok(h):
            self.audit("control", method=h.command, path=path)
            return {"user": "service-launcher", "name": "service launcher", "role": "operator"}
        need = role or self.required_role(h.command, h.path)
        s = self.current_user(h)
        if not s:
            if h.command in ("GET", "HEAD") and "text/html" in (h.headers.get("Accept") or ""):
                self._send(h, 302, headers=[("Location", "/auth/signin?next=" + quote(h.path, safe=""))])
            else:
                self._send(h, 401, {"error": "sign_in_required"})
            return None
        if RANK[s["role"]] < RANK[need]:
            self.audit("denied", user=s["user"], role=s["role"], need=need, method=h.command, path=path)
            self._send(h, 403, {"error": "forbidden", "need": need, "role": s["role"]})
            return None
        if need == "approver" and self.clock() - s["auth_time"] > self.cfg.reauth_s:
            self.audit("reauth_required", user=s["user"], path=path)
            self._send(h, 401, {"error": "reauth_required"})
            return None
        with self.lock:
            s["last_seen"] = self.clock()
        if h.command not in ("GET", "HEAD"):
            self.audit("request", user=s["user"], role=s["role"], method=h.command, path=path)
        return s

    def _need_manager(self, h):
        """Session of a signed-in project administrator (accounts mode), else the response is sent, None returned."""
        s = self.current_user(h)
        if not s:
            self._send(h, 401, {"error": "sign_in_required"})
            return None
        if not s.get("admin"):
            self.audit("denied", user=s["user"], need="manager", method=h.command, path=urlsplit(h.path).path)
            self._send(h, 403, {"error": "forbidden", "need": "manager"})
            return None
        if self.clock() - s["auth_time"] > self.cfg.reauth_s:
            self._send(h, 401, {"error": "reauth_required"})
            return None
        with self.lock:
            s["last_seen"] = self.clock()
        return s

    # ---- /auth/* routes
    def handle(self, h) -> bool:
        """Serve /auth/* (sign-in page, invitation claim, sign-in calls, access requests, administrator page)."""
        parts = urlsplit(h.path)
        p, m = parts.path, h.command
        if not p.startswith("/auth/"):
            return False
        if p == "/auth/signin" and m == "GET":
            self._signin_page(h, parse_qs(parts.query).get("next", ["/"])[0])
            return True
        if p == "/auth/admin" and m == "GET":
            self._admin_page(h)
            return True
        # The invitation claim is protected by the invitation code and an e-mailed code, not by the console token,
        # so a person who is not at the console's own screen can set up their project.
        if p == "/auth/claim" and m == "GET":
            self._claim_page(h)
            return True
        if p == "/auth/claim/start" and m == "POST":
            self._claim_start(h)
            return True
        if p == "/auth/claim/verify" and m == "POST":
            self._claim_verify(h)
            return True
        if p == "/auth/claim/resend" and m == "POST":
            self._register_resend(h, claim=True)
            return True
        if not self._token_ok(h):
            self._send(h, 401, {"error": "console_token_required"})
            return True
        if p == "/auth/login" and m == "POST":
            self._login_account(h) if self.cfg.mode == "accounts" else self._login(h)
        elif p == "/auth/register" and m == "POST":
            self._send(h, 400, {"error": "verification_required",
                                "message": "Requests now need an emailed code. Reload the page and try again."})
        elif p == "/auth/register/start" and m == "POST":
            self._register_start(h)
        elif p == "/auth/register/verify" and m == "POST":
            self._register_verify(h)
        elif p == "/auth/register/resend" and m == "POST":
            self._register_resend(h)
        elif p == "/auth/poll" and m == "GET":
            self._poll(h)
        elif p == "/auth/logout" and m == "POST":
            sid, s = self._session(h)
            if sid:
                with self.lock:
                    self.sessions.pop(sid, None)
                self.audit("sign_out", user=s["user"])
            self._send(h, 200, {"ok": True}, headers=[("Set-Cookie", self._cookie_header(self.cookie, "", 0))])
        elif p == "/auth/me" and m == "GET":
            self._me(h)
        elif p.startswith("/auth/admin/") and self.cfg.mode == "accounts":
            self._admin_api(h, p, m)
        else:
            self._send(h, 404, {"error": "not_found"})
        return True

    def _me(self, h):
        s = self.current_user(h)
        if not s:
            self._send(h, 401, {"error": "sign_in_required"})
            return
        out = {"user": s["user"], "name": s.get("name", ""), "role": s["role"], "mode": self.cfg.mode,
               "admin": bool(s.get("admin")),
               "idle_expires_in_s": int(self.cfg.idle_s - (self.clock() - s["last_seen"]))}
        if self.trust and self.trust.project():
            out["project"] = self.trust.project()["name"]
        if s.get("admin") and self.accounts:
            out["pending"] = len(self.accounts.pending())
        self._send(h, 200, out)

    # ---- async sign-in (windows / entra / local)
    def _login(self, h):
        if self.cfg.mode not in ("local", "windows", "entra"):
            self._send(h, 404, {"error": "not_found"})
            return
        if self.access.error:
            self.audit("sign_in_blocked", reason=self.access.error)
            self._send(h, 503, {"error": "access_list", "detail": "Access list missing or unreadable"})
            return
        with self.lock:
            if self.login_running:
                self._send(h, 409, {"error": "sign_in_in_progress"})
                return
            self.login_running = True
            aid = secrets.token_urlsafe(32)
            self.attempts[aid] = {"state": "running", "started": self.clock()}
        cookie = self._cookie_header(self.pending_cookie, aid, 300)
        if self.cfg.mode == "local":
            self._finish(aid, lambda: {"_local": local_identity()})
        else:
            signer = self.verifier if self.cfg.mode == "windows" else self.signer
            threading.Thread(target=self._finish, args=(aid, signer), daemon=True).start()
        self._send(h, 202, {"ok": True, "mode": self.cfg.mode}, headers=[("Set-Cookie", cookie)])

    def _finish(self, aid, signer):
        try:
            result = self._authorise(signer())
        except Exception as exc:  # noqa: BLE001
            result = {"state": "error", "reason": f"sign-in failed: {type(exc).__name__}: {str(exc)[:200]}"}
        with self.lock:
            self.login_running = False
            if aid in self.attempts:
                self.attempts[aid].update(result)
        if result["state"] == "done":
            self.audit("sign_in", user=result["user"], role=result["role"], mode=self.cfg.mode, method=result.get("method"))
        else:
            self.audit("sign_in_denied", user=result.get("user"), reason=result.get("reason"))

    def _authorise(self, claims: dict) -> dict:
        now = self.clock()
        for key, src in (("_windows", "windows"), ("_local", "local")):
            if key in claims:
                user = claims[key]
                role = self._role_for_ident(user)
                if not role:
                    return {"state": "error", "user": user, "reason": f"{user} is not on the access list"}
                return {"state": "done", "user": user, "name": claims.get("name", user), "oid": "", "role": role,
                        "src": src, "method": claims.get("method")}
        if claims.get("tid", "").lower() != self.cfg.tenant_id.lower():
            return {"state": "error", "reason": "account is from another tenant"}
        if claims.get("aud", "").lower() != self.cfg.client_id.lower():
            return {"state": "error", "reason": "token was issued for another application"}
        if float(claims.get("exp", 0)) < now:
            return {"state": "error", "reason": "token expired"}
        if self.cfg.require_mfa and "mfa" not in (claims.get("amr") or []):
            return {"state": "error", "reason": "multi-factor sign-in required"}
        upn = claims.get("preferred_username") or claims.get("upn") or ""
        oid = claims.get("oid", "")
        role = self.access.role_for(upn=upn, oid=oid)
        if not role:
            return {"state": "error", "user": upn, "reason": f"{upn or 'this account'} is not on the access list"}
        return {"state": "done", "user": upn, "name": claims.get("name", upn), "oid": oid, "role": role, "src": "entra"}

    def _poll(self, h):
        aid = self._cookies(h).get(self.pending_cookie)
        with self.lock:
            a = self.attempts.get(aid) if aid else None
            if a and a["state"] == "running" and self.clock() - a["started"] > 240:
                a.update(state="error", reason="sign-in timed out")
                self.login_running = False
        if not a:
            self._send(h, 400, {"error": "no_sign_in_started"})
            return
        if a["state"] == "running":
            self._send(h, 200, {"state": "running"})
            return
        with self.lock:
            self.attempts.pop(aid, None)
        clear = ("Set-Cookie", self._cookie_header(self.pending_cookie, "", 0))
        if a["state"] == "error":
            self._send(h, 403, {"state": "error", "reason": a.get("reason", "")}, headers=[clear])
            return
        sid = self._new_session(a)
        self._send(h, 200, {"state": "done", "user": a["user"], "role": a["role"]},
                   headers=[clear, ("Set-Cookie", self._cookie_header(self.cookie, sid, self.cfg.max_s))])

    # ---- accounts mode: sign in
    _LOGIN_FAIL = {"bad": (401, "Invalid username or password."),
                   "pending": (403, "Your access request is waiting for approval by a project administrator."),
                   "rejected": (403, "Your access request was not approved."),
                   "disabled": (403, "This account is disabled. Contact a project administrator."),
                   "domain": (403, "Your email domain is no longer allowed on this console. Contact a project administrator.")}

    def _login_account(self, h):
        b = self._json(h)
        username, password = str(b.get("username", "")), str(b.get("password", ""))
        code, rec, retry = self.accounts.verify(username, password)
        if code == "ok" and not self._account_allowed(rec):
            code = "domain"
        known = self.accounts.get(username)
        who = known["username"] if known else None      # never log text typed into the box for a name we do not know
        if code == "ok":
            sid = self._new_session({"user": rec["username"], "name": rec["name"], "role": rec["role"],
                                     "src": "account", "admin": rec.get("admin")})
            self.audit("sign_in", user=rec["username"], role=rec["role"], mode="accounts", method="password")
            self._send(h, 200, {"state": "done", "user": rec["username"], "role": rec["role"]},
                       headers=[("Set-Cookie", self._cookie_header(self.cookie, sid, self.cfg.max_s))])
            return
        self.audit("sign_in_denied", user=who, reason=code)
        if code == "locked":
            self._send(h, 429, {"error": "locked", "retry_after_s": retry,
                                "message": f"Too many attempts. Try again in {max(1, retry // 60)} minute(s)."})
            return
        status, msg = self._LOGIN_FAIL.get(code, (401, "Invalid username or password."))
        err = {"bad": "invalid_credentials", "domain": "domain_not_allowed"}.get(code, code)
        self._send(h, status, {"error": err, "message": msg})

    # ---- accounts mode: request access (details -> emailed code -> pending request)
    _REG_STATUS = {"username_taken": 409, "email_taken": 409, "domain_not_allowed": 403, "rate_limited": 429,
                   "too_many": 429, "too_soon": 429, "too_many_codes": 429, "too_many_attempts": 429,
                   "expired": 410, "not_found": 410, "email_failed": 502, "registration_closed": 403,
                   "platform_controlled": 403, "claim_locked": 429, "invitation_closed": 410}

    def _reg_error(self, h, e: accts.AccountError, **extra):
        body = {"error": e.code, "message": e.message}
        body.update(extra)
        self._send(h, self._REG_STATUS.get(e.code, 400), body)

    def _registration_open(self, h) -> bool:
        if self.cfg.mode != "accounts" or not self.cfg.allow_registration:
            self._send(h, 404, {"error": "not_found"})
            return False
        return True

    def _check_domain(self, email: str):
        if not self.domains.list():
            raise accts.AccountError("registration_closed", "Access requests are not open. Contact a project administrator.")
        if not self.domains.allows(email):
            raise accts.AccountError("domain_not_allowed",
                                     "Use your work email address. This email domain cannot request access.")

    def _deliver_code(self, details: dict, code: str):
        minutes = self.cfg.verify_minutes
        if self.cfg.verify_delivery == "log":
            path = self.audit_path.parent / "verification-codes.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.clock()))}  "
                         f"{details['email']}  code {code}  (valid {minutes} min)\n")
            return
        try:
            self.notifier.send_code(details["email"], details["name"], code, minutes)
        except Exception as exc:  # noqa: BLE001
            self.audit("verification_failed", user=details["username"], reason=f"send: {type(exc).__name__}")
            raise accts.AccountError("email_failed", "The verification email could not be sent. Try again in a few "
                                                     "minutes, or contact a project administrator.")

    def _verify_payload(self, vid, email, rec):
        now = self.clock()
        return {"state": "verify", "id": vid, "email": accts.mask_email(email),
                "expires_in_s": max(0, int(rec["expires"] - now)),
                "resend_in_s": max(0, int(rec["last_sent"] + self.verifications.resend_after_s - now)),
                "minutes": self.cfg.verify_minutes}

    def _register_start(self, h):
        if not self._registration_open(h):
            return
        b = self._json(h)
        email = str(b.get("email", "")).strip()
        try:
            if not accts.EMAIL.match(email):
                raise accts.AccountError("bad_email", "Enter a valid work email address.")
            self._check_domain(email)                   # before anything else: outsiders learn nothing about users
            username, name, email = self.accounts.validate_request(b.get("username"), b.get("name"), email,
                                                                   str(b.get("password", "")))
            details = {"username": username, "name": name, "email": email,
                       "pw": accts.hash_password(str(b.get("password", ""))),
                       "reason": str(b.get("reason") or "").strip()[:500]}
            vid, code, rec = self.verifications.start(details)
            try:
                self._deliver_code(details, code)
            except accts.AccountError:
                self.verifications.cancel(vid)       # a code nobody received must not stay valid
                raise
        except accts.AccountError as e:
            self.audit("registration_refused", email_domain=accts.email_domain(email) or None, reason=e.code)
            self._reg_error(h, e)
            return
        self.audit("verification_sent", user=username, email=email)
        self._send(h, 202, self._verify_payload(vid, email, rec))

    def _register_resend(self, h, claim=False):
        if claim:
            if not self._claim_enabled(h):
                return
        elif not self._registration_open(h):
            return
        b = self._json(h)
        vid = str(b.get("id", ""))
        try:
            code, rec = self.verifications.resend(vid)
            self._deliver_code(rec["details"], code)
        except accts.AccountError as e:
            info = self.verifications.info(vid)
            extra = {"resend_in_s": max(0, int(info["last_sent"] + self.verifications.resend_after_s - self.clock()))} if info else {}
            self._reg_error(h, e, **extra)
            return
        self.audit("verification_sent", user=rec["details"]["username"], email=rec["details"]["email"], resend=True)
        self._send(h, 200, self._verify_payload(vid, rec["details"]["email"], rec))

    def _register_verify(self, h):
        if not self._registration_open(h):
            return
        b = self._json(h)
        try:
            d = self.verifications.check(str(b.get("id", "")), str(b.get("code", "")))
        except accts.AccountError as e:
            self.audit("verification_failed", reason=e.code)
            self._reg_error(h, e)
            return
        if d.get("claim"):                              # a code issued for an invitation cannot create an ordinary request
            self._send(h, 400, {"error": "invalid_code", "message": "That code belongs to a different step."})
            return
        try:
            self._check_domain(d["email"])               # the domain may have been removed while the code was out
            rec = self.accounts.register(d["username"], d["name"], d["email"], reason=d["reason"], pw_hash=d["pw"])
        except accts.AccountError as e:
            self._reg_error(h, e)
            return
        self.audit("email_verified", user=rec["username"], email=rec["email"])
        self.audit("registration_requested", user=rec["username"], email=rec["email"])
        self.notifier.request_submitted(rec)
        self._send(h, 201, {"state": "pending",
                            "message": "Your request has been sent. A project administrator will review it; you can "
                                       "sign in once it is approved."})

    # ---- platform mode: claiming the first-administrator invitation
    def _has_admin(self) -> bool:
        return bool(self.accounts and self.accounts.active_admins())

    def _claim_invite(self):
        """The invitation a person could claim right now, or None."""
        if not (self.trust and self.cfg.mode == "accounts"):
            return None
        return self.trust.claimable_invite(self._has_admin())

    def _claim_enabled(self, h) -> bool:
        if self.cfg.mode != "accounts" or not self.trust:
            self._send(h, 404, {"error": "not_found"})
            return False
        return True

    def _claim_blocked(self) -> bool:
        now = self.clock()
        self._claim_fails = [t for t in self._claim_fails if now - t < 900]
        return len(self._claim_fails) >= 10

    def _claim_page(self, h):
        if not self._claim_enabled(h):
            return
        import claim_page
        inv, proj = self._claim_invite(), self.trust.project()
        hint = f"You were invited to set up this console for {proj['name']}." if (inv and proj) else ""
        page, csp = claim_page.render(project_name=proj["name"] if proj else "", is_open=bool(inv),
                                      admin_hint=hint, min_password=self.cfg.min_password)
        self._send(h, 200, page, ctype="text/html", headers=[("Content-Security-Policy", csp)])

    def _claim_start(self, h):
        if not self._claim_enabled(h):
            return
        b = self._json(h)
        if self._claim_blocked():
            self._send(h, 429, {"error": "claim_locked",
                                "message": "Too many wrong invitation codes. Try again in 15 minutes."})
            return
        inv = self._claim_invite()
        if not inv or not self.trust.token_matches(inv, str(b.get("token", ""))):
            self._claim_fails.append(self.clock())
            self.audit("claim_refused", reason="invalid_invitation")
            self._send(h, 400, {"error": "invalid_invitation",
                                "message": "That invitation code is not valid, or it has expired. Check it, or ask the "
                                           "platform team for a new one."})
            return
        try:
            username, name, email = self.accounts.validate_request(b.get("username"), b.get("name"), inv["admin_email"],
                                                                   str(b.get("password", "")))
            details = {"username": username, "name": name, "email": email,
                       "pw": accts.hash_password(str(b.get("password", ""))), "reason": "invitation",
                       "claim": inv["invite_hash"]}
            vid, code, rec = self.verifications.start(details)
            try:
                self._deliver_code(details, code)
            except accts.AccountError:
                self.verifications.cancel(vid)
                raise
        except accts.AccountError as e:
            self.audit("claim_refused", reason=e.code)
            self._reg_error(h, e)
            return
        self.audit("claim_code_sent", user=username, project=inv["project_id"])
        self._send(h, 202, self._verify_payload(vid, email, rec))

    def _claim_verify(self, h):
        if not self._claim_enabled(h):
            return
        b = self._json(h)
        try:
            d = self.verifications.check(str(b.get("id", "")), str(b.get("code", "")))
        except accts.AccountError as e:
            self.audit("claim_verification_failed", reason=e.code)
            self._reg_error(h, e)
            return
        if not d.get("claim"):
            self._send(h, 400, {"error": "invalid_code", "message": "That code belongs to a different step."})
            return
        with self.claim_lock:
            inv = self._claim_invite()
            if not inv or inv["invite_hash"] != d["claim"]:
                self.audit("claim_refused", reason="invitation_closed")
                self._send(h, 410, {"error": "invitation_closed",
                                    "message": "This invitation is no longer open. Ask the platform team for a new one."})
                return
            try:
                rec = self.accounts.create_admin_with_hash(d["username"], d["name"], d["email"], d["pw"])
                self.trust.consume(inv["invite_hash"], by=rec["username"])
            except (accts.AccountError, pt.TrustError) as e:
                self._reg_error(h, accts.AccountError(getattr(e, "code", "bad_request"), getattr(e, "message", str(e))))
                return
        self.audit("project_claimed", user=rec["username"], project=inv["project_id"], purpose=inv["purpose"],
                   serial=inv["serial"])
        self._send(h, 201, {"state": "ready", "message": "Your console is ready. Sign in with your username and password."})

    # ---- administrator API (accounts mode)
    def _domain_rows(self):
        return self.domains.rows(self.accounts.count_active_in_domain)

    def _domain_payload(self):
        return {"control": self.domain_control, "project": self.trust.project() if self.trust else None,
                "domains": self._domain_rows()}

    def _admin_api(self, h, p, m):
        if p == "/auth/admin/requests" and m == "GET":
            if self._need_manager(h):
                self._send(h, 200, {"requests": self.accounts.pending()})
        elif p == "/auth/admin/users" and m == "GET":
            if self._need_manager(h):
                self._send(h, 200, {"users": [u for u in self.accounts.list_users() if u["status"] != "pending"]})
        elif p == "/auth/admin/domains" and m == "GET":
            if self._need_manager(h):
                self._send(h, 200, self._domain_payload())
        elif p == "/auth/admin/domains" and m == "POST":
            s = self._need_manager(h)
            if s:
                b = self._json(h)
                action, value = str(b.get("action", "")), str(b.get("domain", ""))
                platform = self.domain_control == "platform"
                try:
                    if action == "add":
                        d = self.domains.add(value)
                        self.audit("domain_added", domain=d, by=s["user"])
                    elif action == "remove":
                        d = value.strip().lower().lstrip("@")
                        affected = self.accounts.count_active_in_domain(d)
                        if not self.domains.remove(d):
                            raise accts.AccountError("not_found", "That domain is not on the list.")
                        self.audit("domain_paused" if platform else "domain_removed", domain=d, by=s["user"],
                                   affected_users=affected)
                    elif action == "restore":
                        d = value.strip().lower().lstrip("@")
                        if not self.domains.restore(d):
                            raise accts.AccountError("not_found", "That domain is not paused.")
                        self.audit("domain_resumed", domain=d, by=s["user"])
                    else:
                        raise accts.AccountError("bad_action", "Action must be add, remove or restore.")
                except accts.AccountError as e:
                    self._send(h, 403 if e.code == "platform_controlled" else 400, {"error": e.code, "message": e.message})
                    return
                self._send(h, 200, dict(self._domain_payload(), ok=True))
        elif p == "/auth/admin/grants" and m == "POST":
            s = self._need_manager(h)
            if s:
                b = self._json(h)
                if not self.trust:
                    self._send(h, 400, {"error": "not_platform_mode",
                                        "message": "This console is not managed by the platform team."})
                    return
                try:
                    summary = self.trust.apply(str(b.get("grant", "")))
                except pt.TrustError as e:
                    self.audit("grant_refused", by=s["user"], reason=e.code)
                    self._send(h, 400, {"error": e.code, "message": e.message})
                    return
                self.audit("grant_applied", by=s["user"], kind=summary["kind"], serial=summary["serial"],
                           already=summary["already"])
                self._send(h, 200, dict(self._domain_payload(), ok=True, summary=summary))
        elif p == "/auth/admin/decide" and m == "POST":
            s = self._need_manager(h)
            if s:
                b = self._json(h)
                decision = str(b.get("decision", ""))
                try:
                    target = self.accounts.get(str(b.get("username", "")))
                    if decision == "approve" and target and not self.domains.allows(target.get("email", "")):
                        raise accts.AccountError("domain_not_allowed", "This person's email domain is not on the allowed "
                                                                       "list. Ask for the domain to be added, or reject "
                                                                       "the request.")
                    rec = self.accounts.decide(b.get("username", ""), decision, by=s["user"], role=b.get("role"))
                except accts.AccountError as e:
                    self._send(h, 409 if e.code in ("not_pending", "domain_not_allowed") else 400,
                               {"error": e.code, "message": e.message})
                    return
                self.audit("access_approved" if decision == "approve" else "access_rejected", user=rec["username"],
                           by=s["user"], role=rec.get("role"))
                self.notifier.decided(rec, decision, s["user"])
                self._send(h, 200, {"ok": True, "user": rec})
        elif p == "/auth/admin/user" and m == "POST":
            s = self._need_manager(h)
            if s:
                b = self._json(h)
                try:
                    rec = self.accounts.set_user(b.get("username", ""), by=s["user"], role=b.get("role"),
                                                 disabled=b.get("disabled"), admin=b.get("admin"))
                except accts.AccountError as e:
                    self._send(h, 400, {"error": e.code, "message": e.message})
                    return
                self.audit("user_changed", user=rec["username"], by=s["user"], role=rec.get("role"),
                           status=rec["status"], manager=bool(rec.get("admin")))
                self._send(h, 200, {"ok": True, "user": rec})
        else:
            self._send(h, 404, {"error": "not_found"})

    def _admin_page(self, h):
        if self.cfg.mode != "accounts":
            self._send(h, 404, {"error": "not_found"})
            return
        s = self.current_user(h)
        if not s:
            self._send(h, 302, headers=[("Location", "/auth/signin?next=" + quote("/auth/admin", safe=""))])
            return
        if not s.get("admin"):
            self._send(h, 403, "Only a project administrator can open this page.", ctype="text/plain")
            return
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self._send(h, 200, ADMIN_HTML.replace("__NONCE__", nonce), ctype="text/html",
                   headers=[("Content-Security-Policy", csp)])

    # ---- sign-in page
    def _signin_page(self, h, nxt):
        """The page itself lives in signin_page.py (it also builds the matching Content-Security-Policy).
        A platform-mode console that has no administrator yet sends the visitor to the invitation claim page."""
        if self._claim_invite() and not self._has_admin():
            self._send(h, 302, headers=[("Location", "/auth/claim")])
            return
        import signin_page
        page, csp = signin_page.render(self.cfg.mode, nxt, registration=self.cfg.allow_registration,
                                       min_password=self.cfg.min_password, code_minutes=self.cfg.verify_minutes)
        self._send(h, 200, page, ctype="text/html", headers=[("Content-Security-Policy", csp)])


def install(hub, port):
    """Build the gate for a running console. Raises AuthConfigError if the settings are unsafe."""
    return AuthGate(AuthConfig(), Path(hub.logs) / "run-console" / "auth-audit.jsonl",
                    port=port, console_token=hub.token)


def record(handler, event, **fields):
    """Audit a decision (used for Approve / Reject). No-op when auth is not installed."""
    gate = getattr(getattr(handler, "server", None), "auth", None)
    if gate is not None:
        u = getattr(handler, "user", None) or {}
        gate.audit(event, user=u.get("user"), role=u.get("role"), **fields)


# --------------------------------------------------------------------------- start-up check
def startup_check(agent_dir):
    """Raise RuntimeError with a plain-English fix if the console could not read its secrets or could not sign
    anyone in. Called before the console or the background service starts."""
    import importlib.util
    import secret_store
    env_path = Path(agent_dir).resolve().parent / ".env"
    for k, v in secret_store.read_env_file(env_path).items():
        os.environ.setdefault(k, v)                       # same rule as the console's own .env loader
    try:
        secret_store.inject(required=True)
    except secret_store.SecretStoreError as exc:
        raise RuntimeError(f"Secrets: {exc}")
    try:
        cfg = AuthConfig()
    except AuthConfigError as exc:
        raise RuntimeError(f"Sign-in settings: {exc}. For a laptop use CONSOLE_AUTH_MODE=windows; for a shared "
                           "console CONSOLE_AUTH_MODE=accounts; free single-user demo: CONSOLE_AUTH_MODE=local and "
                           "CONSOLE_AUTH_ALLOW_LOCAL=true, then python console_auth.py init")
    if cfg.mode == "accounts":
        store = accts.AccountStore(cfg.accounts_file)
        if store.error:
            raise RuntimeError(f"Sign-in: {store.error}")
        trust = pt.InstanceTrust(cfg.data_dir, cfg.platform_key) if cfg.platform_key else None
        waiting_for_claim = False
        if not store.active_admins():
            if trust is None:
                raise RuntimeError(f"Sign-in: {cfg.accounts_file} has no active project administrator. Create the first "
                                   "one with: python console_auth.py bootstrap-admin --user <name> --email <address>")
            status, why = trust.invite_status(False)
            if status != "open":
                raise RuntimeError(f"Sign-in: this console has no project administrator and no open invitation ({why}). "
                                   "Ask the platform team for a project grant, then run: "
                                   "python console_auth.py grant apply <file>")
            waiting_for_claim = True
        if trust is not None:
            if trust.error:
                raise RuntimeError(f"Sign-in: {trust.error}. Ask the platform team for a fresh grant file.")
            dom = pt.PlatformDomains(trust, cfg.data_dir / "domain-pauses.json")
        else:
            dom = accts.DomainPolicy(cfg.domains_file)
        if dom.error:
            raise RuntimeError(f"Sign-in: {dom.error}")
        if not dom.list() and not waiting_for_claim:
            raise RuntimeError("Sign-in: no email domains are allowed, so only project administrators could sign in. " +
                               ("Ask the platform team for a domain update (or resume a paused domain)."
                                if trust is not None else
                                "Add one: python console_auth.py domains add <your-domain>   (for example deloitte.ca)"))
        if (cfg.allow_registration or waiting_for_claim) and cfg.verify_delivery == "email":
            if not accts.Notifier(None, cfg.env).enabled:
                raise RuntimeError("Sign-in: verification codes are sent by email, but email is not set "
                                   "up. Set CONSOLE_SMTP_HOST and CONSOLE_SMTP_FROM (see SMTP settings), or for local "
                                   "testing CONSOLE_VERIFY_DELIVERY=log, or turn requests off with "
                                   "CONSOLE_ALLOW_REGISTRATION=false")
        return cfg
    if cfg.mode == "windows" and os.name != "nt":
        raise RuntimeError("Sign-in: CONSOLE_AUTH_MODE=windows only works on Windows. Use CONSOLE_AUTH_MODE=accounts "
                           "(username and password with administrator approval) for Docker or a server.")
    if cfg.mode == "entra" and importlib.util.find_spec("msal") is None:
        raise RuntimeError("Sign-in: the 'msal' package is missing. Run: pip install msal")
    al = AccessList(cfg.access_file)
    if al.error:
        raise RuntimeError(f"Sign-in: {al.error}. Create it with: python console_auth.py init")
    if not any(u["role"] == "approver" for u in al.users()):
        raise RuntimeError(f"Sign-in: {cfg.access_file} has no approver. Run: python console_auth.py add --role approver ...")
    return cfg


# --------------------------------------------------------------------------- page additions
UI_CSS = """
.aubar{display:flex;gap:14px;align-items:center;margin:0 0 14px;padding:12px 16px;border-radius:12px;background:var(--amber-bg);color:var(--amber-ink);font-weight:650;font-size:13px}
.aubar span{flex:1}.ok-btn:disabled,.no-btn:disabled{opacity:.45;cursor:not-allowed}
"""

UI_JS = r"""
(function(){
try{
  var bar=document.querySelector('.topbar'),main=document.querySelector('main'),theme=document.getElementById('bTheme');
  if(!bar||!main||typeof TOKEN==='undefined')return;
  var RANK={viewer:0,operator:1,approver:2},AU={me:null,lost:false};
  var chip=document.createElement('span');chip.className='chip';chip.id='auChip';chip.innerHTML='<i></i><span id="auT">Checking sign-in\u2026</span>';
  var adm=document.createElement('button');adm.className='btn';adm.id='bAdm';adm.hidden=true;adm.textContent='Access requests';
  var out=document.createElement('button');out.className='btn';out.id='bOut';out.textContent='Sign out';out.hidden=true;
  bar.insertBefore(chip,theme);bar.insertBefore(adm,theme);bar.insertBefore(out,theme);
  var band=document.createElement('div');band.className='aubar';band.id='auBar';band.hidden=true;main.insertBefore(band,main.firstChild);
  function setText(el,v){if(el&&el.textContent!==v)el.textContent=v}
  function signin(){location.href='/auth/signin?next='+encodeURIComponent('/')}
  function showBand(msg){band.hidden=false;if(band.dataset.m!==msg){band.dataset.m=msg;band.innerHTML='<span></span><button class="ok-btn" id="auGo">Sign in</button>';band.firstChild.textContent=msg}}
  function lock(el,need,why){if(el&&r<need){el.disabled=true;el.title=why}}
  var r=-1;
  function paint(){
    var c=document.getElementById('auChip'),t=document.getElementById('auT');if(!c)return;
    r=AU.me?RANK[AU.me.role]:-1;
    if(AU.me){c.className='chip ok';setText(t,(AU.me.name||AU.me.user)+' \u00b7 '+AU.me.role+(AU.me.admin?' \u00b7 administrator':''));c.title='Signed in as '+AU.me.user+(AU.me.project?' \u00b7 '+AU.me.project:'');out.hidden=false;
      adm.hidden=!AU.me.admin;if(AU.me.admin){var n=AU.me.pending||0;setText(adm,'Access requests'+(n?' ('+n+')':''));adm.className=n?'btn ok-btn':'btn'}}
    else if(AU.lost){c.className='chip bad';setText(t,'Not signed in');out.hidden=true;adm.hidden=true}
    ['bStart','bStop','bPre','bDash'].forEach(function(id){lock(document.getElementById(id),1,'Needs the operator role')});
    document.querySelectorAll('[data-ans]').forEach(function(el){lock(el,2,'Needs the approver role')});
  }
  async function me(){
    try{
      var rs=await fetch('/auth/me',{headers:{'X-Console-Token':TOKEN},credentials:'same-origin'});
      if(rs.status===200){AU.me=await rs.json();AU.lost=false;band.hidden=true}
      else if(rs.status===401){AU.me=null;AU.lost=true;showBand('Your sign-in has ended. Sign in again to keep using the console.')}
    }catch(e){}
    paint();
  }
  var f0=window.fetch.bind(window);
  window.fetch=async function(input,init){
    var rs=await f0(input,init),u=typeof input==='string'?input:((input&&input.url)||'');
    if(u.indexOf('/api/')!==0&&u.indexOf(location.origin+'/api/')!==0)return rs;
    if(rs.status===401||rs.status===403){
      var j={};try{j=await rs.clone().json()}catch(e){}
      var msg=null;
      if(j.error==='reauth_required'){msg='Please sign in again to approve (your last sign-in was over an hour ago).';showBand(msg)}
      else if(j.error==='sign_in_required'){msg='Your sign-in has ended. Sign in again to continue.';AU.me=null;AU.lost=true;showBand(msg);paint()}
      else if(j.error==='forbidden'){msg='Your role ('+j.role+') cannot do this. It needs the '+j.need+' role.'}
      if(msg)return new Response(JSON.stringify({error:msg}),{status:rs.status,headers:{'Content-Type':'application/json'}});
    }
    return rs;
  };
  document.addEventListener('click',async function(e){
    if(e.target.closest('#auGo')){signin();return}
    if(e.target.closest('#bAdm')){location.href='/auth/admin';return}
    if(e.target.closest('#bOut')){try{await f0('/auth/logout',{method:'POST',headers:{'X-Console-Token':TOKEN},credentials:'same-origin'})}catch(x){}signin()}
  });
  var pend=0;new MutationObserver(function(){if(!pend)pend=setTimeout(function(){pend=0;paint()},0)}).observe(document.body,{childList:true,subtree:true});
  me();setInterval(me,20000);
}catch(e){console.error('sign-in panel not loaded',e)}
})();
"""


def ui_snippet() -> str:
    return f"<style>{UI_CSS}</style><script>{UI_JS}</script>"


def inject_ui(page: bytes) -> bytes:
    text = page.decode("utf-8")
    i = text.lower().rfind("</body>")
    snippet = ui_snippet()
    return (text[:i] + snippet + text[i:] if i >= 0 else text + snippet).encode("utf-8")


ADMIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Access management - AI Delivery Console</title>
<style nonce="__NONCE__">
[hidden]{display:none!important}
body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,Calibri,Arial,sans-serif;background:#f3f3f3;color:#1b1b1b;font-size:14px}
header{background:#111;color:#fff;padding:0 28px;height:52px;display:flex;gap:14px;align-items:center}
header .mk{width:24px;height:24px;border-radius:5px;background:#86BC25;display:grid;place-items:center}
header .mk svg{width:15px;height:15px}
header b{font-size:14px;font-weight:600} header span.sep{color:#666} header .proj{color:#bbb}
header a{color:#ddd;font-size:13px;margin-left:auto;text-decoration:none}
header a:hover{color:#fff;text-decoration:underline}
main{max-width:1040px;margin:28px auto;padding:0 24px}
h1{font-size:22px;font-weight:600;margin:0 0 4px} .lead{margin:0 0 22px;color:#5c5c5c}
section{background:#fff;border:1px solid #e1e1e1;border-radius:6px;margin:0 0 20px}
section h2{font-size:15px;font-weight:600;margin:0;padding:14px 18px;border-bottom:1px solid #ededed}
section h3{font-size:13px;font-weight:600;margin:0 0 6px}
section .hint{margin:0;padding:10px 18px 0;color:#5c5c5c;font-size:13px}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 18px;text-align:left;font-size:13px;border-bottom:1px solid #f0f0f0;vertical-align:top} th{color:#5c5c5c;font-size:12px;font-weight:600}
tr:last-child td{border-bottom:0}
button,select,input,textarea{font:inherit;font-size:13px;padding:6px 10px;border-radius:4px;border:1px solid #c8c8c8;background:#fff;cursor:pointer}
#nd{cursor:text;min-width:240px}
textarea{cursor:text;width:100%;box-sizing:border-box;min-height:90px;font-family:Consolas,"Cascadia Mono",monospace;font-size:12px}
input[type=checkbox]{width:16px;height:16px;vertical-align:-3px;margin:0 6px 0 0;cursor:pointer}
button.ok{background:#111;border-color:#111;color:#fff;font-weight:600} button.ok:hover{background:#333}
button.no{color:#a4262c;border-color:#d4a5a8} button.no:hover{background:#fdf3f4}
button:focus-visible,select:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid #86BC25;outline-offset:1px}
.addrow{display:flex;gap:8px;padding:14px 18px;border-top:1px solid #f0f0f0}
.upd{padding:14px 18px;border-top:1px solid #f0f0f0}.upd p{margin:0 0 8px;color:#5c5c5c;font-size:13px}.upd button{margin-top:8px}
#msg{margin:0 0 16px;font-size:13px;min-height:18px} #msg.err{color:#a4262c} #msg.ok{color:#2d6a0f} .mut{color:#5c5c5c}
p.empty{margin:0;padding:14px 18px;color:#5c5c5c}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:#e6f2d9;color:#2d6a0f}.pill.p{background:#fff4ce;color:#5c4400}
</style></head><body><header><span class="mk"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M8 21 13 16l-5-5M17 22h7" fill="none" stroke="#000" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg></span><b>AI Delivery Console</b><span class="sep">/</span><span>Access management</span><span class="proj" id="proj"></span><a id="back" href="/">Back to the console</a></header>
<main><h1>Access management</h1><p class="lead">Review access requests, manage people and the allowed email domains for this project.</p>
<div id="msg" role="status" aria-live="polite"></div>
<section><h2>Access requests</h2><div id="req"></div></section>
<section><h2>People</h2><div id="usr"></div></section>
<section><h2>Allowed email domains</h2>
<p class="hint" id="domHint"></p>
<div id="dom"></div>
<form class="addrow" id="fDom" autocomplete="off" hidden><label for="nd" hidden>Domain</label><input id="nd" placeholder="example.com" maxlength="253" spellcheck="false"><button class="ok" type="submit">Add domain</button></form>
<div class="upd" id="upd" hidden><h3>Update from the platform team</h3><p>Only the platform team can add a domain. Paste the update file they sent you (it is signed, so it cannot be changed or made up).</p>
<textarea id="grant" spellcheck="false" aria-label="Platform update file"></textarea><button class="ok" id="applyGrant" type="button">Apply update</button></div>
</section></main>
<script nonce="__NONCE__">
const TOKEN=new URLSearchParams(location.hash.slice(1)).get('t')||sessionStorage.getItem('rcTok')||'';
const H={'X-Console-Token':TOKEN,'Content-Type':'application/json'};
const $=id=>document.getElementById(id); const ROLES=['viewer','operator','approver'];
function say(t,c){const m=$('msg');m.textContent=t;m.className=c||'';}
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function table(head,rows,empty){
  if(!rows.length){return el('p',empty,'empty');}
  const t=el('table'),h=el('tr');head.forEach(x=>h.appendChild(el('th',x)));t.appendChild(h);rows.forEach(r=>t.appendChild(r));return t;}
function roleSelect(cur){const s=el('select');ROLES.forEach(r=>{const o=el('option',r);o.value=r;if(r===cur)o.selected=true;s.appendChild(o);});return s;}
async function call(path,method,body){
  const r=await fetch(path,{method,headers:H,credentials:'same-origin',body:body?JSON.stringify(body):undefined});
  const j=await r.json().catch(()=>({}));
  if(r.status===401){say(j.error==='reauth_required'?'Sign in again to continue (your last sign-in is over an hour old).':'Your sign-in has ended.','err');setTimeout(()=>location.href='/auth/signin?next='+encodeURIComponent('/auth/admin'),1500);throw new Error('auth');}
  if(!r.ok)throw new Error(j.message||j.error||('HTTP '+r.status));
  return j;}
async function decide(u,decision,role){
  try{await call('/auth/admin/decide','POST',{username:u,decision,role});say((decision==='approve'?'Approved ':'Rejected ')+u+'.','ok');load();}catch(e){if(e.message!=='auth')say(e.message,'err');}}
async function change(u,patch){
  try{await call('/auth/admin/user','POST',Object.assign({username:u},patch));say('Saved '+u+'.','ok');load();}catch(e){if(e.message!=='auth')say(e.message,'err');load();}}
let CONTROL='local';
async function domain(action,d,affected){
  const word=CONTROL==='platform'?'pause':'remove';
  if(action==='remove'&&affected>0&&!confirm(affected+' active user'+(affected===1?'':'s')+' on '+d+' will lose access at once. '+(word==='pause'?'Pause':'Remove')+' '+d+'?'))return;
  try{await call('/auth/admin/domains','POST',{action,domain:d});say((action==='add'?'Added ':action==='restore'?'Resumed ':(word==='pause'?'Paused ':'Removed '))+d+'.','ok');if(action==='add')$('nd').value='';load();}catch(e){if(e.message!=='auth')say(e.message,'err');}}
$('fDom').addEventListener('submit',e=>{e.preventDefault();const v=$('nd').value.trim();if(v)domain('add',v,0);});
$('applyGrant').addEventListener('click',async()=>{
  const g=$('grant').value.trim();if(!g){say('Paste the update file first.','err');return;}
  try{const j=await call('/auth/admin/grants','POST',{grant:g});say(j.summary.already?'That update was already applied.':'Update applied (serial '+j.summary.serial+').','ok');$('grant').value='';load();}
  catch(e){if(e.message!=='auth')say(e.message,'err');}});
async function load(){
  if(!TOKEN){say('Open this page from the console (the Access requests button).','err');return;}
  try{
    const [a,b,c]=await Promise.all([call('/auth/admin/requests','GET'),call('/auth/admin/users','GET'),call('/auth/admin/domains','GET')]);
    CONTROL=c.control;$('proj').textContent=c.project?' \u00b7 '+c.project.name:'';
    const rows=a.requests.map(r=>{const tr=el('tr'),sel=roleSelect('viewer');
      const who=el('td');who.appendChild(el('b',r.name));who.appendChild(el('div',r.email,'mut'));who.appendChild(el('div','username: '+r.username,'mut'));
      tr.appendChild(who);tr.appendChild(el('td',r.reason||'(no reason given)'));tr.appendChild(el('td',new Date((r.requested||0)*1000).toLocaleString()));
      const act=el('td');const ok=el('button','Approve','ok'),no=el('button','Reject','no');
      ok.addEventListener('click',()=>decide(r.username,'approve',sel.value));no.addEventListener('click',()=>decide(r.username,'reject'));
      act.appendChild(sel);act.appendChild(document.createTextNode(' '));act.appendChild(ok);act.appendChild(document.createTextNode(' '));act.appendChild(no);tr.appendChild(act);return tr;});
    $('req').replaceChildren(table(['Person','Reason','Requested','Role and decision'],rows,'No requests are waiting.'));
    const urows=b.users.map(u=>{const tr=el('tr'),sel=roleSelect(u.role);
      const who=el('td');who.appendChild(el('b',u.name));who.appendChild(el('div',u.email,'mut'));tr.appendChild(who);
      tr.appendChild(el('td',u.username));
      const rc=el('td');rc.appendChild(sel);sel.addEventListener('change',()=>change(u.username,{role:sel.value}));tr.appendChild(rc);
      const mc=el('td'),cb=el('input');cb.type='checkbox';cb.checked=!!u.admin;cb.addEventListener('change',()=>change(u.username,{admin:cb.checked}));mc.appendChild(cb);mc.appendChild(document.createTextNode(' project administrator'));tr.appendChild(mc);
      const sc=el('td'),dis=u.status==='disabled',bt=el('button',dis?'Enable':'Disable',dis?'ok':'no');bt.addEventListener('click',()=>change(u.username,{disabled:!dis}));
      sc.appendChild(el('span',dis?'disabled  ':'active  ','mut'));sc.appendChild(bt);tr.appendChild(sc);return tr;});
    $('usr').replaceChildren(table(['Person','Username','Role','Administrator','Status'],urows,'Nobody yet.'));
    const plat=c.control==='platform';
    $('domHint').textContent=plat?'These domains were approved by the platform team. You can pause a domain, which stops everyone on it from signing in, and resume it later. Only the platform team can add a domain. Administrators can always sign in.'
      :'Only email addresses on these domains can request access or sign in. Administrators can always sign in. Use *.example.com to allow every sub-domain.';
    $('fDom').hidden=plat;$('upd').hidden=!plat;
    const drows=c.domains.map(d=>{const tr=el('tr');tr.appendChild(el('td',d.domain));
      tr.appendChild(el('td',d.active_users+' active user'+(d.active_users===1?'':'s'),'mut'));
      const stc=el('td');if(plat){stc.appendChild(el('span',d.state==='paused'?'Paused':'Allowed','pill'+(d.state==='paused'?' p':'')));}tr.appendChild(stc);
      const ac=el('td');let bt;
      if(plat&&d.state==='paused'){bt=el('button','Resume','ok');bt.addEventListener('click',()=>domain('restore',d.domain,d.active_users));}
      else{bt=el('button',plat?'Pause':'Remove','no');bt.addEventListener('click',()=>domain('remove',d.domain,d.active_users));}
      ac.appendChild(bt);tr.appendChild(ac);return tr;});
    $('dom').replaceChildren(table(['Domain','People','',''],drows,plat?'The platform team has not approved any domain yet.':'No domains are allowed. Nobody can request access, and only administrators can sign in.'));
  }catch(e){if(e.message!=='auth')say(e.message,'err');}}
$('back').addEventListener('click',e=>{e.preventDefault();location.href='/';});
load();setInterval(load,30000);
</script></body></html>"""


# --------------------------------------------------------------------------- command line
def _prompt_password(label="Password") -> str:
    a = getpass.getpass(f"{label} (hidden): ")
    b = getpass.getpass("Repeat it: ")
    if a != b:
        raise accts.AccountError("mismatch", "The two passwords do not match.")
    return a


def _cli_env() -> dict:
    """Settings for command-line use: the real environment, then non-secret values from the repository's .env."""
    env = dict(os.environ)
    try:
        import secret_store
        for k, v in secret_store.read_env_file(Path(__file__).resolve().parent.parent / ".env").items():
            env.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    return env


def _cli(argv) -> int:
    ap = argparse.ArgumentParser(prog="console_auth.py", description="Console sign-in and access")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init")
    p.add_argument("--upn", default="")
    p.add_argument("--name", default="")
    p = sub.add_parser("add")
    p.add_argument("--upn", default="")
    p.add_argument("--windows", default="")
    p.add_argument("--role", choices=ROLES, default="viewer")
    p.add_argument("--name", default="")
    p = sub.add_parser("remove")
    p.add_argument("--upn", default="")
    p.add_argument("--windows", default="")
    sub.add_parser("list")
    sub.add_parser("check")
    sub.add_parser("project")
    p = sub.add_parser("grant")
    p.add_argument("action", choices=("apply", "show"))
    p.add_argument("file", nargs="?", default="")
    p = sub.add_parser("windows-test")
    p.add_argument("--method", choices=("auto", "hello", "password"), default=None)
    p = sub.add_parser("domains")
    p.add_argument("action", nargs="?", choices=("list", "add", "remove", "restore"), default="list")
    p.add_argument("domain", nargs="?", default="")
    p = sub.add_parser("bootstrap-admin")
    p.add_argument("--user", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--email", required=True)
    for alias in ("make-admin", "make-manager"):
        p = sub.add_parser(alias)
        p.add_argument("--user", required=True)
    sub.add_parser("accounts")
    sub.add_parser("requests")
    for name in ("approve", "reject", "disable", "enable", "reset-password"):
        p = sub.add_parser(name)
        p.add_argument("--user", required=True)
        if name == "approve":
            p.add_argument("--role", choices=ROLES, default="viewer")
    a = ap.parse_args(argv)
    path = default_access_file()
    cfg_env = _cli_env()

    def store():
        return accts.AccountStore(data_dir(cfg_env) / "accounts.json",
                                  min_len=_env_int(cfg_env, "CONSOLE_MIN_PASSWORD_LENGTH", 12))

    def trust():
        key = (cfg_env.get("CONSOLE_PLATFORM_PUBLIC_KEY") or "").strip()
        return pt.InstanceTrust(data_dir(cfg_env), key) if key else None

    def domains():
        t = trust()
        return pt.PlatformDomains(t, data_dir(cfg_env) / "domain-pauses.json") if t else \
            accts.DomainPolicy(data_dir(cfg_env) / "domains.json")

    try:
        if a.cmd == "init":
            if path.exists() and _read_users(path):
                print(f"[auth] {path} already exists. Use 'add' to change it.")
                return 1
            me = {"windows": local_identity(), "role": "approver", "name": a.name or getpass.getuser()}
            if a.upn:
                me["upn"] = a.upn.strip()
            _write_access(path, [me])
            print(f"[auth] created {path}\n       you are approver as {local_identity()}" + (f" / {a.upn}" if a.upn else ""))
            print("[auth] add to .env:\n         CONSOLE_AUTH_MODE=windows   (asks for your PIN / fingerprint / password)\n"
                  "       or CONSOLE_AUTH_MODE=local with CONSOLE_AUTH_ALLOW_LOCAL=true (no prompt)")
            return 0
        if a.cmd == "add":
            if not (a.upn or a.windows):
                print("[auth] give --upn and/or --windows")
                return 1
            users = _read_users(path)
            hit = next((u for u in users if (a.upn and u.get("upn", "").lower() == a.upn.lower())
                        or (a.windows and u.get("windows", "").lower() == a.windows.lower())), None)
            if hit is None:
                hit = {}
                users.append(hit)
            hit.update({k: v for k, v in (("upn", a.upn), ("windows", a.windows), ("name", a.name)) if v})
            hit["role"] = a.role
            _write_access(path, users)
            print(f"[auth] {a.upn or a.windows}: {a.role}")
            return 0
        if a.cmd == "remove":
            users = _read_users(path)
            keep = [u for u in users if not ((a.upn and u.get("upn", "").lower() == a.upn.lower())
                                             or (a.windows and u.get("windows", "").lower() == a.windows.lower()))]
            _write_access(path, keep)
            print(f"[auth] removed {len(users) - len(keep)} entry(ies)")
            return 0
        if a.cmd == "list":
            users = _read_users(path)
            print(f"[auth] {path}")
            for u in users:
                print(f"  {u.get('role', '?'):<9} {u.get('upn', '-'):<34} {u.get('windows', '-')}")
            return 0
        if a.cmd == "project":
            t = trust()
            if t is None:
                print("[auth] standalone console (no platform key): domains are managed locally. A project that the "
                      "platform team onboarded sets CONSOLE_PLATFORM_PUBLIC_KEY in .env.")
                return 0
            st = t.state()
            if st["error"]:
                print(f"[auth] FAIL {st['error']}")
                return 1
            print("[auth] platform-managed console")
            if st["project"]:
                print(f"  project   : {st['project']['name']}  ({st['project']['id']})   grants applied up to serial {st['serial']}")
            else:
                print("  project   : none yet - apply the project grant: python console_auth.py grant apply <file>")
            dp = domains()
            print(f"  domains   : {', '.join(dp.list()) or '(none)'}"
                  + (f"   paused: {', '.join(dp.paused())}" if dp.paused() else ""))
            has_admin = bool(store().active_admins())
            print(f"  admin     : {'active project administrator present' if has_admin else 'none yet'}")
            print(f"  invitation: {t.invite_status(has_admin)[1]}")
            return 0
        if a.cmd == "grant":
            t = trust()
            if t is None:
                print("[auth] FAIL set CONSOLE_PLATFORM_PUBLIC_KEY in .env first (the platform team gives you the key).")
                return 1
            if a.action == "show":
                for g in t.state()["grants"]:
                    extra = f"domains={','.join(g['domains'])}" if g["kind"] == "project-grant" else \
                        f"add={','.join(g['add'])} remove={','.join(g['remove'])}"
                    print(f"  serial {g['serial']:<3} {g['kind']:<14} {extra}")
                return 0
            if not a.file:
                print("[auth] usage: grant apply FILE")
                return 1
            try:
                summary = t.apply(Path(a.file).read_text(encoding="utf-8-sig"))
            except OSError as exc:
                print(f"[auth] FAIL cannot read {a.file}: {exc.strerror}")
                return 1
            except pt.TrustError as exc:
                print(f"[auth] FAIL {exc.message}")
                return 1
            print(f"[auth] {'already applied' if summary['already'] else 'applied'}: {summary['kind']} serial "
                  f"{summary['serial']} for {summary['project_id']}")
            return 0
        if a.cmd == "domains":
            dp = domains()
            platform = trust() is not None
            if a.action == "add":
                if not a.domain:
                    print("[auth] usage: domains add deloitte.ca")
                    return 1
                print(f"[auth] allowed: {dp.add(a.domain)}")
            elif a.action == "remove":
                if not a.domain:
                    print("[auth] usage: domains remove gov.bc.ca")
                    return 1
                n = store().count_active_in_domain(a.domain.strip().lower().lstrip("@"))
                if dp.remove(a.domain):
                    print(f"[auth] {'paused' if platform else 'removed'}: {a.domain}"
                          + (f"  ({n} active user(s) lose access now)" if n else ""))
                else:
                    print(f"[auth] {a.domain} is not currently allowed")
                    return 1
            elif a.action == "restore":
                if not a.domain:
                    print("[auth] usage: domains restore gov.bc.ca")
                    return 1
                if dp.restore(a.domain):
                    print(f"[auth] resumed: {a.domain}")
                else:
                    print(f"[auth] {a.domain} is not paused")
                    return 1
            items = dp.list()
            print(f"[auth] {'domains approved by the platform team' if platform else dp.path}")
            for d in items:
                print(f"  {d}")
            if platform and dp.paused():
                print("  paused: " + ", ".join(dp.paused()))
            if not items:
                print("  (none: nobody can request access, and only project administrators can sign in)")
            return 0
        if a.cmd == "windows-test":
            import windows_login
            method = a.method or (cfg_env.get("CONSOLE_WINDOWS_VERIFY") or "auto")
            print(f"[auth] opening the Windows sign-in prompt (method: {method})...")
            try:
                got = windows_login.WindowsVerifier(method)()
            except PermissionError as exc:
                print(f"[auth] FAIL {exc}")
                return 1
            ident = got["_windows"]
            al = AccessList(path)
            role = al.role_for(upn=ident) if "@" in ident else al.role_for(windows=ident)
            print(f"[auth] OK  Windows verified you as {ident} using {got['method']}")
            print(f"[auth] access list: {'role ' + role if role else 'NOT listed - run: python console_auth.py add --windows \"' + ident + '\" --role approver'}")
            return 0 if role else 1
        if a.cmd == "bootstrap-admin":
            if trust() is not None:
                print("[auth] FAIL this console is managed by the platform team: the first project administrator "
                      "claims an invitation at /auth/claim. Nobody sets another person's password.")
                return 1
            rec = store().bootstrap_admin(a.user, a.name, a.email, _prompt_password())
            print(f"[auth] created project administrator {rec['username']} (role approver, can manage access)")
            dp = domains()
            if not dp.list():
                print(f"[auth] allowed email domain: {dp.add(accts.email_domain(rec['email']))}")
            print("[auth] add to .env:  CONSOLE_AUTH_MODE=accounts")
            return 0
        if a.cmd in ("make-admin", "make-manager"):
            rec = store().make_admin(a.user)
            print(f"[auth] {rec['username']} is now an active project administrator (role approver)")
            return 0
        if a.cmd == "accounts":
            for u in store().list_users():
                print(f"  {u['status']:<9} {str(u.get('role')):<9} {'admin  ' if u.get('admin') else '       '} {u['username']:<20} {u['email']}")
            return 0
        if a.cmd == "requests":
            rows = store().pending()
            for u in rows:
                print(f"  {u['username']:<20} {u['email']:<32} {u.get('reason', '')[:60]}")
            print(f"[auth] {len(rows)} pending")
            return 0
        if a.cmd in ("approve", "reject"):
            rec = store().decide(a.user, a.cmd, by="cli", role=getattr(a, "role", None))
            print(f"[auth] {rec['username']}: {rec['status']}" + (f" as {rec['role']}" if rec.get("role") else ""))
            return 0
        if a.cmd in ("disable", "enable"):
            rec = store().set_user(a.user, by="cli", disabled=(a.cmd == "disable"))
            print(f"[auth] {rec['username']}: {rec['status']}")
            return 0
        if a.cmd == "reset-password":
            store().set_password(a.user, _prompt_password("New password"))
            print(f"[auth] password changed for {a.user}")
            return 0
        if a.cmd == "check":
            try:
                cfg = startup_check(Path(__file__).resolve().parent)
            except RuntimeError as exc:
                print(f"[auth] FAIL {exc}")
                return 1
            extra = ""
            if cfg.mode == "accounts":
                dp = domains()
                extra = (f"  domain control={'platform' if trust() else 'standalone'}  domains={','.join(dp.list())}"
                         f"  codes={cfg.verify_delivery}")
            print(f"[auth] OK  mode={cfg.mode}{extra}  secrets loaded (values not shown)")
            return 0
    except (accts.AccountError, pt.TrustError) as exc:
        print(f"[auth] FAIL {exc.message}")
        return 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
