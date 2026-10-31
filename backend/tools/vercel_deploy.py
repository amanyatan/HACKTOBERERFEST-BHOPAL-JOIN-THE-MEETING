import re
import time

import httpx

from tools.github_push import DeployError

API = "https://api.vercel.com"


def project_name(repo_name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", repo_name.lower()).strip("-")[:90] or "site"


def deploy(client: httpx.Client, token: str, name: str, files: list[dict], timeout: float = 90, sleep=time.sleep) -> str:
    """Upload the files directly (no git integration) and wait until READY. Returns the https URL."""
    h = {"Authorization": f"Bearer {token}"}
    r = client.post(f"{API}/v13/deployments", headers=h, params={"skipAutoDetectionConfirmation": "1"},
                    json={"name": project_name(name), "target": "production", "projectSettings": {"framework": None},
                          "files": [{"file": f["path"], "data": f["content"]} for f in files]})
    if r.status_code >= 400:
        raise DeployError(f"Vercel refused the deployment (HTTP {r.status_code})")
    d = r.json()
    waited = 0.0
    while d.get("readyState") not in ("READY", "ERROR", "CANCELED"):
        if waited >= timeout:
            raise DeployError("Vercel deployment timed out")
        sleep(2)
        waited += 2
        d = client.get(f"{API}/v13/deployments/{d['id']}", headers=h).json()
    if d["readyState"] != "READY":
        raise DeployError(f"Vercel deployment {d['readyState'].lower()}")
    return "https://" + (d.get("alias") or [d["url"]])[0]
