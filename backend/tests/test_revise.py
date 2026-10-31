import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi.testclient import TestClient

import db
import main
import personas
from deploy_flow import redeploy_site
from orchestrator import build_and_test
from tools import github_push, vercel_deploy
from tools.github_push import DeployError

c = TestClient(main.app)
CID = "123e4567-e89b-42d3-a456-426614174000"
OTHER = "223e4567-e89b-42d3-a456-426614174999"
H = {"X-Client-Id": CID}
OLD = [{"path": "index.html", "content": "<title>Old</title><h1>old</h1>"}, {"path": "style.css", "content": "h1{color:blue}"}]
NEW = [{"path": "index.html", "content": "<title>Old</title><h1>old</h1>"}, {"path": "style.css", "content": "h1{color:red}"}]
OK = {"ok": True, "issues": [], "summary": "fine"}


def events(r):
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


# ---- the personas ----------------------------------------------------------------------------------------------------

def tool_names(agent):
    return [d["name"] for t in personas.live_config(agent).get("tools", []) for d in t["function_declarations"]]


def test_revision_personas_exist_with_the_tools_the_page_listens_for():
    assert tool_names("ceo_revise") == ["confirm_changes"]
    assert tool_names("designer_revise") == ["confirm_design_changes"]
    assert tool_names("developer_revise") == ["confirm_code_changes"]
    assert tool_names("tester_revise") == ["confirm_redeploy", "confirm_deploy", "skip_deploy"]


def test_revision_personas_know_the_site_exists_and_the_tester_never_overclaims():
    for name in ("ceo_revise", "designer_revise", "developer_revise", "tester_revise"):
        cfg = personas.live_config(name, "SITE CONTEXT")
        text = cfg["system_instruction"]
        assert "already built" in text and "SITE CONTEXT" in text  # the existing site is handed over as context
        assert "voice meeting with one user who wants a website built" not in text  # not the first-build wording
    tester = personas.live_config("tester_revise")["system_instruction"]
    assert "Never say or imply that anything has been tested, pushed or deployed" in tester
    assert "never call confirm_deploy in the same turn" in tester
    assert "hand this to the Developer" in personas.live_config("designer_revise")["system_instruction"]


# ---- orchestrator ----------------------------------------------------------------------------------------------------

def test_first_round_can_be_a_revision_and_later_rounds_are_tester_fixes():
    calls = []

    def dev(ctx, feedback, previous, kind):
        calls.append((feedback, previous, kind))
        return NEW

    bad = {"ok": False, "issues": ["x"], "summary": "bad"}
    verdicts = iter([bad, OK])
    ev = list(build_and_test("brief", dev, lambda f: {"id": "a", "url": "/sites/a/"}, lambda u: {"screenshot": b"", "console_errors": [], "failed_requests": []},
                             lambda c_, f, r: next(verdicts), first_kind="revise", first_feedback="make it red", first_previous=OLD))
    assert calls[0] == ("make it red", OLD, "revise") and calls[1][2] == "fix"
    assert ev[0]["status"] == "Applying your changes" and ev[-1]["passed"] is True


# ---- /api/build in revise mode ---------------------------------------------------------------------------------------

