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


def parse_repo_url(url: str) -> tuple[str, str]:
    """('owner', 'repo') from https://github.com/owner/repo, or DeployError."""
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:\.git)?/?", url or "")
    if not m or not valid_repo_name(m.group(2)):
        raise DeployError("The saved GitHub link for this site is not valid")
    return m.group(1), m.group(2)


def _json(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise DeployError(f"GitHub refused to {what} (HTTP {r.status_code})")
    return r.json()


def update_repo(client: httpx.Client, token: str, owner: str, repo: str, files: list[dict], message: str) -> dict:
    """Add or change `files` in the repo's default branch as ONE commit (fast-forward only, never forced). Files already in the
    repo that are not in `files` are left alone. Returns {'changed': bool, 'commit_url': str | None}; nothing is committed when
    the files are identical to what is already there."""
    h = _h(token)
    base = f"{API}/repos/{owner}/{repo}"
    branch = _json(client.get(base, headers=h), "open the repository")["default_branch"]
    head = _json(client.get(f"{base}/git/ref/heads/{branch}", headers=h), "read the branch")["object"]["sha"]
    tree_sha = _json(client.get(f"{base}/git/commits/{head}", headers=h), "read the latest commit")["tree"]["sha"]
    entries = []
    for f in files:
        blob = _json(client.post(f"{base}/git/blobs", headers=h, json={"content": f["content"], "encoding": "utf-8"}), f"upload {f['path']}")
        entries.append({"path": f["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = _json(client.post(f"{base}/git/trees", headers=h, json={"base_tree": tree_sha, "tree": entries}), "build the new file tree")["sha"]
    if new_tree == tree_sha:
        return {"changed": False, "commit_url": None}
    commit = _json(client.post(f"{base}/git/commits", headers=h, json={"message": message[:200], "tree": new_tree, "parents": [head]}), "create the commit")
    _json(client.patch(f"{base}/git/refs/heads/{branch}", headers=h, json={"sha": commit["sha"]}), "move the branch to the new commit")
    return {"changed": True, "commit_url": commit.get("html_url")}
