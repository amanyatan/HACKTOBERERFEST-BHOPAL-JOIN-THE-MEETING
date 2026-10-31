import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from agents import developer, tester
from deploy_flow import deploy_site, redeploy_site
from orchestrator import build_and_test
from personas import PERSONAS, live_config
from tools import github_push, vercel_deploy
from tools.run_playwright_check import available as browser_available
from tools.run_playwright_check import run_playwright_check
from tools.write_files import InvalidFiles, validate, write_files

ON_VERCEL = bool(os.getenv("VERCEL"))
ROOT = Path(__file__).resolve().parent.parent
if not ON_VERCEL:
    load_dotenv(ROOT / "backend" / ".env")  # on Vercel the project's environment variables are the only source

# Verified to open a Live audio session with an ephemeral token; override with LIVE_MODEL.
LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
PUBLIC = ROOT / "public"  # the website itself; Vercel serves this folder from its CDN


def _vercel_token() -> str:
    """Vercel reserves names starting with VERCEL_, so its dashboard will not let you set VERCEL_TOKEN (the Value box says
    'Populated by System'). The variable to set there is DEPLOY_VERCEL_TOKEN; the old name still works in a local .env."""
    return os.getenv("DEPLOY_VERCEL_TOKEN") or os.getenv("VERCEL_TOKEN") or ""


def _sites_dir() -> Path:
    """Generated sites live here. Serverless filesystems are read-only apart from the temp directory."""
    tmp = Path(tempfile.gettempdir()) / "generated"
    for candidate in ([tmp] if ON_VERCEL else [ROOT / "generated", tmp]):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    raise RuntimeError("No writable folder for generated sites")


SITES = _sites_dir()

# Generated sites are untrusted: no network calls, no forms posting out, nothing but themselves.
SITE_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com data:; img-src 'self' data: https://picsum.photos https://fastly.picsum.photos; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'"
)
SITE_HEADERS = {"Content-Security-Policy": SITE_CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
ANON_CLIENT = "00000000-0000-0000-0000-000000000000"  # owner of sites built without a client id

app = FastAPI()
_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(ip: str, limit: int = 10, window: int = 60, bucket: str = "") -> None:
    # shortcut: in-memory, so per process. On a serverless host each instance counts separately, which makes this a soft
    # limit; use a shared store (Redis, the database) if you need a hard one.
    now = time.monotonic()
    q = _hits[f"{bucket}:{ip}"]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests")
    q.append(now)


def _ip(request: Request) -> str:
    if ON_VERCEL:  # behind Vercel's proxy request.client is the proxy, so every visitor would share one counter
        for name in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
            value = request.headers.get(name)
            if value:
                return value.split(",")[0].strip()
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


RESTORE_TTL = 5.0  # seconds a copy restored from the database is trusted before it is checked again
_restored_at: dict[str, float] = {}
_site_locks: defaultdict = defaultdict(threading.Lock)


def _ensure_site(site_id: str) -> Path | None:
    """Folder holding a site's files. Serverless instances do not share a disk, so a site written by one request may be
    asked for from another instance: restore it from the database, and re-check a restored copy every few seconds so a
    later fix round is picked up. Callers must have validated site_id."""
    root = SITES / site_id
    with _site_locks[site_id]:
        restored = _restored_at.get(site_id)
        if root.is_dir() and (restored is None or time.monotonic() - restored < RESTORE_TTL):
            return root  # written by this instance, or restored moments ago
        if db.configured():
            try:
                files = db.site_files(site_id)
                if files:
                    write_files(root, files)  # validates paths and sizes again
                    _restored_at[site_id] = time.monotonic()
                    return root
            except (db.DbError, InvalidFiles, OSError):
                pass  # a slightly stale copy beats an error
        return root if root.is_dir() else None


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "cutu_configured": bool(os.getenv("GEMINI_API_KEY_BOT")),
        "database": db.configured(),
        "deploy_configured": bool(os.getenv("GITHUB_TOKEN") and _vercel_token()),
        "browser_tests": browser_available(),
        "vercel": ON_VERCEL,
    }


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
        raise HTTPException(503, "Cutu is not configured" if req.agent == "cutu" else "GEMINI_API_KEY is not set")
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

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


class BuildReq(BaseModel):
    context: str
    site_id: str = ""  # set to change a site you already built, instead of building a new one
    changes: str = ""  # what to change (only with site_id)


