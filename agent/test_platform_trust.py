"""Offline tests for platform_trust.py: signing, grants, invitations, and the instance's view of them.
Run from agent\\:  python test_platform_trust.py"""
import copy
import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_accounts as accts  # noqa: E402
import platform_trust as pt  # noqa: E402

try:
    from cryptography.hazmat.primitives import serialization as _S
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except Exception:  # noqa: BLE001
    Ed25519PrivateKey = None

PID = "prj-ab12cd34"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def project_grant(seed, serial=1, purpose="initial", domains=("gov.bc.ca",), token="AAAAA-BBBBB-CCCCC-DDDDD",
                  email="lead@gov.bc.ca", expires=NOW + 72 * 3600, pid=PID, name="Ministry X intake"):
    return pt.sign_payload(seed, {
        "v": 1, "kind": "project-grant", "purpose": purpose, "project_id": pid, "project_name": name, "client": "Ministry X",
        "serial": serial, "issued_at": NOW, "issued_by": "WIN\\alice", "request_id": "req-1", "domains": list(domains),
        "admin_email": email, "admin_name": "Lead Person", "invite_hash": pt.invite_hash(pid, token),
        "invite_expires": expires})


def domain_grant(seed, serial, add=(), remove=(), pid=PID):
    return pt.sign_payload(seed, {"v": 1, "kind": "domain-grant", "project_id": pid, "serial": serial, "issued_at": NOW,
                                  "issued_by": "WIN\\alice", "request_id": "req-2", "add": list(add), "remove": list(remove)})


# ============================================================================ Ed25519
class Ed25519Tests(unittest.TestCase):
    VECTORS = [
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ]

    def test_rfc8032_vectors(self):
        for sk, pk, m, sig in self.VECTORS:
            sk, pk, m, sig = map(bytes.fromhex, (sk, pk, m, sig))
            self.assertEqual(pt.ed25519_public(sk), pk)
            self.assertEqual(pt.ed25519_sign(sk, m), sig)
            self.assertTrue(pt.ed25519_verify(pk, m, sig))

    @unittest.skipIf(Ed25519PrivateKey is None, "the 'cryptography' package is not installed here")
    def test_agrees_with_an_independent_implementation(self):
        for _ in range(8):
            seed, msg = secrets.token_bytes(32), secrets.token_bytes(secrets.randbelow(200))
            lib = Ed25519PrivateKey.from_private_bytes(seed)
            pub = lib.public_key().public_bytes(_S.Encoding.Raw, _S.PublicFormat.Raw)
            self.assertEqual(pt.ed25519_public(seed), pub)
            self.assertEqual(pt.ed25519_sign(seed, msg), lib.sign(msg))
            self.assertTrue(pt.ed25519_verify(pub, msg, lib.sign(msg)))
            Ed25519PublicKey.from_public_bytes(pub).verify(pt.ed25519_sign(seed, msg), msg)

    def test_tampering_is_rejected(self):
        seed = secrets.token_bytes(32)
        pub, sig = pt.ed25519_public(seed), pt.ed25519_sign(seed, b"hello")
        self.assertTrue(pt.ed25519_verify(pub, b"hello", sig))
        self.assertFalse(pt.ed25519_verify(pub, b"hellp", sig))
        for i in (0, 31, 32, 63):
            bad = bytearray(sig)
            bad[i] ^= 1
            self.assertFalse(pt.ed25519_verify(pub, b"hello", bytes(bad)), i)
        self.assertFalse(pt.ed25519_verify(pt.ed25519_public(secrets.token_bytes(32)), b"hello", sig))
        self.assertFalse(pt.ed25519_verify(pub, b"hello", sig[:-1]))
        self.assertFalse(pt.ed25519_verify(pub[:-1], b"hello", sig))
        high_s = sig[:32] + (pt._Q + 1).to_bytes(32, "little")                    # s >= q must be refused
        self.assertFalse(pt.ed25519_verify(pub, b"hello", high_s))


