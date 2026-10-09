"""Offline tests for the generic secrets layer (v13): HashiCorp Vault, platform environment variables, the provider
registry and plugins, step-by-step guides, doctor, guided setup, and .env settings editing.
No cloud account and no network: Vault is a small fake server on 127.0.0.1.
Run from agent\\:  python test_secret_providers.py"""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_store as ss  # noqa: E402

KEYS = ("SECRETS_BACKEND", "SECRETS_ALLOW_DOTENV", "SECRETS_EXTRA", "SECRETS_NAMESPACE", "SECRETS_PLUGINS",
        "AZURE_KEY_VAULT_URL", "AWS_REGION", "GCP_PROJECT_ID", "VAULT_ADDR", "VAULT_TOKEN", "VAULT_KV_MOUNT",
        "VAULT_NAMESPACE", "JIRA_API_TOKEN", "GITHUB_TOKEN")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)
        self.env = self.dir / ".env"
        self.saved = {k: os.environ.pop(k, None) for k in KEYS}
        os.environ["SECRETS_NAMESPACE"] = "ai-delivery-test"
        self.out = []
        ss._STORE = None

    def tearDown(self):
        for k in KEYS:
            os.environ.pop(k, None)
            if self.saved[k] is not None:
                os.environ[k] = self.saved[k]
        ss.PROVIDERS.pop("demo", None)
        sys.modules.pop("fake_secrets_plugin", None)
        ss._STORE = None
        self.tmp.cleanup()


class Mem:
    """A writable in-memory provider for doctor/setup tests."""
    label = "Memory"

    def __init__(self, data=None, fail=False):
        self.d, self.fail = dict(data or {}), fail

    def get(self, n):
        if self.fail:
            raise ss.SecretStoreError("access denied")
        return self.d.get(n)

    def set(self, n, v):
        self.d[n] = v

    def delete(self, n):
        return self.d.pop(n, None) is not None


