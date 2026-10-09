"""Integration tests for the v15 invitation, platform-controlled domains and project-administrator changes.
The real gate is served over real HTTP; the platform's tool signs real grants; nothing is mocked except e-mail.
Run from agent\\:  python test_access_v15.py"""
import contextlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_accounts as accts  # noqa: E402
import console_auth as ca  # noqa: E402
import platform_admin as pa  # noqa: E402
import platform_trust as pt  # noqa: E402

accts.SCRYPT_N = 2 ** 10
TOK = "console-token"
GOOD = "correct horse battery"
GOOD2 = "another long passphrase 42"
ALICE, BOB = "WIN\\alice", "WIN\\bob"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def code_in(msg) -> str:
    m = re.search(r"\n {4}(\d{6})\n", msg.get_content())
    return m.group(1) if m else ""


def token_in(path) -> str:
    return re.search(r"Your invitation code:\s+(\S+)", Path(path).read_text(encoding="utf-8")).group(1)


class Client:
    def __init__(self, port):
        self.port, self.cookies = port, {}

    def req(self, method, path, body=None, token=TOK, accept="application/json"):
        h = {"Host": f"127.0.0.1:{self.port}", "Accept": accept}
        if token is not None:
            h["X-Console-Token"] = token
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        for sc in r.msg.get_all("Set-Cookie") or []:
            k, v = sc.split(";")[0].split("=", 1)
            if v:
                self.cookies[k] = v
            else:
                self.cookies.pop(k, None)
        c.close()
        try:
            js = json.loads(raw)
        except ValueError:
            js = {}
        return r.status, js, raw.decode("utf-8", "replace"), r


class Base(unittest.TestCase):
    EXTRA_ENV = {}
    PLATFORM_MODE = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.inst, self.out = root / "instance", root / "grants"
        self.clock = Clock(time.time())
        self.who, self.mail = [ALICE], []
        self.keys = pa.MemoryKeyring()
        self.platform = pa.Platform(root / "platform", self.keys, identity=lambda: self.who[0], clock=self.clock)
        self.pub = self.platform.create_keys()
        env = {"CONSOLE_AUTH_MODE": "accounts", "CONSOLE_DATA_DIR": str(self.inst),
               "CONSOLE_ACCESS_FILE": str(self.inst / "access.json"),
               "CONSOLE_SMTP_HOST": "smtp.example.com", "CONSOLE_SMTP_FROM": "console@example.com"}
        if self.PLATFORM_MODE:
            env["CONSOLE_PLATFORM_PUBLIC_KEY"] = self.pub
        env.update(self.EXTRA_ENV)
        self.env = env
        self.cfg = ca.AuthConfig(env)
        self.store = accts.AccountStore(self.cfg.accounts_file, clock=self.clock)
        self.trust = pt.InstanceTrust(self.inst, self.pub, clock=self.clock) if self.PLATFORM_MODE else None
        notifier = accts.Notifier(self.store, env, sender=self.mail.append, threaded=False)
        self.gate = ca.AuthGate(self.cfg, self.inst / "logs" / "audit.jsonl", port=7, console_token=TOK,
                                accounts=self.store, notifier=notifier, clock=self.clock, trust=self.trust)
        gate = self.gate

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                if gate.handle(self):
                    return
                u = gate.require(self)
                if u is not None:
                    gate._send(self, 200, {"ok": True, "user": u["user"]})

            do_GET = do_POST = _go

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.audit = self.inst / "logs" / "audit.jsonl"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def events(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()] if self.audit.exists() else []

    # ---- the platform side
    def as_(self, who):
        self.who[0] = who

    def project(self, **kw):
        args = dict(name="Ministry X intake", client="Ministry X", lead_email="lead@gov.bc.ca", lead_name="Lead Person",
                    domains=["gov.bc.ca"])
        args.update(kw)
        return self.platform.new_project(**args)

    def verified(self, req):
        self.as_(BOB)
        self.platform.verify(req["id"], "checked")
        self.as_(ALICE)

    def onboard(self, apply=True, **kw):
        proj, req = self.project(**kw)
        self.verified(req)
        res = self.platform.issue(req["id"], self.out)
        if apply:
            self.trust.apply(Path(res["grant_file"]).read_text(encoding="utf-8"))
        return proj, res, token_in(res["invitation_file"])

    def issue_change(self, project_id, add=(), remove=(), apply=False):
        req = self.platform.change_domains(project_id, add=add, remove=remove, reason="test")
        self.verified(req)
        res = self.platform.issue(req["id"], self.out)
        text = Path(res["grant_file"]).read_text(encoding="utf-8")
        if apply:
            self.trust.apply(text)
        return text

    # ---- the instance side
    def start_claim(self, token, client=None, **kw):
        c = client or Client(self.port)
        body = dict(token=token, name="Lead Person", username="lead", password=GOOD)
        body.update(kw)
        return c, c.req("POST", "/auth/claim/start", body, token=None)

    def claim(self, token, **kw):
        c, (st, js, _, _) = self.start_claim(token, **kw)
        self.assertEqual(st, 202, js)
        code = code_in(self.mail[-1])
        st2, js2, _, _ = c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code}, token=None)
        self.assertEqual(st2, 201, js2)
        return js2

    def admin(self, username="lead", password=GOOD):
        c = Client(self.port)
        st, js, _, _ = c.req("POST", "/auth/login", {"username": username, "password": password})
        self.assertEqual(st, 200, js)
        return c

    def claimed(self, **kw):
        proj, res, token = self.onboard(**kw)
        self.claim(token)
        return proj, res, token, self.admin()

    def user(self, username="dev1", email="dev1@gov.bc.ca", role="viewer", admin=None):
        c = Client(self.port)
        st, js, _, _ = c.req("POST", "/auth/register/start", dict(username=username, name="Dev One", email=email,
                                                                  password=GOOD2, reason="team member"))
        self.assertEqual(st, 202, js)
        st, js, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})
        self.assertEqual(st, 201, js)
        (admin or self.admin()).req("POST", "/auth/admin/decide", {"username": username, "decision": "approve", "role": role})
        u = Client(self.port)
        self.assertEqual(u.req("POST", "/auth/login", {"username": username, "password": GOOD2})[0], 200)
        return u


