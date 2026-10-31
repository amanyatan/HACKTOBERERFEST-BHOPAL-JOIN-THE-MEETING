"""Vercel entrypoint. Vercel looks for `app` in a root-level app.py; the real application lives in backend/.

Locally, run the backend directly instead (see DEPLOYMENT.md):  uvicorn main:app --app-dir backend
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from main import app  # noqa: E402,F401  (re-exported for Vercel)
