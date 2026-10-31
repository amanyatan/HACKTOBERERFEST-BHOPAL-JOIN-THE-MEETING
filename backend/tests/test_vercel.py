import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
from fastapi.testclient import TestClient
from starlette.requests import Request

import db
import main
from tools import run_playwright_check as browser

c = TestClient(main.app)


# ---- the deployment files ----------------------------------------------------------------------------------------

def pins(path: Path) -> dict[str, str]:
    return dict(line.strip().split("==") for line in path.read_text().splitlines() if "==" in line)


def test_vercel_json_points_at_the_entrypoint_and_stays_within_the_plan_limit():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert list(cfg["functions"]) == ["app.py"] and (ROOT / "app.py").is_file()
    assert 1 <= cfg["functions"]["app.py"]["maxDuration"] <= 300  # Hobby maximum
    assert any(h["key"] == "Permissions-Policy" and "microphone=(self)" in h["value"] for rule in cfg["headers"] for h in rule["headers"])


def test_root_requirements_are_runtime_only_and_match_the_dev_pins():
    prod, dev = pins(ROOT / "requirements.txt"), pins(BACKEND / "requirements.txt")
    assert {"fastapi", "httpx", "google-genai", "python-dotenv"} <= set(prod)
    assert not {"playwright", "pytest", "uvicorn"} & set(prod)  # no browser on Vercel, no test or dev server
    assert all(dev[name] == version for name, version in prod.items())  # one set of pins, so dev and Vercel cannot drift


def test_python_version_is_one_vercel_supports():
    assert (ROOT / ".python-version").read_text().strip() in {"3.12", "3.13", "3.14"}


def test_vercelignore_keeps_the_virtualenv_and_secrets_out_of_the_upload():
    ignored = (ROOT / ".vercelignore").read_text().split()
    assert {"backend/.venv", "backend/.env", "node_modules", ".tmp", "generated"} <= set(ignored)


def test_public_folder_exists_and_contains_no_secrets():
    assert (ROOT / "public" / "index.html").is_file()
    pattern = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|vcp_[A-Za-z0-9]{20,}|AQ\.[A-Za-z0-9_-]{30,}|AIza[0-9A-Za-z_-]{30,}")
    for f in (ROOT / "public").rglob("*"):
        if f.is_file() and f.suffix in {".html", ".js", ".css", ".svg", ".txt", ".json"}:
            assert not pattern.search(f.read_text(encoding="utf-8", errors="ignore")), f"secret-like text in {f}"


def test_root_app_py_exposes_the_same_fastapi_app():
    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / "app.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert entry.app is main.app


def test_vercel_mode_uses_tmp_ignores_dotenv_and_mounts_no_static_folder(tmp_path):
    code = ("import os, app, main; print(main.ON_VERCEL); print(main.SITES); print(os.getenv('GEMINI_API_KEY'));"
            "print([getattr(r, 'name', None) for r in main.app.routes])")
    env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    env.update(VERCEL="1", TMPDIR=str(tmp_path), TEMP=str(tmp_path), TMP=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    on_vercel, sites, key, routes = out.stdout.strip().splitlines()
    assert on_vercel == "True" and Path(sites).is_relative_to(tmp_path) and Path(sites).is_dir()
    assert key == "None"  # backend/.env is not read on Vercel; the dashboard's variables are the only source
    assert "public" not in routes  # the CDN serves public/, and Vercel's docs say not to mount it


# ---- serverless behaviour ----------------------------------------------------------------------------------------

def req_with(headers: dict, client=("10.0.0.1", 1)) -> Request:
    return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()], "client": client})


def test_client_ip_comes_from_the_proxy_header_on_vercel_only(monkeypatch):
    spoof = req_with({"x-forwarded-for": "6.6.6.6"})
    monkeypatch.setattr(main, "ON_VERCEL", False)
    assert main._ip(spoof) == "10.0.0.1"  # locally a client-supplied header must not choose its own rate-limit bucket
    monkeypatch.setattr(main, "ON_VERCEL", True)
    assert main._ip(req_with({"x-vercel-forwarded-for": "1.2.3.4, 5.6.7.8", "x-forwarded-for": "9.9.9.9"})) == "1.2.3.4"
    assert main._ip(req_with({"x-real-ip": "2.2.2.2"})) == "2.2.2.2"
    assert main._ip(req_with({})) == "10.0.0.1"


def test_browser_testing_is_reported_unavailable_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert browser.available() is False
    with pytest.raises(browser.BrowserUnavailable):
        browser.run_playwright_check("http://localhost/")


def test_health_reports_what_is_configured_without_leaking_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-main")
    monkeypatch.delenv("GEMINI_API_KEY_BOT", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "secret-service")
    r = c.get("/api/health")
    body = r.json()
    assert body["ok"] and body["key_configured"] and body["database"] and body["cutu_configured"] is False
    assert {"deploy_configured", "browser_tests", "vercel"} <= set(body)
    assert "secret-main" not in r.text and "secret-service" not in r.text