# ============================================================================ claiming the invitation
class ClaimTests(Base):
    def test_before_any_grant_there_is_nothing_to_claim(self):
        st, _, page, r = Client(self.port).req("GET", "/auth/claim", token=None, accept="text/html")
        self.assertEqual(st, 200)
        self.assertRegex(page, r'id="vStart" hidden')
        self.assertNotRegex(page, r'id="vClosed" hidden')
        self.assertNotIn("unsafe-inline", r.getheader("Content-Security-Policy"))
        st, js, _, _ = Client(self.port).req("POST", "/auth/claim/start", dict(token="AAAAA-BBBBB-CCCCC-DDDDD", username="lead",
                                                                                 name="Lead Person", password=GOOD), token=None)
        self.assertEqual((st, js["error"]), (400, "invalid_invitation"))

    def test_the_page_names_the_project_but_not_the_person_or_the_code(self):
        _, res, token = self.onboard()
        st, _, page, r = Client(self.port).req("GET", "/auth/claim", token=None, accept="text/html")
        self.assertEqual(st, 200)
        self.assertRegex(page, r'id="vClosed" hidden')
        self.assertNotRegex(page, r'id="vStart" hidden')
        self.assertIn("Ministry X intake", page)
        for secret in ("lead@gov.bc.ca", "Lead Person", token, token.replace("-", ""), "invite_hash"):
            self.assertNotIn(secret, page)

    def test_the_whole_claim(self):
        proj, res, token = self.onboard()
        self.assertEqual(self.store.active_admins(), [])
        c, (st, js, _, _) = self.start_claim(token)
        self.assertEqual((st, js["state"], js["email"]), (202, "verify", accts.mask_email("lead@gov.bc.ca")))
        self.assertEqual(self.mail[-1]["To"], "lead@gov.bc.ca")                     # always the invited address
        self.assertTrue(880 <= js["expires_in_s"] <= 900)
        self.assertEqual(self.store.active_admins(), [])                            # nothing exists before the code
        code = code_in(self.mail[-1])
        wrong = "000000" if code != "000000" else "111111"
        st, js2, _, _ = c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": wrong}, token=None)
        self.assertEqual((st, js2["error"]), (400, "invalid_code"))
        st, js2, _, _ = c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code}, token=None)
        self.assertEqual((st, js2["state"]), (201, "ready"))
        admins = self.store.active_admins()
        self.assertEqual([(a["username"], a["role"], a["admin"], a["email"]) for a in admins],
                         [("lead", "approver", True, "lead@gov.bc.ca")])
        me = self.admin().req("GET", "/auth/me")[1]
        self.assertEqual((me["admin"], me["role"], me["project"]), (True, "approver", "Ministry X intake"))
        ev = [e["event"] for e in self.events()]
        for k in ("claim_code_sent", "project_claimed", "sign_in"):
            self.assertIn(k, ev)

    def test_nobody_else_ever_knows_the_password(self):
        _, res, token = self.onboard()
        self.claim(token)
        for f in [p for p in Path(self.tmp.name).rglob("*") if p.is_file()]:
            text = f.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(GOOD, text, f.name)
        rec = json.loads((self.inst / "accounts.json").read_text())["users"]["lead"]
        self.assertTrue(rec["pw"].startswith("scrypt$"))
        self.assertEqual(rec["decided_by"], "invitation")

    def test_the_claim_page_calls_need_no_console_token_but_the_rest_still_do(self):
        _, res, token = self.onboard()
        c, (st, js, _, _) = self.start_claim(token)                                  # token=None: no console token sent
        self.assertEqual(st, 202)
        for path in ("/auth/register/start", "/auth/register/verify", "/auth/register/resend", "/auth/login", "/auth/me"):
            self.assertEqual(Client(self.port).req("POST" if "register" in path or "login" in path else "GET", path, {}, token=None)[0], 401, path)

    def test_the_invited_address_cannot_be_changed_by_the_person_claiming(self):
        _, res, token = self.onboard()
        self.start_claim(token, email="attacker@evil.com")
        self.assertEqual([m["To"] for m in self.mail], ["lead@gov.bc.ca"])

    def test_wrong_codes_are_refused_and_counted(self):
        self.onboard()
        for bad in ("", "AAAAA-BBBBB-CCCCC-DDDDD", "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", "x" * 500):
            _, (st, js, _, _) = self.start_claim(bad)
            self.assertEqual((st, js["error"]), (400, "invalid_invitation"))
        self.assertEqual(self.mail, [])                                              # no e-mail goes out for a wrong code
        self.assertEqual(len([e for e in self.events() if e["event"] == "claim_refused"]), 4)

    def test_guessing_is_rate_limited(self):
        _, res, token = self.onboard()
        for _ in range(10):
            self.start_claim("AAAAA-BBBBB-CCCCC-DDDDD")
        _, (st, js, _, _) = self.start_claim(token)                                  # even the right code is paused
        self.assertEqual((st, js["error"]), (429, "claim_locked"))
        self.clock.t += 901
        _, (st, js, _, _) = self.start_claim(token)
        self.assertEqual(st, 202)

    def test_the_code_typed_in_any_reasonable_way_works(self):
        _, res, token = self.onboard()
        _, (st, _, _, _) = self.start_claim(" " + token.lower().replace("-", " ") + " ")
        self.assertEqual(st, 202)

    def test_choices_are_checked_before_any_e_mail_goes_out(self):
        _, res, token = self.onboard()
        for kw, code in ((dict(password="short"), "weak_password"), (dict(username="a!"), "bad_username"),
                         (dict(name=""), "bad_name"), (dict(password="my-lead-password-1"), "weak_password")):
            _, (st, js, _, _) = self.start_claim(token, **kw)
            self.assertEqual((st, js["error"]), (400, code), kw)
        self.assertEqual(self.mail, [])

    def test_a_used_invitation_cannot_be_used_again(self):
        _, res, token = self.onboard()
        self.claim(token)
        _, (st, js, _, _) = self.start_claim(token, username="intruder")
        self.assertEqual((st, js["error"]), (400, "invalid_invitation"))
        st, _, page, _ = Client(self.port).req("GET", "/auth/claim", token=None, accept="text/html")
        self.assertRegex(page, r'id="vStart" hidden')                                   # closed again
        self.assertEqual(len(self.store.active_admins()), 1)

    def test_only_the_newest_attempt_can_finish_and_only_once(self):
        _, res, token = self.onboard()
        c1, (s1, j1, _, _) = self.start_claim(token, username="lead")
        code1 = code_in(self.mail[-1])
        c2, (s2, j2, _, _) = self.start_claim(token, username="lead2")               # same invited address: replaces the first
        code2 = code_in(self.mail[-1])
        self.assertEqual((s1, s2), (202, 202))
        st, js, _, _ = c1.req("POST", "/auth/claim/verify", {"id": j1["id"], "code": code1}, token=None)
        self.assertEqual((st, js["error"]), (410, "not_found"))
        self.assertEqual(c2.req("POST", "/auth/claim/verify", {"id": j2["id"], "code": code2}, token=None)[0], 201)
        st, js, _, _ = c2.req("POST", "/auth/claim/verify", {"id": j2["id"], "code": code2}, token=None)   # a code works once
        self.assertEqual(st, 410)
        self.assertEqual([a["username"] for a in self.store.active_admins()], ["lead2"])

    def test_an_invitation_closed_between_start_and_finish_cannot_be_finished(self):
        _, res, token = self.onboard()
        c, (st, js, _, _) = self.start_claim(token)
        code = code_in(self.mail[-1])
        self.trust.consume(self.trust.claimable_invite(False)["invite_hash"], by="someone-else")   # used in the meantime
        st, js2, _, _ = c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code}, token=None)
        self.assertEqual((st, js2["error"]), (410, "invitation_closed"))
        self.assertEqual(self.store.active_admins(), [])

    def test_an_expired_invitation_is_refused(self):
        _, res, token = self.onboard()
        self.clock.t += 73 * 3600
        _, (st, js, _, _) = self.start_claim(token)
        self.assertEqual((st, js["error"]), (400, "invalid_invitation"))
        st, _, page, _ = Client(self.port).req("GET", "/auth/claim", token=None, accept="text/html")
        self.assertRegex(page, r'id="vStart" hidden')

    def test_the_invitation_can_expire_while_the_email_code_is_out(self):
        _, res, token = self.onboard()
        c, (st, js, _, _) = self.start_claim(token)
        self.clock.t += 73 * 3600
        st, js2, _, _ = c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code_in(self.mail[-1])}, token=None)
        self.assertIn(st, (410,))
        self.assertEqual(self.store.active_admins(), [])

    def test_the_emailed_code_expires_after_15_minutes(self):
        _, res, token = self.onboard()
        c, (st, js, _, _) = self.start_claim(token)
        self.clock.t += 15 * 60 + 1
        st, js2, _, _ = c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code_in(self.mail[-1])}, token=None)
        self.assertEqual((st, js2["error"]), (410, "expired"))
        self.assertEqual(self.store.active_admins(), [])

    def test_resend_works_even_when_registration_is_off(self):
        self.cfg.allow_registration = False
        _, res, token = self.onboard()
        c, (st, js, _, _) = self.start_claim(token)
        self.clock.t += 61
        st, js2, _, _ = c.req("POST", "/auth/claim/resend", {"id": js["id"]}, token=None)
        self.assertEqual(st, 200)
        self.assertEqual(len(self.mail), 2)
        self.assertEqual(c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code_in(self.mail[-1])}, token=None)[0], 201)

    def test_a_claim_code_cannot_be_used_to_register_an_ordinary_account(self):
        _, res, token = self.onboard()
        c, (st, js, _, _) = self.start_claim(token)
        st, js2, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})
        self.assertEqual((st, js2["error"]), (400, "invalid_code"))
        self.assertEqual(self.store.list_users(), [])

    def test_an_email_that_cannot_be_sent_leaves_no_valid_code(self):
        _, res, token = self.onboard()
        self.gate.notifier._sender = lambda m: (_ for _ in ()).throw(ConnectionRefusedError("smtp down"))
        _, (st, js, _, _) = self.start_claim(token)
        self.assertEqual((st, js["error"]), (502, "email_failed"))
        self.assertEqual(self.gate.verifications._open, {})

    def test_the_audit_log_never_holds_the_code_the_password_or_the_email_code(self):
        _, res, token = self.onboard()
        self.start_claim("AAAAA-BBBBB-CCCCC-DDDDD")
        c, (st, js, _, _) = self.start_claim(token)
        code = code_in(self.mail[-1])
        c.req("POST", "/auth/claim/verify", {"id": js["id"], "code": code}, token=None)
        raw = self.audit.read_text()
        for secret in (token, token.replace("-", ""), GOOD, code, js["id"], "AAAAA"):
            self.assertNotIn(secret, raw)

    def test_the_sign_in_page_sends_a_new_visitor_to_the_claim_page(self):
        self.onboard()
        st, _, _, r = Client(self.port).req("GET", "/auth/signin", token=None, accept="text/html")
        self.assertEqual((st, r.getheader("Location")), (302, "/auth/claim"))

    def test_after_the_claim_the_sign_in_page_is_normal(self):
        proj, res, token, admin = self.claimed()
        st, _, page, _ = Client(self.port).req("GET", "/auth/signin", token=None, accept="text/html")
        self.assertEqual(st, 200)
        self.assertIn("const MODE=", page)

    def test_a_tampered_grant_closes_the_claim(self):
        _, res, token = self.onboard()
        data = json.loads((self.inst / "grants.json").read_text())
        data["grants"][0]["payload"]["admin_email"] = "attacker@gov.bc.ca"
        (self.inst / "grants.json").write_text(json.dumps(data))
        _, (st, js, _, _) = self.start_claim(token)
        self.assertEqual((st, js["error"]), (400, "invalid_invitation"))
        self.assertEqual(self.mail, [])


