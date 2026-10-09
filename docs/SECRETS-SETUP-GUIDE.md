# Secrets: setup guide for a new instance

Each instance of the AI Delivery Console keeps its tokens in ONE secret provider, chosen with `SECRETS_BACKEND` in `.env`. The console reads them at start-up and never stores them in `.env`.

Generated from `secret_store.py` (`python secret_store.py guide --all --markdown`). `<namespace>` is the instance's prefix, from `SECRETS_NAMESPACE` (default `ai-delivery-<repo folder>`).

| Provider | `SECRETS_BACKEND` | Best for | Settings | Install |
|---|---|---|---|---|
| Windows Credential Manager | `credman` | A laptop or one Windows machine. Free. | none | nothing |
| Azure Key Vault | `azure` | Projects on Azure or a Microsoft tenant. | `AZURE_KEY_VAULT_URL` | azure-identity, azure-keyvault-secrets |
| AWS Secrets Manager | `aws` | Projects on AWS. | `AWS_REGION` | boto3 |
| Google Secret Manager | `gcp` | Projects on Google Cloud. | `GCP_PROJECT_ID` | google-cloud-secret-manager |
| HashiCorp Vault | `vault` | Organizations that run HashiCorp Vault. | `VAULT_ADDR`, `VAULT_KV_MOUNT` (optional), `VAULT_NAMESPACE` (optional) | nothing |
| Platform environment variables | `env` | Containers and managed platforms that inject secrets. | none | nothing |

Quick start: `python secret_store.py setup` walks through the steps below and tests the result. `python secret_store.py doctor` checks an instance at any time.

## Windows Credential Manager (`credman`)

Best for: A laptop or one Windows machine. Free.

**1. When to use it**

```
One Windows machine (a laptop or a single VM). Free. Nothing to install.
Secrets are stored for the Windows user who runs the console, encrypted by Windows.
Not for containers, Linux or several machines: use a cloud provider instead.
```

**2. Point the console at it**

```
Add to .env:  SECRETS_BACKEND=credman
```

**3. Move the secrets into it**

```
python secret_store.py doctor            (checks settings, packages and access)
python secret_store.py migrate --yes     (if the tokens are still in .env), or
python secret_store.py copy --to credman --yes   (if they are in another provider), or
python secret_store.py set JIRA_API_TOKEN   (and the same for every name in: python secret_store.py status)
python secret_store.py doctor            (every line should say OK)
```

**4. Where to see them**

```
Control Panel > Credential Manager > Windows Credentials > entries starting with <namespace>/
```

## Azure Key Vault (`azure`)

Best for: Projects on Azure or a Microsoft tenant.

**1. Before you start**

```
An Azure subscription your organization owns, and the Azure CLI (az).
pip install azure-identity azure-keyvault-secrets
```

**2. Create the vault (once per instance)**

```
az login
az group create -n rg-ai-delivery -l canadacentral
az keyvault create -n <vault-name> -g rg-ai-delivery -l canadacentral --enable-rbac-authorization true --enable-purge-protection true --retention-days 90
Keep the vault in the client's own subscription when the project is for a client.
```

**3. Give access**

```
People who set secrets:  role 'Key Vault Secrets Officer' on the vault
The console when it runs:  role 'Key Vault Secrets User' on the vault
az role assignment create --role "Key Vault Secrets Officer" --assignee <your-upn> --scope $(az keyvault show -n <vault-name> --query id -o tsv)
Role assignments can take a few minutes to apply.
```

**4. How the console signs in**

```
Laptop: 'az login' as a person with the role above.
Hosted on Azure (App Service, Container Apps, AKS, VM): a managed identity with 'Key Vault Secrets User'.
No client secret is needed in either case (DefaultAzureCredential).
```

**5. Point the console at it**

```
Add to .env:
  SECRETS_BACKEND=azure
  AZURE_KEY_VAULT_URL=https://<vault-name>.vault.azure.net/
```

**6. Move the secrets into it**

```
python secret_store.py doctor            (checks settings, packages and access)
python secret_store.py migrate --yes     (if the tokens are still in .env), or
python secret_store.py copy --to azure --yes   (if they are in another provider), or
python secret_store.py set JIRA_API_TOKEN   (and the same for every name in: python secret_store.py status)
python secret_store.py doctor            (every line should say OK)
```

**7. Where to see them**

```
Azure portal > Key vaults > <vault-name> > Objects > Secrets > names starting with <namespace>--
```

## AWS Secrets Manager (`aws`)

Best for: Projects on AWS.

**1. Before you start**

```
An AWS account your organization owns, and the AWS CLI.
pip install boto3
```

**2. Give access (IAM policy)**

```
Allow on resources arn:aws:secretsmanager:ca-central-1:<account-id>:secret:<namespace>/*
  read:   secretsmanager:GetSecretValue, secretsmanager:DescribeSecret
  write:  secretsmanager:CreateSecret, secretsmanager:PutSecretValue, secretsmanager:DeleteSecret
Give read+write to the people who set secrets; read only to the console when it runs.
```

**3. How the console signs in**