# ============================================================================ envelopes
class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.seed, self.pub = pt.generate_keypair()

    def test_sign_and_verify_round_trip(self):
        env = project_grant(self.seed)
        self.assertEqual(env["key_id"], pt.key_id(self.pub))
        p = pt.verify_envelope(self.pub, json.loads(json.dumps(env)))
        self.assertEqual((p["project_id"], p["serial"], p["domains"]), (PID, 1, ["gov.bc.ca"]))

    def test_changing_any_signed_field_breaks_the_signature(self):
        env = project_grant(self.seed)
        for field, value in (("domains", ["gov.bc.ca", "gmail.com"]), ("serial", 9), ("project_id", "prj-ffffffff"),
                             ("admin_email", "attacker@gov.bc.ca"), ("invite_hash", "0" * 64), ("purpose", "recovery"),
                             ("invite_expires", NOW + 10 ** 9), ("project_name", "Another project"), ("admin_name", "Someone Else")):
            bad = copy.deepcopy(env)
            bad["payload"][field] = value
            with self.subTest(field=field), self.assertRaises(pt.TrustError) as cm:
                pt.verify_envelope(self.pub, bad)
            self.assertEqual(cm.exception.code, "bad_signature")

    def test_signature_from_another_key_or_garbled_is_refused(self):
        env = project_grant(self.seed)
        other_pub = pt.generate_keypair()[1]
        with self.assertRaises(pt.TrustError) as cm:
            pt.verify_envelope(other_pub, env)
        self.assertEqual(cm.exception.code, "bad_signature")
        for sig in ("", "not base64!!", pt.b64e(b"x" * 10)):
            bad = dict(env, sig=sig)
            with self.assertRaises(pt.TrustError):
                pt.verify_envelope(self.pub, bad)
        for junk in (None, [], {}, {"payload": {}}, {"sig": "x"}, "text"):
            with self.assertRaises(pt.TrustError):
                pt.verify_envelope(self.pub, junk)

    def test_a_grant_cannot_be_reused_as_another_kind_of_message(self):
        """Signatures are tied to this grant format, so they cannot be lifted from or into anything else."""
        seed = pt.b64d(self.seed, 32)
        payload = pt.validate_payload({"v": 1, "kind": "domain-grant", "project_id": PID, "serial": 2, "issued_at": NOW,
                                       "issued_by": "WIN\\a", "request_id": "r", "add": ["a.ca"], "remove": []})
        raw_sig = pt.ed25519_sign(seed, pt.canonical(payload))                    # signed WITHOUT the format tag
        with self.assertRaises(pt.TrustError):
            pt.verify_envelope(self.pub, {"payload": payload, "sig": pt.b64e(raw_sig)})

    def test_unknown_fields_are_ignored_not_trusted(self):
        env = domain_grant(self.seed, 2, add=["a.ca"])
        env["payload"]["domains"] = ["evil.com"]
        env["payload"]["admin_email"] = "evil@evil.com"
        p = pt.verify_envelope(self.pub, env)
        self.assertNotIn("domains", p)
        self.assertNotIn("admin_email", p)

    def test_payload_validation(self):
        good = {"v": 1, "kind": "domain-grant", "project_id": PID, "serial": 2, "issued_at": NOW, "issued_by": "x",
                "request_id": "r", "add": ["a.ca"], "remove": []}
        pt.validate_payload(good)
        for field, value in (("v", 2), ("kind", "other"), ("project_id", "PRJ-AB12CD34"), ("project_id", "x"),
                             ("serial", 0), ("serial", True), ("serial", "1"), ("serial", 1.5), ("issued_at", "now"),
                             ("add", []), ("add", ["not a domain"]), ("add", "a.ca"), ("remove", ["a.ca"]),
                             ("issued_by", ""), ("request_id", "")):
            bad = dict(good, **{field: value})
            if field == "add" and value == []:
                bad["remove"] = []
            with self.subTest(field=field, value=value), self.assertRaises(pt.TrustError):
                pt.validate_payload(bad)
        with self.assertRaises(pt.TrustError):
            pt.validate_payload(dict(good, add=[], remove=[]))

    def test_project_grant_validation(self):
        base = project_grant(self.seed)["payload"]
        for field, value in (("purpose", "other"), ("admin_email", "nope"), ("invite_hash", "abc"), ("invite_hash", "G" * 64),
                             ("project_name", "ab"), ("admin_name", "x"), ("domains", []), ("domains", ["bad domain"]),
                             ("invite_expires", "later")):
            with self.subTest(field=field), self.assertRaises(pt.TrustError):
                pt.validate_payload(dict(base, **{field: value}))

    def test_domains_are_normalised_in_grants(self):
        p = pt.validate_payload(project_grant(self.seed, domains=("Gov.BC.ca", "@x.example.ca", "gov.bc.ca"))["payload"])
        self.assertEqual(p["domains"], ["gov.bc.ca", "x.example.ca"])

    def test_keys(self):
        with self.assertRaises(pt.TrustError):
            pt.check_public_key("short")
        with self.assertRaises(pt.TrustError):
            pt.check_public_key("!!!")
        self.assertEqual(pt.public_from_seed(self.seed), self.pub)
        self.assertEqual(len(pt.key_id(self.pub)), 12)


