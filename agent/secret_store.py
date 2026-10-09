"""secret_store.py - where the console keeps its secrets, for any instance, on any platform (v13).

The console never keeps secrets in .env. Each instance picks ONE secret provider with SECRETS_BACKEND:

  credman   Windows Credential Manager     a laptop or a single Windows machine (free, nothing to install)
  azure     Azure Key Vault                Azure subscription
  aws       AWS Secrets Manager            AWS account
  gcp       Google Secret Manager          Google Cloud project
  vault     HashiCorp Vault (KV v2)        your own Vault, on-premises or HCP
  env       platform environment variables the hosting platform injects them (Docker, Kubernetes,
                                           App Service Key Vault references, ECS, Cloud Run); read-only

The rest of the console only ever calls get() / inject(); it does not know or care which provider is behind it.
Extra providers can be plugged in without changing this file (SECRETS_PLUGINS, see register_provider()).

Setting up an instance (run from agent\\):
  python secret_store.py providers               list the providers and what each needs
  python secret_store.py guide azure             step-by-step setup for one provider (or --all --markdown)
  python secret_store.py setup                   guided setup: choose a provider, follow the steps, test, store
  python secret_store.py doctor                  check settings, packages, access and every secret

Day to day:
  python secret_store.py status                  names, store and length - never values
  python secret_store.py set JIRA_API_TOKEN      prompt (hidden), store, verify
  python secret_store.py delete NAME
  python secret_store.py migrate [--yes]         move secrets out of ..\\.env into the active provider
  python secret_store.py copy --to aws [--yes]   copy every secret to another provider (then switch SECRETS_BACKEND)
  python secret_store.py check-env               exit 1 if ..\\.env still holds a secret
  python secret_store.py exec -- <command ...>   run one command with the secrets loaded

Rules
  * a provider that is configured but fails (locked, offline, no permission) STOPS the run: it fails closed
  * .env is read for secrets only if SECRETS_ALLOW_DOTENV=true (break-glass, prints a warning); never written to
  * values are never printed or logged; commands show names and lengths only
  * values are cached in memory for SECRETS_CACHE_SECONDS (default 300); the console re-reads on watcher start

From Python:
  import secret_store
  secret_store.inject()          # load secrets into os.environ (this process and its children)
  secret_store.get("GITHUB_TOKEN")
"""
from __future__ import annotations

import getpass
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

BASE_SECRETS = ("JIRA_API_TOKEN", "GITHUB_TOKEN")
SECRET_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|AUTH_URL)$", re.I)
NOT_SECRETS = {"MAX_TOKENS", "CONTEXT_PACK_MAX_TOKENS"}
PROBE = "DOCTOR_PROBE"


class SecretStoreError(RuntimeError):
    """A configured secret store failed. The caller should stop."""


def is_secret_name(name: str) -> bool:
    name = name.strip().upper()
    if name in NOT_SECRETS:
        return False
    return name in BASE_SECRETS or bool(SECRET_PATTERN.search(name))


def secret_names() -> list[str]:
    extra = [n.strip().upper() for n in os.getenv("SECRETS_EXTRA", "").split(",") if n.strip()]
    out: list[str] = []
    for n in list(BASE_SECRETS) + extra:
        if n not in out:
            out.append(n)
    return out


def namespace() -> str:
    """Prefix for every secret, so two instances sharing one vault never collide."""
    ns = os.getenv("SECRETS_NAMESPACE") or f"ai-delivery-{REPO_ROOT.name}"
    return re.sub(r"[^A-Za-z0-9-]", "-", ns).strip("-").lower()


# --------------------------------------------------------------------------- .env parsing and editing
def parse_env_lines(text: str) -> list[tuple[str | None, str]]:
    rows: list[tuple[str | None, str]] = []
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            rows.append((None, line))
            continue
        key = s.split("=", 1)[0].strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        rows.append((key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else None, line))
    return rows


def env_value(line: str) -> str:
    v = line.split("=", 1)[1].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()   # never treat a trailing comment as part of a value


def read_env_file(path: Path) -> dict[str, str]:
    if not Path(path).exists():
        return {}
    return {k: env_value(line) for k, line in parse_env_lines(Path(path).read_text(encoding="utf-8-sig")) if k}


def secrets_in_env_file(path: Path = DEFAULT_ENV_FILE) -> list[str]:
    return [k for k, v in read_env_file(path).items() if v and is_secret_name(k)]


def update_env_file(path: Path, settings: dict[str, str]) -> None:
    """Set non-secret settings in .env: replace existing lines, append new ones. Keeps every other line, the line
    endings and a byte-order mark. Refuses secret names. Atomic."""
    for k, v in settings.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise SecretStoreError(f"Not a valid setting name: {k!r}")
        if is_secret_name(k):
            raise SecretStoreError(f"Refusing to write {k} to .env: it is a secret")
        if any(c in str(v) for c in "\r\n"):
            raise SecretStoreError(f"Setting {k} must be on one line")
    path = Path(path)
    raw = path.read_bytes() if path.exists() else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    rows = parse_env_lines(text)
    left = dict(settings)
    out = []
    for k, line in rows:
        if k in left:
            out.append(f"{k}={left.pop(k)}{nl}")
        else:
            out.append(line)
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += nl
    out += [f"{k}={v}{nl}" for k, v in left.items()]
    data = (b"\xef\xbb\xbf" if bom else b"") + "".join(out).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- providers
# Every provider has: label, get(name) -> str|None, set(name, value), delete(name) -> bool.
# "Not found" returns None. Anything else raises SecretStoreError (fail closed).

