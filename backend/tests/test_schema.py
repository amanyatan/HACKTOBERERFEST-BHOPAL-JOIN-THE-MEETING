import re
from pathlib import Path

SQL = (Path(__file__).parent.parent.parent / "supabase" / "schema.sql").read_text()


def columns(table: str) -> set[str]:
    body = re.search(rf"create table if not exists public\.{table} \((.*?)\n\);", SQL, re.S).group(1)
    return {line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith("--")}


def test_sites_table_has_every_column_the_code_reads_or_writes():
    # main.py writes these (save/persist), lists them (/api/sites) and reads files back (/sites/<id>/, /api/sites/<id>)
    written = {"client_id", "site_id", "title", "brief", "files", "passed", "issues", "rounds"}
    listed = {"site_id", "title", "passed", "issues", "rounds", "created_at"}
    assert (written | listed | {"files"}) <= columns("sites")


def test_site_id_is_unique_because_every_build_round_upserts_on_it():
    assert re.search(r"site_id\s+text not null unique", SQL)


def test_deployments_table_has_every_column_the_code_writes_and_lists():
    assert {"client_id", "site_id", "repo_name", "visibility", "repo_url", "live_url", "created_at"} <= columns("deployments")
    assert "check (visibility in ('private', 'public'))" in SQL  # the same two values /api/deploy accepts


def test_both_tables_are_locked_to_the_service_key_and_the_script_is_safe_to_rerun():
    assert "alter table public.sites enable row level security" in SQL
    assert "alter table public.deployments enable row level security" in SQL
    assert "create policy" not in SQL.lower()  # no policy means the public anon key can read and write nothing
    assert "drop table" not in SQL.lower() and "truncate" not in SQL.lower()  # re-running must never delete saved work
    assert SQL.count("create table if not exists") == 2 and "notify pgrst, 'reload schema'" in SQL
