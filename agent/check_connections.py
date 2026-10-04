"""Step 3 check: confirms Jira and GitHub credentials work.
Never prints token values."""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

REQUIRED = [
    "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
    "GITHUB_TOKEN", "GITHUB_REPO",
]


def check_env():
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        sys.exit("Missing settings in .env: " + ", ".join(missing))
    print("OK  .env loaded (token values hidden)")


def check_jira():
    base = os.getenv("JIRA_BASE_URL")
    auth = (os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN"))

    r = requests.get(base + "/rest/api/3/myself", auth=auth, timeout=30)
    if r.status_code != 200:
        sys.exit("FAIL Jira auth: HTTP " + str(r.status_code))
    print("OK  Jira user:", r.json().get("displayName"))

    key = os.getenv("JIRA_PROJECT_KEY")
    r = requests.get(base + "/rest/api/3/project/" + key, auth=auth, timeout=30)
    if r.status_code != 200:
        sys.exit("FAIL Jira project " + key + ": HTTP " + str(r.status_code))
    print("OK  Jira project:", r.json().get("name"))


def check_github():
    repo = os.getenv("GITHUB_REPO")
    headers = {
        "Authorization": "Bearer " + os.getenv("GITHUB_TOKEN"),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = requests.get("https://api.github.com/repos/" + repo,
                     headers=headers, timeout=30)
    if r.status_code != 200:
        sys.exit("FAIL GitHub repo " + repo + ": HTTP " + str(r.status_code))
    data = r.json()
    print("OK  GitHub repo:", data.get("full_name"),
          "| default branch:", data.get("default_branch"))


if __name__ == "__main__":
    try:
        check_env()
        check_jira()
        check_github()
        print("\nAll connections OK.")
    except requests.exceptions.SSLError:
        sys.exit("FAIL SSL/certificate error - re-check step 3.4")
    except requests.exceptions.ConnectionError as e:
        sys.exit("FAIL Network error: " + type(e).__name__)