def setup_build(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    monkeypatch.setattr(main, "SITES", tmp_path)
    monkeypatch.setattr(db, "select", lambda table, params: rows(params))
    upserts, calls = [], []
    monkeypatch.setattr(db, "upsert", lambda table, row, conflict: upserts.append(row))
    monkeypatch.setattr(main.developer, "build", lambda ctx, fb, prev, kind="build": (calls.append((ctx, fb, prev, kind)), NEW)[1])
    monkeypatch.setattr(main, "run_playwright_check", lambda url: {"screenshot": b"j", "console_errors": [], "failed_requests": []})
    monkeypatch.setattr(main.tester, "review", lambda ctx, f, r: OK)
    main._hits.clear()
    return upserts, calls


def owner_only(params):
    return [{"files": OLD, "brief": "ORIGINAL BRIEF"}] if params.get("client_id") == f"eq.{CID}" and params.get("site_id") == "eq.abc12345" else []


def test_revise_changes_the_same_site_with_its_saved_files_and_saves_the_new_version(monkeypatch, tmp_path):
    upserts, calls = setup_build(monkeypatch, tmp_path, owner_only)
    body = {"context": "[CEO] change the header", "site_id": "ABC12345", "changes": "  make the heading red  "}
    final = events(c.post("/api/build", json=body, headers=H))[-1]
    assert final["site"]["id"] == "abc12345" and final["passed"] is True and final["saved"] is True  # same site, not a new one
    ctx, feedback, previous, kind = calls[0]
    assert kind == "revise" and feedback == "make the heading red" and previous == OLD
    assert "ORIGINAL BRIEF" in ctx and "[CEO] change the header" in ctx  # the developer sees the old brief and the new request
    assert all(u["site_id"] == "abc12345" for u in upserts) and "ORIGINAL BRIEF" in upserts[-1]["brief"] and upserts[-1]["files"] == NEW
    assert (tmp_path / "abc12345" / "style.css").read_text() == "h1{color:red}"


def test_revise_refuses_someone_elses_site_a_missing_client_id_and_empty_changes(monkeypatch, tmp_path):
    setup_build(monkeypatch, tmp_path, owner_only)
    body = {"context": "x", "site_id": "abc12345", "changes": "make it red"}
    assert c.post("/api/build", json=body, headers={"X-Client-Id": OTHER}).status_code == 404  # not theirs: no overwrite
    assert c.post("/api/build", json=body).status_code == 400
    assert c.post("/api/build", json={**body, "changes": "   "}, headers=H).status_code == 400
    assert c.post("/api/build", json={**body, "site_id": "../etc"}, headers=H).status_code == 404
    assert c.post("/api/build", json={**body, "site_id": "zzzzzzzz"}, headers=H).status_code == 404


def test_a_normal_build_is_unchanged_by_the_revise_option(monkeypatch, tmp_path):
    upserts, calls = setup_build(monkeypatch, tmp_path, owner_only)
    final = events(c.post("/api/build", json={"context": "[CEO] a new site"}, headers=H))[-1]
    assert calls[0][3] == "build" and calls[0][1] == "" and calls[0][2] is None
    assert final["site"]["id"] != "abc12345" and upserts[-1]["brief"] == "[CEO] a new site"


# ---- GitHub: update an existing repository ----------------------------------------------------------------------------

def fake_github(same_tree=False, fail_repo=False):
    log = []

    def handler(request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        log.append((method, path, body))
        if path == "/repos/me/site" and method == "GET":
            return httpx.Response(404, json={}) if fail_repo else httpx.Response(200, json={"default_branch": "main"})
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "HEAD1"}})
        if path.endswith("/git/commits/HEAD1"):
            return httpx.Response(200, json={"tree": {"sha": "TREE1"}})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob-" + str(len([x for x in log if x[1].endswith('/git/blobs')]))})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "TREE1" if same_tree else "TREE2"})
        if path.endswith("/git/commits") and method == "POST":
            return httpx.Response(201, json={"sha": "COMMIT2", "html_url": "https://github.com/me/site/commit/COMMIT2"})
        if path.endswith("/git/refs/heads/main") and method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"unexpected": path})

    return log, httpx.Client(transport=httpx.MockTransport(handler))


def test_update_repo_makes_one_commit_on_top_of_the_current_head_without_forcing():
    log, client = fake_github()
    out = github_push.update_repo(client, "tok", "me", "site", NEW, "Update site: red heading")
    assert out == {"changed": True, "commit_url": "https://github.com/me/site/commit/COMMIT2"}
    posts = [(m, p.split("/repos/me/site")[1]) for m, p, _ in log if m != "GET"]
    assert posts.count(("POST", "/git/blobs")) == 2 and posts[-2:] == [("POST", "/git/commits"), ("PATCH", "/git/refs/heads/main")]
    tree_body = next(b for m, p, b in log if p.endswith("/git/trees"))
    assert tree_body["base_tree"] == "TREE1" and [e["path"] for e in tree_body["tree"]] == ["index.html", "style.css"]  # nothing is deleted
    commit_body = next(b for m, p, b in log if p.endswith("/git/commits") and m == "POST")
    assert commit_body["parents"] == ["HEAD1"] and commit_body["message"] == "Update site: red heading"
    patch_body = next(b for m, p, b in log if m == "PATCH")
    assert patch_body == {"sha": "COMMIT2"}  # no "force": a moved branch makes GitHub refuse instead of overwrite


