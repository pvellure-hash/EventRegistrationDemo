"""Offline tests for platform_admin.py: the register, the verification rules, signing, invitations and the audit chain.
Run from agent\\:  python test_platform_admin.py"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_admin as pa  # noqa: E402
import platform_trust as pt  # noqa: E402

ALICE, BOB, CARA = "WIN\\alice", "WIN\\bob", "WIN\\cara"


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


def token_in(path) -> str:
    return re.search(r"Your invitation code:\s+(\S+)", Path(path).read_text(encoding="utf-8")).group(1)


class Base(unittest.TestCase):
    MIN = 1

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name) / "platform"
        self.out = Path(self.tmp.name) / "grants"
        self.who, self.clock, self.sent = [ALICE], Clock(), []
        self.keys = pa.MemoryKeyring()
        self.pl = pa.Platform(self.dir, self.keys, identity=lambda: self.who[0], clock=self.clock, min_verifiers=self.MIN,
                              sender=lambda to, subject, body: self.sent.append((to, subject, body)))
        self.pub = self.pl.create_keys()

    def tearDown(self):
        self.tmp.cleanup()

    def as_(self, who):
        self.who[0] = who

    def project(self, **kw):
        args = dict(name="Ministry X intake", client="Ministry X", lead_email="lead@gov.bc.ca", lead_name="Lead Person",
                    domains=["gov.bc.ca"], hosting="client-cloud")
        args.update(kw)
        return self.pl.new_project(**args)

    def onboard(self, verifiers=(BOB,), **kw):
        proj, req = self.project(**kw)
        for v in verifiers:
            self.as_(v)
            self.pl.verify(req["id"], "checked with the engagement lead")
        self.as_(ALICE)
        return proj, req

    def issue(self, req, **kw):
        kw.setdefault("out_dir", self.out)
        return self.pl.issue(req["id"], **kw)

    def trust(self, tmp_name="instance"):
        return pt.InstanceTrust(Path(self.tmp.name) / tmp_name, self.pub, clock=self.clock)

    def apply(self, trust, result):
        return trust.apply(Path(result["grant_file"]).read_text(encoding="utf-8"))


# ============================================================================ audit chain
class AuditTests(Base):
    def test_chain_is_intact_and_continues_across_instances(self):
        self.pl.audit.append("one", ALICE, a=1)
        again = pa.ChainedAudit(self.dir / "platform-audit.jsonl", self.clock)
        again.append("two", BOB)
        ok, n, bad = again.verify()
        self.assertEqual((ok, bad), (True, None))
        self.assertGreaterEqual(n, 3)                                       # keys_created + one + two

    def _edit(self, fn):
        lines = (self.dir / "platform-audit.jsonl").read_text().splitlines()
        (self.dir / "platform-audit.jsonl").write_text("\n".join(fn(lines)) + "\n")

    def test_editing_a_line_is_detected(self):
        self.project()
        self._edit(lambda ls: [ls[0]] + [ls[1].replace("Ministry X intake", "Another")] + ls[2:])
        ok, _, bad = self.pl.audit.verify()
        self.assertEqual((ok, bad), (False, 2))

    def test_deleting_or_swapping_lines_is_detected(self):
        self.project()
        self.pl.audit.append("extra", ALICE)
        self._edit(lambda ls: ls[:1] + ls[2:])
        self.assertFalse(self.pl.audit.verify()[0])
        self._edit(lambda ls: ls)                                            # still broken
        self.assertFalse(self.pl.audit.verify()[0])

    def test_garbage_line_is_detected(self):
        self._edit(lambda ls: ls + ["not json"])
        self.assertEqual(self.pl.audit.verify()[:1], (False,))

    def test_empty_log_is_fine(self):
        self.assertEqual(pa.ChainedAudit(self.dir / "none.jsonl").verify(), (True, 0, None))


# ============================================================================ keys
class KeyTests(Base):
    def test_key_is_created_once_and_never_printed_or_logged(self):
        seed = self.keys.get()
        self.assertEqual(pt.public_from_seed(seed), self.pub)
        with self.assertRaises(pa.PlatformError) as cm:
            self.pl.create_keys()
        self.assertEqual(cm.exception.code, "key_exists")
        self.assertEqual(self.keys.get(), seed)
        for f in self.dir.glob("*"):
            self.assertNotIn(seed, f.read_text())

    def test_rotation_needs_force_and_is_logged(self):
        old = self.keys.get()
        new_pub = self.pl.create_keys(force=True)
        self.assertNotEqual(self.keys.get(), old)
        self.assertNotEqual(new_pub, self.pub)
        self.assertTrue(any(e["event"] == "keys_created" and e.get("replaced") for e in self.pl.audit.entries()))

    def test_no_key_means_nothing_can_be_signed(self):
        pl = pa.Platform(self.dir, pa.MemoryKeyring(), identity=lambda: ALICE, clock=self.clock)
        with self.assertRaises(pa.PlatformError) as cm:
            pl.public_key()
        self.assertEqual(cm.exception.code, "no_key")


# ============================================================================ requests and verification
class RequestTests(Base):
    def test_new_project_is_pending_and_logged(self):
        proj, req = self.project()
        self.assertRegex(proj["id"], r"^prj-[0-9a-f]{8}$")
        self.assertEqual((proj["status"], proj["serial"], proj["domains"]), ("onboarding", 0, []))
        self.assertEqual((req["kind"], req["status"], req["requested_by"]), ("onboard", "pending", ALICE))
        self.assertEqual(req["params"]["domains"], ["gov.bc.ca"])
        self.assertIn("project_requested", [e["event"] for e in self.pl.audit.entries()])

    def test_validation(self):
        cases = (dict(name="ab"), dict(client="x"), dict(lead_name="x"), dict(hosting="mars"), dict(domains=[]),
                 dict(domains=["not a domain"]), dict(domains=[f"d{i}.example.ca" for i in range(11)]),
                 dict(lead_email="nope"), dict(lead_email="lead@gmail.com"))
        for kw in cases:
            with self.subTest(kw=kw), self.assertRaises(pa.PlatformError):
                self.project(**kw)
        self.assertEqual(self.pl.projects(), [])

    def test_the_lead_must_be_on_one_of_the_domains(self):
        _, req = self.project(domains=["*.gov.bc.ca"], lead_email="lead@justice.gov.bc.ca")
        self.assertEqual(req["params"]["domains"], ["*.gov.bc.ca"])
        with self.assertRaises(pa.PlatformError) as cm:
            self.project(name="Other project", domains=["*.gov.bc.ca"], lead_email="lead@gov.bc.ca")
        self.assertEqual(cm.exception.code, "domain_mismatch")

    def test_duplicate_project_is_refused(self):
        self.project()
        with self.assertRaises(pa.PlatformError) as cm:
            self.project(name="MINISTRY X INTAKE")
        self.assertEqual(cm.exception.code, "duplicate")

    def test_you_cannot_verify_your_own_request_or_verify_twice(self):
        _, req = self.project()
        with self.assertRaises(pa.PlatformError) as cm:
            self.pl.verify(req["id"])
        self.assertEqual(cm.exception.code, "self_verify")
        self.as_(BOB)
        self.pl.verify(req["id"])
        with self.assertRaises(pa.PlatformError) as cm:
            self.pl.verify(req["id"])
        self.assertEqual(cm.exception.code, "already_verified")

    def test_nothing_is_signed_before_verification(self):
        _, req = self.project()
        with self.assertRaises(pa.PlatformError) as cm:
            self.issue(req)
        self.assertEqual(cm.exception.code, "not_verified")
        self.assertFalse(self.out.exists())

    def test_unknown_and_finished_requests(self):
        for fn in (lambda: self.pl.verify("req-000000"), lambda: self.pl.cancel("req-000000"), lambda: self.issue({"id": "req-000000"})):
            with self.assertRaises(pa.PlatformError) as cm:
                fn()
            self.assertEqual(cm.exception.code, "no_request")
        _, req = self.onboard()
        self.issue(req)
        for fn in (lambda: self.issue(req), lambda: self.pl.cancel(req["id"]), lambda: self.pl.verify(req["id"])):
            with self.assertRaises(pa.PlatformError) as cm:
                fn()
            self.assertEqual(cm.exception.code, "not_pending")

    def test_cancel(self):
        _, req = self.project()
        self.assertEqual(self.pl.cancel(req["id"], "wrong client")["status"], "cancelled")
        self.assertEqual(self.pl.requests(), [])
        self.assertEqual(len(self.pl.requests(open_only=False)), 1)

    def test_roster_limits_who_can_act(self):
        pl = pa.Platform(self.dir, self.keys, identity=lambda: self.who[0], clock=self.clock, team=[ALICE, BOB])
        self.as_(CARA)
        for fn in (lambda: pl.new_project("Ministry X intake", "Ministry X", "lead@gov.bc.ca", "Lead Person", ["gov.bc.ca"]),
                   lambda: pl.verify("req-000000"), lambda: pl.create_keys(force=True)):
            with self.assertRaises(pa.PlatformError) as cm:
                fn()
            self.assertEqual(cm.exception.code, "not_on_team")
        self.as_("win\\ALICE")                                               # case does not matter
        pl.new_project("Ministry X intake", "Ministry X", "lead@gov.bc.ca", "Lead Person", ["gov.bc.ca"])

    def test_unreadable_register_is_never_overwritten(self):
        (self.dir / "projects.json").write_text("{oops")
        with self.assertRaises(pa.PlatformError) as cm:
            self.project()
        self.assertEqual(cm.exception.code, "unreadable")
        self.assertEqual((self.dir / "projects.json").read_text(), "{oops")


class SoleOperatorTests(Base):
    MIN = 0

    def test_zero_waives_verification_and_the_audit_log_says_so(self):
        proj, req = self.project()
        r = self.issue(req)                                           # nobody verified: allowed only because MIN is 0
        self.assertEqual(r["serial"], 1)
        issued = [e for e in self.pl.audit.entries() if e["event"] == "grant_issued"][0]
        self.assertTrue(issued["verification_waived"])
        self.assertEqual(issued["verifiers"], "")

    def test_the_default_is_one_and_never_below_zero(self):
        self.assertEqual(pa.Platform(self.dir, self.keys, min_verifiers=-5).min_verifiers, 0)
        self.assertEqual(pa.make_platform(env={"PLATFORM_DATA_DIR": str(self.dir)}, keyring=self.keys).min_verifiers, 1)
        self.assertEqual(pa.make_platform(env={"PLATFORM_DATA_DIR": str(self.dir), "PLATFORM_MIN_VERIFIERS": "x"}, keyring=self.keys).min_verifiers, 1)
        self.assertEqual(pa.make_platform(env={"PLATFORM_DATA_DIR": str(self.dir), "PLATFORM_MIN_VERIFIERS": "2"}, keyring=self.keys).min_verifiers, 2)

    def test_a_normal_audit_line_has_no_waiver_flag(self):
        pl = pa.Platform(self.dir, self.keys, identity=lambda: self.who[0], clock=self.clock, min_verifiers=1)
        _, req = self.project(name="Another intake")
        self.as_(BOB)
        pl.verify(req["id"])
        self.as_(ALICE)
        pl.issue(req["id"], self.out)
        self.assertNotIn("verification_waived", [e for e in pl.audit.entries() if e["event"] == "grant_issued"][0])


class TwoVerifierTests(Base):
    MIN = 2

    def test_two_different_people_are_needed(self):
        _, req = self.project()
        self.as_(BOB)
        self.pl.verify(req["id"])
        self.as_(ALICE)
        with self.assertRaises(pa.PlatformError) as cm:
            self.issue(req)
        self.assertEqual(cm.exception.code, "not_verified")
        self.as_(CARA)
        self.pl.verify(req["id"])
        self.as_(ALICE)
        self.assertEqual(self.issue(req)["serial"], 1)
        issued = [e for e in self.pl.audit.entries() if e["event"] == "grant_issued"][0]
        self.assertEqual(issued["verifiers"], f"{BOB},{CARA}")


# ============================================================================ issuing
class IssueTests(Base):
    def test_the_grant_is_signed_and_an_instance_accepts_it(self):
        proj, req = self.onboard()
        r = self.issue(req, hours=48, console_url="https://console.example.com/")
        self.assertEqual((r["kind"], r["serial"], r["purpose"]), ("project-grant", 1, "initial"))
        env = json.loads(Path(r["grant_file"]).read_text())
        p = pt.verify_envelope(self.pub, env)
        self.assertEqual((p["project_id"], p["domains"], p["admin_email"], p["issued_by"]), (proj["id"], ["gov.bc.ca"], "lead@gov.bc.ca", ALICE))
        self.assertEqual(p["invite_expires"], self.clock.t + 48 * 3600)
        inst = self.trust()
        self.assertEqual(self.apply(inst, r)["serial"], 1)
        self.assertEqual(inst.project()["name"], "Ministry X intake")
        self.assertTrue(inst.token_matches(inst.claimable_invite(False), token_in(r["invitation_file"])))

    def test_the_invitation_text(self):
        _, req = self.onboard()
        r = self.issue(req, console_url="https://console.example.com/")
        text = Path(r["invitation_file"]).read_text(encoding="utf-8")
        for needle in ("lead@gov.bc.ca", "Lead Person", "Ministry X intake", "https://console.example.com/auth/claim",
                       "first project administrator", "expires on", "72 hours", "Nobody on the platform team knows or sets your password"):
            self.assertIn(needle, text)
        self.assertRegex(token_in(r["invitation_file"]), r"^[A-Z2-9]{5}(-[A-Z2-9]{5}){3}$")

    def test_the_invitation_code_is_in_exactly_one_file(self):
        _, req = self.onboard()
        r = self.issue(req)
        token = token_in(r["invitation_file"])
        raw = token.replace("-", "")
        for f in [p for p in Path(self.tmp.name).rglob("*") if p.is_file()]:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if f == Path(r["invitation_file"]):
                continue
            self.assertNotIn(token, text, f.name)
            self.assertNotIn(raw, text, f.name)
        self.assertNotIn("subject_text", r)
        self.assertNotIn(token, json.dumps(r))

    def test_register_and_status_after_issue(self):
        proj, req = self.onboard()
        self.issue(req)
        p, reqs = self.pl.project(proj["id"])
        self.assertEqual((p["status"], p["serial"], p["domains"]), ("invited", 1, ["gov.bc.ca"]))
        self.assertEqual((reqs[0]["status"], reqs[0]["issued"]["serial"]), ("issued", 1))

    def test_hours_are_bounded(self):
        _, req = self.onboard()
        for h in (0, -1, 337):
            with self.assertRaises(pa.PlatformError) as cm:
                self.issue(req, hours=h)
            self.assertEqual(cm.exception.code, "bad_hours")
        self.assertEqual(self.pl.requests()[0]["id"], req["id"])             # still pending: nothing was consumed

    def test_files_are_never_overwritten(self):
        proj, req = self.onboard()
        r1 = self.issue(req)
        r2 = self.pl.reinvite(proj["id"], self.out)
        self.assertNotEqual(r1["grant_file"], r2["grant_file"])
        self.assertTrue(Path(r1["grant_file"]).exists() and Path(r2["grant_file"]).exists())

    def test_reinvite_replaces_the_old_invitation(self):
        proj, req = self.onboard()
        r1 = self.issue(req)
        r2 = self.pl.reinvite(proj["id"], self.out, hours=24)
        self.assertEqual(r2["serial"], 2)
        inst = self.trust()
        self.apply(inst, r1)
        self.apply(inst, r2)
        inv = inst.claimable_invite(False)
        self.assertFalse(inst.token_matches(inv, token_in(r1["invitation_file"])))
        self.assertTrue(inst.token_matches(inv, token_in(r2["invitation_file"])))
        self.assertTrue(any(e["event"] == "invitation_reissued" for e in self.pl.audit.entries()))

    def test_reinvite_needs_an_issued_onboarding(self):
        proj, req = self.onboard()
        with self.assertRaises(pa.PlatformError) as cm:
            self.pl.reinvite(proj["id"], self.out)
        self.assertEqual(cm.exception.code, "not_issued")

    # ---- changing domains
    def test_domain_change(self):
        proj, req = self.onboard()
        self.issue(req)
        inst = self.trust()
        self.apply(inst, {"grant_file": next(self.out.rglob("project-grant-1.json"))})
        dreq = self.pl.change_domains(proj["id"], add=["Deloitte.ca"], remove=[], reason="Deloitte staff join the project")
        self.as_(BOB)
        self.pl.verify(dreq["id"])
        self.as_(ALICE)
        r = self.issue(dreq)
        self.assertEqual((r["kind"], r["serial"]), ("domain-grant", 2))
        self.apply(inst, r)
        self.assertEqual(inst.effective_domains(), ["deloitte.ca", "gov.bc.ca"])
        self.assertEqual(self.pl.project(proj["id"])[0]["domains"], ["deloitte.ca", "gov.bc.ca"])

    def test_domain_change_rules(self):
        proj, req = self.onboard()
        with self.assertRaises(pa.PlatformError) as cm:
            self.pl.change_domains(proj["id"], add=["a.ca"], reason="x")
        self.assertEqual(cm.exception.code, "not_issued")
        self.issue(req)
        for kw, code in ((dict(add=[], remove=[], reason="x"), "nothing"), (dict(add=["a.ca"], reason=""), "reason"),
                         (dict(remove=["other.ca"], reason="x"), "not_allowed"), (dict(add=["bad domain"], reason="x"), "bad_domain")):
            with self.subTest(code=code), self.assertRaises(pa.PlatformError) as cm:
                self.pl.change_domains(proj["id"], **kw)
            self.assertEqual(cm.exception.code, code)

    # ---- recovering an administrator
    def test_recovery(self):
        proj, req = self.onboard()
        r1 = self.issue(req)
        inst = self.trust()
        self.apply(inst, r1)
        inst.consume(inst.claimable_invite(False)["invite_hash"], by="lead")        # the first administrator claimed it
        rec = self.pl.recover_admin(proj["id"], "new.lead@gov.bc.ca", "New Lead", "The lead left the project")
        self.as_(BOB)
        self.pl.verify(rec["id"], "confirmed with the client sponsor")
        self.as_(ALICE)
        r2 = self.issue(rec)
        self.assertEqual((r2["serial"], r2["purpose"]), (2, "recovery"))
        self.apply(inst, r2)
        inv = inst.claimable_invite(True)
        self.assertEqual((inv["purpose"], inv["admin_email"]), ("recovery", "new.lead@gov.bc.ca"))
        self.assertEqual(inst.effective_domains(), ["gov.bc.ca"])

    def test_recovery_rules(self):
        proj, req = self.onboard()
        with self.assertRaises(pa.PlatformError) as cm:
            self.pl.recover_admin(proj["id"], "x@gov.bc.ca", "Someone", "reason")
        self.assertEqual(cm.exception.code, "not_issued")
        self.issue(req)
        for kw, code in ((dict(admin_email="x@gmail.com"), "domain_mismatch"), (dict(admin_name="x"), "bad_lead"),
                         (dict(reason=""), "reason")):
            args = dict(admin_email="new@gov.bc.ca", admin_name="New Lead", reason="reason")
            args.update(kw)
            with self.subTest(code=code), self.assertRaises(pa.PlatformError) as cm:
                self.pl.recover_admin(proj["id"], **args)
            self.assertEqual(cm.exception.code, code)

    # ---- e-mail
    def test_send_uses_the_sender_and_logs_without_the_code(self):
        _, req = self.onboard()
        r = self.issue(req, send=True)
        self.assertTrue(r["sent"])
        to, subject, body = self.sent[0]
        self.assertEqual(to, "lead@gov.bc.ca")
        self.assertIn("Ministry X intake", subject)
        self.assertIn(token_in(r["invitation_file"]), body)
        self.assertNotIn("Subject:", body)
        log = (self.dir / "platform-audit.jsonl").read_text()
        self.assertNotIn(token_in(r["invitation_file"]), log)
        self.assertIn("invitation_sent", log)

    def test_a_failed_send_does_not_undo_the_grant(self):
        pl = pa.Platform(self.dir, self.keys, identity=lambda: self.who[0], clock=self.clock,
                         sender=lambda *a: (_ for _ in ()).throw(ConnectionRefusedError("smtp down")))
        _, req = self.project()
        self.as_(BOB)
        pl.verify(req["id"])
        self.as_(ALICE)
        r = pl.issue(req["id"], self.out, send=True)
        self.assertFalse(r["sent"])
        self.assertIn("ConnectionRefusedError", r["send_error"])
        self.assertTrue(Path(r["invitation_file"]).exists())
        self.assertIn("invitation_send_failed", (self.dir / "platform-audit.jsonl").read_text())

    def test_send_without_email_set_up_reports_it(self):
        pl = pa.Platform(self.dir, self.keys, identity=lambda: self.who[0], clock=self.clock)
        _, req = self.project()
        self.as_(BOB)
        pl.verify(req["id"])
        self.as_(ALICE)
        r = pl.issue(req["id"], self.out, send=True)
        self.assertFalse(r["sent"])
        self.assertIn("not configured", r["send_error"])


# ============================================================================ command line
class CliTests(Base):
    def run_cli(self, *argv):
        lines = []
        code = pa.main(list(argv), platform=self.pl, out=lines.append)
        return code, "\n".join(lines)

    def test_the_whole_journey_by_command(self):
        code, text = self.run_cli("public-key")
        self.assertEqual((code, text), (0, f"CONSOLE_PLATFORM_PUBLIC_KEY={self.pub}"))
        code, text = self.run_cli("new-project", "--name", "Ministry X intake", "--client", "Ministry X", "--lead-email",
                                  "lead@gov.bc.ca", "--lead-name", "Lead Person", "--domain", "gov.bc.ca")
        self.assertEqual(code, 0)
        rid = re.search(r"(req-[0-9a-f]{6})", text).group(1)
        self.assertEqual(self.run_cli("verify", rid)[0], 1)                           # your own request
        self.as_(BOB)
        code, text = self.run_cli("verify", rid, "--note", "checked")
        self.assertIn("1 of 1 needed", text)
        self.as_(ALICE)
        code, text = self.run_cli("issue", rid, "--out", str(self.out), "--console-url", "https://c.example.com")
        self.assertEqual(code, 0)
        token = token_in(next(self.out.rglob("invitation-1.txt")))
        self.assertNotIn(token, text)                                               # the code is never printed
        self.assertNotIn(token.replace("-", ""), text)
        self.assertIn("grant apply", text)
        for argv in (("requests",), ("requests", "--all"), ("projects",)):
            code, text = self.run_cli(*argv)
            self.assertEqual(code, 0)
        pid = re.search(r"(prj-[0-9a-f]{8})", self.run_cli("projects")[1]).group(1)
        code, text = self.run_cli("show", pid)
        self.assertIn("verified by " + BOB, text)
        code, text = self.run_cli("audit")
        self.assertIn("grant_issued", text)
        self.assertNotIn(token, text)
        self.assertIn("chain intact", self.run_cli("audit", "--verify")[1])

    def test_audit_verify_reports_a_broken_chain(self):
        self.project()
        f = self.dir / "platform-audit.jsonl"
        f.write_text(f.read_text().replace("Ministry X intake", "Edited"))
        code, text = self.run_cli("audit", "--verify")
        self.assertEqual(code, 1)
        self.assertIn("BROKEN at line", text)

    def test_failures_are_one_plain_line(self):
        for argv in (("verify", "req-000000"), ("show", "prj-00000000"), ("keygen",)):
            code, text = self.run_cli(*argv)
            self.assertEqual(code, 1)
            self.assertTrue(text.startswith("[platform] FAIL "), text)
            self.assertNotIn("Traceback", text)

    def test_keygen_prints_only_the_public_key(self):
        pl = pa.Platform(self.dir, pa.MemoryKeyring(), identity=lambda: ALICE, clock=self.clock)
        lines = []
        self.assertEqual(pa.main(["keygen"], platform=pl, out=lines.append), 0)
        text = "\n".join(lines)
        self.assertIn("CONSOLE_PLATFORM_PUBLIC_KEY=", text)
        self.assertNotIn(pl.keyring.get(), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
