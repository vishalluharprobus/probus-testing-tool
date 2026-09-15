"""
Find out WHY the app is stuck on "Loading".

    python probe_stuck.py

The app's failure mode is silent: it shows a spinner and the word "Loading"
forever, never errors, never times out. That tells a human nothing, so this
watches the network instead and reports the three things that actually explain
it:

    - requests that FAILED        (the call errored outright)
    - requests that never ANSWERED (still in flight when we gave up - this is
                                    almost always the one that matters)
    - console error              (what the app itself complained about)

It logs in and opens screen 1 exactly as a real run does, then waits and
reports. It creates nothing, so it is safe to run at any write ceiling.
"""
from __future__ import annotations

import sys
from collections import OrderedDict

from config import settings
from core import auth, browser, console, safety

WATCH_SECONDS = 35


def main() -> int:
    console.use_utf8()
    cfg = settings.load(settings.DEFAULT_TARGET)
    print(f"\nWatching {cfg.base_url} for {WATCH_SECONDS}s to see what hangs.\n")

    with browser.browser_session(cfg, headed=False) as (context, run_dir):
        # Ordered so the report reads in the order things happened, which is
        # usually the order they depend on each other.
        started: "OrderedDict[str, str]" = OrderedDict()
        finished: set[str] = set()
        failed: list[str] = []
        errors: list[str] = []

        def on_request(request):
            # Only the app's own API traffic. Fonts, images and analytics are
            # noise here and would bury the one call that matters.
            if "/api/" in request.url.lower():
                started[request.url] = request.method

        context.on("request", on_request)
        context.on("requestfinished", lambda r: finished.add(r.url))
        context.on("requestfailed",
                   lambda r: failed.append(f"{r.method} {r.url} - "
                                           f"{r.failure or 'failed'}"))

        page = auth.log_in(context, cfg)
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" else None)

        safety.verify_environment(page, cfg)
        print("  logged in, staging confirmed")

        page.goto(f"{cfg.base_url}/two-wheeler", wait_until="domcontentloaded")
        print(f"  opened the two-wheeler screen, watching ...")
        page.wait_for_timeout(WATCH_SECONDS * 1000)

        body = ""
        try:
            body = " ".join(page.inner_text("body").split())[:200]
        except Exception:
            pass

        print(f"\n{'=' * 62}")
        print("WHAT THE BROWSER SAW")
        print("=" * 62)
        print(f"\n  URL now  : {page.url}")
        print(f"  On screen: {body or '(nothing)'}")

        pending = [url for url in started if url not in finished]
        print(f"\n  API calls started : {len(started)}")
        print(f"  Still unanswered  : {len(pending)}")

        if pending:
            print("\n  NEVER ANSWERED - this is what the app is waiting for:")
            for url in pending:
                print(f"    {started[url]} {_short(url)}")
        if failed:
            print("\n  FAILED OUTRIGHT:")
            for line in failed[:10]:
                print(f"    {_short(line)}")
        if errors:
            print("\n  CONSOLE ERRORS:")
            for line in errors[:8]:
                print(f"    {line[:150]}")
        if not pending and not failed and not errors:
            print("\n  Nothing failed and nothing is pending - the app is not "
                  "stuck on the network.\n  If the screen is still blank the "
                  "problem is in the page itself, not its data.")

        print(f"\n  Trace: {run_dir / 'trace.zip'}\n")
        return 0


def _short(url: str) -> str:
    """Trim the host so the path - the part that identifies the call - shows."""
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            host, _, path = rest.partition("/")
            return f"{host}/{path}"[:150]
    return url[:150]


if __name__ == "__main__":
    sys.exit(main())