def make_site(root: Path, site_id="abc12345", files=None):
    (root / site_id).mkdir(parents=True)
    for name, text in (files or {"index.html": "<h1>x</h1>", "style.css": "b{}"}).items():
        (root / site_id / name).write_text(text)


def test_sites_are_served_with_security_headers_and_cannot_escape_their_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SITES", tmp_path)
    make_site(tmp_path)
    (tmp_path / "secret.txt").write_text("nope")
    page = c.get("/sites/abc12345/")
    assert page.status_code == 200 and "<h1>x</h1>" in page.text
    assert "connect-src 'none'" in page.headers["content-security-policy"] and page.headers["x-content-type-options"] == "nosniff"
    assert c.get("/sites/abc12345/style.css").headers["content-type"].startswith("text/css")
    redirect = c.get("/sites/abc12345", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"] == "/sites/abc12345/"
    assert "nope" not in c.get("/sites/abc12345/%2e%2e/secret.txt").text
    assert "nope" not in c.get("/sites/abc12345/..%2fsecret.txt").text
    assert c.get("/sites/abc12345/.env").status_code == 404
    assert c.get("/sites/NOT-VALID/").status_code == 404
    assert c.get("/sites/zzzzzzzz/").status_code == 404


def test_site_missing_on_this_instance_is_restored_from_the_database_then_refreshed(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SITES", tmp_path)
    main._restored_at.clear()
    state, calls = {"v": 1}, []

    def site_files(site_id):
        calls.append(site_id)
        return [{"path": "index.html", "content": f"<p>version {state['v']}</p>"}]

    monkeypatch.setattr(db, "configured", lambda: True)
    monkeypatch.setattr(db, "site_files", site_files)
    assert "version 1" in c.get("/sites/abc12345/").text
    state["v"] = 2
    assert "version 1" in c.get("/sites/abc12345/").text and len(calls) == 1  # trusted for a few seconds: no round trip per asset
    monkeypatch.setattr(main, "RESTORE_TTL", 0)
    assert "version 2" in c.get("/sites/abc12345/").text and len(calls) == 2  # a later fix round is picked up


def test_a_site_this_instance_wrote_is_never_overwritten_from_the_database(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SITES", tmp_path)
    main._restored_at.clear()
    make_site(tmp_path, "mine1234", {"index.html": "<p>local</p>"})
    monkeypatch.setattr(db, "configured", lambda: True)
    monkeypatch.setattr(db, "site_files", lambda sid: (_ for _ in ()).throw(AssertionError("must not hit the database")))
    assert "local" in c.get("/sites/mine1234/").text


def test_unknown_site_and_database_trouble_are_a_clean_404_or_stale_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SITES", tmp_path)
    main._restored_at.clear()
    monkeypatch.setattr(db, "configured", lambda: True)
    monkeypatch.setattr(db, "site_files", lambda sid: None)
    assert c.get("/sites/nothere1/").status_code == 404

    def down(sid):
        raise db.DbError("down")

    monkeypatch.setattr(db, "site_files", down)
    assert c.get("/sites/nothere1/").status_code == 404
    make_site(tmp_path, "stale123", {"index.html": "<p>old copy</p>"})
    main._restored_at["stale123"] = 0  # looks restored long ago, so it is re-checked and the database is down
    assert "old copy" in c.get("/sites/stale123/").text  # a slightly stale copy beats an error


def test_root_page_is_served_by_a_route_so_it_works_with_or_without_the_cdn():
    r = c.get("/")
    assert r.status_code == 200 and "Join a Meeting" in r.text


def test_sse_responses_are_not_buffered_or_transformed():
    r = main.sse(iter([]), "x", "failed")
    assert "no-transform" in r.headers["cache-control"] and r.headers["x-accel-buffering"] == "no"


# ---- the script that copies settings into Vercel -----------------------------------------------------------------

spec = importlib.util.spec_from_file_location("push_env", ROOT / "scripts" / "push_env_to_vercel.py")
push_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_env)

SECRET_VALUES = {"GEMINI_API_KEY": "gem-SECRET-1", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_KEY": "svc-SECRET-2",
                 "VERCEL_TOKEN": "vcl-SECRET-3", "SUPABASE_ANON_KEY": "anon-SECRET-4", "LIVE_MODEL": "gemini-3.8-live"}


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for name in push_env.NAMES + ["SUPABASE_ANON_KEY", "VERCEL_TOKEN"]:  # the real .env was loaded into os.environ when main was imported
        monkeypatch.delenv(name, raising=False)
    f = tmp_path / ".env"
    f.write_text("\n".join(f"{k}={v}" for k, v in SECRET_VALUES.items()) + "\nGITHUB_TOKEN=\n")
    return f


def test_env_script_dry_run_prints_names_only_and_sends_nothing(env_file, capsys):
    def boom(request):
        raise AssertionError("a dry run must not call the network")

    code = push_env.main(["--project", "demo"], client=httpx.Client(transport=httpx.MockTransport(boom)), env_file=env_file)
    out = capsys.readouterr().out
    assert code == 0 and "Dry run" in out and "GEMINI_API_KEY" in out and "SUPABASE_SERVICE_KEY" in out
    assert not any(v in out for v in SECRET_VALUES.values() if v != "gemini-3.8-live" and "supabase.co" not in v)  # no secret ever printed
    assert "SUPABASE_ANON_KEY" not in out  # unused key is never sent


def test_env_script_apply_posts_each_variable_with_the_right_type_and_never_the_anon_key(env_file, capsys):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((str(request.url), request.headers["authorization"], body))
        return httpx.Response(201, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    code = push_env.main(["--project", "demo", "--team", "team_1", "--target", "production", "--apply", "--yes"], client=client, env_file=env_file)
    assert code == 0
    by_key = {b["key"]: b for _, _, b in sent}
    assert set(by_key) == {"GEMINI_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "DEPLOY_VERCEL_TOKEN", "LIVE_MODEL"}  # empty GITHUB_TOKEN skipped
    assert "VERCEL_TOKEN" not in by_key  # Vercel would refuse that name
    assert by_key["DEPLOY_VERCEL_TOKEN"]["value"] == "vcl-SECRET-3" and by_key["DEPLOY_VERCEL_TOKEN"]["type"] == "sensitive"  # old local name, new remote name
    assert by_key["GEMINI_API_KEY"]["type"] == "sensitive" and by_key["SUPABASE_URL"]["type"] == "plain"
    assert all(b["target"] == ["production"] for b in by_key.values())
    url, auth, _ = sent[0]
    assert "/v10/projects/demo/env" in url and "upsert=true" in url and "teamId=team_1" in url and auth == "Bearer vcl-SECRET-3"
    assert not any(v in capsys.readouterr().out for k, v in SECRET_VALUES.items() if k.endswith(("KEY", "TOKEN")))


def test_env_script_refuses_without_required_settings_and_without_a_token(tmp_path, monkeypatch, capsys):
    for name in push_env.NAMES + ["VERCEL_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("LIVE_MODEL=x\n")
    assert push_env.main(["--project", "demo", "--apply", "--yes"], env_file=empty) == 2
    assert "Missing required" in capsys.readouterr().out
    no_token = tmp_path / "no_token.env"
    no_token.write_text("GEMINI_API_KEY=a\nSUPABASE_URL=https://x\nSUPABASE_SERVICE_KEY=b\n")
    assert push_env.main(["--project", "demo", "--apply", "--yes"], env_file=no_token) == 2
    assert "DEPLOY_VERCEL_TOKEN (or the older VERCEL_TOKEN) is needed" in capsys.readouterr().out


def test_env_script_reports_api_failures_without_the_value(env_file, capsys):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": {"message": "Forbidden"}})))
    assert push_env.main(["--project", "demo", "--apply", "--yes"], client=client, env_file=env_file) == 1
    out = capsys.readouterr().out
    assert "HTTP 403 Forbidden" in out and "gem-SECRET-1" not in out


def test_env_script_prefers_the_new_token_name_over_the_old_one(tmp_path, monkeypatch):
    for name in push_env.NAMES + ["VERCEL_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    f = tmp_path / ".env"
    f.write_text("VERCEL_TOKEN=old-one\nDEPLOY_VERCEL_TOKEN=new-one\n")
    assert push_env.collect(f) == {"DEPLOY_VERCEL_TOKEN": "new-one"}


def test_deploy_token_works_under_the_allowed_name_and_the_old_one_still_works_locally(monkeypatch):
    monkeypatch.delenv("DEPLOY_VERCEL_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    assert main._vercel_token() == ""
    monkeypatch.setenv("VERCEL_TOKEN", "old")
    assert main._vercel_token() == "old"
    monkeypatch.setenv("DEPLOY_VERCEL_TOKEN", "new")
    assert main._vercel_token() == "new"  # the allowed name wins


def test_health_counts_deploy_as_configured_with_only_the_new_name(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "g")
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.setenv("DEPLOY_VERCEL_TOKEN", "v")
    assert c.get("/api/health").json()["deploy_configured"] is True
    monkeypatch.delenv("DEPLOY_VERCEL_TOKEN")
    assert c.get("/api/health").json()["deploy_configured"] is False


def test_deploy_endpoint_names_the_variable_that_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SITES", tmp_path)
    (tmp_path / "abc12345").mkdir()
    (tmp_path / "abc12345" / "index.html").write_text("<h1>x</h1>")
    monkeypatch.setenv("GITHUB_TOKEN", "g")
    monkeypatch.delenv("DEPLOY_VERCEL_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    main._hits.clear()
    r = c.post("/api/deploy", json={"site_id": "abc12345", "repo_name": "r", "visibility": "private", "confirmed": True})
    assert r.status_code == 503 and "DEPLOY_VERCEL_TOKEN" in r.json()["detail"]
