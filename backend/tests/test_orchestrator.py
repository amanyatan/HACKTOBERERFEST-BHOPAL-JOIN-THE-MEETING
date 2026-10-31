import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator import build_and_test

FILES = [{"path": "index.html", "content": "<h1>x</h1>"}]
REPORT = {"screenshot": b"png", "console_errors": [], "failed_requests": []}


def run(verdicts):
    calls = []

    def dev(ctx, feedback, previous):
        calls.append((feedback, previous))
        return FILES

    it = iter(verdicts)
    events = list(build_and_test("brief", dev, lambda f: {"id": "a", "url": "/sites/a/"}, lambda u: REPORT,
                                 lambda c, f, r: next(it)))
    return events, calls


def test_happy_path():
    events, calls = run([{"ok": True, "issues": [], "summary": "fine"}])
    assert len(calls) == 1
    assert events[-1]["final"] and events[-1]["passed"] is True and events[-1]["rounds"] == 0
    assert [e["agent"] for e in events[:4]] == ["developer", "developer", "tester", "tester"]


def test_failing_tester_triggers_fix_then_passes():
    bad = {"ok": False, "issues": ["hero is blank"], "summary": "bad"}
    events, calls = run([bad, {"ok": True, "issues": [], "summary": "fixed"}])
    assert len(calls) == 2 and calls[1][0] == "hero is blank" and calls[1][1] == FILES
    assert events[-1]["passed"] is True and events[-1]["rounds"] == 1


def test_a_host_without_a_browser_still_finishes_the_build_and_says_it_was_not_tested():
    def no_browser(url):
        raise RuntimeError("Browser testing is not available on this host")

    events = list(build_and_test("brief", lambda c, f, p: FILES, lambda f: {"id": "a", "url": "/sites/a/"}, no_browser, lambda c, f, r: 1 / 0))
    assert events[-1]["final"] and events[-1]["passed"] is None and events[-1]["status"] == "Not verified"
    assert any(e["status"] == "Test skipped" for e in events)


def test_gives_up_after_two_fix_rounds_and_says_so():
    bad = {"ok": False, "issues": ["still broken"], "summary": "bad"}
    events, calls = run([bad, bad, bad])
    assert len(calls) == 3  # initial build + 2 fixes
    assert events[-1]["passed"] is False and events[-1]["rounds"] == 2 and events[-1]["issues"] == ["still broken"]
