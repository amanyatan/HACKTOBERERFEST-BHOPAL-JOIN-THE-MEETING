import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi.testclient import TestClient

import db
import main

c = TestClient(main.app)
CID = "123e4567-e89b-42d3-a456-426614174000"
H = {"X-Client-Id": CID}
FILES = [{"path": "index.html", "content": "<title>Ember &amp; Roast</title><h1>hi</h1>"}]
REPORT = {"screenshot": b"jpg", "console_errors": [], "failed_requests": []}


def configure(monkeypatch, handler=None):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key-for-tests")
    monkeypatch.setattr(db, "_transport", httpx.MockTransport(handler) if handler else None)
    main._hits.clear()


def events(r):
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def fake_build(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(main.developer, "build", lambda ctx, fb, prev, kind="build": FILES)
    monkeypatch.setattr(main, "run_playwright_check", lambda url: REPORT)
    monkeypatch.setattr(main.tester, "review", lambda ctx, f, r: {"ok": True, "issues": [], "summary": "fine"})


def test_list_is_scoped_to_the_client_id(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=[{"site_id": "a1b2c3d4", "title": "T"}])

    configure(monkeypatch, handler)
    r = c.get("/api/sites", headers=H)
    assert r.status_code == 200 and r.json()["sites"][0]["title"] == "T"
    assert f"client_id=eq.{CID}" in str(seen[0].url)
    assert "service-key-for-tests" not in r.text  # the key never reaches the browser


def test_list_needs_client_id_and_configured_database(monkeypatch):
    configure(monkeypatch)
    assert c.get("/api/sites").status_code == 400
    assert c.get("/api/sites", headers={"X-Client-Id": "not-a-uuid"}).status_code == 400
    monkeypatch.delenv("SUPABASE_URL")
    assert c.get("/api/sites", headers=H).status_code == 503


def test_missing_tables_gives_actionable_message(monkeypatch):
    configure(monkeypatch, lambda req: httpx.Response(404, json={"code": "PGRST205"}))
    r = c.get("/api/sites", headers=H)
    assert r.status_code == 502 and "schema.sql" in r.json()["detail"]


def test_build_saves_site_under_client_id(monkeypatch):
    rows = []
    configure(monkeypatch)
    monkeypatch.setattr(db, "upsert", lambda table, row, conflict: rows.append((table, row, conflict)))
    fake_build(monkeypatch)
    final = events(c.post("/api/build", json={"context": "a site"}, headers=H))[-1]
    assert final["saved"] is True
    early, last = rows[0], rows[-1]
    assert early[0] == "sites" and early[2] == "site_id" and "passed" not in early[1]  # saved as soon as the files exist
    assert last[1]["client_id"] == CID and last[1]["title"] == "Ember & Roast" and last[1]["passed"] is True


def test_build_without_client_id_is_not_saved(monkeypatch):
    rows = []
    configure(monkeypatch)
    monkeypatch.setattr(db, "upsert", lambda table, row, conflict: rows.append(row))
    fake_build(monkeypatch)
    assert events(c.post("/api/build", json={"context": "a"}))[-1]["saved"] is False
    # the files are still stored so the preview works across instances, but under no one's account and without a result
    assert rows and all(r["client_id"] == main.ANON_CLIENT and "passed" not in r for r in rows)


def test_database_failure_never_breaks_a_build(monkeypatch):
    configure(monkeypatch)

    def boom(*a):
        raise db.DbError("down")

    monkeypatch.setattr(db, "upsert", boom)
    fake_build(monkeypatch)
    final = events(c.post("/api/build", json={"context": "a"}, headers=H))[-1]
    assert final["passed"] is True and final["saved"] is False and "error" not in final


def test_page_loads_do_not_use_up_the_build_rate_limit(monkeypatch):
    configure(monkeypatch)
    fake_build(monkeypatch)
    for _ in range(25):
        c.get("/api/sites")  # separate bucket from /api/build
    assert c.post("/api/build", json={"context": "a"}).status_code == 200


def test_get_site_restores_files_to_disk(monkeypatch, tmp_path):
    configure(monkeypatch, lambda req: httpx.Response(200, json=[{"site_id": "a1b2c3d4", "title": "T", "files": FILES}]))
    monkeypatch.setattr(main, "SITES", tmp_path)
    assert c.get("/api/sites/a1b2c3d4", headers=H).status_code == 200
    assert (tmp_path / "a1b2c3d4" / "index.html").exists()
    assert c.get("/api/sites/..%2Fevil", headers=H).status_code == 404


def test_deploy_saves_the_deployment(monkeypatch, tmp_path):
    rows = []
    configure(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("VERCEL_TOKEN", "x")
    monkeypatch.setattr(main, "SITES", tmp_path)
    (tmp_path / "a1b2c3d4").mkdir()
    (tmp_path / "a1b2c3d4" / "index.html").write_text("<h1>x</h1>")
    monkeypatch.setattr(db, "insert", lambda table, row: rows.append((table, row)))
    final_event = {"agent": "tester", "status": "Live", "final": True, "repo_url": "https://github.com/me/r", "live_url": "https://r.vercel.app"}
    monkeypatch.setattr(main, "deploy_site", lambda *a: iter([final_event]))
    body = {"site_id": "a1b2c3d4", "repo_name": "r", "visibility": "private", "confirmed": True}
    final = events(c.post("/api/deploy", json=body, headers=H))[-1]
    assert final["saved"] is True
    assert rows[0][0] == "deployments" and rows[0][1]["client_id"] == CID and rows[0][1]["live_url"] == "https://r.vercel.app"