# ============================================================================ recovery
class RecoveryTests(Base):
    def test_the_platform_can_restore_an_administrator(self):
        proj, res, token, admin = self.claimed()
        req = self.platform.recover_admin(proj["id"], "new.lead@gov.bc.ca", "New Lead", "The lead left the project")
        self.verified(req)
        r2 = self.platform.issue(req["id"], self.out)
        self.trust.apply(Path(r2["grant_file"]).read_text(encoding="utf-8"))
        t2 = token_in(r2["invitation_file"])
        self.assertNotEqual(t2, token)
        self.claim(t2, username="newlead", name="New Lead")
        self.assertEqual(sorted(a["username"] for a in self.store.active_admins()), ["lead", "newlead"])
        self.assertIn("recovery", [e.get("purpose") for e in self.events()])
        _, (st, js, _, _) = self.start_claim(t2, username="third")                  # a recovery invitation is single use too
        self.assertEqual((st, js["error"]), (400, "invalid_invitation"))

    def test_an_old_initial_invitation_cannot_be_reused_after_a_claim(self):
        proj, res, token, admin = self.claimed()
        res2 = self.platform.reinvite(proj["id"], self.out)
        self.trust.apply(Path(res2["grant_file"]).read_text(encoding="utf-8"))
        _, (st, js, _, _) = self.start_claim(token_in(res2["invitation_file"]), username="second")
        self.assertEqual(st, 400)                                                    # an initial invitation needs "no administrator yet"
        self.assertEqual(len(self.store.active_admins()), 1)

    def test_a_reissued_invitation_replaces_the_first_before_it_was_used(self):
        proj, res, token = self.onboard()
        res2 = self.platform.reinvite(proj["id"], self.out)
        self.trust.apply(Path(res2["grant_file"]).read_text(encoding="utf-8"))
        _, (st, js, _, _) = self.start_claim(token)
        self.assertEqual(st, 400)
        _, (st, js, _, _) = self.start_claim(token_in(res2["invitation_file"]))
        self.assertEqual(st, 202)