def _load_for_revision(site_id: str, cid: str) -> tuple[list[dict], str]:
    """Files and brief of a saved site the caller owns, or an HTTP error. Without a database (local use) the folder is the source."""
    if db.configured():
        if not cid:
            raise HTTPException(400, "Missing client id")
        try:
            rows = db.select("sites", {"select": "files,brief", "client_id": f"eq.{cid}", "site_id": f"eq.{site_id}", "limit": "1"})
        except db.DbError:
            raise HTTPException(502, "Could not load the site")
        if not rows:
            raise HTTPException(404, "Unknown site")
        files, brief = rows[0]["files"], rows[0].get("brief") or ""
    else:
        root = _ensure_site(site_id)
        if root is None:
            raise HTTPException(404, "Unknown site")
        files, brief = _read_site(root), ""
    try:
        return validate(files), brief
    except InvalidFiles as e:
        raise HTTPException(422, f"Saved files rejected: {e}")


@app.post("/api/build")
def build(request: Request, req: BuildReq, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=5, bucket="build")
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(503, "GEMINI_API_KEY is not set")
    cid = _cid(x_client_id)
    revise_of = req.site_id.strip().lower()
    if revise_of:
        if not SITE_ID_RE.match(revise_of):
            raise HTTPException(404, "Unknown site")
        changes = req.changes.strip()[:3000]
        if not changes:
            raise HTTPException(400, "Say what to change")
        previous, old_brief = _load_for_revision(revise_of, cid)  # also proves the caller owns the site
        site_id, first = revise_of, ("revise", changes, previous)
        context = f"{old_brief}\n{req.context}".strip()
        brief = context[-4000:]
    else:
        site_id = secrets.token_urlsafe(8).lower().replace("_", "x").replace("-", "y")
        first, context = ("build", "", None), req.context
        brief = req.context[:4000]
    base = str(request.base_url).rstrip("/")

    def save(files):
        write_files(SITES / site_id, files)
        _restored_at.pop(site_id, None)  # this instance wrote it, so it is the freshest copy here
        if db.configured():
            # Saved on every round, not only at the end: the preview is requested while the build is still running and may
            # land on another serverless instance, which can only get the files from the database.
            try:
                db.upsert("sites", {"client_id": cid or ANON_CLIENT, "site_id": site_id, "title": _title(files),
                                    "brief": brief, "files": files}, "site_id")
            except db.DbError:
                pass
        return {"id": site_id, "url": f"/sites/{site_id}/"}

    def persist(e):
        if not cid or not db.configured():
            return False
        try:
            db.upsert("sites", {"client_id": cid, "site_id": site_id, "title": _title(e["files"]), "brief": brief,
                                "files": e["files"], "passed": e["passed"], "issues": e["issues"], "rounds": e["rounds"]}, "site_id")
            return True
        except db.DbError:
            return False

    events = build_and_test(
        context,
        lambda ctx, fb, prev, kind: developer.build(ctx, fb, prev, kind=kind),
        save,
        lambda url: run_playwright_check(base + url),
        lambda ctx, files, report: tester.review(ctx, files, report),
        first_kind=first[0], first_feedback=first[1], first_previous=first[2],
    )
    return sse(_with_save(events, persist), "developer", "Build failed")


class DeployReq(BaseModel):
    site_id: str
    repo_name: str
    visibility: str
    confirmed: bool = False


def _read_site(root: Path) -> list[dict]:
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
    root = _ensure_site(req.site_id) if SITE_ID_RE.match(req.site_id) else None
    if root is None:
        raise HTTPException(404, "Unknown site")
    gh, vc = os.getenv("GITHUB_TOKEN"), _vercel_token()
    if not gh or not vc:
        raise HTTPException(503, "GITHUB_TOKEN and DEPLOY_VERCEL_TOKEN are not set")
    try:
        files = _read_site(root)
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


class RedeployReq(BaseModel):
    site_id: str
    summary: str = ""
    confirmed: bool = False


