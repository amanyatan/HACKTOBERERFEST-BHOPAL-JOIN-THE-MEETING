import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi.testclient import TestClient

import main

c = TestClient(main.app)


def test_health():
    assert c.get("/api/health").json()["ok"] is True


def test_token_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    main._hits.clear()
    assert c.post("/api/token").status_code == 503


def test_token_rate_limited(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    main._hits.clear()
    codes = [c.post("/api/token").status_code for _ in range(12)]
    assert codes[-1] == 429


def test_token_rejects_unknown_agent():
    main._hits.clear()
    assert c.post("/api/token", json={"agent": "hacker"}).status_code == 400