# ============================================================================ domains controlled by the platform team
class DomainControlTests(Base):
    def test_the_admin_page_data_says_who_controls_the_domains(self):
        proj, res, token, admin = self.claimed()
        st, js, _, _ = admin.req("GET", "/auth/admin/domains")
        self.assertEqual((st, js["control"], js["project"]["name"]), (200, "platform", "Ministry X intake"))
        self.assertEqual(js["domains"], [{"domain": "gov.bc.ca", "active_users": 0, "state": "allowed"}])

    def test_a_project_administrator_cannot_add_a_domain(self):
        proj, res, token, admin = self.claimed()
        for dom in ("gmail.com", "evil.example.ca", "*.gov.bc.ca"):
            st, js, _, _ = admin.req("POST", "/auth/admin/domains", {"action": "add", "domain": dom})
            self.assertEqual((st, js["error"]), (403, "platform_controlled"), dom)
        self.assertEqual(self.gate.domains.list(), ["gov.bc.ca"])
        self.assertFalse(any(e["event"] == "domain_added" for e in self.events()))
        _, (st, js, _, _) = (None, Client(self.port).req("POST", "/auth/register/start",
                                                          dict(username="x1y2z", name="Out Sider", email="o@gmail.com", password=GOOD2)))
        self.assertEqual((st, js["error"]), (403, "domain_not_allowed"))

    def test_people_on_an_approved_domain_can_request_and_sign_in(self):
        proj, res, token, admin = self.claimed()
        u = self.user()
        self.assertEqual(u.req("GET", "/api/state")[0], 200)
        _, js, _, _ = admin.req("GET", "/auth/admin/domains")
        self.assertEqual(js["domains"][0]["active_users"], 1)

    def test_pausing_a_domain_ends_access_at_once_and_resuming_restores_it(self):
        proj, res, token, admin = self.claimed()
        u = self.user()
        st, js, _, _ = admin.req("POST", "/auth/admin/domains", {"action": "remove", "domain": "gov.bc.ca"})
        self.assertEqual((st, js["domains"][0]["state"]), (200, "paused"))
        self.assertEqual(u.req("GET", "/api/state")[0], 401)                         # open session ended
        st, js, _, _ = Client(self.port).req("POST", "/auth/login", {"username": "dev1", "password": GOOD2})
        self.assertEqual((st, js["error"]), (403, "domain_not_allowed"))
        self.assertEqual(admin.req("GET", "/api/state")[0], 200)                     # administrators are never locked out
        _, (st, js, _, _) = (None, Client(self.port).req("POST", "/auth/register/start",
                                                          dict(username="dev2x", name="Dev Two", email="d2@gov.bc.ca", password=GOOD2)))
        self.assertEqual((st, js["error"]), (403, "registration_closed"))            # nothing allowed while it is paused
        st, js, _, _ = admin.req("POST", "/auth/admin/domains", {"action": "restore", "domain": "gov.bc.ca"})
        self.assertEqual((st, js["domains"][0]["state"]), (200, "allowed"))
        self.assertEqual(Client(self.port).req("POST", "/auth/login", {"username": "dev1", "password": GOOD2})[0], 200)
        ev = {e["event"]: e for e in self.events()}
        self.assertEqual((ev["domain_paused"]["by"], ev["domain_paused"]["affected_users"]), ("lead", 1))
        self.assertIn("domain_resumed", ev)

    def test_pause_and_restore_edge_cases(self):
        proj, res, token, admin = self.claimed()
        for action, dom, want in (("remove", "other.ca", 400), ("restore", "gov.bc.ca", 400), ("zap", "gov.bc.ca", 400)):
            self.assertEqual(admin.req("POST", "/auth/admin/domains", {"action": action, "domain": dom})[0], want, (action, dom))

    def test_only_administrators_can_use_these_calls(self):
        proj, res, token, admin = self.claimed()
        u = self.user(role="approver")
        for m, p, b in (("GET", "/auth/admin/domains", None), ("POST", "/auth/admin/domains", {"action": "remove", "domain": "gov.bc.ca"}),
                        ("POST", "/auth/admin/grants", {"grant": "{}"})):
            st, js, _, _ = u.req(m, p, b)
            self.assertEqual((p, st, js.get("need")), (p, 403, "manager"))
            self.assertEqual(Client(self.port).req(m, p, b)[0], 401)

    # ---- grants applied through the screen
    def test_applying_a_domain_update_from_the_platform_team(self):
        proj, res, token, admin = self.claimed()
        text = self.issue_change(proj["id"], add=["*.gov.bc.ca"])
        st, js, _, _ = admin.req("POST", "/auth/admin/grants", {"grant": text})
        self.assertEqual((st, js["summary"]["serial"], js["summary"]["already"]), (200, 2, False))
        self.assertEqual([d["domain"] for d in js["domains"]], ["*.gov.bc.ca", "gov.bc.ca"])
        self.assertTrue(self.gate.domains.allows("a@justice.gov.bc.ca"))
        st, js, _, _ = admin.req("POST", "/auth/admin/grants", {"grant": text})        # pasting it twice is harmless
        self.assertEqual((st, js["summary"]["already"]), (200, True))
        ev = [e for e in self.events() if e["event"] == "grant_applied"]
        self.assertEqual([(e["by"], e["serial"], e["already"]) for e in ev], [("lead", 2, False), ("lead", 2, True)])

    def test_the_platform_removing_a_domain_ends_access_for_the_people_on_it(self):
        proj, res, token, admin = self.claimed()
        text = self.issue_change(proj["id"], add=["deloitte.ca"])
        admin.req("POST", "/auth/admin/grants", {"grant": text})
        u = self.user("dev1", "dev1@gov.bc.ca")
        text = self.issue_change(proj["id"], remove=["gov.bc.ca"])
        st, js, _, _ = admin.req("POST", "/auth/admin/grants", {"grant": text})
        self.assertEqual(st, 200)
        self.assertEqual(u.req("GET", "/api/state")[0], 401)
        self.assertEqual(admin.req("GET", "/api/state")[0], 200)

    def test_forged_old_wrong_project_and_garbled_updates_are_refused(self):
        proj, res, token, admin = self.claimed()
        good = self.issue_change(proj["id"], add=["a.example.ca"])
        admin.req("POST", "/auth/admin/grants", {"grant": good})
        other_seed = pt.generate_keypair()[0]
        forged = json.dumps(pt.sign_payload(other_seed, {"v": 1, "kind": "domain-grant", "project_id": proj["id"], "serial": 9,
                                                         "issued_at": 1.0, "issued_by": "x", "request_id": "r", "add": ["evil.com"], "remove": []}))
        tampered = json.loads(good)
        tampered["payload"]["serial"], tampered["payload"]["add"] = 7, ["evil.com"]
        wrong_project = json.dumps(pt.sign_payload(self.keys.get(), {"v": 1, "kind": "domain-grant", "project_id": "prj-00000000",
                                                                      "serial": 8, "issued_at": 1.0, "issued_by": "x", "request_id": "r",
                                                                      "add": ["evil.com"], "remove": []}))
        older = json.dumps(pt.sign_payload(self.keys.get(), {"v": 1, "kind": "domain-grant", "project_id": proj["id"], "serial": 1,
                                                              "issued_at": 1.0, "issued_by": "x", "request_id": "r", "add": ["evil.com"], "remove": []}))
        for label, g, code in (("forged", forged, "bad_signature"), ("tampered", json.dumps(tampered), "bad_signature"),
                               ("wrong project", wrong_project, "wrong_project"), ("old serial", older, "old_grant"),
                               ("garbled", "{not json", "bad_grant"), ("empty", "", "bad_grant")):
            st, js, _, _ = admin.req("POST", "/auth/admin/grants", {"grant": g})
            self.assertEqual((label, st, js["error"]), (label, 400, code))
        self.assertNotIn("evil.com", self.gate.domains.list())
        self.assertEqual(len([e for e in self.events() if e["event"] == "grant_refused"]), 6)

    def test_a_hand_edited_grants_file_allows_nobody_but_administrators(self):
        proj, res, token, admin = self.claimed()
        u = self.user()
        data = json.loads((self.inst / "grants.json").read_text())
        data["grants"][0]["payload"]["domains"].append("gmail.com")
        (self.inst / "grants.json").write_text(json.dumps(data))
        self.assertEqual(u.req("GET", "/api/state")[0], 401)
        self.assertEqual(Client(self.port).req("POST", "/auth/login", {"username": "dev1", "password": GOOD2})[0], 403)
        self.assertEqual(admin.req("GET", "/api/state")[0], 200)
        st, js, _, _ = admin.req("GET", "/auth/admin/domains")
        self.assertEqual(js["domains"], [])

    def test_the_admin_page_is_served_with_the_platform_controls_and_no_innerHTML(self):
        proj, res, token, admin = self.claimed()
        st, _, body, r = admin.req("GET", "/auth/admin", accept="text/html")
        self.assertEqual(st, 200)
        for needle in ("Update from the platform team", "Only the platform team can add a domain", "applyGrant", "Pause", "Resume",
                       "project administrator"):
            self.assertIn(needle, body)
        self.assertNotIn("innerHTML", body)
        self.assertNotIn("unsafe-inline", r.getheader("Content-Security-Policy"))
        self.assertNotIn("can manage access", body)                                    # the old wording is gone