# ============================================================================ HashiCorp Vault
class FakeVault:
    """Just enough of Vault's KV v2 HTTP API, on a real socket."""

    def __init__(self, token="s.good", mount="secret", namespace_header=None):
        self.data, self.token, self.mount, self.seen = {}, token, mount, []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self):
                owner.seen.append((self.command, self.path, dict(self.headers)))
                if self.headers.get("X-Vault-Token") != owner.token:
                    return self._reply(403, {"errors": ["permission denied"]}), None, None
                pre = f"/v1/{owner.mount}/"
                if not self.path.startswith(pre):
                    return self._reply(404, {"errors": []}), None, None
                kind, _, key = self.path[len(pre):].partition("/")
                return None, kind, key

            def do_GET(self):
                done, kind, key = self._route()
                if kind is None:
                    return
                if kind == "data" and key in owner.data:
                    return self._reply(200, {"data": {"data": {"value": owner.data[key]}, "metadata": {"version": 1}}})
                self._reply(404, {"errors": []})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                done, kind, key = self._route()
                if kind is None:
                    return
                owner.data[key] = body["data"]["value"]
                self._reply(200, {"data": {"version": 1}})

            def do_DELETE(self):
                done, kind, key = self._route()
                if kind is None:
                    return
                if kind == "metadata" and key in owner.data:
                    del owner.data[key]
                    return self._reply(204)
                self._reply(404, {"errors": []})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addr = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class VaultTests(Base):
    def setUp(self):
        super().setUp()
        self.v = FakeVault()

    def tearDown(self):
        self.v.close()
        super().tearDown()

    def backend(self, **kw):
        kw.setdefault("token", "s.good")
        return ss.VaultBackend(addr=self.v.addr, **kw)

    def test_contract_over_real_http(self):
        b = self.backend()
        self.assertIsNone(b.get("JIRA_API_TOKEN"))
        b.set("JIRA_API_TOKEN", "v1")
        self.assertEqual(b.get("JIRA_API_TOKEN"), "v1")
        b.set("JIRA_API_TOKEN", "v2")
        self.assertEqual(b.get("JIRA_API_TOKEN"), "v2")
        self.assertTrue(b.delete("JIRA_API_TOKEN"))
        self.assertFalse(b.delete("JIRA_API_TOKEN"))
        self.assertIsNone(b.get("JIRA_API_TOKEN"))

    def test_paths_and_headers(self):
        b = self.backend(vault_namespace="team-a")
        b.set("GITHUB_TOKEN", "x")
        method, path, headers = self.v.seen[-1]
        self.assertEqual((method, path), ("POST", "/v1/secret/data/ai-delivery-test/GITHUB_TOKEN"))
        self.assertEqual(headers.get("X-Vault-Namespace"), "team-a")
        self.assertIn("ai-delivery-test/GITHUB_TOKEN", self.v.data)

    def test_custom_mount(self):
        self.v.mount = "kv/projects"
        b = self.backend(mount="kv/projects")
        b.set("GITHUB_TOKEN", "x")
        self.assertEqual(b.get("GITHUB_TOKEN"), "x")

    def test_wrong_token_fails_closed(self):
        b = self.backend(token="s.bad")
        with self.assertRaises(ss.SecretStoreError) as cm:
            b.get("GITHUB_TOKEN")
        self.assertIn("refused", str(cm.exception))

    def test_unreachable_fails_closed(self):
        b = ss.VaultBackend(addr="http://127.0.0.1:1", token="t")
        with self.assertRaises(ss.SecretStoreError) as cm:
            b.get("GITHUB_TOKEN")
        self.assertIn("not reachable", str(cm.exception))

    def test_token_sources(self):
        real_home = ss.Path.home
        ss.Path.home = staticmethod(lambda: self.dir)
        try:
            with self.assertRaises(ss.SecretStoreError) as cm:
                ss.VaultBackend(addr=self.v.addr).get("X")                 # no env token, no ~/.vault-token
            self.assertIn("vault login", str(cm.exception))
            (self.dir / ".vault-token").write_text("s.good\n")
            self.assertIsNone(ss.VaultBackend(addr=self.v.addr).get("NOPE"))    # 'vault login' file is used
            (self.dir / ".vault-token").unlink()
            os.environ["VAULT_TOKEN"] = "s.good"
            self.assertIsNone(ss.VaultBackend(addr=self.v.addr).get("NOPE"))    # environment variable is used
        finally:
            ss.Path.home = real_home

    def test_address_and_mount_validation(self):
        for bad in ("", "vault.example.com", "http://vault.example.com:8200", "ftp://x"):
            with self.subTest(bad=bad), self.assertRaises(ss.SecretStoreError):
                ss.VaultBackend(addr=bad, token="t")
        ss.VaultBackend(addr="https://vault.example.com:8200", token="t")
        ss.VaultBackend(addr="http://localhost:8200", token="t")
        for bad in ("../x", "a b", "/"):
            with self.subTest(mount=bad), self.assertRaises(ss.SecretStoreError):
                ss.VaultBackend(addr="https://v.example.com", token="t", mount=bad)

    def test_token_never_appears_in_errors(self):
        b = self.backend(token="s.super-secret-token")
        try:
            b.get("X")
        except ss.SecretStoreError as e:
            self.assertNotIn("s.super-secret-token", str(e))

    def test_selected_through_the_registry(self):
        os.environ.update({"SECRETS_BACKEND": "vault", "VAULT_ADDR": self.v.addr, "VAULT_TOKEN": "s.good"})
        ss.store().primary.set("GITHUB_TOKEN", "abc123")
        self.assertEqual(ss.get("GITHUB_TOKEN"), "abc123")

    def test_copy_from_credman_like_store_to_vault(self):
        src = Mem({"JIRA_API_TOKEN": "a1", "GITHUB_TOKEN": "g1"})
        self.assertEqual(ss.copy_all(src, self.backend(), apply=True, out=self.out.append), 0)
        self.assertEqual(self.v.data["ai-delivery-test/JIRA_API_TOKEN"], "a1")


# ============================================================================ platform environment variables
class EnvBackendTests(Base):
    def test_reads_only(self):
        b = ss.EnvironmentBackend({"GITHUB_TOKEN": "g", "JIRA_API_TOKEN": ""})
        self.assertEqual(b.get("GITHUB_TOKEN"), "g")
        self.assertIsNone(b.get("JIRA_API_TOKEN"))
        for call in (lambda: b.set("GITHUB_TOKEN", "x"), lambda: b.delete("GITHUB_TOKEN")):
            with self.assertRaises(ss.SecretStoreError) as cm:
                call()
            self.assertIn("hosting platform", str(cm.exception))

    def test_inject_with_env_provider(self):
        os.environ.update({"SECRETS_BACKEND": "env", "GITHUB_TOKEN": "g", "JIRA_API_TOKEN": "j"})
        rep = ss.inject(required=True)
        self.assertEqual(set(rep.values()), {"environment"})
        os.environ.pop("JIRA_API_TOKEN")
        ss._STORE = None
        with self.assertRaises(ss.SecretStoreError):
            ss.inject(required=True)


