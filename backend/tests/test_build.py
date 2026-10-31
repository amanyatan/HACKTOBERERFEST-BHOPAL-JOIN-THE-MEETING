import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi.testclient import TestClient

import main

c = TestClient(main.app)
REPORT = {"screenshot": b"jpg", "console_errors": [], "failed_requests": []}
OK = {"ok": True, "issues": [], "summary": "fine"}


def _events(r):
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def _ready(monkeypatch, files):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(main.developer, "build", lambda ctx, fb, prev: files)
    monkeypatch.setattr(main, "run_playwright_check", lambda url: REPORT)
    monkeypatch.setattr(main.tester, "review", lambda ctx, f, r: OK)
    main._hits.clear()


def test_build_streams_events_and_serves_site_with_csp(monkeypatch):
    _ready(monkeypatch, [{"path": "index.html", "content": "<h1>hi</h1>"}])
    ev = _events(c.post("/api/build", json={"context": "a site"}))
    final = ev[-1]
    assert final["final"] and final["passed"] is True
    page = c.get(final["site"]["url"])
    assert "<h1>hi</h1>" in page.text
    assert "connect-src 'none'" in page.headers["content-security-policy"]


def test_build_rejects_malicious_paths(monkeypatch):
    _ready(monkeypatch, [{"path": "index.html", "content": "x"}, {"path": "../evil.js", "content": "x"}])
    final = _events(c.post("/api/build", json={"context": "a"}))[-1]
    assert final["error"] and "rejected" in final["message"]


def test_deploy_requires_confirmation_and_valid_site(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("VERCEL_TOKEN", "x")
    main._hits.clear()
    body = {"site_id": "abcdefgh", "repo_name": "r", "visibility": "private"}
    assert c.post("/api/deploy", json=body).status_code == 400  # not confirmed
    assert c.post("/api/deploy", json={**body, "confirmed": True}).status_code == 404  # no such site
    assert c.post("/api/deploy", json={**body, "confirmed": True, "site_id": "../x"}).status_code == 404
    main._hits.clear()
    assert c.post("/api/deploy", json={**body, "confirmed": True, "visibility": "secret"}).status_code == 400
    main._hits.clear()
    assert [c.post("/api/deploy", json=body).status_code for _ in range(4)][-1] == 429