@app.post("/api/redeploy")
def redeploy(request: Request, req: RedeployReq, x_client_id: str = Header(default="")):
    """Push a changed site to the GitHub repo it was deployed to, then redeploy it on Vercel."""
    rate_limit(_ip(request), limit=3, bucket="deploy")
    if not req.confirmed:
        raise HTTPException(400, "Redeploying must be confirmed by the user")
    cid = _cid(x_client_id)
    if not SITE_ID_RE.match(req.site_id):
        raise HTTPException(404, "Unknown site")
    rows = _db_call(lambda c: db.select("deployments", {"select": "repo_name,repo_url,visibility", "client_id": f"eq.{c}", "site_id": f"eq.{req.site_id}",
                                                        "order": "created_at.desc", "limit": "1"}), x_client_id)
    if not rows:
        raise HTTPException(404, "This site has not been deployed yet, so there is no repository to update")
    last = rows[0]
    try:
        owner, repo = github_push.parse_repo_url(last["repo_url"])
    except github_push.DeployError as e:
        raise HTTPException(422, str(e))
    gh, vc = os.getenv("GITHUB_TOKEN"), _vercel_token()
    if not gh or not vc:
        raise HTTPException(503, "GITHUB_TOKEN and DEPLOY_VERCEL_TOKEN are not set")
    root = _ensure_site(req.site_id)
    if root is None:
        raise HTTPException(404, "Unknown site")
    try:
        files = _read_site(root)
    except (InvalidFiles, UnicodeDecodeError) as e:
        raise HTTPException(422, f"Site files rejected: {e}")
    message = "Update site: " + (" ".join(req.summary.split()) or "changes made in a meeting")[:120]

    def run():
        with httpx.Client(timeout=30) as c:
            yield from redeploy_site(
                files, owner, repo, message,
                lambda o, r, f, m: github_push.update_repo(c, gh, o, r, f, m),
                lambda n, f: vercel_deploy.deploy(c, vc, n, f),
                lambda url: c.get(url, follow_redirects=True).status_code,
            )

    def persist(e):
        try:
            db.insert("deployments", {"client_id": cid, "site_id": req.site_id, "repo_name": last["repo_name"], "visibility": last["visibility"],
                                      "repo_url": e["repo_url"], "live_url": e["live_url"]})
            return True
        except db.DbError:
            return False

    return sse(_with_save(run(), persist), "tester", "Redeploy failed")


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
    def fetch(cid):
        found = db.select("sites", {"select": "site_id,title,brief,files,passed,issues,rounds,created_at", "client_id": f"eq.{cid}",
                                    "site_id": f"eq.{site_id}", "limit": "1"})
        last = db.select("deployments", {"select": "repo_name,repo_url,live_url,visibility", "client_id": f"eq.{cid}", "site_id": f"eq.{site_id}",
                                         "order": "created_at.desc", "limit": "1"}) if found else []
        return found, (last[0] if last else None)

    rows, deployment = _db_call(fetch, x_client_id)
    if not rows:
        raise HTTPException(404, "Unknown site")
    if not (SITES / site_id).is_dir():
        try:
            write_files(SITES / site_id, rows[0]["files"])
        except InvalidFiles as e:
            raise HTTPException(422, f"Saved files rejected: {e}")
    return {**rows[0], "deployment": deployment}  # deployment: where this site was last published, or null


@app.get("/api/deployments")
def list_deployments(request: Request, x_client_id: str = Header(default="")):
    rate_limit(_ip(request), limit=30, bucket="read")
    rows = _db_call(lambda cid: db.select("deployments", {"select": "site_id,repo_name,visibility,repo_url,live_url,created_at", "client_id": f"eq.{cid}",
                                                          "order": "created_at.desc", "limit": "50"}), x_client_id)
    return {"deployments": rows}


# Generated sites are served by plain routes, not app.mount(): on Vercel a mounted static folder is promoted to the CDN at
# build time, which would skip the security headers above and the restore-from-database step.
@app.get("/sites/{site_id}", include_in_schema=False)
def site_slash(site_id: str):
    return RedirectResponse(f"/sites/{site_id}/", status_code=307)  # relative links in the site need the trailing slash


@app.get("/sites/{site_id}/{path:path}", include_in_schema=False)
def site_file(site_id: str, path: str):
    if not SITE_ID_RE.match(site_id) or any(part.startswith(".") for part in path.split("/")):
        raise HTTPException(404, "Not found")
    root = _ensure_site(site_id)
    if root is None:
        raise HTTPException(404, "Unknown site")
    target = (root / (path or "index.html")).resolve()
    if target.is_dir():
        target = target / "index.html"
    if root.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target, headers=SITE_HEADERS)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(PUBLIC / "index.html")


# Locally the app serves the website itself. On Vercel the CDN serves public/, and Vercel's docs say not to mount it.
if not ON_VERCEL:
    app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="public")
