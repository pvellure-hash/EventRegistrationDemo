"""Offline tests for secret_store.py v12 (fake stores; on Windows also a real Credential Manager round-trip).
Run from agent\\:  python test_secret_store.py"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_store as ss  # noqa: E402


class FakeWinApi:
    def __init__(self, fail=False, corrupt=False):
        self.d, self.fail, self.corrupt = {}, fail, corrupt

    def read(self, t):
        if self.fail:
            raise ss.SecretStoreError("locked")
        v = self.d.get(t)
        return v + "x" if (v and self.corrupt) else v

    def write(self, t, v, user=""):
        self.d[t] = v

    def delete(self, t):
        return self.d.pop(t, None) is not None


def credman(**k):
    return ss.CredentialManagerBackend(api=FakeWinApi(**k))


def _exc(name, **attrs):
    e = type(name, (Exception,), {})()
    for a, v in attrs.items():
        setattr(e, a, v)
    return e


class FakeKV:
    def __init__(self):
        self.d = {}

    def get_secret(self, n):
        if n not in self.d:
            raise _exc("ResourceNotFoundError")
        return type("S", (), {"value": self.d[n]})()

    def set_secret(self, n, v):
        self.d[n] = v

    def begin_delete_secret(self, n):
        self.d.pop(n)


class FakeAWS:
    def __init__(self):
        self.d = {}

    def _nf(self):
        return _exc("ClientError", response={"Error": {"Code": "ResourceNotFoundException"}})

    def get_secret_value(self, SecretId):
        if SecretId not in self.d:
            raise self._nf()
        return {"SecretString": self.d[SecretId]}

    def put_secret_value(self, SecretId, SecretString):
        if SecretId not in self.d:
            raise self._nf()
        self.d[SecretId] = SecretString

    def create_secret(self, Name, SecretString):
        self.d[Name] = SecretString

    def delete_secret(self, SecretId, RecoveryWindowInDays):
        self.d.pop(SecretId)


class FakeGCP:
    def __init__(self):
        self.secrets, self.versions = set(), {}

    def create_secret(self, request):
        p = f"{request['parent']}/secrets/{request['secret_id']}"
        if p in self.secrets:
            raise _exc("AlreadyExists")
        self.secrets.add(p)

    def add_secret_version(self, request):
        self.versions[request["parent"]] = request["payload"]["data"]

    def access_secret_version(self, request):
        p = request["name"].rsplit("/versions/", 1)[0]
        if p not in self.versions:
            raise _exc("NotFound")
        return type("R", (), {"payload": type("P", (), {"data": self.versions[p]})()})()

    def delete_secret(self, request):
        self.secrets.discard(request["name"])
        self.versions.pop(request["name"], None)


ENV = ("# header\r\nJIRA_BASE_URL=https://x.atlassian.net\r\nJIRA_API_TOKEN=atl-123456\r\n"
       "GITHUB_TOKEN=\"ghp_abcdef\"\r\nAGENT_ENABLED=true\r\nCONTEXT_PACK_MAX_TOKENS=12000\r\n"
       "SF_TARGET_ORG=eventreg-dev\r\nAZURE_CLIENT_SECRET=\r\n")
KEYS = ("SECRETS_BACKEND", "SECRETS_ALLOW_DOTENV", "SECRETS_EXTRA", "SECRETS_NAMESPACE", "AZURE_KEY_VAULT_URL")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_bytes(ENV.encode())
        self.out = []
        self.saved = {k: os.environ.pop(k, None) for k in KEYS}
        os.environ["SECRETS_NAMESPACE"] = "ai-delivery-test"
        ss._STORE = None

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        ss._STORE = None

    def test_secret_name_rule(self):
        for n in ("JIRA_API_TOKEN", "GITHUB_TOKEN", "SF_SFDX_AUTH_URL", "AZURE_CLIENT_SECRET", "X_API_KEY"):
            self.assertTrue(ss.is_secret_name(n), n)
        for n in ("CONTEXT_PACK_MAX_TOKENS", "JIRA_BASE_URL", "SF_TARGET_ORG", "SECRETS_BACKEND"):
            self.assertFalse(ss.is_secret_name(n), n)

    def test_extra_names(self):
        os.environ["SECRETS_EXTRA"] = "smtp_password, JIRA_API_TOKEN"
        self.assertEqual(ss.secret_names(), ["JIRA_API_TOKEN", "GITHUB_TOKEN", "SMTP_PASSWORD"])

    def test_env_parsing(self):
        self.assertEqual(sorted(ss.secrets_in_env_file(self.env)), ["GITHUB_TOKEN", "JIRA_API_TOKEN"])
        self.assertEqual(ss.env_value("A=0.35 # note"), "0.35")
        self.assertEqual(ss.env_value('A="x y"'), "x y")

    def test_namespace_sanitised(self):
        os.environ["SECRETS_NAMESPACE"] = "AI Delivery/Event_App"
        self.assertEqual(ss.namespace(), "ai-delivery-event-app")

    def test_bad_backend_name(self):
        os.environ["SECRETS_BACKEND"] = "plaintext"
        with self.assertRaises(ss.SecretStoreError):
            ss.default_backends()

    def test_azure_url_validated(self):
        for bad in ("", "https://evil.example.com/", "http://kv-x.vault.azure.net/"):
            with self.assertRaises(ss.SecretStoreError):
                ss.AzureKeyVaultBackend(bad, client=FakeKV())

    def test_gcp_needs_project(self):
        os.environ.pop("GCP_PROJECT_ID", None)
        with self.assertRaises(ss.SecretStoreError):
            ss.GcpSecretManagerBackend(client=FakeGCP())

    def backends(self):
        return [credman(), ss.AzureKeyVaultBackend("https://kv-test01.vault.azure.net/", client=FakeKV()),
                ss.AwsSecretsManagerBackend(client=FakeAWS()), ss.GcpSecretManagerBackend(client=FakeGCP(), project="p")]

    def test_contract_all_backends(self):
        for b in self.backends():
            with self.subTest(b.label):
                self.assertIsNone(b.get("JIRA_API_TOKEN"))
                b.set("JIRA_API_TOKEN", "v1")
                self.assertEqual(b.get("JIRA_API_TOKEN"), "v1")
                b.set("JIRA_API_TOKEN", "v2")
                self.assertEqual(b.get("JIRA_API_TOKEN"), "v2")
                self.assertTrue(b.delete("JIRA_API_TOKEN"))
                self.assertIsNone(b.get("JIRA_API_TOKEN"))

    def test_names_in_each_store(self):
        self.assertEqual(credman().target("GITHUB_TOKEN"), "ai-delivery-test/GITHUB_TOKEN")
        self.assertEqual(ss.AzureKeyVaultBackend.key("GITHUB_TOKEN"), "ai-delivery-test--github-token")
        self.assertEqual(ss.AwsSecretsManagerBackend.key("GITHUB_TOKEN"), "ai-delivery-test/GITHUB_TOKEN")

    def test_locked_store_fails_closed_even_with_dotenv(self):
        st = ss.SecretStore([credman(fail=True), ss.DotEnvBackend(self.env)])
        with self.assertRaises(ss.SecretStoreError):
            st.get("GITHUB_TOKEN")

    def test_not_found_falls_to_dotenv_only_when_allowed(self):
        st = ss.SecretStore([credman(), ss.DotEnvBackend(self.env)])
        self.assertEqual(st.lookup("GITHUB_TOKEN"), ("ghp_abcdef", ".env file (break-glass)"))
        self.assertIsNone(ss.SecretStore([credman()]).get("GITHUB_TOKEN"))

    def test_dotenv_never_written(self):
        with self.assertRaises(ss.SecretStoreError):
            ss.SecretStore([ss.DotEnvBackend(self.env)]).primary

    def test_cache_ttl_picks_up_rotation(self):
        b, c = credman(), Clock()
        b.set("GITHUB_TOKEN", "old")
        st = ss.SecretStore([b], ttl=300, clock=c)
        self.assertEqual(st.get("GITHUB_TOKEN"), "old")
        b.set("GITHUB_TOKEN", "new")
        c.t = 100
        self.assertEqual(st.get("GITHUB_TOKEN"), "old")
        c.t = 301
        self.assertEqual(st.get("GITHUB_TOKEN"), "new")

    def test_inject(self):
        b = credman()
        b.set("JIRA_API_TOKEN", "a")
        b.set("GITHUB_TOKEN", "b")
        ss._STORE = ss.SecretStore([b])
        env = {"GITHUB_TOKEN": "from-actions", "JIRA_API_TOKEN": ""}
        rep = ss.inject(environ=env)
        self.assertEqual(env, {"GITHUB_TOKEN": "from-actions", "JIRA_API_TOKEN": "a"})
        self.assertEqual(rep, {"JIRA_API_TOKEN": "Windows Credential Manager", "GITHUB_TOKEN": "environment"})

    def test_force_and_refresh_pick_up_rotation(self):
        b = credman()
        b.set("JIRA_API_TOKEN", "old-token-1")
        b.set("GITHUB_TOKEN", "gh-token-1")
        ss._STORE = ss.SecretStore([b], ttl=300)
        env = {}
        ss.inject(environ=env)
        b.set("JIRA_API_TOKEN", "new-token-2")
        self.assertEqual(ss.inject(environ=env)["JIRA_API_TOKEN"], "environment")
        self.assertEqual(ss.refresh_environment(environ=env), ["JIRA_API_TOKEN"])
        self.assertEqual(env["JIRA_API_TOKEN"], "new-token-2")
        self.assertEqual(ss.refresh_environment(environ=env), [])

    def test_force_keeps_env_value_when_store_has_none(self):
        ss._STORE = ss.SecretStore([credman()])
        env = {"JIRA_API_TOKEN": "from-env"}
        rep = ss.inject(["JIRA_API_TOKEN"], environ=env, force=True)
        self.assertEqual((env["JIRA_API_TOKEN"], rep["JIRA_API_TOKEN"]), ("from-env", "environment"))

    def test_inject_required_missing(self):
        ss._STORE = ss.SecretStore([credman()])
        self.assertEqual(ss.inject(environ={})["GITHUB_TOKEN"], "MISSING")
        with self.assertRaises(ss.SecretStoreError):
            ss.inject(environ={}, required=True)

    def test_secret_values_for_redaction(self):
        b = credman()
        b.set("JIRA_API_TOKEN", "atl-123456")
        b.set("GITHUB_TOKEN", "abc")
        ss._STORE = ss.SecretStore([b])
        self.assertEqual(ss.secret_values(), ["atl-123456"])

    def test_migrate_dry_run(self):
        b = credman()
        ss.migrate(self.env, apply=False, dest=b, out=self.out.append)
        self.assertEqual(self.env.read_bytes(), ENV.encode())
        self.assertEqual(b.api.d, {})
        self.assertFalse(any("atl-123456" in o for o in self.out))

    def test_migrate_moves_and_keeps_rest(self):
        b = credman()
        ss.migrate(self.env, apply=True, dest=b, out=self.out.append)
        self.assertEqual(b.get("JIRA_API_TOKEN"), "atl-123456")
        self.assertEqual(b.get("GITHUB_TOKEN"), "ghp_abcdef")
        txt = self.env.read_bytes().decode()
        self.assertNotIn("atl-123456", txt)
        self.assertIn("# header\r\n", txt)
        self.assertIn("CONTEXT_PACK_MAX_TOKENS=12000\r\n", txt)
        self.assertIn("AZURE_CLIENT_SECRET=\r\n", txt)
        self.assertEqual(ss.secrets_in_env_file(self.env), [])
        self.assertFalse((self.env.parent / ".env.tmp").exists())

    def test_migrate_readback_failure_keeps_env(self):
        with self.assertRaises(ss.SecretStoreError):
            ss.migrate(self.env, apply=True, dest=credman(corrupt=True), out=self.out.append)
        self.assertEqual(self.env.read_bytes(), ENV.encode())

    def test_migrate_bom_and_idempotent(self):
        self.env.write_bytes(b"\xef\xbb\xbf" + ENV.encode())
        b = credman()
        ss.migrate(self.env, apply=True, dest=b, out=self.out.append)
        before = self.env.read_bytes()
        ss.migrate(self.env, apply=True, dest=b, out=self.out.append)
        self.assertEqual(self.env.read_bytes(), before)

    def test_check_env_cli(self):
        self.assertEqual(ss._cli(["check-env", "--env", str(self.env)]), 1)
        ss.migrate(self.env, apply=True, dest=credman(), out=self.out.append)
        self.assertEqual(ss._cli(["check-env", "--env", str(self.env)]), 0)

    def test_copy_to_every_cloud(self):
        for dest in self.backends()[1:]:
            with self.subTest(dest.label):
                src = credman()
                src.set("JIRA_API_TOKEN", "atl-123456")
                src.set("GITHUB_TOKEN", "ghp_abcdef")
                self.assertEqual(ss.copy_all(src, dest, apply=False, out=self.out.append), 0)
                self.assertIsNone(dest.get("GITHUB_TOKEN"))
                self.assertEqual(ss.copy_all(src, dest, apply=True, out=self.out.append), 0)
                self.assertEqual(dest.get("GITHUB_TOKEN"), "ghp_abcdef")
                self.assertEqual(src.get("GITHUB_TOKEN"), "ghp_abcdef")

    def test_copy_reports_missing(self):
        src = credman()
        src.set("JIRA_API_TOKEN", "a")
        self.assertEqual(ss.copy_all(src, credman(), apply=True, out=self.out.append), 1)

    def test_no_value_ever_printed(self):
        src = credman()
        src.set("JIRA_API_TOKEN", "atl-123456")
        src.set("GITHUB_TOKEN", "ghp_abcdef")
        ss.copy_all(src, credman(), apply=True, out=self.out.append)
        ss.migrate(self.env, apply=True, dest=credman(), out=self.out.append)
        joined = "\n".join(self.out)
        self.assertNotIn("atl-123456", joined)
        self.assertNotIn("ghp_abcdef", joined)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_real_credential_manager_roundtrip(self):
        os.environ["SECRETS_NAMESPACE"] = "ai-delivery-selftest"
        b = ss.CredentialManagerBackend()
        try:
            b.set("SELFTEST_TOKEN", "value-éü-123")
            self.assertEqual(b.get("SELFTEST_TOKEN"), "value-éü-123")
        finally:
            b.delete("SELFTEST_TOKEN")
        self.assertIsNone(b.get("SELFTEST_TOKEN"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
