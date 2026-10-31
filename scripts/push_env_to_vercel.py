"""Copy this app's server-side settings from backend/.env into a Vercel project, through the Vercel REST API.

Dry run by default: it prints the NAMES it would send, never the values. Add --apply to actually send them.
Secrets are stored as 'sensitive' variables (write-only: Vercel will not show them again). Model names are plain.

    python scripts/push_env_to_vercel.py --project my-project                       # dry run
    python scripts/push_env_to_vercel.py --project my-project --apply               # asks you to confirm
    python scripts/push_env_to_vercel.py --project my-project --team team_abc --apply --yes

Needs a Vercel token with access to the project: DEPLOY_VERCEL_TOKEN, or the older VERCEL_TOKEN, in backend/.env or the environment.
Vercel reserves names starting with VERCEL_, so the token is stored in the project as DEPLOY_VERCEL_TOKEN either way.
"""
import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.vercel.com"
REQUIRED = ["GEMINI_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
SECRETS = ["GEMINI_API_KEY", "GEMINI_API_KEY_BOT", "GITHUB_TOKEN", "DEPLOY_VERCEL_TOKEN", "SUPABASE_SERVICE_KEY"]
PLAIN = ["SUPABASE_URL", "LIVE_MODEL", "TEXT_MODEL", "FALLBACK_MODEL", "THINKING_LEVEL"]
NAMES = SECRETS + PLAIN  # SUPABASE_ANON_KEY is deliberately not sent: the app never uses it
LEGACY = {"DEPLOY_VERCEL_TOKEN": "VERCEL_TOKEN"}  # the old local name; Vercel itself will not accept a VERCEL_* variable


def collect(env_file: Path) -> dict[str, str]:
    """Values for NAMES from the .env file, with real environment variables taking priority. Empty values are skipped."""
    file_values = dotenv_values(env_file) if env_file.exists() else {}
    merged = {name: os.environ.get(name) or file_values.get(name) or "" for name in NAMES}
    for new, old in LEGACY.items():
        if not merged[new]:
            merged[new] = os.environ.get(old) or file_values.get(old) or ""
    return {k: v.strip() for k, v in merged.items() if v and v.strip()}


def push(client: httpx.Client, token: str, project: str, team: str | None, targets: list[str], values: dict[str, str]) -> list[str]:
    """Upsert each variable. Returns a list of problems (empty = all good). Never includes a value in a message."""
    problems = []
    params = {"upsert": "true", **({"teamId": team} if team else {})}
    for name, value in values.items():
        r = client.post(f"{API}/v10/projects/{project}/env", params=params, headers={"Authorization": f"Bearer {token}"},
                        json={"key": name, "value": value, "type": "sensitive" if name in SECRETS else "plain", "target": targets})
        if r.status_code >= 400:
            try:
                reason = r.json().get("error", {}).get("message", "")
            except ValueError:
                reason = ""
            problems.append(f"{name}: HTTP {r.status_code} {reason}".strip())
    return problems


def main(argv=None, client: httpx.Client | None = None, env_file: Path = ROOT / "backend" / ".env") -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="Vercel project name or id")
    ap.add_argument("--team", help="team id (team_...), if the project belongs to a team")
    ap.add_argument("--target", action="append", choices=["production", "preview"], help="repeat for several (default: production and preview)")
    ap.add_argument("--apply", action="store_true", help="actually send the variables (default is a dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation question")
    args = ap.parse_args(argv)
    targets = args.target or ["production", "preview"]

    values = collect(env_file)
    missing = [n for n in REQUIRED if n not in values]
    print(f"Project: {args.project}   targets: {', '.join(targets)}")
    for name in NAMES:
        kind = "secret" if name in SECRETS else "plain"
        print(f"  {'will send' if name in values else 'not set  '}  {name:22} ({kind}){'   <- REQUIRED' if name in REQUIRED and name not in values else ''}")
    if missing:
        print(f"\nMissing required settings: {', '.join(missing)}. Add them to backend/.env first.")
        return 2
    optional_off = [n for n in ("GEMINI_API_KEY_BOT", "GITHUB_TOKEN", "DEPLOY_VERCEL_TOKEN") if n not in values]
    if optional_off:
        print(f"\nNote: without {', '.join(optional_off)} the matching feature (Cutu / deploying from the app) will be switched off.")
    if not args.apply:
        print("\nDry run only. Nothing was sent. Re-run with --apply to send these.")
        return 0
    token = values.get("DEPLOY_VERCEL_TOKEN")
    if not token:
        print("\nDEPLOY_VERCEL_TOKEN (or the older VERCEL_TOKEN) is needed to talk to the Vercel API. Add it to backend/.env or the environment.")
        return 2
    if not args.yes and input(f"\nSend {len(values)} variables to Vercel project '{args.project}'? Type the project name to confirm: ").strip() != args.project:
        print("Cancelled. Nothing was sent.")
        return 1
    with (client or httpx.Client(timeout=30)) as c:
        problems = push(c, token, args.project, args.team, targets, values)
    if problems:
        print("\nSome variables were not saved:\n  " + "\n  ".join(problems))
        return 1
    print(f"\nDone: {len(values)} variables saved. Redeploy the project so they take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