class CredentialManagerBackend:
    label = "Windows Credential Manager"

    def __init__(self, api=None):
        if api is None:
            if os.name != "nt":
                raise SecretStoreError("Windows Credential Manager is only available on Windows")
            api = _win_cred_api()
        self.api = api

    def target(self, name: str) -> str:
        return f"{namespace()}/{name}"          # e.g. ai-delivery-eventregistrationapp/JIRA_API_TOKEN

    def get(self, name):
        return self.api.read(self.target(name))

    def set(self, name, value):
        self.api.write(self.target(name), value, user=getpass.getuser())

    def delete(self, name):
        return self.api.delete(self.target(name))


class AzureKeyVaultBackend:
    """Names: <namespace>--jira-api-token (Key Vault allows letters, digits, '-')."""
    label = "Azure Key Vault"
    URL = re.compile(r"^https://[a-z0-9-]{3,24}\.vault\.azure\.net/?$", re.I)

    def __init__(self, url=None, client=None):
        url = url or os.getenv("AZURE_KEY_VAULT_URL", "").strip()
        if not self.URL.match(url):
            raise SecretStoreError(f"AZURE_KEY_VAULT_URL is not a Key Vault URL: {url!r}")
        if client is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
            except ImportError as exc:
                raise SecretStoreError("pip install azure-identity azure-keyvault-secrets") from exc
            client = SecretClient(vault_url=url, credential=DefaultAzureCredential(
                exclude_interactive_browser_credential=True))
        self.url, self.client = url, client

    @staticmethod
    def key(name):
        return f"{namespace()}--{name.lower().replace('_', '-')}"

    def get(self, name):
        try:
            return self.client.get_secret(self.key(name)).value
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResourceNotFoundError" or getattr(exc, "status_code", None) == 404:
                return None
            raise SecretStoreError(f"Key Vault read failed for {name}: {type(exc).__name__} "
                                   "(run 'az login'; you need 'Key Vault Secrets User')") from exc

    def set(self, name, value):
        try:
            self.client.set_secret(self.key(name), value)
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"Key Vault write failed for {name}: {type(exc).__name__} "
                                   "(you need 'Key Vault Secrets Officer')") from exc

    def delete(self, name):
        try:
            self.client.begin_delete_secret(self.key(name))
            return True
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResourceNotFoundError":
                return False
            raise SecretStoreError(f"Key Vault delete failed for {name}: {type(exc).__name__}") from exc


class AwsSecretsManagerBackend:
    """Names: <namespace>/JIRA_API_TOKEN."""
    label = "AWS Secrets Manager"

    def __init__(self, client=None):
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise SecretStoreError("pip install boto3") from exc
            region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
            if not region:
                raise SecretStoreError("Set AWS_REGION (e.g. ca-central-1)")
            client = boto3.client("secretsmanager", region_name=region)
        self.client = client

    @staticmethod
    def key(name):
        return f"{namespace()}/{name}"

    @staticmethod
    def _code(exc):
        return getattr(exc, "response", {}).get("Error", {}).get("Code", "")

    def get(self, name):
        try:
            return self.client.get_secret_value(SecretId=self.key(name))["SecretString"]
        except Exception as exc:  # noqa: BLE001
            if self._code(exc) == "ResourceNotFoundException":
                return None
            raise SecretStoreError(f"AWS read failed for {name}: {self._code(exc) or type(exc).__name__}") from exc

    def set(self, name, value):
        try:
            self.client.put_secret_value(SecretId=self.key(name), SecretString=value)
        except Exception as exc:  # noqa: BLE001
            if self._code(exc) != "ResourceNotFoundException":
                raise SecretStoreError(f"AWS write failed for {name}: {self._code(exc) or type(exc).__name__}") from exc
            self.client.create_secret(Name=self.key(name), SecretString=value)

    def delete(self, name):
        try:
            self.client.delete_secret(SecretId=self.key(name), RecoveryWindowInDays=7)
            return True
        except Exception as exc:  # noqa: BLE001
            if self._code(exc) == "ResourceNotFoundException":
                return False
            raise SecretStoreError(f"AWS delete failed for {name}") from exc


class GcpSecretManagerBackend:
    """Names: <namespace>--JIRA_API_TOKEN (letters, digits, '-', '_')."""
    label = "Google Secret Manager"

    def __init__(self, client=None, project=None):
        self.project = project or os.getenv("GCP_PROJECT_ID", "").strip()
        if not self.project:
            raise SecretStoreError("Set GCP_PROJECT_ID")
        if client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise SecretStoreError("pip install google-cloud-secret-manager") from exc
            client = secretmanager.SecretManagerServiceClient()
        self.client = client

    def _id(self, name):
        return f"{namespace()}--{name}"

    def _path(self, name):
        return f"projects/{self.project}/secrets/{self._id(name)}"

    def get(self, name):
        try:
            r = self.client.access_secret_version(request={"name": self._path(name) + "/versions/latest"})
            return r.payload.data.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "NotFound":
                return None
            raise SecretStoreError(f"GCP read failed for {name}: {type(exc).__name__}") from exc

    def set(self, name, value):
        try:
            try:
                self.client.create_secret(request={"parent": f"projects/{self.project}", "secret_id": self._id(name),
                                                   "secret": {"replication": {"automatic": {}}}})
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "AlreadyExists":
                    raise
            self.client.add_secret_version(request={"parent": self._path(name),
                                                    "payload": {"data": value.encode("utf-8")}})
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"GCP write failed for {name}: {type(exc).__name__}") from exc

    def delete(self, name):
        try:
            self.client.delete_secret(request={"name": self._path(name)})
            return True
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "NotFound":
                return False
            raise SecretStoreError(f"GCP delete failed for {name}") from exc