# ============================================================================ invitation codes
class TokenTests(unittest.TestCase):
    def test_format_and_alphabet(self):
        seen = set()
        for _ in range(300):
            t = pt.new_invite_token()
            self.assertRegex(t, r"^[A-Z2-9]{5}(-[A-Z2-9]{5}){3}$")
            for bad in "01OIL":
                self.assertNotIn(bad, t.replace("-", "").replace("L", "") if bad != "L" else t.replace("-", "")[:0])
            seen.add(t)
        self.assertEqual(len(seen), 300)                              # 100 bits: no repeats

    def test_normalising_what_a_person_types(self):
        t = "ABCDE-FGHJK-LMNPQ-RSTUV"
        for typed in (t, t.lower(), t.replace("-", ""), " " + t.lower().replace("-", " ") + " ", t.replace("-", "--")):
            self.assertEqual(pt.normalise_token(typed), "ABCDEFGHJKLMNPQRSTUV")
            self.assertEqual(pt.invite_hash(PID, typed), pt.invite_hash(PID, t))

    def test_hash_is_tied_to_the_project_and_the_code(self):
        t = pt.new_invite_token()
        self.assertNotEqual(pt.invite_hash(PID, t), pt.invite_hash("prj-00000000", t))
        self.assertNotEqual(pt.invite_hash(PID, t), pt.invite_hash(PID, pt.new_invite_token()))
        self.assertRegex(pt.invite_hash(PID, t), r"^[0-9a-f]{64}$")
        self.assertNotIn(pt.normalise_token(t), pt.invite_hash(PID, t))