# ============================================================================ the command line in a platform-managed console
class PlatformCliTests(Base):
    KEYS = ("CONSOLE_DATA_DIR", "CONSOLE_ACCESS_FILE", "CONSOLE_PLATFORM_PUBLIC_KEY", "JIRA_API_TOKEN", "GITHUB_TOKEN",
            "CONSOLE_AUTH_MODE", "CONSOLE_VERIFY_DELIVERY", "CONSOLE_SMTP_HOST", "CONSOLE_SMTP_FROM")

    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ.update({"CONSOLE_DATA_DIR": str(self.inst), "CONSOLE_ACCESS_FILE": str(self.inst / "access.json"),
                           "CONSOLE_PLATFORM_PUBLIC_KEY": self.pub, "JIRA_API_TOKEN": "t" * 12, "GITHUB_TOKEN": "t" * 12,
                           "CONSOLE_AUTH_MODE": "accounts", "CONSOLE_VERIFY_DELIVERY": "log"})
        os.environ.pop("CONSOLE_SMTP_HOST", None)
        os.environ.pop("CONSOLE_SMTP_FROM", None)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ca._cli(list(argv))
        return code, buf.getvalue()

    def startup(self):
        try:
            return ca.startup_check(Path(self.tmp.name) / "agent").mode
        except RuntimeError as e:
            return str(e)

    def test_apply_a_grant_file_and_look_at_the_project(self):
        code, text = self.cli("project")
        self.assertIn("none yet", text)
        proj, req = self.project()
        self.verified(req)
        res = self.platform.issue(req["id"], self.out)
        code, text = self.cli("grant", "apply", res["grant_file"])
        self.assertEqual(code, 0)
        self.assertIn("applied: project-grant serial 1", text)
        self.assertIn("already applied", self.cli("grant", "apply", res["grant_file"])[1])
        code, text = self.cli("project")
        self.assertIn("Ministry X intake", text)
        self.assertIn("gov.bc.ca", text)
        self.assertIn("waiting to be claimed", text)
        self.assertNotIn(token_in(res["invitation_file"]), text)
        self.assertIn("project-grant", self.cli("grant", "show")[1])

    def test_a_forged_or_missing_grant_file_is_one_plain_line(self):
        bad = Path(self.tmp.name) / "forged.json"
        bad.write_text(json.dumps(pt.sign_payload(pt.generate_keypair()[0], {
            "v": 1, "kind": "domain-grant", "project_id": "prj-ab12cd34", "serial": 1, "issued_at": 1.0, "issued_by": "x",
            "request_id": "r", "add": ["evil.com"], "remove": []})))
        for argv in (("grant", "apply", str(bad)), ("grant", "apply", str(bad) + ".missing"), ("grant", "apply")):
            code, text = self.cli(*argv)
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", text)
        self.assertFalse((self.inst / "grants.json").exists())

    def test_the_command_line_cannot_add_domains_or_set_a_password_for_someone(self):
        proj, req = self.project()
        self.verified(req)
        self.cli("grant", "apply", self.platform.issue(req["id"], self.out)["grant_file"])
        code, text = self.cli("bootstrap-admin", "--user", "pavan", "--email", "p@gov.bc.ca")
        self.assertEqual(code, 1)
        self.assertIn("platform team", text)
        self.assertEqual(self.store.active_admins(), [])
        code, text = self.cli("domains", "add", "gmail.com")
        self.assertEqual(code, 1)
        self.assertIn("Only the platform team", text)
        self.assertEqual(self.cli("domains")[1].count("gmail.com"), 0)

    def test_pause_and_resume_by_command(self):
        proj, req = self.project()
        self.verified(req)
        self.cli("grant", "apply", self.platform.issue(req["id"], self.out)["grant_file"])
        code, text = self.cli("domains", "remove", "gov.bc.ca")
        self.assertIn("paused: gov.bc.ca", text)
        self.assertIn("paused: gov.bc.ca", self.cli("domains")[1])
        self.assertEqual(self.cli("domains", "remove", "gov.bc.ca")[0], 1)
        self.assertIn("resumed", self.cli("domains", "restore", "gov.bc.ca")[1])
        self.assertEqual(self.cli("domains", "restore", "gov.bc.ca")[0], 1)

    def test_make_admin_and_its_old_name(self):
        self.store.create_admin_with_hash("boss", "The Boss", "boss@gov.bc.ca", accts.hash_password(GOOD2))
        self.store.register("erin", "Erin Example", "erin@gov.bc.ca", GOOD)
        self.store.register("fred", "Fred Example", "fred@gov.bc.ca", GOOD)
        self.assertEqual(self.cli("make-admin", "--user", "erin")[0], 0)
        code, text = self.cli("make-manager", "--user", "fred")
        self.assertEqual(code, 0)
        self.assertIn("project administrator", text)
        self.assertTrue(all(self.store.get(u)["admin"] for u in ("erin", "fred")))

    def test_startup_check_walks_through_a_platform_managed_console(self):
        self.assertIn("grant apply", self.startup())                               # nothing yet: ask the platform team
        proj, req = self.project()
        self.verified(req)
        res = self.platform.issue(req["id"], self.out, hours=1)
        self.cli("grant", "apply", res["grant_file"])
        self.assertEqual(self.startup(), "accounts")                              # waiting for the first administrator
        os.environ["CONSOLE_VERIFY_DELIVERY"] = "email"
        self.assertIn("CONSOLE_SMTP_HOST", self.startup())                        # codes need e-mail (or log delivery)

    def test_startup_check_says_so_when_the_invitation_has_run_out(self):
        proj, req = self.project()
        self.verified(req)
        self.clock.t = time.time() - 5 * 3600                                      # issued five hours ago, valid for one
        res = self.platform.issue(req["id"], self.out, hours=1)
        self.cli("grant", "apply", res["grant_file"])
        msg = self.startup()
        self.assertIn("expired", msg)
        self.assertIn("grant apply", msg)

    def test_startup_check_after_the_claim(self):
        proj, res, token, admin = self.claimed()
        self.assertEqual(self.startup(), "accounts")
        self.gate.domains.remove("gov.bc.ca")
        msg = self.startup()
        self.assertIn("platform team", msg)
        self.assertIn("resume", msg)

    def test_startup_check_refuses_a_bad_key_and_a_bad_grants_file(self):
        os.environ["CONSOLE_PLATFORM_PUBLIC_KEY"] = "not-a-key"
        self.assertIn("not a valid public key", self.startup())
        os.environ["CONSOLE_PLATFORM_PUBLIC_KEY"] = self.pub
        proj, res, token, admin = self.claimed()
        data = json.loads((self.inst / "grants.json").read_text())
        data["grants"][0]["payload"]["domains"].append("gmail.com")
        (self.inst / "grants.json").write_text(json.dumps(data))
        self.assertIn("fresh grant", self.startup())

    def test_a_different_platform_key_trusts_nothing(self):
        proj, res, token, admin = self.claimed()
        os.environ["CONSOLE_PLATFORM_PUBLIC_KEY"] = pt.generate_keypair()[1]
        self.assertIn("grant", self.startup())


