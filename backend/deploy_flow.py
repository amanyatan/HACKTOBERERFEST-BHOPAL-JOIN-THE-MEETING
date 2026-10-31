"""Tester's deploy step: GitHub repo + files, then Vercel. Yields UI events; only called after the user confirmed on screen."""
from tools.github_push import DeployError, valid_repo_name


def _ev(status, message="", **extra):
    return {"agent": "tester", "status": status, "message": message, **extra}


def deploy_site(files, repo_name, private, gh_create, gh_push, vercel, probe):
    """gh_create(name, private)->(owner,url,branch); gh_push(owner,name,files,branch); vercel(name,files)->url; probe(url)->http status."""
    if not valid_repo_name(repo_name):
        yield _ev("Deploy failed", "That repository name is not valid", final=True, error=True)
        return
    try:
        yield _ev("Creating the repo", f"{'Private' if private else 'PUBLIC'} repository {repo_name}")
        owner, repo_url, branch = gh_create(repo_name, private)
        yield _ev(f"Pushing {len(files)} files", repo_url, repo_url=repo_url)
        gh_push(owner, repo_name, files, branch)
        yield _ev("Deploying to Vercel", "Uploading the files and waiting for the build")
        live_url = vercel(repo_name, files)
    except DeployError as e:
        yield _ev("Deploy failed", str(e), final=True, error=True)
        return
    warning = ""
    try:
        if probe(live_url) in (401, 403):
            warning = "Vercel Deployment Protection is on, so visitors may be asked to log in. Turn it off in the Vercel project settings."
    except Exception:
        warning = "Could not check whether the live URL is publicly reachable."
    yield _ev("Live", live_url, final=True, repo_url=repo_url, live_url=live_url, warning=warning)
