# Deploying to Vercel

The whole app (website + FastAPI backend) deploys as one Vercel project. The website is served from `public/` by Vercel's CDN;
the API and the generated-site routes run as one Python function (`app.py` re-exports the app in `backend/`).

## What is different on Vercel

| | On your computer | On Vercel |
|---|---|---|
| Voice agents, Cutu, building a site, Your Code, Deploy | yes | yes |
| Automated browser test (the Tester's screenshot and fix rounds) | yes | **no**: serverless cannot run Chromium. Sites build fine and are marked **Not tested** |
| Generated sites | files in `generated/` | stored in Supabase, restored on demand, served at `/sites/<id>/` |
| Rate limits | per process | **per function instance**: a soft limit only |
| Supabase | optional | **required**: without it a preview can 404 when a request lands on another instance |

Anyone who has a `/sites/<id>/` link can open that site (the id is random and unguessable, but it is not private).

## One-time setup

1. **Supabase tables.** Dashboard -> SQL Editor -> New query -> paste `supabase/schema.sql` -> Run.
2. **Import the repo** at vercel.com/new (root directory = the repo root). Vercel detects FastAPI from `requirements.txt`.
   No build command, no output directory. Python 3.12 comes from `.python-version`.
3. **Environment variables** (Project Settings -> Environment Variables, tick Production and Preview):

   | Name | Needed for | Notes |
   |---|---|---|
   | `GEMINI_API_KEY` | voice agents, Developer, Tester | **required** |
   | `SUPABASE_URL` | saving sites and deployments | **required** |
   | `SUPABASE_SERVICE_KEY` | same | **required**, mark Sensitive. Server only: it bypasses row-level security |
   | `GEMINI_API_KEY_BOT` | Cutu on the About page | optional: without it Cutu says it is unavailable |
   | `GITHUB_TOKEN`, `VERCEL_TOKEN` | the Deploy feature | optional: classic GitHub token with `repo`, and a Vercel token |
   | `LIVE_MODEL`, `TEXT_MODEL`, `FALLBACK_MODEL`, `THINKING_LEVEL` | model tuning | optional, defaults are fine |

   Or copy them from `backend/.env` with the helper (dry run first; it prints names, never values):

   ```bash
   backend/.venv/Scripts/python scripts/push_env_to_vercel.py --project <your-project>
   backend/.venv/Scripts/python scripts/push_env_to_vercel.py --project <your-project> --apply
   ```

   Add `--team team_xxx` if the project belongs to a team. Redeploy afterwards: variables apply to new deployments only.
4. **Deploy**, then run the checks below.

## After the first deploy

- `https://<your-app>.vercel.app/api/health` should show `"ok": true`, `"key_configured": true`, `"database": true`,
  `"vercel": true` and `"browser_tests": false` (that last one is expected). `"cutu_configured"` and `"deploy_configured"`
  are true only if you set those optional variables.
- Open the site, press **Join a Meeting**, allow the microphone, and talk to the CEO.
- Build a site, then check **Your Code** lists it and **Preview** opens it.
- About page: **Start conversation** with Cutu.

## Local development is unchanged

```bash
backend/.venv/Scripts/python -m uvicorn main:app --app-dir backend --port 8000
backend/.venv/Scripts/python -m pytest backend
```

Locally the app reads `backend/.env`. On Vercel it does not read any file; only the project's environment variables count.

## Limits to know

- A function may run for at most 300 s (`vercel.json`; the Hobby plan maximum). A build without browser tests takes well under that.
- A response body is capped at 4.5 MB (Vercel limit).
- The first request after a quiet period is slower (cold start).
- The microphone needs HTTPS, which Vercel provides.
- Keys you have pasted into chats or screenshots should be rotated: Gemini keys in Google AI Studio, tokens on GitHub and Vercel,
  and the Supabase service key by resetting the project's JWT secret.