# ============================================================================ the instance side
class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)
        self.seed, self.pub = pt.generate_keypair()
        self.clock = Clock()
        self.t = pt.InstanceTrust(self.dir, self.pub, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_applied_yet(self):
        self.assertEqual((self.t.project(), self.t.effective_domains(), self.t.error), (None, [], None))
        self.assertEqual(self.t.invite_status(False)[0], "none")
        self.assertIsNone(self.t.claimable_invite(False))

    def test_project_grant_sets_project_domains_and_invitation(self):
        r = self.t.apply(project_grant(self.seed, domains=("gov.bc.ca", "*.gov.bc.ca")))
        self.assertEqual((r["kind"], r["serial"], r["already"]), ("project-grant", 1, False))
        self.assertEqual(self.t.project(), {"id": PID, "name": "Ministry X intake", "client": "Ministry X"})
        self.assertEqual(self.t.effective_domains(), ["*.gov.bc.ca", "gov.bc.ca"])
        inv = self.t.claimable_invite(False)
        self.assertEqual((inv["admin_email"], inv["purpose"]), ("lead@gov.bc.ca", "initial"))
        self.assertTrue(self.t.token_matches(inv, "AAAAA-BBBBB-CCCCC-DDDDD"))
        self.assertTrue(self.t.token_matches(inv, "aaaaa bbbbb ccccc ddddd"))
        self.assertFalse(self.t.token_matches(inv, "AAAAA-BBBBB-CCCCC-DDDDE"))
        self.assertFalse(self.t.token_matches(inv, ""))

    def test_apply_accepts_text_or_a_dict_and_is_repeatable(self):
        env = project_grant(self.seed)
        self.assertFalse(self.t.apply(json.dumps(env))["already"])
        self.assertTrue(self.t.apply(env)["already"])
        self.assertEqual(len(json.loads((self.dir / "grants.json").read_text())["grants"]), 1)

    def test_domain_grants_add_and_remove_in_serial_order(self):
        self.t.apply(project_grant(self.seed, domains=("gov.bc.ca",)))
        self.t.apply(domain_grant(self.seed, 2, add=["a.example.ca", "b.example.ca"]))
        self.t.apply(domain_grant(self.seed, 3, remove=["a.example.ca"]))
        self.assertEqual(self.t.effective_domains(), ["b.example.ca", "gov.bc.ca"])
        self.assertEqual(self.t.state()["serial"], 3)

    def test_old_and_replayed_grants_are_refused(self):
        self.t.apply(project_grant(self.seed))
        g2 = domain_grant(self.seed, 2, add=["a.example.ca"])
        g3 = domain_grant(self.seed, 3, remove=["a.example.ca"])
        self.t.apply(g2)
        self.t.apply(g3)
        replay = domain_grant(self.seed, 2, add=["z.example.ca"])                 # same serial, different content
        for env in (replay, domain_grant(self.seed, 1, add=["z.example.ca"])):
            with self.assertRaises(pt.TrustError) as cm:
                self.t.apply(env)
            self.assertEqual(cm.exception.code, "old_grant")
        self.assertEqual(self.t.effective_domains(), ["gov.bc.ca"])              # a replayed ADD did not come back

    def test_a_skipped_older_serial_cannot_be_slipped_in_later(self):
        """Apply serial 1 and 3. A grant with serial 2 that was never applied must not be accepted afterwards:
        it could re-add a domain the platform team removed in serial 3."""
        self.t.apply(project_grant(self.seed))
        self.t.apply(domain_grant(self.seed, 3, remove=["gov.bc.ca"], add=["b.example.ca"]))
        with self.assertRaises(pt.TrustError) as cm:
            self.t.apply(domain_grant(self.seed, 2, add=["gov.bc.ca"]))
        self.assertEqual(cm.exception.code, "old_grant")
        self.assertEqual(self.t.effective_domains(), ["b.example.ca"])

    def test_grants_for_another_project_or_in_the_wrong_order_are_refused(self):
        with self.assertRaises(pt.TrustError) as cm:
            self.t.apply(domain_grant(self.seed, 1, add=["a.example.ca"]))        # no project yet
        self.assertEqual(cm.exception.code, "wrong_order")
        with self.assertRaises(pt.TrustError) as cm:
            self.t.apply(project_grant(self.seed, purpose="recovery"))
        self.assertEqual(cm.exception.code, "wrong_order")
        self.t.apply(project_grant(self.seed))
        for env in (domain_grant(self.seed, 2, add=["a.example.ca"], pid="prj-00000000"),
                    project_grant(self.seed, serial=2, pid="prj-00000000")):
            with self.assertRaises(pt.TrustError) as cm:
                self.t.apply(env)
            self.assertEqual(cm.exception.code, "wrong_project")

    def test_forged_and_malformed_grants_never_touch_the_file(self):
        self.t.apply(project_grant(self.seed))
        before = (self.dir / "grants.json").read_bytes()
        other_seed = pt.generate_keypair()[0]
        for bad in (domain_grant(other_seed, 2, add=["evil.com"]), "not json", "[]", '{"payload": 1}'):
            with self.assertRaises(pt.TrustError):
                self.t.apply(bad)
        self.assertEqual((self.dir / "grants.json").read_bytes(), before)
        self.assertEqual(self.t.effective_domains(), ["gov.bc.ca"])

    def test_one_bad_signature_in_the_file_refuses_the_whole_file(self):
        self.t.apply(project_grant(self.seed))
        self.t.apply(domain_grant(self.seed, 2, add=["a.example.ca"]))
        data = json.loads((self.dir / "grants.json").read_text())
        data["grants"][1]["payload"]["add"] = ["evil.com"]                        # edited after signing
        (self.dir / "grants.json").write_text(json.dumps(data))
        fresh = pt.InstanceTrust(self.dir, self.pub, clock=self.clock)
        self.assertIn("refused", fresh.error)
        self.assertEqual(fresh.effective_domains(), [])                          # fail closed
        self.assertIsNone(fresh.claimable_invite(False))
        with self.assertRaises(pt.TrustError):
            fresh.apply(domain_grant(self.seed, 3, add=["b.example.ca"]))        # and it refuses to add to a bad file

    def test_a_file_with_grants_removed_or_reordered_is_caught_or_harmless(self):
        self.t.apply(project_grant(self.seed))
        self.t.apply(domain_grant(self.seed, 2, add=["a.example.ca"]))
        self.t.apply(domain_grant(self.seed, 3, remove=["a.example.ca"]))
        data = json.loads((self.dir / "grants.json").read_text())
        data["grants"].reverse()                                                  # order in the file does not matter: serials do
        (self.dir / "grants.json").write_text(json.dumps(data))
        self.assertEqual(pt.InstanceTrust(self.dir, self.pub).effective_domains(), ["gov.bc.ca"])
        data["grants"] = [g for g in data["grants"] if g["payload"]["serial"] != 1]   # first (project) grant deleted
        (self.dir / "grants.json").write_text(json.dumps(data))
        self.assertIn("inconsistent", pt.InstanceTrust(self.dir, self.pub).error)

    def test_unreadable_file_fails_closed(self):
        (self.dir / "grants.json").write_text("{oops")
        t = pt.InstanceTrust(self.dir, self.pub)
        self.assertIn("unreadable", t.error)
        self.assertEqual(t.effective_domains(), [])

    def test_a_different_public_key_trusts_nothing(self):
        self.t.apply(project_grant(self.seed))
        other = pt.InstanceTrust(self.dir, pt.generate_keypair()[1])
        self.assertIn("refused", other.error)
        self.assertEqual(other.effective_domains(), [])

    def test_changes_made_by_another_process_are_seen(self):
        other = pt.InstanceTrust(self.dir, self.pub, clock=self.clock)
        self.t.apply(project_grant(self.seed))
        self.assertEqual(other.effective_domains(), ["gov.bc.ca"])
        self.t.apply(domain_grant(self.seed, 2, add=["a.example.ca"]))
        self.assertEqual(other.effective_domains(), ["a.example.ca", "gov.bc.ca"])

    # ---- invitations
    def test_invitation_is_single_use(self):
        self.t.apply(project_grant(self.seed))
        inv = self.t.claimable_invite(False)
        self.t.consume(inv["invite_hash"], by="lead")
        self.assertEqual(self.t.invite_status(False)[0], "used")
        self.assertIsNone(self.t.claimable_invite(False))
        with self.assertRaises(pt.TrustError):
            self.t.consume(inv["invite_hash"], by="someone")

    def test_invitation_expires(self):
        self.t.apply(project_grant(self.seed, expires=NOW + 100))
        self.assertIsNotNone(self.t.claimable_invite(False))
        self.clock.t = NOW + 100
        self.assertEqual(self.t.invite_status(False)[0], "expired")
        self.assertIsNone(self.t.claimable_invite(False))

    def test_an_initial_invitation_is_not_needed_once_there_is_an_administrator(self):
        self.t.apply(project_grant(self.seed))
        self.assertEqual(self.t.invite_status(True)[0], "not_needed")
        self.assertIsNone(self.t.claimable_invite(True))

    def test_a_recovery_invitation_works_even_with_an_administrator(self):
        self.t.apply(project_grant(self.seed))
        self.t.consume(self.t.claimable_invite(False)["invite_hash"], by="lead")
        self.t.apply(project_grant(self.seed, serial=2, purpose="recovery", token="RRRRR-SSSSS-TTTTT-UUUUU", email="new@gov.bc.ca"))
        inv = self.t.claimable_invite(True)
        self.assertEqual((inv["purpose"], inv["admin_email"]), ("recovery", "new@gov.bc.ca"))

    def test_a_newer_grant_replaces_an_older_invitation(self):
        self.t.apply(project_grant(self.seed, serial=1, token="AAAAA-BBBBB-CCCCC-DDDDD"))
        self.t.apply(project_grant(self.seed, serial=2, token="EEEEE-FFFFF-GGGGG-HHHHH"))
        inv = self.t.claimable_invite(False)
        self.assertFalse(self.t.token_matches(inv, "AAAAA-BBBBB-CCCCC-DDDDD"))
        self.assertTrue(self.t.token_matches(inv, "EEEEE-FFFFF-GGGGG-HHHHH"))

    def test_the_invitation_code_is_not_in_any_file(self):
        self.t.apply(project_grant(self.seed))
        self.t.consume(self.t.claimable_invite(False)["invite_hash"], by="lead")
        for f in (self.dir / "grants.json", self.dir / "invites.json"):
            self.assertNotIn("AAAAA", f.read_text())
            self.assertNotIn("BBBBB", f.read_text())


# ============================================================================ platform-controlled domains
class PlatformDomainsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)
        self.seed, self.pub = pt.generate_keypair()
        self.t = pt.InstanceTrust(self.dir, self.pub)
        self.t.apply(project_grant(self.seed, domains=("gov.bc.ca", "deloitte.ca")))
        self.d = pt.PlatformDomains(self.t, self.dir / "domain-pauses.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_effective_list_comes_only_from_grants(self):
        self.assertEqual(self.d.list(), ["deloitte.ca", "gov.bc.ca"])
        self.assertTrue(self.d.allows("a@gov.bc.ca"))
        self.assertFalse(self.d.allows("a@gmail.com"))
        self.assertFalse(self.d.allows("a@sub.gov.bc.ca"))

    def test_an_administrator_cannot_add_a_domain(self):
        with self.assertRaises(accts.AccountError) as cm:
            self.d.add("gmail.com")
        self.assertEqual(cm.exception.code, "platform_controlled")
        self.assertNotIn("gmail.com", self.d.list())
        self.assertFalse((self.dir / "domain-pauses.json").exists())

    def test_hand_editing_the_pause_file_cannot_add_a_domain(self):
        (self.dir / "domain-pauses.json").write_text(json.dumps({"paused": [], "domains": ["evil.com"], "allowed": ["evil.com"]}))
        self.assertEqual(self.d.list(), ["deloitte.ca", "gov.bc.ca"])
        self.assertFalse(self.d.allows("a@evil.com"))

    def test_pause_and_resume(self):
        self.assertTrue(self.d.remove("gov.bc.ca"))
        self.assertEqual(self.d.list(), ["deloitte.ca"])
        self.assertFalse(self.d.allows("a@gov.bc.ca"))
        self.assertEqual(self.d.paused(), ["gov.bc.ca"])
        self.assertFalse(self.d.remove("gov.bc.ca"))                          # already paused
        self.assertFalse(self.d.remove("gmail.com"))                          # never granted
        self.assertTrue(self.d.restore("gov.bc.ca"))
        self.assertEqual(self.d.list(), ["deloitte.ca", "gov.bc.ca"])
        self.assertFalse(self.d.restore("gov.bc.ca"))

    def test_rows_show_state_and_people(self):
        self.d.remove("deloitte.ca")
        rows = self.d.rows(lambda dom: 3 if dom == "gov.bc.ca" else 0)
        self.assertEqual(rows, [{"domain": "deloitte.ca", "active_users": 0, "state": "paused"},
                                {"domain": "gov.bc.ca", "active_users": 3, "state": "allowed"}])

    def test_a_pause_does_not_survive_the_platform_removing_the_domain(self):
        self.d.remove("deloitte.ca")
        self.t.apply(domain_grant(self.seed, 2, remove=["deloitte.ca"]))
        self.assertEqual(self.d.list(), ["gov.bc.ca"])
        self.assertEqual(self.d.paused(), [])
        self.t.apply(domain_grant(self.seed, 3, add=["deloitte.ca"]))            # re-granted later: allowed again
        self.assertEqual(self.d.list(), ["deloitte.ca", "gov.bc.ca"])

    def test_a_tampered_grants_file_allows_nobody(self):
        data = json.loads((self.dir / "grants.json").read_text())
        data["grants"][0]["payload"]["domains"].append("evil.com")
        (self.dir / "grants.json").write_text(json.dumps(data))
        self.assertTrue(self.d.error)
        self.assertEqual(self.d.list(), [])
        self.assertFalse(self.d.allows("a@gov.bc.ca"))

    def test_an_unreadable_pause_file_is_never_overwritten(self):
        (self.dir / "domain-pauses.json").write_text("{oops")
        self.assertIn("unreadable", self.d.error)
        self.assertEqual(self.d.list(), [])
        with self.assertRaises(accts.AccountError):
            self.d.remove("gov.bc.ca")
        self.assertEqual((self.dir / "domain-pauses.json").read_text(), "{oops")



# ============================================================================ short Windows file locks
class FlakyReplace:
    """Makes os.replace fail with 'Access is denied' the first `fails` times for each target file, the way Windows does
    while a virus scanner or an indexer has the file open, then lets it through."""

    def __init__(self, fails, error=PermissionError):
        self.fails, self.error, self.seen, self.calls = fails, error, {}, 0

    def __enter__(self):
        self.real, self.real_sleep = os.replace, accts.REPLACE_SLEEP
        self.sleeps = []

        def flaky(src, dst, *a, **k):
            self.calls += 1
            n = self.seen.get(str(dst), 0)
            self.seen[str(dst)] = n + 1
            if n < self.fails:
                raise self.error(5, "Access is denied")
            return self.real(src, dst, *a, **k)

        os.replace, accts.REPLACE_SLEEP = flaky, self.sleeps.append
        return self

    def __exit__(self, *exc):
        os.replace, accts.REPLACE_SLEEP = self.real, self.real_sleep


class FileLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_short_lock_is_waited_out_with_growing_pauses(self):
        target = self.dir / "a.json"
        with FlakyReplace(fails=3) as f:
            accts._atomic_write(target, {"x": 1})
        self.assertEqual(json.loads(target.read_text()), {"x": 1})
        self.assertEqual(f.calls, 4)
        self.assertEqual(f.sleeps, [0.02, 0.04, 0.08])
        self.assertEqual([p.name for p in self.dir.iterdir()], ["a.json"])          # no temporary file left behind

    def test_a_lock_that_does_not_clear_raises_the_real_error_and_leaves_the_old_file(self):
        target = self.dir / "a.json"
        target.write_text(json.dumps({"old": True}))
        with FlakyReplace(fails=99) as f:
            with self.assertRaises(PermissionError):
                accts._atomic_write(target, {"new": True})
        self.assertEqual(f.calls, accts.REPLACE_TRIES)
        self.assertLess(sum(f.sleeps), 4)                                          # it never waits more than a few seconds
        self.assertEqual(json.loads(target.read_text()), {"old": True})            # the old file is untouched
        self.assertEqual([p.name for p in self.dir.iterdir()], ["a.json"])          # and no temporary file is left

    def test_other_errors_are_not_retried(self):
        target = self.dir / "a.json"
        with FlakyReplace(fails=99, error=FileNotFoundError) as f:
            with self.assertRaises(FileNotFoundError):
                accts._atomic_write(target, {"x": 1})
        self.assertEqual((f.calls, f.sleeps), (1, []))

    def test_every_write_in_every_module_uses_the_retry(self):
        import console_auth as ca
        import platform_admin as pa
        with FlakyReplace(fails=2):
            # 1. the platform register and its signing
            pl = pa.Platform(self.dir / "platform", pa.MemoryKeyring(), identity=lambda: "WIN\\alice", clock=Clock())
            pl.create_keys()
            proj, req = pl.new_project("Ministry X intake", "Ministry X", "lead@gov.bc.ca", "Lead Person", ["gov.bc.ca"])
            self.assertEqual(pl.projects()[0]["id"], proj["id"])
            # 2. an instance applying a grant and pausing a domain
            seed, pub = pt.generate_keypair()
            inst = pt.InstanceTrust(self.dir / "inst", pub, clock=Clock())
            inst.apply(project_grant(seed))
            inst.consume(inst.claimable_invite(False)["invite_hash"], by="lead")
            doms = pt.PlatformDomains(inst, self.dir / "inst" / "domain-pauses.json")
            self.assertTrue(doms.remove("gov.bc.ca"))
            # 3. accounts and domains
            store = accts.AccountStore(self.dir / "inst" / "accounts.json")
            store.create_admin_with_hash("lead", "Lead Person", "lead@gov.bc.ca", accts.hash_password("correct horse battery"))
            accts.DomainPolicy(self.dir / "inst" / "domains.json").add("deloitte.ca")
            # 4. the access list
            ca._write_access(self.dir / "inst" / "access.json", [{"windows": "CORP\\a", "role": "approver"}])
        for name in ("platform/projects.json", "inst/grants.json", "inst/invites.json", "inst/domain-pauses.json",
                     "inst/accounts.json", "inst/domains.json", "inst/access.json"):
            json.loads((self.dir / name).read_text())                                # every file is whole and valid
        leftovers = [p.name for p in self.dir.rglob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_reading_waits_out_a_short_lock_too(self):
        target = self.dir / "a.json"
        target.write_text('{"x": 1}')
        real, real_sleep, sleeps, n = Path.read_text, accts.REPLACE_SLEEP, [], [0]

        def flaky(self_, *a, **k):
            n[0] += 1
            if n[0] <= 2:
                raise PermissionError(13, "Permission denied")
            return real(self_, *a, **k)

        Path.read_text, accts.REPLACE_SLEEP = flaky, sleeps.append
        try:
            self.assertEqual(json.loads(accts.read_text(target)), {"x": 1})
            self.assertEqual((n[0], sleeps), (3, [0.02, 0.04]))
            n[0], sleeps[:] = -99, []
            with self.assertRaises(PermissionError):                                 # a lock that never clears still shows
                accts.read_text(target)
            self.assertEqual(len(sleeps), accts.REPLACE_TRIES - 1)
        finally:
            Path.read_text, accts.REPLACE_SLEEP = real, real_sleep

    def test_a_missing_file_is_still_just_missing(self):
        with self.assertRaises(FileNotFoundError):
            accts.read_text(self.dir / "nope.json")
        self.assertEqual(accts.DomainPolicy(self.dir / "nope.json").list(), [])
        self.assertEqual(accts.AccountStore(self.dir / "nope.json").list_users(), [])

    def test_two_writes_never_share_a_temporary_name(self):
        names = set()
        real = accts.replace_file
        accts.replace_file = lambda src, dst, tries=None: (names.add(Path(src).name), real(src, dst, tries))
        try:
            for i in range(20):
                accts._atomic_write(self.dir / "a.json", {"i": i})
        finally:
            accts.replace_file = real
        self.assertEqual(len(names), 20)
        self.assertTrue(all(n.startswith("a.json.") and n.endswith(".tmp") for n in names))
        self.assertEqual(json.loads((self.dir / "a.json").read_text()), {"i": 19})

    def test_many_threads_writing_and_reading_never_see_a_broken_file(self):
        import threading
        target, errors, stop = self.dir / "a.json", [], threading.Event()
        accts._atomic_write(target, {"n": 0})

        def writer(k):
            for i in range(40):
                try:
                    accts._atomic_write(target, {"n": k * 1000 + i, "pad": "x" * 2000})
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"write {type(exc).__name__}")

        def reader():
            while not stop.is_set():
                try:
                    json.loads(accts.read_text(target))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"read {type(exc).__name__}")

        rs = [threading.Thread(target=reader) for _ in range(3)]
        ws = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
        for t in rs + ws:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        for t in rs:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([p.name for p in self.dir.iterdir()], ["a.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
