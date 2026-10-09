"""platform_admin.py - the platform team's tool for onboarding projects (v15). Not part of any project's instance.

What it does
  * keeps the register of projects and requests (projects.json) and the platform team's own audit log
    (platform-audit.jsonl, hash-chained so a changed or deleted line is detectable)
  * enforces the onboarding rules: a project is verified by people other than the one who asked, and by at least
    PLATFORM_MIN_VERIFIERS of them, before anything is signed
  * signs the grant files an instance trusts (see platform_trust.py) and writes the first administrator's
    invitation. The invitation code appears only in that one text file; the register keeps just a hash

The lifecycle of a request
  new-project / change-domains / recover-admin   ->  pending  ->  verify (by others)  ->  issue  ->  issued
  Anyone can be named on the roster (PLATFORM_TEAM); with no roster the tool trusts the signed-in Windows account.

Setup (once, on the platform team's own machine)
  python platform_admin.py keygen                    creates the signing key in your secret store, prints the PUBLIC key
  python platform_admin.py public-key                shows it again (instances put it in .env as CONSOLE_PLATFORM_PUBLIC_KEY)

Onboard a project
  python platform_admin.py new-project --name "Ministry X intake" --client "Ministry X" ^
        --lead-email lead@gov.bc.ca --lead-name "Lead Person" --domain gov.bc.ca --hosting client-cloud
  python platform_admin.py verify REQ-ID --note "Confirmed with the engagement lead"     (a different person)
  python platform_admin.py issue  REQ-ID --out .\\grants --console-url https://console.example.com
        -> grants\\prj-xxxxxxxx\\project-grant-1.json   (install on the instance: console_auth.py grant apply ...)
           grants\\prj-xxxxxxxx\\invitation-1.txt       (send to the lead; contains the single-use code)

Later
  change-domains --project ID --add d.example.ca [--remove old.example.ca] --reason "..."    then verify, issue
  recover-admin  --project ID --admin-email new@x.ca --reason "..."                          then verify, issue
  reinvite --project ID --out DIR        a fresh invitation if the first expired before it was used
  requests | projects | show ID | cancel REQ-ID | audit [--verify]

Settings (.env or environment; none are secrets):  PLATFORM_DATA_DIR, PLATFORM_MIN_VERIFIERS (default 1; use 2;
0 = one person only, for a demo: verification is waived and every grant_issued audit line says so),
PLATFORM_TEAM (comma-separated Windows identities allowed to act), CONSOLE_SMTP_* (for issue --send).
The signing key lives in the secret store as PLATFORM_SIGNING_SECRET (or that environment variable).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import console_accounts as accts
import platform_trust as pt

HOSTING = ("laptop", "client-cloud", "deloitte-cloud")
KEY_NAME = "PLATFORM_SIGNING_SECRET"


class PlatformError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def local_identity() -> str:
    dom = os.getenv("USERDOMAIN", "")
    return f"{dom}\\{getpass.getuser()}" if dom else getpass.getuser()


# --------------------------------------------------------------------------- the signing key
class SecretKeyring:
    """The platform signing key (private seed, base64) kept in the secret provider the machine is set up with."""

    def get(self):
        v = os.environ.get(KEY_NAME, "").strip()
        if v:
            return v
        try:
            import secret_store
            return secret_store.get(KEY_NAME)
        except Exception:  # noqa: BLE001
            return None

    def set(self, seed_b64: str):
        import secret_store
        dest = secret_store.store().primary
        secret_store._write_verified(dest, KEY_NAME, seed_b64)
        secret_store.store().forget(KEY_NAME)


class MemoryKeyring:
    def __init__(self, seed=None):
        self.seed = seed

    def get(self):
        return self.seed

    def set(self, seed_b64):
        self.seed = seed_b64


# --------------------------------------------------------------------------- the audit log (hash-chained)
class ChainedAudit:
    """One JSON line per event. Each line carries the hash of the line before it, so editing or deleting a line
    breaks every hash after it. `verify()` finds the first broken line. Secrets are never written here."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.lock = threading.Lock()

    @staticmethod
    def _hash(prev: str, entry: dict) -> str:
        return hashlib.sha256((prev + json.dumps(entry, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest()

    def _lines(self):
        try:
            return [ln for ln in accts.read_text(self.path, "utf-8").splitlines() if ln.strip()]
        except FileNotFoundError:
            return []

    def append(self, event: str, by: str, **fields):
        with self.lock:
            lines = self._lines()
            prev = json.loads(lines[-1])["hash"] if lines else "0" * 64
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "event": event, "by": by}
            entry.update({k: v for k, v in fields.items() if v is not None})
            entry["prev"] = prev
            entry["hash"] = self._hash(prev, entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            return entry

    def entries(self):
        return [json.loads(ln) for ln in self._lines()]

    def verify(self) -> tuple[bool, int, int | None]:
        """(ok, lines checked, first bad line number or None)."""
        prev, n = "0" * 64, 0
        for i, ln in enumerate(self._lines(), 1):
            try:
                e = json.loads(ln)
                claimed = e.pop("hash")
            except (ValueError, KeyError):
                return False, n, i
            if e.get("prev") != prev or self._hash(prev, e) != claimed:
                return False, n, i
            prev, n = claimed, n + 1
        return True, n, None


# --------------------------------------------------------------------------- the platform
class Platform:
    def __init__(self, data_dir, keyring, identity=local_identity, clock=time.time, min_verifiers=1,
                 team=None, sender=None):
        self.dir = Path(data_dir)
        self.keyring, self.identity, self.clock = keyring, identity, clock
        self.min_verifiers = max(0, int(min_verifiers))      # 0 = sole operator: verification waived, and the audit log says so
        self.team = [t.strip().lower() for t in (team or []) if t.strip()]
        self.sender = sender                       # callable(to, subject, body) used by --send
        self.registry_path = self.dir / "projects.json"
        self.audit = ChainedAudit(self.dir / "platform-audit.jsonl", clock)
        self.lock = threading.RLock()

    # ---- register
    def _load(self) -> dict:
        try:
            data = json.loads(accts.read_text(self.registry_path, "utf-8"))
        except FileNotFoundError:
            return {"projects": {}, "requests": {}}
        except ValueError as exc:
            raise PlatformError("unreadable", f"The register is unreadable ({exc}); refusing to change it.")
        data.setdefault("projects", {})
        data.setdefault("requests", {})
        return data

    def _save(self, data: dict):
        accts._atomic_write(self.registry_path, data)

    def _who(self) -> str:
        who = self.identity()
        if self.team and who.lower() not in self.team:
            raise PlatformError("not_on_team", f"{who} is not on the platform team roster (PLATFORM_TEAM).")
        return who

    @staticmethod
    def _new_id(prefix: str, existing, nbytes: int) -> str:
        while True:
            v = f"{prefix}-{secrets.token_hex(nbytes)}"
            if v not in existing:
                return v

    # ---- keys
    def create_keys(self, force=False) -> str:
        who = self._who()
        if self.keyring.get() and not force:
            raise PlatformError("key_exists", "A signing key already exists. Replacing it would make every existing "
                                              "instance reject your grants; use --force only if you mean to rotate it.")
        seed, pub = pt.generate_keypair()
        self.keyring.set(seed)
        self.audit.append("keys_created", who, key_id=pt.key_id(pub), replaced=bool(force) or None)
        return pub

    def _seed(self) -> str:
        seed = self.keyring.get()
        if not seed:
            raise PlatformError("no_key", "There is no signing key on this machine. Run: python platform_admin.py keygen")
        return seed

    def public_key(self) -> str:
        return pt.public_from_seed(self._seed())

    # ---- requests
    def _check_lead(self, email: str, domains: list[str]):
        if not accts.EMAIL.match(email or ""):
            raise PlatformError("bad_email", "Enter a valid email address for the administrator.")
        if not any(accts.domain_matches(d, email) for d in domains):
            raise PlatformError("domain_mismatch", "The administrator's email domain must be one of the project's "
                                                   "allowed domains.")

    @staticmethod
    def _clean_domains(values) -> list[str]:
        out = []
        for v in values or []:
            try:
                d = accts.normalise_domain(v)
            except accts.AccountError as e:
                raise PlatformError("bad_domain", e.message)
            if d not in out:
                out.append(d)
        return out

    def new_project(self, name, client, lead_email, lead_name, domains, hosting="client-cloud", notes="") -> tuple[dict, dict]:
        who = self._who()
        name, client, lead_name = (name or "").strip(), (client or "").strip(), " ".join((lead_name or "").split())
        if not 3 <= len(name) <= 80:
            raise PlatformError("bad_name", "The project name must be 3-80 characters.")
        if not 2 <= len(client) <= 80:
            raise PlatformError("bad_client", "Enter the client or organization (2-80 characters).")
        if not 2 <= len(lead_name) <= 80:
            raise PlatformError("bad_lead", "Enter the lead's full name.")
        if hosting not in HOSTING:
            raise PlatformError("bad_hosting", "Hosting must be one of: " + ", ".join(HOSTING))
        doms = self._clean_domains(domains)
        if not 1 <= len(doms) <= 10:
            raise PlatformError("bad_domain", "Give 1-10 email domains for the project.")
        lead_email = (lead_email or "").strip()
        self._check_lead(lead_email, doms)
        with self.lock:
            data = self._load()
            for p in data["projects"].values():
                if p["name"].lower() == name.lower() and p["client"].lower() == client.lower():
                    raise PlatformError("duplicate", f"A project with that name already exists for {client}.")
            pid = self._new_id("prj", data["projects"], 4)           # prj- + 8 hex characters
            proj = {"id": pid, "name": name, "client": client, "hosting": hosting, "created": self.clock(),
                    "created_by": who, "lead_email": lead_email, "lead_name": lead_name, "domains": [], "serial": 0,
                    "status": "onboarding", "notes": (notes or "")[:500]}
            req = self._make_request(data, "onboard", pid, who, {"admin_email": lead_email, "admin_name": lead_name,
                                                                  "domains": doms})
            data["projects"][pid] = proj
            self._save(data)
        self.audit.append("project_requested", who, project=pid, request=req["id"], name=name,
                          domains=",".join(doms), hosting=hosting)
        return proj, req

    def _make_request(self, data, kind, pid, who, params) -> dict:
        rid = self._new_id("req", data["requests"], 3)
        req = {"id": rid, "kind": kind, "project_id": pid, "params": params, "requested_by": who,
               "requested_at": self.clock(), "verifications": [], "status": "pending"}
        data["requests"][rid] = req
        return req

    def _issued_project(self, data, pid):
        p = data["projects"].get(pid)
        if not p:
            raise PlatformError("no_project", f"No project {pid}.")
        if p["serial"] < 1:
            raise PlatformError("not_issued", "That project has not been issued its first grant yet.")
        return p

    def change_domains(self, project_id, add=(), remove=(), reason="") -> dict:
        who = self._who()
        add, rem = self._clean_domains(add), self._clean_domains(remove)
        if not add and not rem:
            raise PlatformError("nothing", "Give at least one domain to add or remove.")
        if not (reason or "").strip():
            raise PlatformError("reason", "Give a reason; it goes in the audit log.")
        with self.lock:
            data = self._load()
            p = self._issued_project(data, project_id)
            missing = [d for d in rem if d not in p["domains"]]
            if missing:
                raise PlatformError("not_allowed", f"Cannot remove {', '.join(missing)}: it is not an approved domain.")
            req = self._make_request(data, "domains", project_id, who, {"add": add, "remove": rem, "reason": reason.strip()[:300]})
            self._save(data)
        self.audit.append("domain_change_requested", who, project=project_id, request=req["id"],
                          add=",".join(add) or None, remove=",".join(rem) or None)
        return req

    def recover_admin(self, project_id, admin_email, admin_name, reason) -> dict:
        who = self._who()
        admin_name = " ".join((admin_name or "").split())
        if not 2 <= len(admin_name) <= 80:
            raise PlatformError("bad_lead", "Enter the new administrator's full name.")
        if not (reason or "").strip():
            raise PlatformError("reason", "Give a reason; it goes in the audit log.")
        with self.lock:
            data = self._load()
            p = self._issued_project(data, project_id)
            self._check_lead(admin_email, p["domains"])
            req = self._make_request(data, "recover", project_id, who, {"admin_email": admin_email.strip(),
                                                                         "admin_name": admin_name,
                                                                         "reason": reason.strip()[:300]})
            self._save(data)
        self.audit.append("recovery_requested", who, project=project_id, request=req["id"])
        return req

    def verify(self, request_id, note="") -> dict:
        who = self._who()
        with self.lock:
            data = self._load()
            req = data["requests"].get(request_id)
            if not req:
                raise PlatformError("no_request", f"No request {request_id}.")
            if req["status"] != "pending":
                raise PlatformError("not_pending", f"That request is {req['status']}.")
            if who.lower() == req["requested_by"].lower():
                raise PlatformError("self_verify", "You cannot verify a request you made. Ask a colleague.")
            if any(v["by"].lower() == who.lower() for v in req["verifications"]):
                raise PlatformError("already_verified", "You have already verified this request.")
            req["verifications"].append({"by": who, "at": self.clock(), "note": (note or "").strip()[:300]})
            self._save(data)
            n = len(req["verifications"])
        self.audit.append("request_verified", who, request=request_id, project=req["project_id"], count=n,
                          needed=self.min_verifiers)
        return req

    def cancel(self, request_id, reason="") -> dict:
        who = self._who()
        with self.lock:
            data = self._load()
            req = data["requests"].get(request_id)
            if not req:
                raise PlatformError("no_request", f"No request {request_id}.")
            if req["status"] != "pending":
                raise PlatformError("not_pending", f"That request is {req['status']}.")
            req["status"] = "cancelled"
            self._save(data)
        self.audit.append("request_cancelled", who, request=request_id, project=req["project_id"], reason=(reason or "")[:200] or None)
        return req

    # ---- issuing
    def _invitation_text(self, proj, payload, token, console_url, hours) -> str:
        expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(payload["invite_expires"]))
        where = (console_url.rstrip("/") + "/auth/claim") if console_url else "<the console address the platform team gave you>/auth/claim"
        purpose = ("the new project administrator for" if payload["purpose"] == "recovery" else "the first project administrator of")
        return (f"Subject: Your invitation to set up the AI Delivery Console for {proj['name']}\n"
                f"To: {payload['admin_email']}\n\n"
                f"Hello {payload['admin_name']},\n\n"
                f"The platform team has set up an AI Delivery Console for {proj['name']} and named you as {purpose} it.\n\n"
                f"  Your invitation code:  {token}\n\n"
                f"It can be used once and expires on {expires} ({hours} hours from issue).\n\n"
                f"What to do\n"
                f"  1. Open {where}\n"
                f"  2. Enter the invitation code, then choose your own username and password.\n"
                f"  3. Enter the 6-digit code the console sends to this email address.\n\n"
                f"Nobody on the platform team knows or sets your password. If you were not expecting this, ignore "
                f"this message; nothing happens without the code.\n")

    def _write(self, out_dir, pid, name, data) -> Path:
        folder = Path(out_dir) / pid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(data, encoding="utf-8")
        return path

    def _grant_invitation(self, data, proj, req_id, purpose, email, name, domains, out_dir, hours, console_url, send) -> dict:
        who = self.identity()
        serial = proj["serial"] + 1
        token = pt.new_invite_token()
        now = self.clock()
        payload = {"v": 1, "kind": "project-grant", "purpose": purpose, "project_id": proj["id"],
                   "project_name": proj["name"], "client": proj["client"], "serial": serial, "issued_at": now,
                   "issued_by": who, "request_id": req_id, "domains": domains, "admin_email": email,
                   "admin_name": name, "invite_hash": pt.invite_hash(proj["id"], token),
                   "invite_expires": now + hours * 3600}
        env = pt.sign_payload(self._seed(), payload)
        gpath = self._write(out_dir, proj["id"], f"project-grant-{serial}.json", json.dumps(env, indent=2))
        ipath = self._write(out_dir, proj["id"], f"invitation-{serial}.txt",
                            self._invitation_text(proj, payload, token, console_url, hours))
        proj["serial"], proj["domains"], proj["status"] = serial, domains, "invited"
        proj["lead_email"], proj["lead_name"] = email, name
        return {"serial": serial, "grant_file": str(gpath), "invitation_file": str(ipath), "to": email,
                "expires": payload["invite_expires"], "invite_prefix": payload["invite_hash"][:8],
                "subject_text": ipath.read_text(encoding="utf-8"), "kind": "project-grant", "purpose": purpose}

    def _maybe_send(self, who, result, send):
        if not send:
            return
        head, _, body = result.get("subject_text", "").partition("\n\n")
        subject = head.split("\n")[0].replace("Subject: ", "", 1)
        try:
            if not self.sender:
                raise RuntimeError("e-mail is not configured (CONSOLE_SMTP_HOST / CONSOLE_SMTP_FROM)")
            self.sender(result["to"], subject, body)
            result["sent"] = True
            self.audit.append("invitation_sent", who, to=result["to"], serial=result["serial"])
        except Exception as exc:  # noqa: BLE001
            result["sent"] = False
            result["send_error"] = f"{type(exc).__name__}: {exc}"
            self.audit.append("invitation_send_failed", who, to=result["to"], serial=result["serial"],
                              reason=type(exc).__name__)

    def issue(self, request_id, out_dir, hours=72, console_url="", send=False) -> dict:
        who = self._who()
        if not 1 <= int(hours) <= 336:
            raise PlatformError("bad_hours", "An invitation must last between 1 and 336 hours.")
        with self.lock:
            data = self._load()
            req = data["requests"].get(request_id)
            if not req:
                raise PlatformError("no_request", f"No request {request_id}.")
            if req["status"] != "pending":
                raise PlatformError("not_pending", f"That request is {req['status']}.")
            if len(req["verifications"]) < self.min_verifiers:
                raise PlatformError("not_verified", f"This request has {len(req['verifications'])} verification(s); "
                                                    f"{self.min_verifiers} needed, from people other than {req['requested_by']}.")
            proj = data["projects"][req["project_id"]]
            prm = req["params"]
            if req["kind"] == "onboard":
                result = self._grant_invitation(data, proj, request_id, "initial", prm["admin_email"], prm["admin_name"],
                                                prm["domains"], out_dir, int(hours), console_url, send)
            elif req["kind"] == "recover":
                result = self._grant_invitation(data, proj, request_id, "recovery", prm["admin_email"], prm["admin_name"],
                                                list(proj["domains"]), out_dir, int(hours), console_url, send)
            else:
                serial = proj["serial"] + 1
                payload = {"v": 1, "kind": "domain-grant", "project_id": proj["id"], "serial": serial,
                           "issued_at": self.clock(), "issued_by": who, "request_id": request_id,
                           "add": prm["add"], "remove": prm["remove"]}
                env = pt.sign_payload(self._seed(), payload)
                gpath = self._write(out_dir, proj["id"], f"domain-grant-{serial}.json", json.dumps(env, indent=2))
                proj["serial"] = serial
                proj["domains"] = sorted((set(proj["domains"]) | set(prm["add"])) - set(prm["remove"]))
                result = {"serial": serial, "grant_file": str(gpath), "kind": "domain-grant"}
            req["status"] = "issued"
            req["issued"] = {"serial": result["serial"], "at": self.clock(), "by": who, "file": result["grant_file"]}
            self._save(data)
        self.audit.append("grant_issued", who, project=proj["id"], request=request_id, kind=result["kind"],
                          serial=result["serial"], invite=result.get("invite_prefix"),
                          expires=result.get("expires"), verifiers=",".join(v["by"] for v in req["verifications"]),
                          verification_waived=True if self.min_verifiers == 0 else None)
        if result["kind"] == "project-grant":
            self._maybe_send(who, result, send)
        result.pop("subject_text", None)
        return result

    def reinvite(self, project_id, out_dir, hours=72, console_url="", send=False) -> dict:
        who = self._who()
        with self.lock:
            data = self._load()
            proj = self._issued_project(data, project_id)
            onboard = [r for r in data["requests"].values() if r["project_id"] == project_id and r["kind"] == "onboard"
                       and r["status"] == "issued"]
            if not onboard:
                raise PlatformError("no_onboarding", "This project has no issued onboarding request to repeat.")
            result = self._grant_invitation(data, proj, onboard[0]["id"], "initial", proj["lead_email"], proj["lead_name"],
                                            list(proj["domains"]), out_dir, int(hours), console_url, send)
            self._save(data)
        self.audit.append("invitation_reissued", who, project=project_id, serial=result["serial"],
                          invite=result["invite_prefix"], expires=result["expires"])
        self._maybe_send(who, result, send)
        result.pop("subject_text", None)
        return result

    # ---- reading
    def requests(self, open_only=True) -> list[dict]:
        data = self._load()
        return sorted((r for r in data["requests"].values() if not open_only or r["status"] == "pending"),
                      key=lambda r: r["requested_at"])

    def projects(self) -> list[dict]:
        return sorted(self._load()["projects"].values(), key=lambda p: p["name"].lower())

    def project(self, pid) -> tuple[dict, list[dict]]:
        data = self._load()
        if pid not in data["projects"]:
            raise PlatformError("no_project", f"No project {pid}.")
        return data["projects"][pid], sorted((r for r in data["requests"].values() if r["project_id"] == pid),
                                              key=lambda r: r["requested_at"])


# --------------------------------------------------------------------------- command line
def _env() -> dict:
    env = dict(os.environ)
    try:
        import secret_store
        for k, v in secret_store.read_env_file(Path(__file__).resolve().parent.parent / ".env").items():
            env.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    return env


def _default_dir(env) -> Path:
    return Path(env["PLATFORM_DATA_DIR"]) if env.get("PLATFORM_DATA_DIR") else Path.home() / ".ai-delivery-platform"


def make_platform(env=None, keyring=None, identity=local_identity, clock=time.time, sender=None) -> Platform:
    env = env if env is not None else _env()
    try:
        minv = int(env.get("PLATFORM_MIN_VERIFIERS") or 1)
    except ValueError:
        minv = 1
    if sender is None:
        notifier = accts.Notifier(None, env)
        sender = (lambda to, subject, body: notifier.send_plain(to, subject, body)) if notifier.enabled else None
    return Platform(_default_dir(env), keyring or SecretKeyring(), identity, clock, minv,
                    team=(env.get("PLATFORM_TEAM") or "").split(","), sender=sender)


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def main(argv=None, platform: Platform | None = None, out=print) -> int:
    ap = argparse.ArgumentParser(prog="platform_admin.py", description="Platform team: onboard and manage projects")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("keygen")
    p.add_argument("--force", action="store_true")
    sub.add_parser("public-key")
    p = sub.add_parser("new-project")
    p.add_argument("--name", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--lead-email", required=True)
    p.add_argument("--lead-name", required=True)
    p.add_argument("--domain", action="append", required=True)
    p.add_argument("--hosting", choices=HOSTING, default="client-cloud")
    p.add_argument("--notes", default="")
    p = sub.add_parser("change-domains")
    p.add_argument("--project", required=True)
    p.add_argument("--add", action="append", default=[])
    p.add_argument("--remove", action="append", default=[])
    p.add_argument("--reason", required=True)
    p = sub.add_parser("recover-admin")
    p.add_argument("--project", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-name", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("verify")
    p.add_argument("request")
    p.add_argument("--note", default="")
    p = sub.add_parser("issue")
    p.add_argument("request")
    p.add_argument("--out", required=True)
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--console-url", default="")
    p.add_argument("--send", action="store_true")
    p = sub.add_parser("reinvite")
    p.add_argument("--project", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--console-url", default="")
    p.add_argument("--send", action="store_true")
    p = sub.add_parser("cancel")
    p.add_argument("request")
    p.add_argument("--reason", default="")
    p = sub.add_parser("requests")
    p.add_argument("--all", action="store_true")
    sub.add_parser("projects")
    p = sub.add_parser("show")
    p.add_argument("project")
    p = sub.add_parser("audit")
    p.add_argument("--verify", action="store_true")
    a = ap.parse_args(argv)
    pl = platform or make_platform()
    try:
        if a.cmd == "keygen":
            pub = pl.create_keys(force=a.force)
            out("[platform] signing key created and stored in your secret provider (never printed).")
            out("[platform] PUBLIC key - put this in every instance's .env:")
            out(f"  CONSOLE_PLATFORM_PUBLIC_KEY={pub}")
            return 0
        if a.cmd == "public-key":
            out(f"CONSOLE_PLATFORM_PUBLIC_KEY={pl.public_key()}")
            return 0
        if a.cmd == "new-project":
            proj, req = pl.new_project(a.name, a.client, a.lead_email, a.lead_name, a.domain, a.hosting, a.notes)
            out(f"[platform] project {proj['id']} ({proj['name']}) requested: {req['id']}")
            if pl.min_verifiers == 0:
                out(f"[platform] verification is WAIVED on this machine (PLATFORM_MIN_VERIFIERS=0); the audit log records that. "
                    f"Next: python platform_admin.py issue {req['id']} --out <folder>")
            else:
                out(f"[platform] next: a colleague runs   python platform_admin.py verify {req['id']}   "
                    f"({pl.min_verifiers} verification(s) needed, not from you)")
            return 0
        if a.cmd == "change-domains":
            req = pl.change_domains(a.project, a.add, a.remove, a.reason)
            out(f"[platform] domain change requested: {req['id']}   next: verify, then issue")
            return 0
        if a.cmd == "recover-admin":
            req = pl.recover_admin(a.project, a.admin_email, a.admin_name, a.reason)
            out(f"[platform] administrator recovery requested: {req['id']}   next: verify (confirm the person with the "
                f"organization first), then issue")
            return 0
        if a.cmd == "verify":
            req = pl.verify(a.request, a.note)
            n = len(req["verifications"])
            out(f"[platform] {req['id']} verified by you ({n} of {pl.min_verifiers} needed)"
                + ("   next: issue" if n >= pl.min_verifiers else ""))
            return 0
        if a.cmd in ("issue", "reinvite"):
            if a.cmd == "issue":
                r = pl.issue(a.request, a.out, a.hours, a.console_url, a.send)
            else:
                r = pl.reinvite(a.project, a.out, a.hours, a.console_url, a.send)
            out(f"[platform] {r['kind']} serial {r['serial']} written: {r['grant_file']}")
            if r.get("invitation_file"):
                out(f"[platform] invitation for {r['to']} written: {r['invitation_file']}")
                out("[platform] the invitation code is in that file only; it is not stored anywhere else and is not shown here.")
                out(f"[platform] expires {_when(r['expires'])} local time. Install the grant on the instance: "
                    "python console_auth.py grant apply <file>")
                if a.send:
                    out("[platform] e-mail: " + ("sent" if r.get("sent") else f"NOT sent ({r.get('send_error')}); send the file yourself"))
            else:
                out("[platform] give the project administrator this file; they apply it under Access management > "
                    "Allowed email domains, or: python console_auth.py grant apply <file>")
            return 0
        if a.cmd == "cancel":
            pl.cancel(a.request, a.reason)
            out(f"[platform] {a.request} cancelled")
            return 0
        if a.cmd == "requests":
            rows = pl.requests(open_only=not a.all)
            for r in rows:
                out(f"  {r['id']:<11} {r['kind']:<9} {r['project_id']:<13} {r['status']:<9} by {r['requested_by']:<24} "
                    f"verified {len(r['verifications'])}/{pl.min_verifiers}")
            out(f"[platform] {len(rows)} request(s)")
            return 0
        if a.cmd == "projects":
            for p in pl.projects():
                out(f"  {p['id']:<13} {p['name']:<34} {p['client']:<22} {p['status']:<11} serial {p['serial']:<3} "
                    f"{','.join(p['domains']) or '-'}")
            return 0
        if a.cmd == "show":
            p, reqs = pl.project(a.project)
            out(f"{p['name']} ({p['id']})  client: {p['client']}  hosting: {p['hosting']}  status: {p['status']}")
            out(f"  lead: {p['lead_name']} <{p['lead_email']}>   domains: {', '.join(p['domains']) or '-'}   serial {p['serial']}")
            for r in reqs:
                out(f"  {r['id']} {r['kind']:<9} {r['status']:<9} requested {_when(r['requested_at'])} by {r['requested_by']}; "
                    f"verified by {', '.join(v['by'] for v in r['verifications']) or 'nobody yet'}")
            return 0
        if a.cmd == "audit":
            if a.verify:
                ok, n, bad = pl.audit.verify()
                out(f"[platform] audit log: {n} line(s) checked, " + ("chain intact" if ok else f"BROKEN at line {bad}"))
                return 0 if ok else 1
            for e in pl.audit.entries():
                extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "event", "by", "prev", "hash"))
                out(f"  {e['ts']}  {e['event']:<26} {e['by']:<24} {extra}")
            return 0
    except (PlatformError, pt.TrustError) as exc:
        out(f"[platform] FAIL {exc.message}")
        return 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
