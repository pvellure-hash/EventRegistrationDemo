"""Offline tests for accounts, allowed domains, email verification, manager approval, e-mail notices and the Windows
prompt (v14). Replaces test_access_v13.py. No network, no Windows API, no real e-mail.
Run from agent\\:  python test_access_v14.py"""
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
import windows_login as wl  # noqa: E402

accts.SCRYPT_N = 2 ** 10                      # fast hashes for tests; stored hashes record their own cost
TOK = "console-token"
GOOD = "correct horse battery"
GOOD2 = "another long passphrase 42"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def code_in(msg) -> str:
    m = re.search(r"\n {4}(\d{6})\n", msg.get_content())
    return m.group(1) if m else ""


# ============================================================================ the account store
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "accounts.json"
        self.clock = Clock()
        self.s = accts.AccountStore(self.path, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def reg(self, user="alice", pw=GOOD, **kw):
        d = dict(name="Alice Example", email=f"{user}@example.com", reason="Need to watch runs")
        d.update(kw)
        return self.s.register(user, d["name"], d["email"], pw, d["reason"])

    def boss(self, user="boss"):
        return self.s.bootstrap_admin(user, "The Boss", f"{user}@example.com", GOOD2)

    def test_hash_is_salted_and_verifies(self):
        a, b = accts.hash_password("x" * 14), accts.hash_password("x" * 14)
        self.assertNotEqual(a, b)
        self.assertTrue(accts.check_password("x" * 14, a))
        self.assertFalse(accts.check_password("x" * 13, a))
        self.assertFalse(accts.check_password("x", "garbage"))

    def test_password_policy(self):
        p = accts.password_problem
        self.assertIsNone(p(GOOD, "alice", "alice@example.com"))
        self.assertIn("at least 12", p("short", "alice"))
        self.assertIn("username", p("my-alice-password-1", "alice"))
        self.assertIn("email name", p("pavan.vellure-secret-1", "bob", "pavan.vellure@x.com"))
        self.assertIn("too easy", p("password1234", "bob"))
        self.assertIn("at most", p("x" * 200, "bob"))

    def test_register_creates_pending_and_stores_no_plain_password(self):
        rec = self.reg()
        self.assertEqual((rec["status"], rec["role"], rec["admin"]), ("pending", None, False))
        self.assertNotIn("pw", rec)
        self.assertNotIn(GOOD, self.path.read_text())

    def test_register_with_a_hash_made_earlier(self):
        h = accts.hash_password(GOOD)
        self.s.register("carol", "Carol Example", "carol@example.com", reason="", pw_hash=h)
        self.boss()
        self.s.decide("carol", "approve", by="boss", role="viewer")
        self.assertEqual(self.s.verify("carol", GOOD)[0], "ok")

    def test_register_validation(self):
        for kw, code in ((dict(user="a!"), "bad_username"), (dict(user="ab"), "bad_username"),
                         (dict(name="x"), "bad_name"), (dict(email="nope"), "bad_email"),
                         (dict(pw="short"), "weak_password")):
            with self.subTest(code=code), self.assertRaises(accts.AccountError) as cm:
                self.reg(**({"user": kw.pop("user")} if "user" in kw else {}), **kw)
            self.assertEqual(cm.exception.code, code)

    def test_one_account_per_email(self):
        self.reg("alice", email="shared@example.com")
        with self.assertRaises(accts.AccountError) as cm:
            self.reg("alice2", email="SHARED@example.com")
        self.assertEqual(cm.exception.code, "email_taken")

    def test_duplicate_and_rejected_can_reapply(self):
        self.reg()
        with self.assertRaises(accts.AccountError) as cm:
            self.reg()
        self.assertEqual(cm.exception.code, "username_taken")
        self.boss()
        self.s.decide("alice", "reject", by="boss")
        self.assertEqual(self.reg(pw=GOOD2)["status"], "pending")

    def test_pending_cap_and_hourly_rate_limit(self):
        s = accts.AccountStore(self.path, clock=self.clock, max_pending=2)
        s.register("user1", "User One", "user1@example.com", GOOD)
        s.register("user2", "User Two", "user2@example.com", GOOD)
        with self.assertRaises(accts.AccountError) as cm:
            s.register("user3", "User Three", "user3@example.com", GOOD)
        self.assertEqual(cm.exception.code, "too_many")

    def test_cannot_sign_in_until_approved_and_status_only_after_the_right_password(self):
        self.reg()
        self.boss()
        self.assertEqual(self.s.verify("alice", "wrong password here")[0], "bad")
        self.assertEqual(self.s.verify("alice", GOOD)[0], "pending")
        self.s.decide("alice", "approve", by="boss", role="operator")
        self.assertEqual(self.s.verify("alice", GOOD)[0], "ok")

    def test_decide_rules_and_last_manager(self):
        self.reg()
        self.boss()
        with self.assertRaises(accts.AccountError) as cm:
            self.s.decide("alice", "approve", by="ALICE", role="viewer")
        self.assertEqual(cm.exception.code, "self_approval")
        with self.assertRaises(accts.AccountError) as cm:
            self.s.decide("alice", "approve", by="boss")
        self.assertEqual(cm.exception.code, "bad_role")
        with self.assertRaises(accts.AccountError) as cm:
            self.s.set_user("boss", by="boss", disabled=True)
        self.assertEqual(cm.exception.code, "last_admin")

    def test_lockout_then_expiry(self):
        self.boss()
        for _ in range(5):
            self.s.verify("boss", "wrong password here")
        self.assertEqual(self.s.verify("boss", GOOD2)[0], "locked")
        self.clock.t += 901
        self.assertEqual(self.s.verify("boss", GOOD2)[0], "ok")

    def test_make_manager(self):
        self.reg()
        rec = self.s.make_manager("alice")
        self.assertEqual((rec["status"], rec["role"], rec["admin"]), ("active", "approver", True))
        with self.assertRaises(accts.AccountError):
            self.s.make_manager("ghost")

    def test_unreadable_file_is_never_overwritten(self):
        self.path.write_text("{not json")
        with self.assertRaises(accts.AccountError):
            self.reg()
        self.assertEqual(self.path.read_text(), "{not json")


# ============================================================================ allowed domains
class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "domains.json"
        self.d = accts.DomainPolicy(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_list_allows_nobody(self):
        self.assertEqual(self.d.list(), [])
        self.assertFalse(self.d.allows("pvellure@deloitte.ca"))

    def test_exact_domains_and_case(self):
        self.d.add("Deloitte.ca")
        self.d.add("@gov.bc.ca")
        self.assertEqual(self.d.list(), ["deloitte.ca", "gov.bc.ca"])
        for ok in ("pvellure@deloitte.ca", "X@DELOITTE.CA", "someone@gov.bc.ca"):
            self.assertTrue(self.d.allows(ok), ok)
        for no in ("a@evildeloitte.ca", "a@deloitte.ca.evil.com", "a@sub.deloitte.ca", "a@deloitte.com", "deloitte.ca",
                   "a@gov.bc.ca@evil.com", "", "a@déloitte.ca"):
            self.assertFalse(self.d.allows(no), no)

    def test_wildcard_means_sub_domains_only(self):
        self.d.add("*.gov.bc.ca")
        self.assertTrue(self.d.allows("a@justice.gov.bc.ca"))
        self.assertTrue(self.d.allows("a@x.y.gov.bc.ca"))
        self.assertFalse(self.d.allows("a@gov.bc.ca"))
        self.assertFalse(self.d.allows("a@notgov.bc.ca"))

    def test_bad_entries_are_refused(self):
        for bad in ("", "deloitte", "*.ca.", "http://deloitte.ca", "dél.ca", "a b.ca", "*.*.ca", "-x.ca"):
            with self.subTest(bad=bad), self.assertRaises(accts.AccountError):
                self.d.add(bad)

    def test_add_is_idempotent_and_remove_works(self):
        self.d.add("deloitte.ca")
        self.d.add("deloitte.ca")
        self.assertEqual(self.d.list(), ["deloitte.ca"])
        self.assertTrue(self.d.remove("deloitte.ca"))
        self.assertFalse(self.d.remove("deloitte.ca"))
        self.assertEqual(self.d.list(), [])

    def test_changes_made_elsewhere_are_seen(self):
        other = accts.DomainPolicy(self.path)
        other.add("gov.bc.ca")
        self.assertTrue(self.d.allows("a@gov.bc.ca"))

    def test_unreadable_file_allows_nobody_and_is_kept(self):
        self.path.write_text("{oops")
        self.assertFalse(self.d.allows("a@deloitte.ca"))
        self.assertIn("unreadable", self.d.error)
        with self.assertRaises(accts.AccountError):
            self.d.add("deloitte.ca")
        self.assertEqual(self.path.read_text(), "{oops")

    def test_mask_email(self):
        self.assertEqual(accts.mask_email("pvellure@deloitte.ca"), "p\u2022\u2022\u2022\u2022\u2022\u2022@deloitte.ca")
        self.assertEqual(accts.mask_email("ab@x.ca"), "a\u2022\u2022@x.ca")


# ============================================================================ verification codes
class VerificationTests(unittest.TestCase):
    D = {"username": "alice", "name": "Alice Example", "email": "alice@deloitte.ca", "pw": "scrypt$hash", "reason": ""}

    def setUp(self):
        self.clock = Clock()
        self.v = accts.VerificationStore(clock=self.clock, ttl_s=900)

    def test_code_is_six_digits_and_only_its_hash_is_kept(self):
        vid, code, rec = self.v.start(dict(self.D))
        self.assertRegex(code, r"^\d{6}$")
        self.assertNotIn(code, json.dumps({k: v for k, v in rec.items() if k != "salt"}, default=str))
        self.assertGreaterEqual(len(vid), 30)

    def test_right_code_works_once(self):
        vid, code, _ = self.v.start(dict(self.D))
        self.assertEqual(self.v.check(vid, f" {code[:3]} {code[3:]} ")["username"], "alice")
        with self.assertRaises(accts.AccountError) as cm:
            self.v.check(vid, code)
        self.assertEqual(cm.exception.code, "not_found")

    def test_code_expires_after_fifteen_minutes(self):
        vid, code, _ = self.v.start(dict(self.D))
        self.clock.t += 900
        rec = self.v._open[vid]                                   # still present until purged
        self.assertLessEqual(rec["expires"], self.clock.t)
        with self.assertRaises(accts.AccountError) as cm:
            self.v.check(vid, code)
        self.assertEqual(cm.exception.code, "expired")

    def test_five_wrong_codes_end_the_request(self):
        vid, code, _ = self.v.start(dict(self.D))
        wrong = "000000" if code != "000000" else "111111"
        for left in (4, 3, 2, 1):
            with self.assertRaises(accts.AccountError) as cm:
                self.v.check(vid, wrong)
            self.assertIn(f"{left} attempt", cm.exception.message)
        with self.assertRaises(accts.AccountError) as cm:
            self.v.check(vid, wrong)
        self.assertEqual(cm.exception.code, "too_many_attempts")
        with self.assertRaises(accts.AccountError):
            self.v.check(vid, code)                                # even the right code is useless now

    def test_badly_formed_code_does_not_use_an_attempt(self):
        vid, code, _ = self.v.start(dict(self.D))
        for bad in ("", "12345", "abcdef", "1234567"):
            with self.assertRaises(accts.AccountError):
                self.v.check(vid, bad)
        self.assertEqual(self.v.check(vid, code)["email"], "alice@deloitte.ca")

    def test_resend_waits_a_minute_replaces_the_code_and_resets_expiry(self):
        vid, code, _ = self.v.start(dict(self.D))
        with self.assertRaises(accts.AccountError) as cm:
            self.v.resend(vid)
        self.assertEqual(cm.exception.code, "too_soon")
        self.clock.t += 600
        new, rec = self.v.resend(vid)
        self.assertEqual(rec["expires"], self.clock.t + 900)
        if new != code:
            with self.assertRaises(accts.AccountError):
                self.v.check(vid, code)                            # the old code no longer works
        self.assertEqual(self.v.check(vid, new)["username"], "alice")

    def test_resend_limit(self):
        vid, _, _ = self.v.start(dict(self.D))
        for _ in range(3):
            self.clock.t += 61
            self.v.resend(vid)
        self.clock.t += 61
        with self.assertRaises(accts.AccountError) as cm:
            self.v.resend(vid)
        self.assertEqual(cm.exception.code, "too_many_codes")

    def test_new_start_for_same_email_replaces_the_old_one(self):
        vid1, code1, _ = self.v.start(dict(self.D))
        vid2, code2, _ = self.v.start(dict(self.D))
        with self.assertRaises(accts.AccountError):
            self.v.check(vid1, code1)
        self.assertEqual(self.v.check(vid2, code2)["username"], "alice")

    def test_starts_per_email_are_limited(self):
        for _ in range(5):
            self.v.start(dict(self.D))
        with self.assertRaises(accts.AccountError) as cm:
            self.v.start(dict(self.D))
        self.assertEqual(cm.exception.code, "rate_limited")
        self.clock.t += 3601
        self.v.start(dict(self.D))

    def test_cancel(self):
        vid, code, _ = self.v.start(dict(self.D))
        self.v.cancel(vid)
        with self.assertRaises(accts.AccountError):
            self.v.check(vid, code)


# ============================================================================ e-mail
class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = accts.AccountStore(Path(self.tmp.name) / "a.json")
        self.store.bootstrap_admin("boss", "The Boss", "boss@example.com", GOOD2)
        self.sent, self.results = [], []
        self.env = {"CONSOLE_SMTP_HOST": "smtp.example.com", "CONSOLE_SMTP_FROM": "console@example.com",
                    "CONSOLE_BASE_URL": "https://console.example.com/"}

    def tearDown(self):
        self.tmp.cleanup()

    def n(self, **env):
        return accts.Notifier(self.store, dict(self.env, **env), sender=self.sent.append,
                              on_result=lambda *a: self.results.append(a), threaded=False)

    REC = {"username": "alice", "name": "Alice Example", "email": "alice@example.com", "reason": "Need access"}

    def test_code_mail(self):
        self.n().send_code("alice@example.com", "Alice Example", "123456", 15)
        m = self.sent[-1]
        self.assertEqual(m["To"], "alice@example.com")
        self.assertNotIn("123456", m["Subject"])
        self.assertEqual(code_in(m), "123456")
        self.assertIn("expires in 15 minutes", m.get_content())

    def test_code_mail_fails_loudly_when_email_is_off(self):
        with self.assertRaises(RuntimeError):
            accts.Notifier(self.store, {}, sender=self.sent.append).send_code("a@b.ca", "A", "123456", 15)

    def test_request_goes_to_managers(self):
        self.assertTrue(self.n().request_submitted(self.REC))
        self.assertEqual(self.sent[-1]["To"], "boss@example.com")
        self.assertIn("verified", self.sent[-1].get_content())

    def test_headers_cannot_be_injected(self):
        self.n().request_submitted(dict(self.REC, name="Eve\r\nBcc: attacker@evil.example"))
        self.assertNotIn("\n", self.sent[-1]["Subject"])
        self.assertIsNone(self.sent[-1]["Bcc"])

    def test_failure_is_reported_not_raised(self):
        def boom(m):
            raise ConnectionRefusedError("smtp down")
        n = accts.Notifier(self.store, self.env, sender=boom, on_result=lambda *a: self.results.append(a), threaded=False)
        self.assertTrue(n.request_submitted(self.REC))
        self.assertEqual(self.results[-1], ("request", False, "ConnectionRefusedError"))


# ============================================================================ the Windows prompt (unchanged from v13)
class WindowsLoginTests(unittest.TestCase):
    def verifier(self, method, hello=None, password=None):
        return wl.WindowsVerifier(method, identity=lambda: "CORP\\pvellure",
                                  hello=hello or (lambda m: ("verified", "Verified")),
                                  password=password or (lambda m: "CORP\\bob"))

    def test_parse_hello_output(self):
        p = wl.parse_hello
        self.assertEqual(p("noise\nRESULT:Verified\n")[0], "verified")
        self.assertEqual(p("RESULT:Canceled")[0], "canceled")
        self.assertEqual(p("AVAILABILITY:DeviceNotPresent")[0], "unavailable")
        self.assertEqual(p("")[0], "error")

    def test_hello_then_password_fallback(self):
        self.assertEqual(self.verifier("auto")()["method"], "hello")
        got = self.verifier("auto", hello=lambda m: ("unavailable", "x"))()
        self.assertEqual((got["_windows"], got["method"]), ("CORP\\bob", "password"))

    def test_cancel_does_not_fall_back(self):
        with self.assertRaises(PermissionError):
            self.verifier("auto", hello=lambda m: ("canceled", "x"), password=lambda m: self.fail("no"))()


class WindowsModeTests(unittest.TestCase):
    def test_windows_mode_ignores_domains_and_uses_the_access_list(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            access = Path(d) / "access.json"
            access.write_text(json.dumps({"users": [{"windows": "CORP\\pvellure", "role": "approver"}]}))
            g = ca.AuthGate(ca.AuthConfig({"CONSOLE_AUTH_MODE": "windows", "CONSOLE_ACCESS_FILE": str(access)}),
                            Path(d) / "audit.jsonl", console_token=TOK,
                            verifier=lambda: {"_windows": "CORP\\pvellure", "method": "hello"})
            self.assertEqual(g._authorise({"_windows": "CORP\\pvellure", "method": "hello"})["role"], "approver")


# ============================================================================ accounts mode over real HTTP
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


class AccountsHTTP(unittest.TestCase):
    EXTRA_ENV = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.mail, self.mail_fail = [], False
        env = {"CONSOLE_AUTH_MODE": "accounts", "CONSOLE_DATA_DIR": str(d), "CONSOLE_ACCESS_FILE": str(d / "access.json"),
               "CONSOLE_SMTP_HOST": "smtp.example.com", "CONSOLE_SMTP_FROM": "console@example.com"}
        env.update(self.EXTRA_ENV)
        self.cfg = ca.AuthConfig(env)
        self.clock = Clock(time.time())
        self.store = accts.AccountStore(self.cfg.accounts_file, min_len=12, clock=self.clock)
        self.store.bootstrap_admin("boss", "The Boss", "boss@deloitte.ca", GOOD2)
        self.domains = accts.DomainPolicy(self.cfg.domains_file)
        self.domains.add("deloitte.ca")
        self.domains.add("gov.bc.ca")

        def sender(m):
            if self.mail_fail:
                raise ConnectionRefusedError("smtp down")
            self.mail.append(m)

        notifier = accts.Notifier(self.store, env, sender=sender, threaded=False,
                                  on_result=lambda *a: self.gate.audit("mail", kind=a[0], ok=a[1]))
        self.gate = ca.AuthGate(self.cfg, d / "logs" / "audit.jsonl", port=7, console_token=TOK, accounts=self.store,
                                notifier=notifier, domains=self.domains, clock=self.clock)
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
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.audit = d / "logs" / "audit.jsonl"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def events(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def details(self, user="alice", email=None, **kw):
        b = dict(username=user, name="Alice Example", email=email or f"{user}@deloitte.ca", password=GOOD,
                 reason="Need to watch runs")
        b.update(kw)
        return b

    def start(self, c=None, **kw):
        c = c or Client(self.port)
        return c, c.req("POST", "/auth/register/start", self.details(**kw))

    def register(self, user="alice", email=None):
        c, (st, js, _, _) = self.start(user=user, email=email)
        self.assertEqual(st, 202, js)
        st2, js2, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})
        self.assertEqual(st2, 201, js2)
        return js2

    def signed_boss(self):
        c = Client(self.port)
        self.assertEqual(c.req("POST", "/auth/login", {"username": "boss", "password": GOOD2})[0], 200)
        return c

    def approved(self, user="alice", role="viewer", email=None):
        self.register(user, email)
        st, js, _, _ = self.signed_boss().req("POST", "/auth/admin/decide", {"username": user, "decision": "approve", "role": role})
        self.assertEqual(st, 200, js)
        c = Client(self.port)
        self.assertEqual(c.req("POST", "/auth/login", {"username": user, "password": GOOD})[0], 200)
        return c

    # ---- the whole journey
    def test_details_then_code_then_manager_then_sign_in(self):
        c, (st, js, _, _) = self.start()
        self.assertEqual((st, js["state"], js["email"]), (202, "verify", accts.mask_email("alice@deloitte.ca")))
        self.assertTrue(880 <= js["expires_in_s"] <= 900, js)
        self.assertEqual(self.mail[-1]["To"], "alice@deloitte.ca")
        self.assertIsNone(self.store.get("alice"))                     # nothing reaches managers before the code
        st, js2, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})
        self.assertEqual((st, js2["state"]), (201, "pending"))
        self.assertEqual(self.store.get("alice")["status"], "pending")
        self.assertEqual(self.mail[-1]["To"], "boss@deloitte.ca")       # manager told only after verification
        _, (st, js3, _, _) = (None, Client(self.port).req("POST", "/auth/login", {"username": "alice", "password": GOOD}))
        self.assertEqual((st, js3["error"]), (403, "pending"))
        boss = self.signed_boss()
        self.assertEqual(boss.req("POST", "/auth/admin/decide", {"username": "alice", "decision": "approve", "role": "operator"})[0], 200)
        u = Client(self.port)
        self.assertEqual(u.req("POST", "/auth/login", {"username": "alice", "password": GOOD})[0], 200)
        self.assertEqual(u.req("POST", "/api/start", {})[0], 200)
        kinds = [e["event"] for e in self.events()]
        for k in ("verification_sent", "email_verified", "registration_requested", "access_approved", "sign_in"):
            self.assertIn(k, kinds)

    def test_old_single_step_route_is_refused(self):
        st, js, _, _ = Client(self.port).req("POST", "/auth/register", self.details())
        self.assertEqual((st, js["error"]), (400, "verification_required"))
        self.assertIsNone(self.store.get("alice"))

    # ---- domains
    def test_email_domain_must_be_allowed(self):
        for email in ("alice@gmail.com", "alice@evildeloitte.ca", "alice@deloitte.ca.evil.com", "alice@sub.deloitte.ca"):
            with self.subTest(email=email):
                _, (st, js, _, _) = self.start(email=email)
                self.assertEqual((st, js["error"]), (403, "domain_not_allowed"))
        self.assertEqual(self.mail, [])                                # no email sent to outsiders
        _, (st, _, _, _) = self.start(user="carl", email="carl@gov.bc.ca")
        self.assertEqual(st, 202)

    def test_outsiders_learn_nothing_about_usernames(self):
        _, (st, js, _, _) = self.start(user="boss", email="x@gmail.com")
        self.assertEqual(js["error"], "domain_not_allowed")             # not "username_taken"

    def test_wildcard_domain(self):
        self.domains.add("*.gov.bc.ca")
        _, (st, _, _, _) = self.start(user="dana", email="dana@justice.gov.bc.ca")
        self.assertEqual(st, 202)

    def test_no_domains_means_requests_are_closed(self):
        for d in self.domains.list():
            self.domains.remove(d)
        _, (st, js, _, _) = self.start()
        self.assertEqual((st, js["error"]), (403, "registration_closed"))

    def test_removing_a_domain_ends_sessions_and_blocks_sign_in_but_not_managers(self):
        c = self.approved("carl", email="carl@gov.bc.ca")
        boss = self.signed_boss()
        self.assertEqual(c.req("GET", "/api/state")[0], 200)
        st, js, _, _ = boss.req("POST", "/auth/admin/domains", {"action": "remove", "domain": "gov.bc.ca"})
        self.assertEqual(st, 200)
        self.assertEqual(c.req("GET", "/api/state")[0], 401)           # open session ended
        st, js, _, _ = Client(self.port).req("POST", "/auth/login", {"username": "carl", "password": GOOD})
        self.assertEqual((st, js["error"]), (403, "domain_not_allowed"))
        self.domains.remove("deloitte.ca")
        self.assertEqual(boss.req("GET", "/api/state")[0], 200)        # managers keep access
        ev = [e for e in self.events() if e["event"] == "domain_removed"][0]
        self.assertEqual((ev["domain"], ev["by"], ev["affected_users"]), ("gov.bc.ca", "boss", 1))

    def test_wrong_password_never_reveals_the_domain_state(self):
        self.approved("carl", email="carl@gov.bc.ca")
        self.domains.remove("gov.bc.ca")
        st, js, _, _ = Client(self.port).req("POST", "/auth/login", {"username": "carl", "password": "wrong password!!"})
        self.assertEqual(js["error"], "invalid_credentials")

    def test_domain_removed_while_the_code_was_out(self):
        c, (st, js, _, _) = self.start(user="carl", email="carl@gov.bc.ca")
        self.domains.remove("gov.bc.ca")
        st, js2, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})
        self.assertEqual((st, js2["error"]), (403, "domain_not_allowed"))
        self.assertIsNone(self.store.get("carl"))

    def test_manager_cannot_approve_a_request_whose_domain_was_removed(self):
        self.register("carl", "carl@gov.bc.ca")
        self.domains.remove("gov.bc.ca")
        st, js, _, _ = self.signed_boss().req("POST", "/auth/admin/decide", {"username": "carl", "decision": "approve", "role": "viewer"})
        self.assertEqual((st, js["error"]), (409, "domain_not_allowed"))

    def test_manager_domain_api(self):
        boss = self.signed_boss()
        st, js, _, _ = boss.req("GET", "/auth/admin/domains")
        self.assertEqual([d["domain"] for d in js["domains"]], ["deloitte.ca", "gov.bc.ca"])
        st, js, _, _ = boss.req("POST", "/auth/admin/domains", {"action": "add", "domain": "*.Gov.BC.ca"})
        self.assertEqual((st, js["domains"][-1]["domain"]), (200, "*.gov.bc.ca"))
        for bad in ({"action": "add", "domain": "not a domain"}, {"action": "remove", "domain": "x.ca"}, {"action": "zap"}):
            self.assertEqual(boss.req("POST", "/auth/admin/domains", bad)[0], 400)
        self.assertIn("domain_added", [e["event"] for e in self.events()])

    def test_only_managers_can_change_domains(self):
        c = self.approved(role="approver")                              # approver is not a manager
        self.assertEqual(c.req("GET", "/auth/admin/domains")[0], 403)
        self.assertEqual(c.req("POST", "/auth/admin/domains", {"action": "add", "domain": "evil.com"})[0], 403)
        self.assertEqual(Client(self.port).req("POST", "/auth/admin/domains", {"action": "add", "domain": "evil.com"})[0], 401)
        self.assertNotIn("evil.com", self.domains.list())

    def test_admin_page_has_the_domain_section_and_no_innerHTML(self):
        st, _, body, r = self.signed_boss().req("GET", "/auth/admin", accept="text/html")
        self.assertEqual(st, 200)
        self.assertIn("Allowed email domains", body)
        self.assertNotIn("innerHTML", body)
        self.assertNotIn("unsafe-inline", r.getheader("Content-Security-Policy"))

    # ---- codes over HTTP
    def test_wrong_code_then_right_code(self):
        c, (_, js, _, _) = self.start()
        code = code_in(self.mail[-1])
        wrong = "000000" if code != "000000" else "111111"
        st, js2, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": wrong})
        self.assertEqual((st, js2["error"]), (400, "invalid_code"))
        self.assertEqual(c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code})[0], 201)

    def test_expired_code(self):
        c, (_, js, _, _) = self.start()
        self.clock.t += 15 * 60 + 1
        st, js2, _, _ = c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})
        self.assertEqual((st, js2["error"]), (410, "expired"))
        self.assertIsNone(self.store.get("alice"))

    def test_resend_over_http(self):
        c, (_, js, _, _) = self.start()
        st, js2, _, _ = c.req("POST", "/auth/register/resend", {"id": js["id"]})
        self.assertEqual((st, js2["error"]), (429, "too_soon"))
        self.assertGreater(js2["resend_in_s"], 0)
        self.clock.t += 61
        st, js3, _, _ = c.req("POST", "/auth/register/resend", {"id": js["id"]})
        self.assertEqual(st, 200)
        self.assertEqual(len(self.mail), 2)
        self.assertEqual(c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code_in(self.mail[-1])})[0], 201)

    def test_email_failure_is_reported_and_the_code_is_not_left_valid(self):
        self.mail_fail = True
        c, (st, js, _, _) = self.start()
        self.assertEqual((st, js["error"]), (502, "email_failed"))
        self.assertEqual(self.gate.verifications._open, {})

    def test_step_one_checks_everything_before_sending(self):
        for kw, code in ((dict(password="short"), "weak_password"), (dict(user="boss"), "username_taken"),
                         (dict(user="b!"), "bad_username"), (dict(name=""), "bad_name"), (dict(email="nope"), "bad_email"),
                         (dict(user="zed", email="boss@deloitte.ca"), "email_taken")):
            with self.subTest(code=code):
                _, (st, js, _, _) = self.start(**kw)
                self.assertEqual(js["error"], code)
        self.assertEqual(self.mail, [])

    def test_registration_endpoints_need_the_console_token(self):
        c = Client(self.port)
        for p in ("/auth/register/start", "/auth/register/verify", "/auth/register/resend"):
            self.assertEqual(c.req("POST", p, {}, token=None)[0], 401)

    def test_registration_can_be_switched_off(self):
        self.cfg.allow_registration = False
        _, (st, _, _, _) = self.start()
        self.assertEqual(st, 404)

    def test_audit_never_holds_passwords_or_codes(self):
        c, (_, js, _, _) = self.start()
        code = code_in(self.mail[-1])
        c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code})
        Client(self.port).req("POST", "/auth/login", {"username": "hunter2-my-secret-password", "password": "x"})
        raw = self.audit.read_text()
        for secret in (GOOD, GOOD2, code, js["id"], "hunter2-my-secret-password"):
            self.assertNotIn(secret, raw)

    def test_keep_alive_stays_in_step_after_refusals(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        hdr = {"Host": f"127.0.0.1:{self.port}", "X-Console-Token": TOK, "Content-Type": "application/json"}
        for i in range(4):
            for path, body, want in (("/auth/admin/decide", {"username": "x"}, 401),
                                     ("/auth/register/start", self.details(email=f"x{i}@gmail.com"), 403),
                                     ("/auth/register/verify", {"id": "nope", "code": "123456"}, 410),
                                     ("/auth/login", {"username": f"nobody{i}", "password": "nope nope nope"}, 401)):
                c.request("POST", path, body=json.dumps(body), headers=hdr)
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, want, path)
            c.request("GET", "/auth/signin", headers={"Host": hdr["Host"], "Accept": "text/html"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
        c.close()

    # ---- still true from v13
    def test_manager_rules_and_role_changes(self):
        c = self.approved(role="viewer")
        self.assertEqual(c.req("POST", "/api/start", {})[0], 403)
        boss = self.signed_boss()
        boss.req("POST", "/auth/admin/user", {"username": "alice", "role": "operator"})
        self.assertEqual(c.req("POST", "/api/start", {})[0], 200)
        boss.req("POST", "/auth/admin/user", {"username": "alice", "disabled": True})
        self.assertEqual(c.req("GET", "/api/state")[0], 401)
        st, js, _, _ = boss.req("POST", "/auth/admin/user", {"username": "boss", "disabled": True})
        self.assertEqual(js["error"], "last_admin")

    def test_lockout_returns_429(self):
        for _ in range(5):
            Client(self.port).req("POST", "/auth/login", {"username": "boss", "password": "wrong password here"})
        st, js, _, _ = Client(self.port).req("POST", "/auth/login", {"username": "boss", "password": GOOD2})
        self.assertEqual((st, js["error"]), (429, "locked"))

    def test_signin_page_is_the_v14_page(self):
        st, _, page, r = Client(self.port).req("GET", "/auth/signin", token=None, accept="text/html")
        self.assertEqual(st, 200)
        for needle in ('const MODE="accounts"', "const REG=true", "Verify your email", "Step 2 of 2", "const MINUTES=15"):
            self.assertIn(needle, page)
        self.assertNotIn("PROOF OF CONCEPT", page.upper())


class LogDeliveryHTTP(AccountsHTTP):
    """CONSOLE_VERIFY_DELIVERY=log: codes go to a file instead of e-mail (local testing)."""
    EXTRA_ENV = {"CONSOLE_VERIFY_DELIVERY": "log"}
    for _name in [n for n in dir(AccountsHTTP) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_code_is_written_to_the_log_file_not_emailed(self):
        c, (st, js, _, _) = self.start()
        self.assertEqual(st, 202)
        self.assertEqual(self.mail, [])
        text = (self.audit.parent / "verification-codes.log").read_text()
        self.assertIn("alice@deloitte.ca", text)
        code = re.search(r"code (\d{6})", text).group(1)
        self.assertEqual(c.req("POST", "/auth/register/verify", {"id": js["id"], "code": code})[0], 201)
        self.assertNotIn(code, self.audit.read_text())


# ============================================================================ start-up checks and the command line
class StartupAndCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.d = Path(self.tmp.name)
        self.keys = ("JIRA_API_TOKEN", "GITHUB_TOKEN", "CONSOLE_AUTH_MODE", "CONSOLE_DATA_DIR", "CONSOLE_ACCESS_FILE",
                     "CONSOLE_SMTP_HOST", "CONSOLE_SMTP_FROM", "CONSOLE_VERIFY_DELIVERY", "CONSOLE_ALLOW_REGISTRATION")
        self.saved = {k: os.environ.get(k) for k in self.keys}
        os.environ.update({"JIRA_API_TOKEN": "t" * 12, "GITHUB_TOKEN": "t" * 12, "CONSOLE_AUTH_MODE": "accounts",
                           "CONSOLE_DATA_DIR": str(self.d), "CONSOLE_ACCESS_FILE": str(self.d / "access.json")})
        for k in ("CONSOLE_SMTP_HOST", "CONSOLE_SMTP_FROM", "CONSOLE_VERIFY_DELIVERY", "CONSOLE_ALLOW_REGISTRATION"):
            os.environ.pop(k, None)
        self.real = ca.getpass.getpass
        ca.getpass.getpass = lambda prompt="": GOOD2

    def tearDown(self):
        ca.getpass.getpass = self.real
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def check(self):
        try:
            return ca.startup_check(self.d / "agent").mode
        except RuntimeError as e:
            return str(e)

    def test_startup_walks_through_each_missing_piece(self):
        self.assertIn("bootstrap-admin", self.check())
        accts.AccountStore(self.d / "accounts.json").bootstrap_admin("boss", "The Boss", "boss@deloitte.ca", GOOD2)
        self.assertIn("domains add", self.check())
        accts.DomainPolicy(self.d / "domains.json").add("deloitte.ca")
        msg = self.check()
        self.assertIn("CONSOLE_SMTP_HOST", msg)
        self.assertIn("CONSOLE_VERIFY_DELIVERY=log", msg)
        os.environ["CONSOLE_VERIFY_DELIVERY"] = "log"
        self.assertEqual(self.check(), "accounts")
        os.environ.pop("CONSOLE_VERIFY_DELIVERY")
        os.environ["CONSOLE_ALLOW_REGISTRATION"] = "false"
        self.assertEqual(self.check(), "accounts")
        os.environ.pop("CONSOLE_ALLOW_REGISTRATION")
        os.environ.update({"CONSOLE_SMTP_HOST": "smtp.example.com", "CONSOLE_SMTP_FROM": "c@example.com"})
        self.assertEqual(self.check(), "accounts")

    def test_bad_settings_are_refused(self):
        for k, v in (("CONSOLE_VERIFY_CODE_MINUTES", "2"), ("CONSOLE_VERIFY_CODE_MINUTES", "120"), ("CONSOLE_VERIFY_DELIVERY", "sms")):
            with self.subTest(k=k, v=v), self.assertRaises(ca.AuthConfigError):
                ca.AuthConfig({"CONSOLE_AUTH_MODE": "accounts", k: v})

    def test_cli_domains_bootstrap_and_make_manager(self):
        self.assertEqual(ca._cli(["bootstrap-admin", "--user", "chief", "--email", "chief@deloitte.ca"]), 0)
        self.assertEqual(accts.DomainPolicy(self.d / "domains.json").list(), ["deloitte.ca"])   # added automatically
        self.assertEqual(ca._cli(["domains", "add", "gov.bc.ca"]), 0)
        self.assertEqual(ca._cli(["domains", "add", "not a domain"]), 1)
        self.assertEqual(ca._cli(["domains"]), 0)
        self.assertEqual(ca._cli(["domains", "remove", "gov.bc.ca"]), 0)
        self.assertEqual(ca._cli(["domains", "remove", "gov.bc.ca"]), 1)
        self.assertEqual(accts.DomainPolicy(self.d / "domains.json").list(), ["deloitte.ca"])
        accts.AccountStore(self.d / "accounts.json").register("erin", "Erin Example", "erin@deloitte.ca", GOOD)
        self.assertEqual(ca._cli(["make-manager", "--user", "erin"]), 0)
        self.assertTrue(accts.AccountStore(self.d / "accounts.json").get("erin")["admin"])
        self.assertEqual(ca._cli(["bootstrap-admin", "--user", "chief", "--email", "chief@deloitte.ca"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