# ============================================================================ registry and plugins
class RegistryTests(Base):
    def test_built_in_providers(self):
        self.assertEqual(list(ss.PROVIDERS)[:6], ["credman", "azure", "aws", "gcp", "vault", "env"])
        self.assertEqual(ss.BACKENDS[:6], ("credman", "azure", "aws", "gcp", "vault", "env"))
        for p in ss.PROVIDERS.values():
            self.assertTrue(p.label and p.best_for)

    def test_unknown_provider(self):
        with self.assertRaises(ss.SecretStoreError) as cm:
            ss.provider("plaintext")
        self.assertIn("vault", str(cm.exception))

    def test_register_validation(self):
        with self.assertRaises(ss.SecretStoreError):
            ss.register_provider(ss.Provider("Bad Name", "x", Mem, "x"))
        with self.assertRaises(ss.SecretStoreError):
            ss.register_provider(ss.Provider("azure", "x", Mem, "x"))

    def test_plugin_module_adds_a_provider(self):
        mod = types.ModuleType("fake_secrets_plugin")
        mem = Mem({"GITHUB_TOKEN": "from-plugin"})
        mod.loaded = True
        sys.modules["fake_secrets_plugin"] = mod
        ss.register_provider(ss.Provider("demo", "Demo store", lambda: mem, "Plugin test",
                                         guide=lambda ns, s: [("Step", ["do the thing"])]))
        os.environ.update({"SECRETS_PLUGINS": "fake_secrets_plugin", "SECRETS_BACKEND": "demo"})
        self.assertEqual(ss.get("GITHUB_TOKEN"), "from-plugin")
        self.assertIn("1. Step", "\n".join(ss.guide_lines("demo")))

    def test_missing_plugin_fails_closed(self):
        os.environ["SECRETS_PLUGINS"] = "no_such_plugin_module_xyz"
        with self.assertRaises(ss.SecretStoreError) as cm:
            ss.provider("credman")
        self.assertIn("could not be loaded", str(cm.exception))

    def test_missing_packages_are_reported_by_pip_name(self):
        p = ss.Provider("demo", "Demo", Mem, "x", packages=(("no_such_module_abc", "no-such-package"), ("json", "json")))
        self.assertEqual(p.missing_packages(), ["no-such-package"])


# ============================================================================ guides
class GuideTests(Base):
    def test_every_provider_has_a_complete_guide(self):
        for name, p in ss.PROVIDERS.items():
            with self.subTest(name=name):
                text = "\n".join(ss.guide_lines(name))
                self.assertIn(f"SECRETS_BACKEND={name}", text)
                self.assertIn("Point the console at it", text)
                for s in p.settings:
                    if s.required:
                        self.assertIn(s.name, text)
                for _, pip_name in p.packages:
                    self.assertIn(pip_name, text)
                self.assertNotIn("{", text.replace("{ capabilities", "").replace("{{", ""))

    def test_guides_use_the_instance_namespace_and_settings(self):
        os.environ["SECRETS_NAMESPACE"] = "proj-bcgov"
        az = "\n".join(ss.guide_lines("azure", {"AZURE_KEY_VAULT_URL": "https://kv-bcgov01.vault.azure.net/"}))
        self.assertIn("kv-bcgov01", az)
        self.assertIn("proj-bcgov--", az)
        self.assertIn("proj-bcgov/*", "\n".join(ss.guide_lines("aws")))
        self.assertIn("secret/data/proj-bcgov/*", "\n".join(ss.guide_lines("vault")))

    def test_hosted_guidance_avoids_long_lived_keys(self):
        joined = "\n".join("\n".join(ss.guide_lines(n)) for n in ("azure", "aws", "gcp", "vault"))
        for phrase in ("managed identity", "IAM role", "service account", "Never put the token in .env"):
            self.assertIn(phrase, joined)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", joined)
        self.assertNotIn("client secret=", joined.lower())

    def test_markdown_guide(self):
        md = ss.guide_markdown()
        self.assertTrue(md.startswith("# Secrets: setup guide for a new instance"))
        for p in ss.PROVIDERS.values():
            self.assertIn(f"## {p.label} (`{p.name}`)", md)
        self.assertEqual(md.count("```") % 2, 0)
        self.assertIn("copy --to <provider> --yes", md)

    def test_cli_guide_and_providers(self):
        self.assertEqual(ss._cli(["guide", "gcp"]), 0)
        self.assertEqual(ss._cli(["guide", "--all", "--markdown"]), 0)
        self.assertEqual(ss._cli(["providers"]), 0)
        self.assertEqual(ss._cli(["guide", "nope"]), 2)