class VaultBackend:
    """HashiCorp Vault, KV version 2. Path: <mount>/data/<namespace>/<NAME>, field "value".
    Token: VAULT_TOKEN, else the file 'vault login' writes (~/.vault-token). No extra package: plain HTTPS."""
    label = "HashiCorp Vault"

    def __init__(self, addr=None, mount=None, token=None, http=None, vault_namespace=None):
        self.addr = (addr or os.getenv("VAULT_ADDR", "")).strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", self.addr):
            raise SecretStoreError(f"VAULT_ADDR is not a URL: {self.addr!r}")
        if self.addr.startswith("http://") and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", self.addr):
            raise SecretStoreError("VAULT_ADDR must use https:// (http:// is allowed only for a local dev server)")
        self.mount = (mount or os.getenv("VAULT_KV_MOUNT") or "secret").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*", self.mount):
            raise SecretStoreError(f"VAULT_KV_MOUNT is not valid: {self.mount!r}")
        self._token = token
        self.vault_namespace = vault_namespace if vault_namespace is not None else os.getenv("VAULT_NAMESPACE", "")
        self.http = http or self._urllib

    def token(self) -> str:
        t = self._token or os.getenv("VAULT_TOKEN", "").strip()
        if not t:
            f = Path.home() / ".vault-token"
            if f.exists():
                t = f.read_text(encoding="utf-8").strip()
        if not t:
            raise SecretStoreError("No Vault token: run 'vault login' (or set VAULT_TOKEN in the environment)")
        return t

    def _url(self, kind, name):
        return f"{self.addr}/v1/{self.mount}/{kind}/{namespace()}/{name}"

    def _headers(self):
        h = {"X-Vault-Token": self.token(), "Content-Type": "application/json"}
        if self.vault_namespace:
            h["X-Vault-Namespace"] = self.vault_namespace
        return h

    @staticmethod
    def _urllib(method, url, headers, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (urllib.error.URLError, OSError) as e:
            raise SecretStoreError(f"Vault is not reachable at this address ({type(e).__name__})") from e

    def _call(self, method, kind, name, body=None):
        status, data = self.http(method, self._url(kind, name), self._headers(), body)
        if status in (401, 403):
            raise SecretStoreError(f"Vault refused access for {name} (HTTP {status}): check the token and its policy")
        return status, data

    def get(self, name):
        status, data = self._call("GET", "data", name)
        if status == 404:
            return None
        if status != 200:
            raise SecretStoreError(f"Vault read failed for {name} (HTTP {status})")
        return ((data.get("data") or {}).get("data") or {}).get("value")

    def set(self, name, value):
        status, _ = self._call("POST", "data", name, {"data": {"value": value}})
        if status not in (200, 204):
            raise SecretStoreError(f"Vault write failed for {name} (HTTP {status})")

    def delete(self, name):
        status, _ = self._call("DELETE", "metadata", name)
        if status == 404:
            return False
        if status not in (200, 204):
            raise SecretStoreError(f"Vault delete failed for {name} (HTTP {status})")
        return True


class EnvironmentBackend:
    """Secrets are injected as environment variables by the hosting platform. Read-only by design."""
    label = "Platform environment variables"

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    def get(self, name):
        return self.environ.get(name) or None

    def set(self, name, value):
        raise SecretStoreError("With SECRETS_BACKEND=env the hosting platform provides the secrets. Set "
                               f"{name} there (see: python secret_store.py guide env).")

    def delete(self, name):
        raise SecretStoreError("With SECRETS_BACKEND=env, remove the secret in the hosting platform.")


class DotEnvBackend:
    label = ".env file (break-glass)"

    def __init__(self, path: Path = DEFAULT_ENV_FILE):
        self.path = path

    def get(self, name):
        return read_env_file(self.path).get(name) or None

    def set(self, name, value):
        raise SecretStoreError("Refusing to write a secret to .env")

    def delete(self, name):
        return False


# --------------------------------------------------------------------------- provider registry
@dataclass
class Setting:
    name: str
    required: bool
    example: str
    help: str


@dataclass
class Provider:
    name: str
    label: str
    factory: Callable[[], object]
    best_for: str
    packages: tuple = ()
    settings: tuple = ()
    platforms: tuple = ()                              # empty = any; ("nt",) = Windows only
    writable: bool = True
    guide: Callable[[str, dict], list] = field(default=lambda ns, s: [])

    def missing_packages(self) -> list[str]:
        out = []
        for mod, pip_name in self.packages:
            try:
                found = importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                found = False
            if not found and pip_name not in out:
                out.append(pip_name)
        return out


PROVIDERS: dict[str, Provider] = {}


def register_provider(p: Provider, replace: bool = False) -> Provider:
    """Add a provider. Use from a plugin module listed in SECRETS_PLUGINS (comma-separated module names)."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", p.name):
        raise SecretStoreError(f"Provider name not valid: {p.name!r}")
    if p.name in PROVIDERS and not replace:
        raise SecretStoreError(f"Provider {p.name!r} is already registered")
    PROVIDERS[p.name] = p
    return p


def load_plugins():
    for mod in [m.strip() for m in os.getenv("SECRETS_PLUGINS", "").split(",") if m.strip()]:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"Secrets plugin {mod!r} could not be loaded: {type(exc).__name__}: {exc}") from exc


# ---- the step-by-step guides (also published as docs/SECRETS-SETUP-GUIDE.md)
def _move_steps(provider):
    return ("Move the secrets into it", [
        "python secret_store.py doctor            (checks settings, packages and access)",
        "python secret_store.py migrate --yes     (if the tokens are still in .env), or",
        f"python secret_store.py copy --to {provider} --yes   (if they are in another provider), or",
        "python secret_store.py set JIRA_API_TOKEN   (and the same for every name in: python secret_store.py status)",
        "python secret_store.py doctor            (every line should say OK)"])


def _guide_credman(ns, s):
    return [
        ("When to use it", ["One Windows machine (a laptop or a single VM). Free. Nothing to install.",
                            "Secrets are stored for the Windows user who runs the console, encrypted by Windows.",
                            "Not for containers, Linux or several machines: use a cloud provider instead."]),
        ("Point the console at it", ["Add to .env:  SECRETS_BACKEND=credman"]),
        _move_steps("credman"),
        ("Where to see them", [f"Control Panel > Credential Manager > Windows Credentials > entries starting with {ns}/"]),
    ]


def _guide_azure(ns, s):
    vault = s.get("AZURE_KEY_VAULT_URL") or "https://<vault-name>.vault.azure.net/"
    name = re.sub(r"^https://([^.]+)\..*$", r"\1", vault) if vault.startswith("https://") else "<vault-name>"
    return [
        ("Before you start", ["An Azure subscription your organization owns, and the Azure CLI (az).",
                              "pip install azure-identity azure-keyvault-secrets"]),
        ("Create the vault (once per instance)", [
            "az login",
            "az group create -n rg-ai-delivery -l canadacentral",
            f"az keyvault create -n {name} -g rg-ai-delivery -l canadacentral --enable-rbac-authorization true "
            "--enable-purge-protection true --retention-days 90",
            "Keep the vault in the client's own subscription when the project is for a client."]),
        ("Give access", [
            "People who set secrets:  role 'Key Vault Secrets Officer' on the vault",
            "The console when it runs:  role 'Key Vault Secrets User' on the vault",
            f"az role assignment create --role \"Key Vault Secrets Officer\" --assignee <your-upn> "
            f"--scope $(az keyvault show -n {name} --query id -o tsv)",
            "Role assignments can take a few minutes to apply."]),
        ("How the console signs in", [
            "Laptop: 'az login' as a person with the role above.",
            "Hosted on Azure (App Service, Container Apps, AKS, VM): a managed identity with 'Key Vault Secrets User'.",
            "No client secret is needed in either case (DefaultAzureCredential)."]),
        ("Point the console at it", ["Add to .env:", "  SECRETS_BACKEND=azure", f"  AZURE_KEY_VAULT_URL={vault}"]),
        _move_steps("azure"),
        ("Where to see them", [f"Azure portal > Key vaults > {name} > Objects > Secrets > names starting with {ns}--"]),
    ]


def _guide_aws(ns, s):
    region = s.get("AWS_REGION") or "ca-central-1"
    return [
        ("Before you start", ["An AWS account your organization owns, and the AWS CLI.", "pip install boto3"]),
        ("Give access (IAM policy)", [
            "Allow on resources arn:aws:secretsmanager:" + region + ":<account-id>:secret:" + ns + "/*",
            "  read:   secretsmanager:GetSecretValue, secretsmanager:DescribeSecret",
            "  write:  secretsmanager:CreateSecret, secretsmanager:PutSecretValue, secretsmanager:DeleteSecret",
            "Give read+write to the people who set secrets; read only to the console when it runs."]),
        ("How the console signs in", [
            "Laptop: 'aws sso login' (or 'aws configure sso' first), then set AWS_PROFILE in the environment.",
            "Hosted on AWS (ECS, EKS, EC2, Lambda): an IAM role attached to the workload. No access keys in files."]),
        ("Point the console at it", ["Add to .env:", "  SECRETS_BACKEND=aws", f"  AWS_REGION={region}"]),
        _move_steps("aws"),
        ("Where to see them", [f"AWS console > Secrets Manager ({region}) > secrets named {ns}/..."]),
    ]


def _guide_gcp(ns, s):
    project = s.get("GCP_PROJECT_ID") or "<project-id>"
    return [
        ("Before you start", ["A Google Cloud project your organization owns, and the gcloud CLI.",
                              "pip install google-cloud-secret-manager"]),
        ("Turn on the API (once per project)", [f"gcloud services enable secretmanager.googleapis.com --project {project}"]),
        ("Give access", [
            "People who set secrets:  roles/secretmanager.admin (or secretmanager.secretVersionAdder plus permission to create secrets)",
            "The console when it runs:  roles/secretmanager.secretAccessor"]),
        ("How the console signs in", [
            "Laptop: gcloud auth application-default login",
            "Hosted on Google Cloud (Cloud Run, GKE, Compute Engine): the workload's service account. No key files."]),
        ("Point the console at it", ["Add to .env:", "  SECRETS_BACKEND=gcp", f"  GCP_PROJECT_ID={project}"]),
        _move_steps("gcp"),
        ("Where to see them", [f"Google Cloud console > Security > Secret Manager > secrets named {ns}--..."]),
    ]


def _guide_vault(ns, s):
    addr = s.get("VAULT_ADDR") or "https://vault.example.com:8200"
    mount = s.get("VAULT_KV_MOUNT") or "secret"
    return [
        ("Before you start", ["A HashiCorp Vault server (self-managed or HCP) with a KV version 2 engine.",
                              "No Python package is needed."]),
        ("Prepare Vault (a Vault administrator does this once)", [
            f"vault secrets enable -path={mount} kv-v2        (skip if the KV v2 engine already exists)",
            "Create a policy, e.g. ai-delivery.hcl:",
            f"  path \"{mount}/data/{ns}/*\"     {{ capabilities = [\"create\", \"read\", \"update\"] }}",
            f"  path \"{mount}/metadata/{ns}/*\" {{ capabilities = [\"read\", \"delete\"] }}",
            "vault policy write ai-delivery ai-delivery.hcl",
            "Attach it to the people who set secrets; a read-only version (read on data/) to the console."]),
        ("How the console signs in", [
            "Laptop: 'vault login' (the token is saved to ~/.vault-token and read from there).",
            "Hosted: provide VAULT_TOKEN through the platform (for example from Vault Agent or the Kubernetes "
            "auth method). Never put the token in .env."]),
        ("Point the console at it", ["Add to .env:", "  SECRETS_BACKEND=vault", f"  VAULT_ADDR={addr}",
                                     f"  VAULT_KV_MOUNT={mount}", "  VAULT_NAMESPACE=<only for Vault Enterprise/HCP namespaces>"]),
        _move_steps("vault"),
        ("Where to see them", [f"vault kv list {mount}/{ns}"]),
    ]


def _guide_env(ns, s):
    names = ", ".join(secret_names())
    return [
        ("When to use it", ["The console runs on a platform that injects secrets as environment variables, usually "
                            "straight from that platform's own vault. The console reads them and never writes them.",
                            f"Variables it needs: {names}"]),
        ("Pick your platform", [
            "Docker:      docker run --env-file <file outside the repo> ...   (never commit that file)",
            "Kubernetes:  a Secret, mapped with env[].valueFrom.secretKeyRef (or envFrom.secretRef)",
            "Azure App Service / Container Apps:  app settings that are Key Vault references",
            "AWS ECS:     'secrets' in the task definition, valueFrom a Secrets Manager ARN",
            "Google Cloud Run:  --set-secrets NAME=secret-name:latest"]),
        ("Point the console at it", ["Set SECRETS_BACKEND=env (as a platform setting or in .env)."]),
        ("Check it", ["python secret_store.py doctor   (inside the running container or app)"]),
    ]


def _builtin_providers():
    for p in (
        Provider("credman", "Windows Credential Manager", CredentialManagerBackend,
                 "A laptop or one Windows machine. Free.", platforms=("nt",), guide=_guide_credman),
        Provider("azure", "Azure Key Vault", AzureKeyVaultBackend, "Projects on Azure or a Microsoft tenant.",
                 packages=(("azure.identity", "azure-identity"), ("azure.keyvault.secrets", "azure-keyvault-secrets")),
                 settings=(Setting("AZURE_KEY_VAULT_URL", True, "https://kv-aidelivery-proj.vault.azure.net/",
                                   "The vault's address (Overview > Vault URI)"),), guide=_guide_azure),
        Provider("aws", "AWS Secrets Manager", AwsSecretsManagerBackend, "Projects on AWS.",
                 packages=(("boto3", "boto3"),),
                 settings=(Setting("AWS_REGION", True, "ca-central-1", "The region that holds the secrets"),),
                 guide=_guide_aws),
        Provider("gcp", "Google Secret Manager", GcpSecretManagerBackend, "Projects on Google Cloud.",
                 packages=(("google.cloud.secretmanager", "google-cloud-secret-manager"),),
                 settings=(Setting("GCP_PROJECT_ID", True, "my-project-123", "The Google Cloud project id"),),
                 guide=_guide_gcp),
        Provider("vault", "HashiCorp Vault", VaultBackend, "Organizations that run HashiCorp Vault.",
                 settings=(Setting("VAULT_ADDR", True, "https://vault.example.com:8200", "The Vault address"),
                           Setting("VAULT_KV_MOUNT", False, "secret", "The KV v2 mount path (default: secret)"),
                           Setting("VAULT_NAMESPACE", False, "", "Only for Vault Enterprise / HCP namespaces")),
                 guide=_guide_vault),
        Provider("env", "Platform environment variables", EnvironmentBackend,
                 "Containers and managed platforms that inject secrets.", writable=False, guide=_guide_env),
    ):
        register_provider(p, replace=True)


_builtin_providers()
BACKENDS = tuple(PROVIDERS)            # kept for older callers


def provider(name: str) -> Provider:
    load_plugins()
    key = (name or "credman").strip().lower()
    if key not in PROVIDERS:
        raise SecretStoreError(f"SECRETS_BACKEND must be one of {', '.join(PROVIDERS)} (got {key!r})")
    return PROVIDERS[key]


def make_backend(kind: str):
    return provider(kind).factory()


def default_backends() -> list:
    backends = [make_backend(os.getenv("SECRETS_BACKEND", "credman"))]
    if os.getenv("SECRETS_ALLOW_DOTENV", "false").strip().lower() == "true":
        backends.append(DotEnvBackend())
    return backends


# --------------------------------------------------------------------------- the store
class SecretStore:
    def __init__(self, backends=None, ttl=None, clock=time.monotonic):
        self.backends = backends if backends is not None else default_backends()
        self.ttl = ttl if ttl is not None else int(os.getenv("SECRETS_CACHE_SECONDS", "300"))
        self.clock = clock
        self._cache: dict[str, tuple[str, str, float]] = {}
        self._warned = False

    @property
    def primary(self):
        for b in self.backends:
            if not isinstance(b, DotEnvBackend):
                return b
        raise SecretStoreError("No writable secret store configured")

    def lookup(self, name):
        hit = self._cache.get(name)
        if hit and self.clock() - hit[2] < self.ttl:
            return hit[0], hit[1]
        for b in self.backends:
            value = b.get(name)              # SecretStoreError propagates: fail closed
            if value:
                if isinstance(b, DotEnvBackend) and not self._warned:
                    print("[secrets] WARNING: secrets read from .env (SECRETS_ALLOW_DOTENV=true). "
                          "Move them: python secret_store.py migrate --yes", file=sys.stderr)
                    self._warned = True
                self._cache[name] = (value, b.label, self.clock())
                return value, b.label
        return None, None

    def get(self, name):
        return self.lookup(name)[0]

    def forget(self, name=None):
        self._cache.clear() if name is None else self._cache.pop(name, None)


_STORE: SecretStore | None = None


def store() -> SecretStore:
    global _STORE
    if _STORE is None:
        _STORE = SecretStore()
    return _STORE


def get(name):
    return store().get(name)


def inject(names=None, environ=None, required=False, force=False) -> dict:
    """Load secrets into the environment. Existing non-empty values are kept (GitHub Actions passes its own) unless
    force=True, which lets the store win whenever it has a value. Returns {name: source} - never values."""
    environ = os.environ if environ is None else environ
    report, missing = {}, []
    for name in names or secret_names():
        if environ.get(name) and not force:
            report[name] = "environment"
            continue
        value, source = store().lookup(name)
        if value:
            environ[name] = value
            report[name] = source
        elif environ.get(name):
            report[name] = "environment"
        else:
            report[name] = "MISSING"
            missing.append(name)
    if missing and required:
        raise SecretStoreError("Missing secrets: " + ", ".join(missing) +
                               " - store them with: python secret_store.py set <NAME>")
    return report


def refresh_environment(names=None, environ=None) -> list[str]:
    """Re-read the secrets from the store (bypassing the cache) so a rotated token is used from the next watcher
    start. Returns the names whose value changed. Never prints values."""
    environ = os.environ if environ is None else environ
    names = list(names or secret_names())
    before = {n: environ.get(n) for n in names}
    store().forget()
    inject(names, environ, force=True)
    return [n for n in names if environ.get(n) != before[n]]


def secret_values() -> list[str]:
    """For log redaction: every known secret value currently available (never print these)."""
    vals = []
    for n in secret_names():
        try:
            v = store().get(n)
        except SecretStoreError:
            v = None
        if v and len(v) >= 6:
            vals.append(v)
    return vals


# --------------------------------------------------------------------------- migration
def _write_verified(dest, name, value):
    dest.set(name, value)
    if dest.get(name) != value:
        raise SecretStoreError(f"Read-back check failed for {name} in {dest.label}")


def migrate(env_path=DEFAULT_ENV_FILE, apply=False, dest=None, out=print) -> int:
    """.env -> active store. Each value is written and read back before its line is removed.
    .env is rewritten atomically, keeping every other line and the line endings. No backup copy."""
    if not Path(env_path).exists():
        out(f"[secrets] {env_path} not found - nothing to migrate")
        return 0
    rows = parse_env_lines(Path(env_path).read_bytes().decode("utf-8-sig"))
    targets = [(k, env_value(line)) for k, line in rows if k and is_secret_name(k) and env_value(line)]
    if not targets:
        out("[secrets] .env holds no secret values. Nothing to do.")
        return 0
    dest = dest or store().primary
    out(f"[secrets] {len(targets)} secret(s) in .env -> {dest.label}:")
    for k, v in targets:
        out(f"           {k}  ({len(v)} chars)")
    if not apply:
        out("[secrets] Dry run. Re-run with --yes to move them.")
        return 0
    for k, v in targets:
        _write_verified(dest, k, v)
        out(f"[secrets] stored and verified {k}")
    moved = {k for k, _ in targets}
    env_path = Path(env_path)
    tmp = env_path.with_name(env_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(line for k, line in rows if k not in moved))
    os.replace(tmp, env_path)
    out(f"[secrets] removed {len(moved)} line(s) from {env_path.name}. Restart the console service.")
    return 0


def copy_all(src, dest, names=None, apply=False, out=print) -> int:
    """Store -> store (e.g. Credential Manager -> Azure Key Vault). Source is left untouched, so you can switch
    back by changing SECRETS_BACKEND."""
    names = names or secret_names()
    found = [(n, src.get(n)) for n in names]
    missing = [n for n, v in found if not v]
    found = [(n, v) for n, v in found if v]
    out(f"[secrets] copy {src.label} -> {dest.label}: {len(found)} secret(s)"
        + (f"; not in source: {', '.join(missing)}" if missing else ""))
    for n, v in found:
        out(f"           {n}  ({len(v)} chars)")
    if not apply:
        out("[secrets] Dry run. Re-run with --yes to copy.")
        return 0
    for n, v in found:
        _write_verified(dest, n, v)
        out(f"[secrets] copied and verified {n}")
    out("[secrets] Done. Now set SECRETS_BACKEND in .env to the new store and restart the console service.")
    return 1 if missing else 0


# --------------------------------------------------------------------------- guide, doctor, setup
def guide_lines(name: str, settings: dict | None = None) -> list[str]:
    p = provider(name)
    settings = dict(os.environ) | dict(settings or {})
    lines = [f"{p.label}  (SECRETS_BACKEND={p.name})", f"Best for: {p.best_for}"]
    if p.platforms == ("nt",):
        lines.append("Runs on: Windows only")
    for i, (title, steps) in enumerate(p.guide(namespace(), settings), 1):
        lines.append("")
        lines.append(f"{i}. {title}")
        lines += [f"   {s}" for s in steps]
    return lines


def guide_markdown() -> str:
    ns = "<namespace>"
    out = ["# Secrets: setup guide for a new instance", "",
           "Each instance of the AI Delivery Console keeps its tokens in ONE secret provider, chosen with "
           "`SECRETS_BACKEND` in `.env`. The console reads them at start-up and never stores them in `.env`.", "",
           "Generated from `secret_store.py` (`python secret_store.py guide --all --markdown`). "
           f"`{ns}` is the instance's prefix, from `SECRETS_NAMESPACE` (default `ai-delivery-<repo folder>`).", "",
           "| Provider | `SECRETS_BACKEND` | Best for | Settings | Install |", "|---|---|---|---|---|"]
    for p in PROVIDERS.values():
        sets = ", ".join(f"`{s.name}`" + ("" if s.required else " (optional)") for s in p.settings) or "none"
        pk = ", ".join(sorted({x for _, x in p.packages})) or "nothing"
        out.append(f"| {p.label} | `{p.name}` | {p.best_for} | {sets} | {pk} |")
    out += ["", "Quick start: `python secret_store.py setup` walks through the steps below and tests the result. "
            "`python secret_store.py doctor` checks an instance at any time.", ""]
    for p in PROVIDERS.values():
        out += [f"## {p.label} (`{p.name}`)", "", f"Best for: {p.best_for}", ""]
        for i, (title, steps) in enumerate(p.guide(ns, {}), 1):
            out += [f"**{i}. {title}**", "", "```"] + list(steps) + ["```", ""]
    out += ["## Moving to another provider later", "",
            "```", "python secret_store.py copy --to <provider> --yes", "(set SECRETS_BACKEND=<provider> in .env)",
            "python secret_store.py doctor", "```", "",
            "The copy reads every value back before reporting success and never changes the source, so you can "
            "switch back by changing `SECRETS_BACKEND`.", ""]
    return "\n".join(out)


def doctor(env_path=DEFAULT_ENV_FILE, out=print, backend=None) -> int:
    """Check one instance end to end. Returns the number of failed checks."""
    fails = 0

    def line(ok, label, detail=""):
        nonlocal fails
        fails += 0 if ok else 1
        out(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f"  -  {detail}" if detail else ""))

    name = os.getenv("SECRETS_BACKEND", "credman").strip().lower()
    out(f"[secrets] doctor  provider={name}  namespace={namespace()}")
    try:
        p = provider(name)
    except SecretStoreError as e:
        line(False, "provider", str(e))
        return fails
    line(True, "provider", p.label)
    if p.platforms and os.name not in p.platforms:
        line(False, "platform", f"{p.label} only works on Windows")
        return fails
    for s in p.settings:
        v = os.getenv(s.name, "").strip()
        if s.required:
            line(bool(v), f"setting {s.name}", v or f"missing - {s.help}, e.g. {s.example}")
    missing = p.missing_packages() if backend is None else []
    if missing:
        line(False, "packages", "pip install " + " ".join(missing))
        return fails
    if p.packages:
        line(True, "packages", "installed")
    try:
        b = backend or p.factory()
        b.get(PROBE)
        line(True, "access", "the provider answered")
    except SecretStoreError as e:
        line(False, "access", str(e))
        return fails
    for n in secret_names():
        try:
            v = b.get(n)
        except SecretStoreError as e:
            line(False, f"secret {n}", str(e))
            continue
        line(bool(v), f"secret {n}", f"present ({len(v)} chars)" if v else
             ("missing - set it on the hosting platform" if not p.writable else
              f"missing - python secret_store.py set {n}"))
    left = secrets_in_env_file(Path(env_path))
    line(not left, ".env holds no secrets", ", ".join(left) + " - python secret_store.py migrate --yes" if left else "")
    out("[secrets] all checks passed" if not fails else f"[secrets] {fails} check(s) failed")
    return fails


def setup(env_path=DEFAULT_ENV_FILE, ask=input, ask_secret=getpass.getpass, out=print, choice=None,
          make=None) -> int:
    """Guided setup for a new instance: choose a provider, follow its steps, save the settings, test, store secrets.
    `make(name)` builds the backend (tests pass a fake)."""
    make = make or make_backend
    names = list(PROVIDERS)
    if choice is None:
        out("Where should this instance keep its secrets?")
        for i, n in enumerate(names, 1):
            p = PROVIDERS[n]
            out(f"  {i}. {p.label:<32} {p.best_for}")
        raw = ask(f"Choose 1-{len(names)}: ").strip()
        choice = names[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(names) else raw.lower()
    p = provider(choice)
    out("")
    for ln in guide_lines(p.name):
        out(ln)
    out("")
    if p.platforms and os.name not in p.platforms:
        out(f"[secrets] {p.label} only works on Windows. Choose another provider.")
        return 1
    current = read_env_file(Path(env_path)) | {k: v for k, v in os.environ.items() if k.isupper()}
    values = {"SECRETS_BACKEND": p.name}
    for s in p.settings:
        default = current.get(s.name, "")
        hint = f" [{default}]" if default else (" (optional, Enter to skip)" if not s.required else f" e.g. {s.example}")
        v = ask(f"{s.name} - {s.help}{hint}: ").strip() or default
        if s.required and not v:
            out(f"[secrets] {s.name} is required. Nothing was saved.")
            return 1
        if v:
            values[s.name] = v
    ans = ask("Have you finished the steps above (store created, access given, signed in)? [y/N]: ").strip().lower()
    if not ans.startswith("y"):
        out("[secrets] Run 'python secret_store.py setup' again when the steps are done. Nothing was saved.")
        return 1
    previous = current.get("SECRETS_BACKEND", "")
    for k, v in values.items():
        os.environ[k] = v
    try:
        b = make(p.name)
        b.get(PROBE)
    except SecretStoreError as e:
        out(f"[secrets] The provider is not reachable yet: {e}")
        out("[secrets] Nothing was saved. Fix the step above and run setup again.")
        return 1
    update_env_file(Path(env_path), values)
    out(f"[secrets] Saved to {Path(env_path).name}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    global _STORE
    _STORE = None
    if not p.writable:
        return 1 if doctor(env_path, out, backend=b) else 0
    if secrets_in_env_file(Path(env_path)) and ask("Move the tokens that are still in .env? [Y/n]: ").strip().lower() != "n":
        migrate(Path(env_path), apply=True, dest=b, out=out)
    if previous and previous != p.name:
        try:
            old = make(previous)
            if any(old.get(n) for n in secret_names()) and \
                    ask(f"Copy the tokens from {PROVIDERS[previous].label if previous in PROVIDERS else previous}? [Y/n]: ").strip().lower() != "n":
                copy_all(old, b, apply=True, out=out)
        except SecretStoreError as e:
            out(f"[secrets] Could not read the previous provider ({e}); enter the tokens instead.")
    for n in secret_names():
        if b.get(n):
            continue
        v = ask_secret(f"{n} (hidden, Enter to skip): ").strip()
        if v:
            _write_verified(b, n, v)
            out(f"[secrets] {n} stored and verified ({len(v)} chars)")
    out("")
    return 1 if doctor(env_path, out, backend=b) else 0


# --------------------------------------------------------------------------- Windows API
def _win_cred_api():
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    PCRED = ctypes.POINTER(CREDENTIALW)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCRED)]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [PCRED, wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    GENERIC, PERSIST_LOCAL_MACHINE, NOT_FOUND, MAX_BLOB = 1, 2, 1168, 2560

    class Api:
        @staticmethod
        def read(target):
            p = PCRED()
            if not adv.CredReadW(target, GENERIC, 0, ctypes.byref(p)):
                err = ctypes.get_last_error()
                if err == NOT_FOUND:
                    return None
                raise SecretStoreError(f"Credential Manager read failed (Windows error {err})")
            try:
                c = p.contents
                return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
            finally:
                adv.CredFree(p)

        @staticmethod
        def write(target, value, user=""):
            data = value.encode("utf-16-le")
            if len(data) > MAX_BLOB:
                raise SecretStoreError("Value too long for Credential Manager (max 1280 characters)")
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            c = CREDENTIALW()
            c.Type, c.TargetName = GENERIC, target
            c.Comment = "AI delivery pipeline secret (secret_store.py)"
            c.CredentialBlobSize = len(data)
            c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
            c.Persist, c.UserName = PERSIST_LOCAL_MACHINE, user
            if not adv.CredWriteW(ctypes.byref(c), 0):
                raise SecretStoreError(f"Credential Manager write failed (Windows error {ctypes.get_last_error()})")

        @staticmethod
        def delete(target):
            if adv.CredDeleteW(target, GENERIC, 0):
                return True
            if ctypes.get_last_error() == NOT_FOUND:
                return False
            raise SecretStoreError(f"Credential Manager delete failed (Windows error {ctypes.get_last_error()})")

    return Api()


# --------------------------------------------------------------------------- CLI
def _cli(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    env_path = Path(rest[rest.index("--env") + 1]) if "--env" in rest else DEFAULT_ENV_FILE
    try:
        if cmd == "providers":
            load_plugins()
            for p in PROVIDERS.values():
                sets = ", ".join(s.name for s in p.settings if s.required) or "-"
                miss = p.missing_packages()
                note = ("needs: pip install " + " ".join(miss)) if miss else "ready"
                if p.platforms and os.name not in p.platforms:
                    note = "Windows only"
                print(f"  {p.name:<8} {p.label:<32} settings: {sets:<22} {note}")
            print("[secrets] details: python secret_store.py guide <name>")
            return 0
        if cmd == "guide":
            if "--all" in rest:
                load_plugins()
                print(guide_markdown() if "--markdown" in rest else
                      "\n\n".join("\n".join(guide_lines(n)) for n in PROVIDERS))
                return 0
            name = next((r for r in rest if not r.startswith("-")), os.getenv("SECRETS_BACKEND", "credman"))
            print("\n".join(guide_lines(name)))
            return 0
        if cmd == "doctor":
            return 1 if doctor(env_path) else 0
        if cmd == "setup":
            pick = rest[rest.index("--provider") + 1] if "--provider" in rest else None
            return setup(env_path, choice=pick)
        if cmd == "check-env":
            left = secrets_in_env_file(env_path)
            print("[secrets] .env clean - no secret values" if not left
                  else f"[secrets] FAIL .env still holds: {', '.join(left)}")
            return 1 if left else 0
        if cmd == "migrate":
            return migrate(env_path, apply="--yes" in rest)
        if cmd == "copy":
            if "--to" not in rest:
                print("[secrets] usage: copy --to " + "|".join(PROVIDERS) + " [--yes]")
                return 1
            src, dest = store().primary, make_backend(rest[rest.index("--to") + 1])
            if type(src) is type(dest):
                print("[secrets] source and destination are the same store")
                return 1
            return copy_all(src, dest, apply="--yes" in rest)
        st = store()
        if cmd == "status":
            print(f"[secrets] store: {' + '.join(b.label for b in st.backends)}   namespace: {namespace()}")
            bad = 0
            for n in secret_names():
                v, src = st.lookup(n)
                print(f"  {n:<22} {'set (' + str(len(v)) + ' chars) in ' + src if v else 'MISSING'}")
                bad += 0 if v else 1
            left = secrets_in_env_file(env_path)
            if left:
                print(f"  WARNING .env still holds: {', '.join(left)} -> python secret_store.py migrate --yes")
            return 1 if bad or left else 0
        if cmd == "set":
            if not rest:
                print("[secrets] usage: set NAME")
                return 1
            name = rest[0].upper()
            if not is_secret_name(name) and name not in secret_names():
                print(f"[secrets] note: add {name} to SECRETS_EXTRA in .env so it is loaded and redacted")
            value = getpass.getpass(f"Value for {name} (hidden, paste then Enter): ").strip()
            if not value:
                print("[secrets] empty value - nothing stored")
                return 1
            _write_verified(st.primary, name, value)
            st.forget(name)
            print(f"[secrets] {name} stored in {st.primary.label} ({len(value)} chars) - verified")
            return 0
        if cmd == "delete":
            name = rest[0].upper()
            print(f"[secrets] {name}: {'deleted' if st.primary.delete(name) else 'not found'}")
            return 0
        if cmd == "exec":
            args = rest[1:] if rest and rest[0] == "--" else rest
            inject(required=True)
            return subprocess.call(args)
    except SecretStoreError as exc:
        print(f"[secrets] FAIL {exc}", file=sys.stderr)
        return 2
    print(f"[secrets] unknown command: {cmd} (try --help)")
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
