import base64
import re

import httpx

API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")


class DeployError(Exception):
    pass


def valid_repo_name(name: str) -> bool:
    return bool(REPO_RE.match(name)) and not name.endswith(".git")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def create_repo(client: httpx.Client, token: str, name: str, private: bool) -> tuple[str, str, str]:
    """Returns (owner, html_url, default_branch). Never overwrites: an existing name is an error."""
    r = client.post(f"{API}/user/repos", headers=_h(token),
                    json={"name": name, "private": private, "auto_init": True, "description": "Built by the Join a Meeting AI team"})
    if r.status_code == 422:
        raise DeployError("A repository with that name already exists, or the name is invalid")
    if r.status_code >= 400:
        raise DeployError(f"GitHub refused to create the repository (HTTP {r.status_code})")
    d = r.json()
    return d["owner"]["login"], d["html_url"], d["default_branch"]


def push_files(client: httpx.Client, token: str, owner: str, repo: str, files: list[dict], branch: str) -> None:
    for f in files:
        r = client.put(f"{API}/repos/{owner}/{repo}/contents/{f['path']}", headers=_h(token),
                       json={"message": f"Add {f['path']}", "branch": branch,
                             "content": base64.b64encode(f["content"].encode()).decode()})
        if r.status_code >= 400:
            raise DeployError(f"GitHub push failed on {f['path']} (HTTP {r.status_code})")
