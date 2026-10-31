import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import developer, tester
from deploy_flow import deploy_site
from orchestrator import build_and_test
from personas import PERSONAS, live_config
from tools import github_push, vercel_deploy
from tools.run_playwright_check import run_playwright_check
from tools.write_files import InvalidFiles, validate, write_files

load_dotenv(Path(__file__).parent / ".env")

# Verified to open a Live audio session with an ephemeral token; override with LIVE_MODEL in .env.
LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
FRONTEND = Path(__file__).parent.parent / "frontend"
SITES = Path(__file__).parent.parent / "generated"
SITES.mkdir(exist_ok=True)

# Generated sites are untrusted: no network calls, no framing by other origins, no forms posting out.
SITE_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com data:; img-src 'self' data: https://picsum.photos https://fastly.picsum.photos; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'"
)

app = FastAPI()
_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(ip: str, limit: int = 10, window: int = 60) -> None:
    # shortcut: in-memory per process, fine for one local server; use Redis if scaled out.
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests")
    q.append(now)


@app.get("/api/health")
def health():
    return {"ok": True, "key_configured": bool(os.getenv("GEMINI_API_KEY"))}


class TokenReq(BaseModel):
    agent: str = "ceo"
    context: str = ""


@app.post("/api/token")
def token(request: Request, req: TokenReq = TokenReq()):
    if req.agent not in PERSONAS:
        raise HTTPException(400, "Unknown agent")
    rate_limit(request.client.host if request.client else "unknown")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(503, "GEMINI_API_KEY not set in backend/.env")
    from google import genai

    client = genai.Client(api_key=key, http_options={"api_version": "v1alpha"})
    now = datetime.now(timezone.utc)
    try:
        t = client.auth_tokens.create(
            config={
                "uses": 1,
                "expire_time": now + timedelta(minutes=10),
                "new_session_expire_time": now + timedelta(minutes=1),
                "live_connect_constraints": {"model": LIVE_MODEL, "config": live_config(req.agent, req.context)},
                "http_options": {"api_version": "v1alpha"},
            }
        )
    except Exception:
        raise HTTPException(502, "Token mint failed")
    return {"token": t.name, "model": LIVE_MODEL}


def sse(events, agent: str, fail_status: str) -> StreamingResponse:
    def stream():
        try:
            for e in events:
                yield f"data: {json.dumps(e)}\n\n"
        except Exception as e:
            msg = f"Generated files rejected: {e}" if isinstance(e, InvalidFiles) else fail_status
            yield f"data: {json.dumps({'agent': agent, 'status': fail_status, 'message': msg, 'final': True, 'error': True})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class BuildReq(BaseModel):
    context: str


@app.post("/api/build")
def build(request: Request, req: BuildReq):
    rate_limit(request.client.host if request.client else "unknown", limit=5)
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(503, "GEMINI_API_KEY not set in backend/.env")
    site_id = secrets.token_urlsafe(8).lower().replace("_", "x").replace("-", "y")
    base = str(request.base_url).rstrip("/")

    def save(files):
        write_files(SITES / site_id, files)
        return {"id": site_id, "url": f"/sites/{site_id}/"}

    events = build_and_test(
        req.context,
        lambda ctx, fb, prev: developer.build(ctx, fb, prev),
        save,
        lambda url: run_playwright_check(base + url),
        lambda ctx, files, report: tester.review(ctx, files, report),
    )
    return sse(events, "developer", "Build failed")


class DeployReq(BaseModel):
    site_id: str
    repo_name: str
    visibility: str
    confirmed: bool = False


def _read_site(site_id: str) -> list[dict]:
    root = SITES / site_id
    files = [{"path": p.relative_to(root).as_posix(), "content": p.read_text(encoding="utf-8")} for p in sorted(root.rglob("*")) if p.is_file()]
    return validate(files)


@app.post("/api/deploy")
def deploy(request: Request, req: DeployReq):
    rate_limit(request.client.host if request.client else "unknown", limit=3)
    if not req.confirmed:
        raise HTTPException(400, "Deployment must be confirmed by the user")
    if req.visibility not in ("private", "public"):
        raise HTTPException(400, "visibility must be private or public")
    if not re.fullmatch(r"[a-z0-9]{6,20}", req.site_id) or not (SITES / req.site_id).is_dir():
        raise HTTPException(404, "Unknown site")
    gh, vc = os.getenv("GITHUB_TOKEN"), os.getenv("VERCEL_TOKEN")
    if not gh or not vc:
        raise HTTPException(503, "GITHUB_TOKEN and VERCEL_TOKEN must be set in backend/.env")
    try:
        files = _read_site(req.site_id)
    except (InvalidFiles, UnicodeDecodeError) as e:
        raise HTTPException(422, f"Site files rejected: {e}")

    def run():
        with httpx.Client(timeout=30) as c:
            yield from deploy_site(
                files, req.repo_name, req.visibility == "private",
                lambda n, p: github_push.create_repo(c, gh, n, p),
                lambda owner, n, f, branch: github_push.push_files(c, gh, owner, n, f, branch),
                lambda n, f: vercel_deploy.deploy(c, vc, n, f),
                lambda url: c.get(url, follow_redirects=True).status_code,
            )

    return sse(run(), "tester", "Deploy failed")


class SiteFiles(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Content-Security-Policy"] = SITE_CSP
        r.headers["X-Content-Type-Options"] = "nosniff"
        return r


app.mount("/sites", SiteFiles(directory=SITES, html=True), name="sites")
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
