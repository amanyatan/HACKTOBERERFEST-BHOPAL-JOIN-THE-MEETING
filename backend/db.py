"""Supabase storage through its REST API (PostgREST) with httpx.

Only the backend holds the service key, and it bypasses row-level security, so every query here must be
scoped by client_id in the caller. The browser never talks to Supabase.
"""
import os

import httpx

_transport = None  # tests inject an httpx.MockTransport


class DbError(Exception):
    def __init__(self, message: str, missing_tables: bool = False):
        super().__init__(message)
        self.missing_tables = missing_tables


def configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))


def _request(method: str, table: str, **kw) -> httpx.Response:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", **kw.pop("headers", {})}
    try:
        with httpx.Client(timeout=15, transport=_transport) as c:
            r = c.request(method, f"{base}/rest/v1/{table}", headers=headers, **kw)
    except httpx.HTTPError:
        raise DbError("Could not reach the database")
    if r.status_code >= 400:
        try:
            code = r.json().get("code", "")
        except ValueError:
            code = ""
        raise DbError(f"Database error (HTTP {r.status_code})", missing_tables=code in ("PGRST205", "42P01"))
    return r


def insert(table: str, row: dict) -> None:
    _request("POST", table, json=row, headers={"Prefer": "return=minimal"})


def upsert(table: str, row: dict, conflict: str) -> None:
    """Insert, or update the existing row with the same `conflict` column. Columns not in `row` are left alone."""
    _request("POST", table, json=row, params={"on_conflict": conflict},
             headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


def site_files(site_id: str) -> list[dict] | None:
    """The saved files of a site by its (unguessable) id, or None. Callers must validate site_id first."""
    rows = select("sites", {"select": "files", "site_id": f"eq.{site_id}", "limit": "1"})
    return rows[0]["files"] if rows else None


def select(table: str, params: dict) -> list[dict]:
    return _request("GET", table, params=params).json()