def test_update_repo_commits_nothing_when_the_files_are_already_there():
    log, client = fake_github(same_tree=True)
    assert github_push.update_repo(client, "tok", "me", "site", OLD, "m") == {"changed": False, "commit_url": None}
    assert not any(m in ("PATCH",) or p.endswith("/git/commits") and m == "POST" for m, p, _ in log)


def test_update_repo_reports_a_missing_repository_without_leaking_the_token():
    _, client = fake_github(fail_repo=True)
    with pytest.raises(DeployError) as e:
        github_push.update_repo(client, "SECRET-TOKEN", "me", "site", NEW, "m")
    assert "HTTP 404" in str(e.value) and "SECRET-TOKEN" not in str(e.value)


@pytest.mark.parametrize("url,expected", [("https://github.com/me/site", ("me", "site")), ("https://github.com/me/site.git", ("me", "site")),
                                          ("https://github.com/me/site/", ("me", "site")), ("https://github.com/Me-1/a.b_c", ("Me-1", "a.b_c"))])
def test_parse_repo_url_accepts_real_github_links(url, expected):
    assert github_push.parse_repo_url(url) == expected


@pytest.mark.parametrize("url", ["", "http://github.com/me/site", "https://evil.com/me/site", "https://github.com/me/site/../x", "https://github.com/me", "javascript:alert(1)"])
def test_parse_repo_url_rejects_everything_else(url):
    with pytest.raises(DeployError):
        github_push.parse_repo_url(url)


# ---- redeploy flow and endpoint --------------------------------------------------------------------------------------

def test_redeploy_flow_pushes_then_redeploys_and_warns_when_vercel_asks_for_a_login():
    ev = list(redeploy_site(NEW, "me", "site", "msg", lambda o, r, f, m: {"changed": True, "commit_url": "https://github.com/me/site/commit/x"},
                            lambda n, f: "https://site.vercel.app", lambda u: 401))
    assert [e["status"] for e in ev] == ["Pushing your changes", "Redeploying to Vercel", "Live"]
    assert ev[-1]["repo_url"] == "https://github.com/me/site" and ev[-1]["live_url"] == "https://site.vercel.app" and "Protection" in ev[-1]["warning"]


def test_redeploy_flow_says_so_when_there_was_nothing_new_and_stops_on_a_github_error():
    ev = list(redeploy_site(OLD, "me", "site", "m", lambda *a: {"changed": False, "commit_url": None}, lambda n, f: "https://s.vercel.app", lambda u: 200))
    assert "Nothing new to push" in [e["status"] for e in ev] and ev[-1]["changed"] is False

    def boom(*a):
        raise DeployError("branch moved")

    deployed = []
    ev = list(redeploy_site(OLD, "me", "site", "m", boom, lambda n, f: deployed.append(1), lambda u: 200))
    assert ev[-1]["error"] and "branch moved" in ev[-1]["message"] and not deployed  # never deploys what it could not push


def setup_redeploy(monkeypatch, tmp_path, deployments):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("DEPLOY_VERCEL_TOKEN", "vc")
    monkeypatch.setattr(main, "SITES", tmp_path)
    (tmp_path / "abc12345").mkdir()
    for f in NEW:
        (tmp_path / "abc12345" / f["path"]).write_text(f["content"])
    monkeypatch.setattr(db, "select", lambda table, params: deployments(params) if table == "deployments" else [])
    inserted, seen = [], {}
    monkeypatch.setattr(db, "insert", lambda table, row: inserted.append((table, row)))

    def fake_flow(files, owner, repo, message, gh_update, vercel, probe):
        seen["update"] = gh_update(owner, repo, files, message)
        seen["live"] = vercel(repo, files)
        yield {"agent": "tester", "status": "Live", "final": True, "repo_url": f"https://github.com/{owner}/{repo}", "live_url": seen["live"]}

    monkeypatch.setattr(main, "redeploy_site", fake_flow)
    monkeypatch.setattr(github_push, "update_repo", lambda cl, tok, o, r, f, m: seen.update(args=(tok, o, r, [x["path"] for x in f], m)) or {"changed": True, "commit_url": None})
    monkeypatch.setattr(vercel_deploy, "deploy", lambda cl, tok, name, f: seen.update(vercel=(tok, name)) or "https://site.vercel.app")
    main._hits.clear()
    return inserted, seen


