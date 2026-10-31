import html
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
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
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


def rate_limit(ip: str, limit: int = 10, window: int = 60, bucket: str = "") -> None:
    # shortcut: in-memory per process, fine for one local server; use Redis if scaled out.
    now = time.monotonic()
    q = _hits[f"{bucket}:{ip}"]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests")
    q.append(now)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SITE_ID_RE = re.compile(r"^[a-z0-9]{6,20}$")


def _cid(raw: str) -> str:
    """The anonymous per-browser id from X-Client-Id, or '' if it is not a UUID."""
    raw = raw.strip().lower()
    return raw if UUID_RE.match(raw) else ""


def _title(files: list[dict]) -> str:
    for f in files:
        if f["path"] == "index.html":
            m = re.search(r"<title[^>]*>(.*?)</title>", f["content"], re.I | re.S)
            if m and m.group(1).strip():
                return html.unescape(m.group(1).strip())[:120]
    return "Untitled site"


def _with_save(events, persist):
    """Pass events through; on the final success event, try to save it and say whether that worked."""
    for e in events:
        if e.get("final") and not e.get("error"):
            e = {**e, "saved": persist(e)}
        yield e


def _db_call(fn, raw_cid: str):
    cid = _cid(raw_cid)
    if not cid:
        raise HTTPException(400, "Missing client id")
    if not db.configured():
        raise HTTPException(503, "Database is not configured")
    try:
        return fn(cid)
    except db.DbError as e:
        if e.missing_tables:
            raise HTTPException(502, "Database tables are missing. Run supabase/schema.sql in the Supabase SQL editor.")
        raise HTTPException(502, str(e))


def _restore_site(site_id: str, cid: str) -> bool:
    """Re-create generated/<site_id> from the saved copy, e.g. after a server restart."""
    if not cid or not db.configured():
        return False
    try:
        rows = db.select("sites", {"select": "files", "client_id": f"eq.{cid}", "site_id": f"eq.{site_id}", "limit": "1"})
        if not rows:
            return False
        write_files(SITES / site_id, rows[0]["files"])
        return True
    except (db.DbError, InvalidFiles):
        return False


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
    rate_limit(_ip(request), bucket="token")
    key = os.getenv("GEMINI_API_KEY_BOT" if req.agent == "cutu" else "GEMINI_API_KEY")
    if not key:
        raise HTTPException(503, "Cutu is not configured" if req.agent == "cutu" else "GEMINI_API_KEY not set in backend/.env")
    from google import genai

    client = genai.Client(api_key=key, http_options={"api_version": "v1alpha"})
    now = datetime.now(timezone.utc)
    try:
        t = client.auth_tokens.create(
            config={
                "uses": 1,
                "expire_time": now + timedelta(minutes=5 if req.agent == "cutu" else 10),
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
def build(request: Request, req: BuildReq, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=5, bucket="build")
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(503, "GEMINI_API_KEY not set in backend/.env")
    site_id = secrets.token_urlsafe(8).lower().replace("_", "x").replace("-", "y")
    base = str(request.base_url).rstrip("/")

    def save(files):
        write_files(SITES / site_id, files)
        return {"id": site_id, "url": f"/sites/{site_id}/"}

    cid = _cid(x_client_id)

    def persist(e):
        if not cid or not db.configured():
            return False
        try:
            db.insert("sites", {"client_id": cid, "site_id": site_id, "title": _title(e["files"]), "brief": req.context[:4000],
                                "files": e["files"], "passed": e["passed"], "issues": e["issues"], "rounds": e["rounds"]})
            return True
        except db.DbError:
            return False

    events = build_and_test(
        req.context,
        lambda ctx, fb, prev: developer.build(ctx, fb, prev),
        save,
        lambda url: run_playwright_check(base + url),
        lambda ctx, files, report: tester.review(ctx, files, report),
    )
    return sse(_with_save(events, persist), "developer", "Build failed")


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
def deploy(request: Request, req: DeployReq, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=3, bucket="deploy")
    if not req.confirmed:
        raise HTTPException(400, "Deployment must be confirmed by the user")
    if req.visibility not in ("private", "public"):
        raise HTTPException(400, "visibility must be private or public")
    cid = _cid(x_client_id)
    if not SITE_ID_RE.match(req.site_id) or not ((SITES / req.site_id).is_dir() or _restore_site(req.site_id, cid)):
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

    def persist(e):
        if not cid or not db.configured():
            return False
        try:
            db.insert("deployments", {"client_id": cid, "site_id": req.site_id, "repo_name": req.repo_name, "visibility": req.visibility,
                                      "repo_url": e["repo_url"], "live_url": e["live_url"]})
            return True
        except db.DbError:
            return False

    return sse(_with_save(run(), persist), "tester", "Deploy failed")


@app.get("/api/sites")
def list_sites(request: Request, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=30, bucket="read")
    rows = _db_call(lambda cid: db.select("sites", {"select": "site_id,title,passed,issues,rounds,created_at", "client_id": f"eq.{cid}",
                                                    "order": "created_at.desc", "limit": "50"}), x_client_id)
    return {"sites": rows}


@app.get("/api/sites/{site_id}")
def get_site(site_id: str, request: Request, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=30, bucket="read")
    if not SITE_ID_RE.match(site_id):
        raise HTTPException(404, "Unknown site")
    rows = _db_call(lambda cid: db.select("sites", {"select": "site_id,title,files,passed,issues,rounds,created_at", "client_id": f"eq.{cid}",
                                                    "site_id": f"eq.{site_id}", "limit": "1"}), x_client_id)
    if not rows:
        raise HTTPException(404, "Unknown site")
    if not (SITES / site_id).is_dir():
        try:
            write_files(SITES / site_id, rows[0]["files"])
        except InvalidFiles as e:
            raise HTTPException(422, f"Saved files rejected: {e}")
    return rows[0]


@app.get("/api/deployments")
def list_deployments(request: Request, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=30, bucket="read")
    rows = _db_call(lambda cid: db.select("deployments", {"select": "site_id,repo_name,visibility,repo_url,live_url,created_at", "client_id": f"eq.{cid}",
                                                          "order": "created_at.desc", "limit": "50"}), x_client_id)
    return {"deployments": rows}


class SiteFiles(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Content-Security-Policy"] = SITE_CSP
        r.headers["X-Content-Type-Options"] = "nosniff"
        return r


app.mount("/sites", SiteFiles(directory=SITES, html=True), name="sites")
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