# ============================================================================ doctor
class DoctorTests(Base):
    def run_doctor(self, backend=None, env_text=""):
        self.env.write_text(env_text)
        fails = ss.doctor(self.env, out=self.out.append, backend=backend)
        return fails, "\n".join(self.out)

    def test_all_good(self):
        os.environ.update({"SECRETS_BACKEND": "vault", "VAULT_ADDR": "https://v.example.com"})
        fails, text = self.run_doctor(Mem({"JIRA_API_TOKEN": "j" * 20, "GITHUB_TOKEN": "g" * 30}))
        self.assertEqual(fails, 0, text)
        self.assertIn("present (20 chars)", text)
        self.assertIn("all checks passed", text)
        self.assertNotIn("jjjj", text)

    def test_reports_each_problem_with_a_fix(self):
        os.environ["SECRETS_BACKEND"] = "azure"
        fails, text = self.run_doctor(Mem({"JIRA_API_TOKEN": "j"}), env_text="GITHUB_TOKEN=ghp_abc\n")
        self.assertIn("FAIL setting AZURE_KEY_VAULT_URL", text)
        self.assertIn("python secret_store.py set GITHUB_TOKEN", text)
        self.assertIn("migrate --yes", text)
        self.assertEqual(fails, 3)

    def test_access_failure_stops(self):
        os.environ["SECRETS_BACKEND"] = "vault"
        os.environ["VAULT_ADDR"] = "https://v.example.com"
        fails, text = self.run_doctor(Mem(fail=True))
        self.assertIn("FAIL access  -  access denied", text)

    def test_missing_package_is_explained(self):
        ss.register_provider(ss.Provider("demo", "Demo", Mem, "x", packages=(("no_such_module_abc", "no-such-pkg"),)))
        os.environ["SECRETS_BACKEND"] = "demo"
        fails, text = self.run_doctor()
        self.assertIn("pip install no-such-pkg", text)

    def test_unknown_provider(self):
        os.environ["SECRETS_BACKEND"] = "nope"
        fails, text = self.run_doctor()
        self.assertEqual(fails, 1)

    def test_credman_off_windows(self):
        if os.name == "nt":
            self.skipTest("Windows")
        os.environ["SECRETS_BACKEND"] = "credman"
        fails, text = self.run_doctor()
        self.assertIn("only works on Windows", text)

    def test_env_provider_hint(self):
        os.environ["SECRETS_BACKEND"] = "env"
        fails, text = self.run_doctor(ss.EnvironmentBackend({"GITHUB_TOKEN": "g" * 10}))
        self.assertIn("set it on the hosting platform", text)


