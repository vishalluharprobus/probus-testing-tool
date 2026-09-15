"""
Login, including the cookie hand-off to localhost.

WHY THIS FILE IS WEIRD
----------------------
The local dev build (localhost:4200) cannot log you in by itself right now -
its own "Login Required" dialog does not work. The working process is:

    1. log in on the deployed test site
    2. open the two-wheeler journey there, which issues a .mob-token-w cookie
    3. copy that cookie onto localhost
    4. reload - localhost now treats you as logged in

The team has described this as a temporary developer-side issue. It is all
contained in this one file on purpose: when it is fixed, delete
`_carry_token_to_local()` and nothing else changes.

Against the deployed test site directly, none of this applies - step 1 is the
whole login.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page

# Cookies that carry the session. .mob-token-w is the one that actually
# authenticates; the other two carry the display name and a logging flag, and
# are copied so the header renders as it does for a real user.
SESSION_COOKIES = (".mob-token-w", ".pibllog", ".pibluname")

SESSION_FILE = Path(__file__).resolve().parent.parent / ".session" / "state.json"

# The .mob-token-w token is SHORT-LIVED - the team reports it dying after a few
# minutes. A dead token does not bring back the login dialog and does not raise
# an error; the journey page simply hangs on "Loading" forever, which is
# indistinguishable from the app being slow.
#
# So we do not try to detect expiry by probing - we just refuse to trust a
# session older than this and log in again. A fresh login costs ~8 seconds,
# which is far cheaper than a 20-second timeout followed by a confusing failure.
SESSION_MAX_AGE_SECONDS = 120

# ...and in practice even that is too generous, so reuse is OFF by default.
#
# Measured behaviour: a token can stop working within a minute, and a fresh
# login on the test site appears to invalidate the previous one - so a cached
# token from an earlier run is often already dead. Reusing it saves about five
# seconds and costs an intermittent 20-second timeout that looks like a broken
# selector. That is a bad trade for a suite people are supposed to trust.
#
# Set REUSE_SESSION = True only if the team later confirms the token is
# long-lived, and expect flakiness if you do.
REUSE_SESSION = False


def log_in(context: BrowserContext, cfg) -> Page:
    """
    Return a page that is logged in and sitting on the target's home journey.
    Reuses a saved session when one is still valid.
    """
    page = context.new_page()

    if _existing_session_works(page, cfg):
        return page

    _log_in_on_test_site(page, cfg)

    if cfg.needs_token_carry:
        _carry_token_to_local(context, page, cfg)

    _save_session(context)
    return page


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #

def _log_in_on_test_site(page: Page, cfg) -> None:
    """Step 1 + 2: sign in on the deployed site and open the journey."""
    page.goto(f"{cfg.auth_base_url}/Account/Login", wait_until="domcontentloaded")

    # The login form has no formcontrolname attributes - it is the older
    # server-rendered page, not the Angular app - so we go by input type.
    page.locator('input[type="text"]').first.fill(cfg.username)
    page.locator('input[type="password"]').first.fill(cfg.password)
    page.get_by_role("button", name="Login").first.click()
    page.wait_for_load_state("networkidle")

    # One retry. A login can bounce for reasons that have nothing to do with the
    # password - a slow round trip, or the site briefly objecting to repeated
    # sign-ins from the same account. Retrying once after a pause costs 8
    # seconds and removes the most common false alarm; retrying forever would
    # just hammer the site, so we stop at one.
    if "/Account/Login" in page.url:
        page.wait_for_timeout(6000)
        page.goto(f"{cfg.auth_base_url}/Account/Login", wait_until="domcontentloaded")
        page.locator('input[type="text"]').first.fill(cfg.username)
        page.locator('input[type="password"]').first.fill(cfg.password)
        page.get_by_role("button", name="Login").first.click()
        page.wait_for_load_state("networkidle")

    if "/Account/Login" in page.url:
        raise LoginFailed(
            "Still on the login page after two attempts.\n"
            "  Most likely: the site is briefly refusing repeated sign-ins - "
            "wait a couple of minutes and run again.\n"
            "  Otherwise: check the username and password in "
            "config/settings.local.json, or open the site by hand and see "
            "whether it shows an error."
        )

    # Opening the journey is what causes .mob-token-w to be issued. Logging in
    # alone is not enough - the cookie does not exist until this page loads.
    page.goto(f"{cfg.auth_base_url}/motor-journey/two-wheeler",
              wait_until="domcontentloaded")
    page.wait_for_timeout(1500)


def _carry_token_to_local(context: BrowserContext, page: Page, cfg) -> None:
    """Step 3 + 4: copy the session cookies from the test site onto localhost."""
    issued = {c["name"]: c for c in context.cookies(cfg.auth_base_url)}

    missing = [n for n in SESSION_COOKIES if n not in issued]
    if ".mob-token-w" in missing:
        raise LoginFailed(
            "Logged in, but the test site never issued a .mob-token-w cookie. "
            f"Cookies present: {sorted(issued)}. The journey page may have "
            "changed, or the account may not have motor access."
        )

    host = urlparse(cfg.base_url).hostname or "localhost"
    context.add_cookies([
        {
            "name": name,
            "value": issued[name]["value"],
            "domain": host,
            "path": "/",
            # Deliberately not copying secure/httpOnly/sameSite from the source:
            # localhost is plain http, and a secure cookie would be dropped.
        }
        for name in SESSION_COOKIES if name in issued
    ])

    page.goto(f"{cfg.base_url}/two-wheeler", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    if _login_dialog_showing(page):
        raise LoginFailed(
            "Copied the token to localhost but the Login Required dialog is "
            "still showing. The local build may expect a cookie we are not "
            "carrying - check the browser's Application tab against "
            f"{SESSION_COOKIES}."
        )


def _existing_session_works(page: Page, cfg) -> bool:
    """
    Try the saved session first - a fresh login costs ~8s, this costs ~3s.

    IMPORTANT: we check that the app actually WORKS, not merely that the login
    dialog is absent. A stale .mob-token-w does not bring the dialog back; the
    app simply hangs on "Loading" forever, because its data calls are rejected
    and nothing re-prompts. Checking for the dialog alone reports a dead session
    as healthy, and the failure then surfaces 20 seconds later as a confusing
    "field never appeared" timeout on whatever screen ran first.

    So the real test is: can the first form field render?
    """
    if not REUSE_SESSION:
        return False
    if not SESSION_FILE.exists():
        return False

    age = time.time() - SESSION_FILE.stat().st_mtime
    if age > SESSION_MAX_AGE_SECONDS:
        # Too old to trust. Say so out loud: "logging in again" is reassuring,
        # whereas a silent 20-second hang three steps later is not.
        print(f"  session is {age / 60:.1f} min old - logging in fresh")
        return False

    try:
        # Check on /two-wheeler, NOT /dontknownumber.
        #
        # Learned the hard way: /dontknownumber sometimes hangs on "Loading"
        # when the app's Firebase connection pool is full, while /two-wheeler
        # still renders perfectly. Checking the fragile page made us decide a
        # perfectly good session was dead, throw it away, and log in again -
        # and hammering the login is what got us rate-limited. Check the page
        # that is reliable, and let the journey itself deal with a slow screen.
        page.goto(f"{cfg.base_url}/two-wheeler", wait_until="domcontentloaded")
        if _login_dialog_showing(page):
            return False
        page.locator('input[placeholder*="MH-02"]').wait_for(
            state="visible", timeout=12_000)
        return True
    except Exception:
        return False


# The app has MORE THAN ONE way of telling you it is not authenticated, and they
# look nothing alike:
#   * a modal titled "Login Required"        - shown over the journey page
#   * a full page headed "Welcome Back"      - shown after the token expires
# Checking for only the first one silently mistakes an expired session for a
# healthy one, and the run then dies 20 seconds later on a missing form field
# with no hint that authentication was the problem. Both must be listed here.
LOGGED_OUT_MARKERS = (
    "Login Required",
    "Welcome Back",
    "Please login to your account",
)


def _login_dialog_showing(page: Page) -> bool:
    """True when the app is telling us, in any of its several ways, to log in."""
    try:
        body = page.inner_text("body")
    except Exception:
        return False
    return any(marker in body for marker in LOGGED_OUT_MARKERS)


def _save_session(context: BrowserContext) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(SESSION_FILE))


class LoginFailed(RuntimeError):
    """Raised when we cannot get authenticated - a setup problem, not a test failure."""
