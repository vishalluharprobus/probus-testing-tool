"""
Browser setup: video, traces, and the run folder everything lands in.

Two things are on by default because they are the whole debugging story:
  * video   - so you can watch what the run actually did
  * trace   - Playwright's time-travel recording; open it and step through the
              run frame by frame with the DOM at each moment

Both cost almost nothing and turn "it failed" into "here is exactly what happened".
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

REPORTS = Path(__file__).resolve().parent.parent / "reports"


@contextmanager
def browser_session(cfg, headed: bool = True, slow_mo_ms: int = 0):
    """
    Yield (context, run_dir).

    headed defaults to True: the point of this tool is that you can watch it
    work. Nightly runs pass headed=False.
    """
    run_dir = REPORTS / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    (run_dir / "video").mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, slow_mo=slow_mo_ms)
        context = browser.new_context(
            base_url=cfg.base_url,
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(run_dir / "video"),
            record_video_size={"width": 1440, "height": 900},
            ignore_https_errors=True,  # the test site's cert is not always clean
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)

        try:
            yield context, run_dir
        finally:
            context.tracing.stop(path=str(run_dir / "trace.zip"))
            context.close()
            browser.close()


def capture_failure(page, run_dir: Path, label: str) -> Path:
    """Screenshot the moment something went wrong. Returns the path for the report."""
    shot = run_dir / f"FAILED-{label}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:
        pass  # a screenshot failing must never mask the real error
    return shot