# ============================================================================ .env settings
class EnvFileTests(Base):
    def test_update_keeps_everything_else(self):
        self.env.write_bytes(b"\xef\xbb\xbf# top\r\nSECRETS_BACKEND=credman\r\nJIRA_BASE_URL=https://x\r\nLAST=1")
        ss.update_env_file(self.env, {"SECRETS_BACKEND": "azure", "AZURE_KEY_VAULT_URL": "https://kv-a.vault.azure.net/"})
        raw = self.env.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw[3:].decode()
        self.assertEqual(text, "# top\r\nSECRETS_BACKEND=azure\r\nJIRA_BASE_URL=https://x\r\nLAST=1\r\n"
                               "AZURE_KEY_VAULT_URL=https://kv-a.vault.azure.net/\r\n")
        self.assertFalse((self.dir / ".env.tmp").exists())

    def test_creates_the_file(self):
        ss.update_env_file(self.env, {"SECRETS_BACKEND": "gcp"})
        self.assertEqual(self.env.read_text(), "SECRETS_BACKEND=gcp\n")

    def test_refuses_secrets_and_injection(self):
        for bad in ({"GITHUB_TOKEN": "x"}, {"VAULT_TOKEN": "x"}, {"A": "x\nGITHUB_TOKEN=y"}, {"bad name": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ss.SecretStoreError):
                ss.update_env_file(self.env, bad)
        self.assertFalse(self.env.exists())


# ============================================================================ guided setup
class SetupTests(Base):
    def run_setup(self, answers, secrets=(), choice=None, stores=None, env_text=""):
        self.env.write_text(env_text)
        answers, secrets = list(answers), list(secrets)
        stores = stores if stores is not None else {}
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answers.pop(0) if answers else ""

        def ask_secret(prompt):
            asked.append(prompt)
            return secrets.pop(0) if secrets else ""

        def make(name):
            if name not in stores:
                raise ss.SecretStoreError(f"{name} not reachable")
            return stores[name]

        code = ss.setup(self.env, ask=ask, ask_secret=ask_secret, out=self.out.append, choice=choice, make=make)
        return code, asked, "\n".join(self.out)

    def test_full_path_choose_configure_store_and_check(self):
        mem = Mem()
        idx = str(list(ss.PROVIDERS).index("vault") + 1)
        code, asked, text = self.run_setup([idx, "https://vault.corp.example:8200", "", "", "y"],
                                           secrets=["jira-token-123", "ghp_abcdefgh"], stores={"vault": mem})
        self.assertEqual(code, 0, text)
        self.assertIn("HashiCorp Vault  (SECRETS_BACKEND=vault)", text)
        self.assertEqual(mem.d, {"JIRA_API_TOKEN": "jira-token-123", "GITHUB_TOKEN": "ghp_abcdefgh"})
        env = ss.read_env_file(self.env)
        self.assertEqual((env["SECRETS_BACKEND"], env["VAULT_ADDR"]), ("vault", "https://vault.corp.example:8200"))
        self.assertNotIn("VAULT_KV_MOUNT", env)                       # optional, skipped
        for secret in ("jira-token-123", "ghp_abcdefgh"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, self.env.read_text())
        self.assertIn("all checks passed", text)

    def test_nothing_saved_until_the_steps_are_confirmed(self):
        code, _, text = self.run_setup(["https://kv-a.vault.azure.net/", "n"], choice="azure", stores={"azure": Mem()})
        self.assertEqual(code, 1)
        self.assertEqual(self.env.read_text(), "")

    def test_nothing_saved_if_the_provider_is_not_reachable(self):
        code, _, text = self.run_setup(["ca-central-1", "y"], choice="aws", stores={})
        self.assertEqual(code, 1)
        self.assertIn("not reachable", text)
        self.assertEqual(self.env.read_text(), "")

    def test_required_setting_must_be_given(self):
        code, _, text = self.run_setup(["", "y"], choice="gcp", stores={"gcp": Mem()})
        self.assertEqual(code, 1)
        self.assertIn("GCP_PROJECT_ID is required", text)

    def test_moves_tokens_out_of_env(self):
        mem = Mem()
        code, _, text = self.run_setup(["my-proj", "y", "y"], choice="gcp", stores={"gcp": mem},
                                       env_text="JIRA_API_TOKEN=jira-abc\nGITHUB_TOKEN=gh-abc\nX=1\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(mem.d, {"JIRA_API_TOKEN": "jira-abc", "GITHUB_TOKEN": "gh-abc"})
        self.assertEqual(ss.secrets_in_env_file(self.env), [])
        self.assertIn("X=1", self.env.read_text())

    def test_copies_tokens_from_the_previous_provider(self):
        old, new = Mem({"JIRA_API_TOKEN": "j-old", "GITHUB_TOKEN": "g-old"}), Mem()
        code, _, text = self.run_setup(["https://kv-a.vault.azure.net/", "y", "y"], choice="azure",
                                       stores={"azure": new, "credman": old}, env_text="SECRETS_BACKEND=credman\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(new.d, old.d)
        self.assertEqual(ss.read_env_file(self.env)["SECRETS_BACKEND"], "azure")

    def test_env_provider_only_checks(self):
        code, asked, text = self.run_setup(["y"], choice="env",
                                           stores={"env": ss.EnvironmentBackend({"JIRA_API_TOKEN": "j" * 9})})
        self.assertEqual(code, 1)                                     # GITHUB_TOKEN is not on the platform yet
        self.assertFalse(any("hidden" in a for a in asked))           # never asks for a value it cannot store
        self.assertIn("set it on the hosting platform", text)

    def test_credman_refused_off_windows(self):
        if os.name == "nt":
            self.skipTest("Windows")
        code, _, text = self.run_setup([], choice="credman", stores={"credman": Mem()})
        self.assertEqual(code, 1)
        self.assertIn("only works on Windows", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
