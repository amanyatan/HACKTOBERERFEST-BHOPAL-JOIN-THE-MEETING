import importlib.util
import os

MAX_SHOT_HEIGHT = 4000


class BrowserUnavailable(RuntimeError):
    pass


def available() -> bool:
    """Real-browser testing needs Playwright's Chromium, which serverless hosts such as Vercel cannot run."""
    return not os.getenv("VERCEL") and importlib.util.find_spec("playwright") is not None


def run_playwright_check(url: str) -> dict:
    """Open the page, scroll it (so scroll-reveal sections show), collect console errors, take a full-page JPEG."""
    if not available():
        raise BrowserUnavailable("Browser testing is not available on this host")
    from playwright.sync_api import sync_playwright  # imported here so the app still starts without Playwright installed

    errors, failed = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})

            def on_console(m):
                if m.type == "error":
                    errors.append(m.text[:200])

            page.on("console", on_console)
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.on("requestfailed", lambda r: failed.append(r.url[:120]))
            page.goto(url, wait_until="load", timeout=20_000)
            page.evaluate(
                "async()=>{for(let y=0;y<document.documentElement.scrollHeight;y+=400){window.scrollTo(0,y);"
                "await new Promise(r=>setTimeout(r,80));}window.scrollTo(0,0);}"
            )
            page.wait_for_timeout(600)
            height = min(page.evaluate("document.documentElement.scrollHeight"), MAX_SHOT_HEIGHT)
            shot = page.screenshot(type="jpeg", quality=55, full_page=True, clip={"x": 0, "y": 0, "width": 1280, "height": height})
        finally:
            browser.close()
    return {"screenshot": shot, "console_errors": errors[:10], "failed_requests": failed[:10]}