LAST = lambda params: [{"repo_name": "site", "repo_url": "https://github.com/me/site", "visibility": "private"}] if params["client_id"] == f"eq.{CID}" else []  # noqa: E731
BODY = {"site_id": "abc12345", "summary": "make the\nheading red", "confirmed": True}


def test_redeploy_updates_the_existing_repo_and_vercel_project_and_records_it(monkeypatch, tmp_path):
    inserted, seen = setup_redeploy(monkeypatch, tmp_path, LAST)
    final = events(c.post("/api/redeploy", json=BODY, headers=H))[-1]
    assert final["live_url"] == "https://site.vercel.app" and final["saved"] is True
    assert seen["args"] == ("gh", "me", "site", ["index.html", "style.css"], "Update site: make the heading red")  # one-line message
    assert seen["vercel"] == ("vc", "site")  # same Vercel project name as the first deploy
    table, row = inserted[0]
    assert table == "deployments" and row["client_id"] == CID and row["repo_name"] == "site" and row["visibility"] == "private"


def test_redeploy_needs_confirmation_a_client_id_an_earlier_deployment_and_tokens(monkeypatch, tmp_path):
    setup_redeploy(monkeypatch, tmp_path, LAST)

    def post(body, headers=H):
        main._hits.clear()  # the deploy limit is 3 a minute; this test makes many calls on purpose
        return c.post("/api/redeploy", json=body, headers=headers)

    assert post({**BODY, "confirmed": False}).status_code == 400
    assert post(BODY, {}).status_code == 400
    assert post(BODY, {"X-Client-Id": OTHER}).status_code == 404  # someone else's site: no repo to touch
    assert post({**BODY, "site_id": "../x"}).status_code == 404
    monkeypatch.delenv("DEPLOY_VERCEL_TOKEN")
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    r = post(BODY)
    assert r.status_code == 503 and "DEPLOY_VERCEL_TOKEN" in r.json()["detail"]


def test_redeploy_rejects_a_saved_repo_link_that_is_not_github(monkeypatch, tmp_path):
    setup_redeploy(monkeypatch, tmp_path, lambda params: [{"repo_name": "site", "repo_url": "https://evil.example/me/site", "visibility": "private"}])
    assert c.post("/api/redeploy", json=BODY, headers=H).status_code == 422


# ---- /api/sites/<id> now says where the site was last published ------------------------------------------------------

def test_site_detail_includes_the_brief_and_the_last_deployment(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    monkeypatch.setattr(main, "SITES", tmp_path)
    dep = {"repo_name": "site", "repo_url": "https://github.com/me/site", "live_url": "https://site.vercel.app", "visibility": "public"}
    site = {"site_id": "abc12345", "title": "T", "brief": "B", "files": NEW, "passed": True, "issues": [], "rounds": 0, "created_at": "2026-10-31T00:00:00Z"}
    calls = []
    monkeypatch.setattr(db, "select", lambda table, params: (calls.append(table), [site] if table == "sites" else [dep])[1])
    main._hits.clear()
    body = c.get("/api/sites/abc12345", headers=H).json()
    assert body["brief"] == "B" and body["deployment"] == dep and calls == ["sites", "deployments"]
    monkeypatch.setattr(db, "select", lambda table, params: [site] if table == "sites" else [])
    main._hits.clear()
    assert c.get("/api/sites/abc12345", headers=H).json()["deployment"] is None  # never deployed