```
Laptop: 'aws sso login' (or 'aws configure sso' first), then set AWS_PROFILE in the environment.
Hosted on AWS (ECS, EKS, EC2, Lambda): an IAM role attached to the workload. No access keys in files.
```

**4. Point the console at it**

```
Add to .env:
  SECRETS_BACKEND=aws
  AWS_REGION=ca-central-1
```

**5. Move the secrets into it**

```
python secret_store.py doctor            (checks settings, packages and access)
python secret_store.py migrate --yes     (if the tokens are still in .env), or
python secret_store.py copy --to aws --yes   (if they are in another provider), or
python secret_store.py set JIRA_API_TOKEN   (and the same for every name in: python secret_store.py status)
python secret_store.py doctor            (every line should say OK)
```

**6. Where to see them**

```
AWS console > Secrets Manager (ca-central-1) > secrets named <namespace>/...
```

## Google Secret Manager (`gcp`)

Best for: Projects on Google Cloud.

**1. Before you start**

```
A Google Cloud project your organization owns, and the gcloud CLI.
pip install google-cloud-secret-manager
```

**2. Turn on the API (once per project)**

```
gcloud services enable secretmanager.googleapis.com --project <project-id>
```

**3. Give access**

```
People who set secrets:  roles/secretmanager.admin (or secretmanager.secretVersionAdder plus permission to create secrets)
The console when it runs:  roles/secretmanager.secretAccessor
```

**4. How the console signs in**

```
Laptop: gcloud auth application-default login
Hosted on Google Cloud (Cloud Run, GKE, Compute Engine): the workload's service account. No key files.
```

**5. Point the console at it**

```
Add to .env:
  SECRETS_BACKEND=gcp
  GCP_PROJECT_ID=<project-id>
```

**6. Move the secrets into it**

```
python secret_store.py doctor            (checks settings, packages and access)
python secret_store.py migrate --yes     (if the tokens are still in .env), or
python secret_store.py copy --to gcp --yes   (if they are in another provider), or
python secret_store.py set JIRA_API_TOKEN   (and the same for every name in: python secret_store.py status)
python secret_store.py doctor            (every line should say OK)
```

**7. Where to see them**

```
Google Cloud console > Security > Secret Manager > secrets named <namespace>--...
```

## HashiCorp Vault (`vault`)

Best for: Organizations that run HashiCorp Vault.

**1. Before you start**

```
A HashiCorp Vault server (self-managed or HCP) with a KV version 2 engine.
No Python package is needed.
```

**2. Prepare Vault (a Vault administrator does this once)**

```
vault secrets enable -path=secret kv-v2        (skip if the KV v2 engine already exists)
Create a policy, e.g. ai-delivery.hcl:
  path "secret/data/<namespace>/*"     { capabilities = ["create", "read", "update"] }
  path "secret/metadata/<namespace>/*" { capabilities = ["read", "delete"] }
vault policy write ai-delivery ai-delivery.hcl
Attach it to the people who set secrets; a read-only version (read on data/) to the console.
```

**3. How the console signs in**

```
Laptop: 'vault login' (the token is saved to ~/.vault-token and read from there).
Hosted: provide VAULT_TOKEN through the platform (for example from Vault Agent or the Kubernetes auth method). Never put the token in .env.
```

**4. Point the console at it**

```
Add to .env:
  SECRETS_BACKEND=vault
  VAULT_ADDR=https://vault.example.com:8200
  VAULT_KV_MOUNT=secret
  VAULT_NAMESPACE=<only for Vault Enterprise/HCP namespaces>
```

**5. Move the secrets into it**

```
python secret_store.py doctor            (checks settings, packages and access)
python secret_store.py migrate --yes     (if the tokens are still in .env), or
python secret_store.py copy --to vault --yes   (if they are in another provider), or
python secret_store.py set JIRA_API_TOKEN   (and the same for every name in: python secret_store.py status)
python secret_store.py doctor            (every line should say OK)
```

**6. Where to see them**

```
vault kv list secret/<namespace>
```

## Platform environment variables (`env`)

Best for: Containers and managed platforms that inject secrets.

**1. When to use it**

```
The console runs on a platform that injects secrets as environment variables, usually straight from that platform's own vault. The console reads them and never writes them.
Variables it needs: JIRA_API_TOKEN, GITHUB_TOKEN
```

**2. Pick your platform**

```
Docker:      docker run --env-file <file outside the repo> ...   (never commit that file)
Kubernetes:  a Secret, mapped with env[].valueFrom.secretKeyRef (or envFrom.secretRef)
Azure App Service / Container Apps:  app settings that are Key Vault references
AWS ECS:     'secrets' in the task definition, valueFrom a Secrets Manager ARN
Google Cloud Run:  --set-secrets NAME=secret-name:latest
```

**3. Point the console at it**

```
Set SECRETS_BACKEND=env (as a platform setting or in .env).
```

**4. Check it**

```
python secret_store.py doctor   (inside the running container or app)
```

## Moving to another provider later

```
python secret_store.py copy --to <provider> --yes
(set SECRETS_BACKEND=<provider> in .env)
python secret_store.py doctor
```

The copy reads every value back before reporting success and never changes the source, so you can switch back by changing `SECRETS_BACKEND`.