# ============================================================================ a console that is not platform-managed (unchanged)
class StandaloneTests(Base):
    PLATFORM_MODE = False

    def setUp(self):
        super().setUp()
        self.store.bootstrap_admin("pavan", "Pavan Vellure", "pvellure@deloitte.ca", GOOD2)
        self.gate.domains.add("deloitte.ca")

    def test_standalone_behaves_as_before(self):
        self.assertEqual(self.gate.domain_control, "local")
        admin = self.admin("pavan", GOOD2)
        st, js, _, _ = admin.req("GET", "/auth/admin/domains")
        self.assertEqual((js["control"], js["project"], js["domains"]), ("local", None, [{"domain": "deloitte.ca", "active_users": 0, "state": "allowed"}]))
        st, js, _, _ = admin.req("POST", "/auth/admin/domains", {"action": "add", "domain": "gov.bc.ca"})
        self.assertEqual((st, [d["domain"] for d in js["domains"]]), (200, ["deloitte.ca", "gov.bc.ca"]))
        st, js, _, _ = admin.req("POST", "/auth/admin/domains", {"action": "remove", "domain": "gov.bc.ca"})
        self.assertEqual(st, 200)
        self.assertEqual(admin.req("POST", "/auth/admin/domains", {"action": "restore", "domain": "deloitte.ca"})[0], 400)
        self.assertIn("domain_removed", [e["event"] for e in self.events()])

    def test_the_claim_and_grant_routes_do_not_exist_without_a_platform_key(self):
        c = Client(self.port)
        self.assertEqual(c.req("GET", "/auth/claim", token=None, accept="text/html")[0], 404)
        self.assertEqual(c.req("POST", "/auth/claim/start", {}, token=None)[0], 404)
        self.assertEqual(self.admin("pavan", GOOD2).req("POST", "/auth/admin/grants", {"grant": "{}"})[1]["error"], "not_platform_mode")

    def test_standalone_sign_in_is_not_redirected(self):
        self.assertEqual(Client(self.port).req("GET", "/auth/signin", token=None, accept="text/html")[0], 200)

    def test_the_old_helper_names_still_work(self):
        self.store.register("erin", "Erin Example", "erin@deloitte.ca", GOOD)
        self.assertTrue(self.store.make_manager("erin")["admin"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
