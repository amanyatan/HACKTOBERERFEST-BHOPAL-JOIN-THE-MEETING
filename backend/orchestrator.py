"""The CEO has the conversation; this runs what happens after the Developer is approved:
Developer builds -> Tester opens + screenshots + reviews -> (fix loop, max 2 rounds) -> done.
Every step is yielded as an event dict so the UI tiles can follow along."""
import base64

MAX_FIX_ROUNDS = 2


def _ev(agent, status, message="", **extra):
    return {"agent": agent, "status": status, "message": message, **extra}


def build_and_test(context, dev_build, save, check, review, first_kind="build", first_feedback="", first_previous=None):
    """dev_build(context, feedback, previous, kind)->files, kind being "build", "revise" (the user's changes) or "fix" (the tester's); save(files)->{'id','url'}; check(url)->report; review(context, files, report)->verdict."""
    revising = first_kind == "revise"
    yield _ev("developer", "Applying your changes" if revising else "Writing code",
              "Changing the existing site" if revising else "Building the site from the brief")
    files = dev_build(context, first_feedback, first_previous, first_kind)
    rounds = 0
    while True:
        site = save(files)
        yield _ev("developer", f"Built {len(files)} files", "Site is ready to test", site=site, files=files)
        yield _ev("tester", "Opening the site", "Loading the page in a real browser")
        try:
            report = check(site["url"])
        except Exception:
            yield _ev("tester", "Test skipped", "The automated browser test could not run here")
            verdict = {"ok": None, "issues": [], "summary": "Automated test could not run"}
            break
        yield _ev("tester", "Reviewing the screenshot", "Checking layout, text and console errors",
                  screenshot=base64.b64encode(report["screenshot"]).decode(), console_errors=report["console_errors"])
        try:
            verdict = review(context, files, report)
        except Exception:
            yield _ev("tester", "Review unavailable", "The AI review failed; using console errors only")
            verdict = {"ok": not report["console_errors"], "issues": report["console_errors"], "summary": "AI review unavailable"}
        if verdict["ok"] or rounds >= MAX_FIX_ROUNDS:
            break
        rounds += 1
        yield _ev("tester", f"Found {len(verdict['issues'])} issues", "; ".join(verdict["issues"]), issues=verdict["issues"])
        yield _ev("developer", f"Fixing (round {rounds} of {MAX_FIX_ROUNDS})", "Applying the tester's feedback")
        files = dev_build(context, "\n".join(verdict["issues"]), files, "fix")
    status = "Passed" if verdict["ok"] else ("Not verified" if verdict["ok"] is None else f"{len(verdict['issues'])} issues left")
    yield _ev("tester", status, verdict["summary"], final=True, site=site, files=files,
              issues=verdict["issues"], rounds=rounds, passed=verdict["ok"])
