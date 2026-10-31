import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from deploy_flow import deploy_site
from tools import github_push, vercel_deploy
from tools.github_push import DeployError, valid_repo_name

FILES = [{"path": "index.html", "content": "<h1>hi</h1>"}, {"path": "css/a.css", "content": "b{}"}]


def mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_repo_name_validation():
    assert valid_repo_name("wedding-photo-site") and valid_repo_name("a.b_c")
    for bad in ["", ".hidden", "../x", "a b", "a/b", "x" * 61, "repo.git", "-x"]:
        assert not valid_repo_name(bad)


def test_github_creates_private_repo_and_pushes_each_file():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/user/repos":
            return httpx.Response(201, json={"owner": {"login": "me"}, "html_url": "https://github.com/me/r", "default_branch": "main"})
        return httpx.Response(201, json={})

    c = mock(handler)
    owner, url, branch = github_push.create_repo(c, "tok", "r", True)
    github_push.push_files(c, "tok", owner, "r", FILES, branch)
    assert seen[0][2]["private"] is True
    assert [s[1] for s in seen[1:]] == ["/repos/me/r/contents/index.html", "/repos/me/r/contents/css/a.css"]


def test_existing_repo_is_an_error_not_an_overwrite():
    c = mock(lambda req: httpx.Response(422, json={}))
    try:
        github_push.create_repo(c, "tok", "r", True)
        assert False
    except DeployError as e:
        assert "already exists" in str(e)


def test_vercel_polls_until_ready():
    states = iter(["BUILDING", "READY"])

    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"id": "d1", "readyState": "QUEUED", "url": "x.vercel.app", "alias": []})
        return httpx.Response(200, json={"id": "d1", "readyState": next(states), "url": "x.vercel.app", "alias": ["site.vercel.app"]})

    assert vercel_deploy.deploy(mock(handler), "tok", "My Site", FILES, sleep=lambda s: None) == "https://site.vercel.app"


def test_flow_happy_path_and_protection_warning():
    ev = list(deploy_site(FILES, "r", True, lambda n, p: ("me", "https://github.com/me/r", "main"), lambda *a: None,
                          lambda n, f: "https://r.vercel.app", lambda u: 401))
    assert ev[-1]["live_url"] == "https://r.vercel.app" and "Protection" in ev[-1]["warning"]


def test_flow_stops_on_github_error_and_never_deploys():
    deployed = []

    def boom(n, p):
        raise DeployError("exists")

    ev = list(deploy_site(FILES, "r", False, boom, lambda *a: None, lambda n, f: deployed.append(1), lambda u: 200))
    assert ev[-1]["error"] and not deployed


def test_flow_rejects_bad_name_before_any_call():
    called = []
    ev = list(deploy_site(FILES, "../x", True, lambda *a: called.append(1), None, None, None))
    assert ev[-1]["error"] and not called
