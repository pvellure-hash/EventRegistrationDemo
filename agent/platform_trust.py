"""platform_trust.py - how a project instance trusts the platform team (v15).

The platform team signs small files ("grants"). An instance holds only the platform team's PUBLIC key, so it can
check a grant but can never create one. Nothing here needs a package: Ed25519 is implemented in pure Python
(RFC 8032) and is checked against the RFC test vectors and an independent library in the tests.

Two kinds of grant, both signed:
  project-grant   starts an instance (purpose "initial") or restores its administrator (purpose "recovery").
                  Carries the approved email domains and a single-use INVITATION for one named person.
                  The invitation code itself is never in the file, only its salted hash.
  domain-grant    adds or removes approved email domains later.

What an instance does with them
  * every grant is verified against the public key each time the grants file changes; a file with even one bad
    signature is refused as a whole and nobody but administrators can sign in (fail closed)
  * grants are applied in serial order; a grant whose serial is not higher than the last one is refused (no replays)
  * the first project-grant binds the instance to one project id; grants for another project are refused
  * the effective email domains come ONLY from grants. A project administrator can pause a domain (narrow access)
    and resume it, but cannot add one
  * an invitation can be claimed once, only before it expires, only by the person it names (their email must
    receive a code), and a newer grant replaces an older invitation

Honest limits: this controls what the console's screens and commands allow. Someone who owns the machine the
instance runs on can still edit its files or its public key; that is visible in the files and the audit log, and
is a matter for the hosting agreement, not for software.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import console_accounts as accts


class TrustError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# =========================================================================== Ed25519 (RFC 8032), pure Python
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    C, D = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _eq(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt):
    zi = _inv(pt[2])
    x, y = pt[0] * zi % _P, pt[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _h(s):
    return int.from_bytes(hashlib.sha512(s).digest(), "little") % _Q


def _expand(seed):
    if len(seed) != 32:
        raise TrustError("bad_key", "A signing key must be 32 bytes.")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_public(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def ed25519_sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _h(prefix + msg)
    rs = _compress(_mul(r, _G))
    s = (r + _h(rs + pub + msg) * a) % _Q
    return rs + int.to_bytes(s, 32, "little")


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    A, R = _decompress(pub), _decompress(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _Q:
        return False
    return _eq(_mul(s, _G), _add(R, _mul(_h(sig[:32] + pub + msg), A)))


# =========================================================================== signed envelopes
TAG = b"aidc-grant-v1\n"                      # separates grant signatures from any other use of the key
PROJECT_ID = re.compile(r"^prj-[a-z0-9]{8}$")
EMAIL = accts.EMAIL


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str, size: int | None = None) -> bytes:
    try:
        raw = base64.b64decode((text or "").strip().encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise TrustError("bad_key", "Not valid base64.")
    if size is not None and len(raw) != size:
        raise TrustError("bad_key", f"Expected {size} bytes.")
    return raw


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def generate_keypair() -> tuple[str, str]:
    """(private seed, public key), both base64. The private seed must only ever live in a secret store."""
    seed = secrets.token_bytes(32)
    return b64e(seed), b64e(ed25519_public(seed))


def public_from_seed(seed_b64: str) -> str:
    return b64e(ed25519_public(b64d(seed_b64, 32)))


def key_id(public_b64: str) -> str:
    return hashlib.sha256(b64d(public_b64, 32)).hexdigest()[:12]


def check_public_key(public_b64: str) -> str:
    b64d(public_b64, 32)
    return public_b64.strip()


def _domains(value, field, allow_empty=False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 50:
        raise TrustError("bad_grant", f"{field} must be a list of email domains.")
    out = []
    for d in value:
        try:
            n = accts.normalise_domain(str(d))
        except accts.AccountError:
            raise TrustError("bad_grant", f"{field} has an invalid domain: {d!r}")
        if n not in out:
            out.append(n)
    return out


def _text(p, field, lo, hi):
    v = p.get(field)
    if not isinstance(v, str) or not (lo <= len(v.strip()) <= hi):
        raise TrustError("bad_grant", f"{field} is missing or the wrong length.")
    return v.strip()


def validate_payload(p) -> dict:
    """Return a cleaned copy of a grant payload, or raise TrustError."""
    if not isinstance(p, dict) or p.get("v") != 1:
        raise TrustError("bad_grant", "This is not a platform grant (unknown version).")
    kind = p.get("kind")
    if kind not in ("project-grant", "domain-grant"):
        raise TrustError("bad_grant", "Unknown grant type.")
    if not isinstance(p.get("project_id"), str) or not PROJECT_ID.match(p["project_id"]):
        raise TrustError("bad_grant", "project_id is not valid.")
    serial = p.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 1:
        raise TrustError("bad_grant", "serial is not valid.")
    if not isinstance(p.get("issued_at"), (int, float)):
        raise TrustError("bad_grant", "issued_at is not valid.")
    out = {"v": 1, "kind": kind, "project_id": p["project_id"], "serial": serial, "issued_at": p["issued_at"],
           "issued_by": _text(p, "issued_by", 1, 120), "request_id": _text(p, "request_id", 1, 40)}
    if kind == "project-grant":
        if p.get("purpose") not in ("initial", "recovery"):
            raise TrustError("bad_grant", "purpose is not valid.")
        email = _text(p, "admin_email", 5, 200)
        if not EMAIL.match(email):
            raise TrustError("bad_grant", "admin_email is not valid.")
        ih = p.get("invite_hash")
        if not isinstance(ih, str) or not re.fullmatch(r"[0-9a-f]{64}", ih):
            raise TrustError("bad_grant", "invite_hash is not valid.")
        if not isinstance(p.get("invite_expires"), (int, float)):
            raise TrustError("bad_grant", "invite_expires is not valid.")
        out.update(purpose=p["purpose"], project_name=_text(p, "project_name", 3, 80),
                   client=str(p.get("client") or "").strip()[:80], domains=_domains(p.get("domains"), "domains"),
                   admin_email=email, admin_name=_text(p, "admin_name", 2, 80), invite_hash=ih,
                   invite_expires=p["invite_expires"])
    else:
        add = _domains(p.get("add", []), "add", allow_empty=True)
        rem = _domains(p.get("remove", []), "remove", allow_empty=True)
        if not add and not rem:
            raise TrustError("bad_grant", "A domain grant must add or remove something.")
        if set(add) & set(rem):
            raise TrustError("bad_grant", "A domain cannot be both added and removed.")
        out.update(add=add, remove=rem)
    return out


def sign_payload(seed_b64: str, payload: dict) -> dict:
    payload = validate_payload(payload)
    seed = b64d(seed_b64, 32)
    return {"payload": payload, "sig": b64e(ed25519_sign(seed, TAG + canonical(payload))),
            "key_id": key_id(b64e(ed25519_public(seed)))}


def verify_envelope(public_b64: str, env) -> dict:
    """The cleaned payload if the signature is valid for this public key, else TrustError."""
    if not isinstance(env, dict) or "payload" not in env or "sig" not in env:
        raise TrustError("bad_grant", "This is not a platform grant file.")
    pub = b64d(public_b64, 32)
    try:
        sig = b64d(str(env["sig"]), 64)
    except TrustError:
        raise TrustError("bad_signature", "The signature is not valid.")
    payload = validate_payload(env["payload"])
    if not ed25519_verify(pub, TAG + canonical(payload), sig):
        raise TrustError("bad_signature", "The signature does not match the platform team's key.")
    return payload


# =========================================================================== invitation codes
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: easy to read aloud and retype


def new_invite_token() -> str:
    """20 characters (100 bits), shown as XXXXX-XXXXX-XXXXX-XXXXX."""
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(20))
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def normalise_token(token: str) -> str:
    return re.sub(r"[\s-]+", "", str(token or "")).upper()


def invite_hash(project_id: str, token: str) -> str:
    return hashlib.sha256(f"{project_id}:{normalise_token(token)}".encode("utf-8")).hexdigest()


# =========================================================================== the instance side
def _stamp(path: Path):
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _atomic_json(path: Path, data):
    accts._atomic_write(path, data)


class InstanceTrust:
    """What this instance knows from the platform team: grants.json (verified every time it changes) and
    invites.json (which invitations were used). Needs only the platform team's public key."""

    def __init__(self, data_dir, public_key_b64: str, clock=time.time):
        self.pub = check_public_key(public_key_b64)
        self.dir = Path(data_dir)
        self.grants_path, self.invites_path = self.dir / "grants.json", self.dir / "invites.json"
        self.clock = clock
        self.lock = threading.RLock()
        self._stamp, self._state = None, None

    # ---- reading
    def _raw(self):
        try:
            return json.loads(accts.read_text(self.grants_path)).get("grants") or []
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError) as exc:
            raise TrustError("unreadable", f"grants file unreadable: {exc}")

    def _compute(self) -> dict:
        empty = {"error": None, "project": None, "domains": [], "serial": 0, "invite": None, "grants": []}
        try:
            payloads = [verify_envelope(self.pub, e) for e in self._raw()]
        except TrustError as exc:
            empty["error"] = f"grants file refused: {exc.message}"
            return empty
        if not payloads:
            return empty
        payloads.sort(key=lambda p: p["serial"])
        pid = payloads[0]["project_id"]
        serials = [p["serial"] for p in payloads]
        if payloads[0]["kind"] != "project-grant" or any(p["project_id"] != pid for p in payloads) \
                or len(set(serials)) != len(serials):
            empty["error"] = "grants file refused: grants are inconsistent"
            return empty
        domains: set[str] = set()
        for p in payloads:
            if p["kind"] == "project-grant":
                domains = set(p["domains"])
            else:
                domains = (domains | set(p["add"])) - set(p["remove"])
        projects = [p for p in payloads if p["kind"] == "project-grant"]
        last = projects[-1]
        return {"error": None, "project": {"id": pid, "name": last["project_name"], "client": last["client"]},
                "domains": sorted(domains), "serial": serials[-1], "invite": last, "grants": payloads}

    def state(self) -> dict:
        with self.lock:
            try:
                st = _stamp(self.grants_path)
            except OSError:
                st = None
            if self._state is None or st != self._stamp:
                self._state, self._stamp = self._compute(), st
            return self._state

    @property
    def error(self):
        return self.state()["error"]

    def project(self):
        return self.state()["project"]

    def effective_domains(self) -> list[str]:
        return list(self.state()["domains"])

    # ---- applying a grant
    def apply(self, text_or_dict) -> dict:
        """Verify a grant and add it. Returns a summary. Safe to repeat: re-applying the same grant is a no-op."""
        try:
            env = json.loads(text_or_dict) if isinstance(text_or_dict, (str, bytes)) else text_or_dict
        except ValueError:
            raise TrustError("bad_grant", "That is not a grant file (not valid JSON).")
        payload = verify_envelope(self.pub, env)
        with self.lock:
            st = self.state()
            if st["error"]:
                raise TrustError("unreadable", f"The grants file has a problem ({st['error']}); nothing was changed.")
            known = {g["serial"]: g for g in st["grants"]}
            if payload["serial"] in known:
                if canonical(known[payload["serial"]]) == canonical(payload):
                    return {"kind": payload["kind"], "serial": payload["serial"], "already": True,
                            "project_id": payload["project_id"]}
                raise TrustError("old_grant", "A different grant with this serial is already applied.")
            if payload["serial"] < st["serial"]:
                raise TrustError("old_grant", "This grant is older than one already applied. It was refused.")
            if st["project"] is None:
                if payload["kind"] != "project-grant" or payload["purpose"] != "initial":
                    raise TrustError("wrong_order", "Apply the project grant from the platform team first.")
            elif payload["project_id"] != st["project"]["id"]:
                raise TrustError("wrong_project", "This grant is for a different project.")
            elif payload["kind"] == "project-grant" and payload["purpose"] == "initial":
                pass                                   # a re-sent invitation for the same project
            raw = self._raw()
            raw.append(env)
            _atomic_json(self.grants_path, {"grants": raw})
            self._state = None
            return {"kind": payload["kind"], "serial": payload["serial"], "already": False,
                    "project_id": payload["project_id"]}

    # ---- invitations
    def _used(self) -> dict:
        try:
            return json.loads(accts.read_text(self.invites_path)).get("used") or {}
        except (OSError, ValueError, AttributeError):
            return {}

    def invite_status(self, has_admin: bool) -> tuple[str, str]:
        """(code, plain sentence): none | unreadable | expired | used | not_needed | open."""
        st = self.state()
        if st["error"]:
            return "unreadable", st["error"]
        p = st["invite"]
        if not p:
            return "none", "No project grant has been applied yet."
        if p["invite_hash"] in self._used():
            return "used", "The invitation has been used."
        if p["invite_expires"] <= self.clock():
            return "expired", "The invitation has expired. Ask the platform team for a new one."
        if p["purpose"] == "initial" and has_admin:
            return "not_needed", "This console already has an administrator."
        return "open", f"An invitation for {p['admin_email']} is waiting to be claimed."

    def claimable_invite(self, has_admin: bool):
        if self.invite_status(has_admin)[0] != "open":
            return None
        return dict(self.state()["invite"])

    @staticmethod
    def token_matches(invite: dict, token: str) -> bool:
        return hmac.compare_digest(invite_hash(invite["project_id"], token), invite["invite_hash"])

    def consume(self, ihash: str, by: str):
        with self.lock:
            used = self._used()
            if ihash in used:
                raise TrustError("used", "The invitation has already been used.")
            used[ihash] = {"at": self.clock(), "by": by}
            _atomic_json(self.invites_path, {"used": used})


class PlatformDomains:
    """The domain list in platform mode. Same interface as accts.DomainPolicy, different rules:
    the allowed domains come only from signed grants; an administrator can pause and resume one, never add one.
    A pause remembers the grant serial it was made at and ends by itself when the platform team next changes that
    domain, because that is a fresh decision."""

    def __init__(self, trust: InstanceTrust, pause_path):
        self.trust, self.path = trust, Path(pause_path)
        self.lock = threading.RLock()
        self._stamp, self._entries, self._error = None, [], None

    def _load(self):
        try:
            st = _stamp(self.path)
        except OSError:
            self._entries, self._stamp, self._error = [], None, None
            return
        if st == self._stamp:
            return
        try:
            raw = json.loads(accts.read_text(self.path)).get("paused") or []
            out = []
            for e in raw:
                if isinstance(e, str):
                    out.append({"domain": e.lower(), "serial": 0})
                elif isinstance(e, dict) and isinstance(e.get("domain"), str):
                    out.append({"domain": e["domain"].lower(), "serial": int(e.get("serial") or 0)})
            self._entries, self._stamp, self._error = out, st, None
        except (ValueError, AttributeError, TypeError) as exc:
            self._entries, self._stamp, self._error = [], st, f"paused-domains file unreadable: {exc}"

    @property
    def error(self):
        with self.lock:
            self._load()
            return self.trust.error or self._error

    def granted(self) -> list[str]:
        return self.trust.effective_domains()

    def _touched_after(self, domain: str, serial: int) -> bool:
        for g in self.trust.state()["grants"]:
            if g["serial"] <= serial:
                continue
            if g["kind"] == "project-grant" or domain in g.get("add", []) or domain in g.get("remove", []):
                return True
        return False

    def _valid(self) -> list[dict]:
        granted = set(self.granted())
        return [e for e in self._entries if e["domain"] in granted and not self._touched_after(e["domain"], e["serial"])]

    def paused(self) -> list[str]:
        with self.lock:
            self._load()
            return sorted({e["domain"] for e in self._valid()})

    def list(self) -> list[str]:
        """The domains people may use right now: granted and not paused."""
        if self.error:
            return []                                   # fail closed
        paused = set(self.paused())
        return [d for d in self.granted() if d not in paused]

    def allows(self, email: str) -> bool:
        return any(accts.domain_matches(e, email) for e in self.list())

    def rows(self, count_users) -> list[dict]:
        paused = set(self.paused())
        return [{"domain": d, "active_users": count_users(d), "state": "paused" if d in paused else "allowed"}
                for d in self.granted()]

    def add(self, value: str) -> str:
        raise accts.AccountError("platform_controlled", "Only the platform team can add an email domain. "
                                                        "Ask them for a domain update, then apply it here.")

    def _write(self, entries: list[dict]):
        _atomic_json(self.path, {"paused": entries})
        self._stamp = None

    def remove(self, value: str) -> bool:
        """Pause a domain (people on it lose access at once). Returns False if it is not currently allowed."""
        d = (value or "").strip().lower().lstrip("@")
        with self.lock:
            self._load()
            if self._error:
                raise accts.AccountError("store_unreadable", "The paused-domains file is unreadable; not changing it.")
            if d not in self.list():
                return False
            self._write(self._valid() + [{"domain": d, "serial": self.trust.state()["serial"]}])
            return True

    def restore(self, value: str) -> bool:
        d = (value or "").strip().lower().lstrip("@")
        with self.lock:
            self._load()
            if self._error:
                raise accts.AccountError("store_unreadable", "The paused-domains file is unreadable; not changing it.")
            if d not in self.paused():
                return False
            self._write([e for e in self._valid() if e["domain"] != d])
            return True
