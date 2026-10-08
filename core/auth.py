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

LOCALHOST FIRST (2026-09-30)
----------------------------
Doing all four steps on every run cost ~15 seconds and a test-site login each
time, which developers noticed. So now the cheapest thing is tried first:

    A. put the saved cookies back and open localhost          ~3 seconds
       -> the journey renders?  done. The test site is never touched.
    B. localhost asks for a login (or hangs on "Loading"):
       open the test site's journey with ITS saved session    ~4 seconds
       -> a fresh .mob-token-w is issued without typing anything
    C. only if the test site also wants a password: the login form

Each run says which of these it needed, so a slow login is never a mystery.
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
# The live site keeps its own saved session, so a live run never overwrites
# the test-site session the other runners rely on (and vice versa).
LIVE_SESSION_FILE = SESSION_FILE.with_name("live.json")

# How long a by-hand login (the live site) may take - password plus OTP.
MANUAL_LOGIN_SECONDS = 300

# Reusing the saved session is ON. It used to be off because a dead token does
# not bring the login dialog back - the app just hangs on "Loading" - and the
# old check trusted any session younger than two minutes. The check below does
# not trust the clock at all: it opens localhost and waits for the journey to
# actually RENDER, and anything else (login dialog, "Welcome Back", a spinner
# that never ends, a 401 from the API) counts as logged out and falls back to
# the test site within seconds.
REUSE_SESSION = True

# How long localhost gets to show the journey before we call the session dead.
# A live session renders in 2-4 seconds; a dead one never does.
PROBE_SECONDS = 12


def log_in(context: BrowserContext, cfg) -> Page:
    """
    Return a page that is logged in and sitting on the target's home journey.

    Cheapest first: the saved session on the target itself, then the test
    site's saved session, then the login form. See LOCALHOST FIRST above.
    """
    page = context.new_page()
    started = time.monotonic()

    restored = REUSE_SESSION and _restore_saved_session(context, cfg)
    if restored and _existing_session_works(page, cfg):
        _save_session(context, cfg)
        print(f"  login: saved session still works on {_host(cfg.base_url)} "
              f"({time.monotonic() - started:.0f}s) - test site not needed")
        return page

    if getattr(cfg, "manual_login", False):
        _log_in_by_hand(page, cfg)
        _save_session(context, cfg)
        print(f"  login: done in {time.monotonic() - started:.0f}s (by hand) - "
              f"saved, so the next journeys log in by themselves")
        return page

    if cfg.needs_token_carry:
        print(f"  login: {_host(cfg.base_url)} "
              f"{'asked for a login' if restored else 'has no saved session'}"
              f" - fetching a fresh token from the test site")
    typed = _log_in_on_test_site(page, cfg)

    if cfg.needs_token_carry:
        _carry_token_to_local(context, page, cfg)

    _save_session(context, cfg)
    print(f"  login: done in {time.monotonic() - started:.0f}s "
          f"({'password typed' if typed else 'test site session reused, no password'})")
    return page


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _session_file(cfg) -> Path:
    return LIVE_SESSION_FILE if getattr(cfg, "live", False) else SESSION_FILE


def _restore_saved_session(context: BrowserContext, cfg) -> bool:
    """
    Put the saved COOKIES back into a fresh browser - cookies only.

    Every run starts a brand-new browser (for a clean video and trace), so
    without this the saved session would never be seen.

    localStorage is deliberately NOT restored. The app sends its API token from
    localStorage 'buy-token', which its auth guard copies from the .mob-token-w
    cookie only when 'buy-token' is EMPTY (Saarthi auth.guard.ts:18-29). An old
    'buy-token' put back from disk would therefore beat every fresh cookie, and
    the app would keep sending a dead token no matter how often we logged in.
    """
    try:
        state = json.loads(_session_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cookies = state.get("cookies") or []
    if not any(c.get("name") == ".mob-token-w" for c in cookies):
        return False
    try:
        context.add_cookies(cookies)
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #

def _log_in_on_test_site(page: Page, cfg) -> bool:
    """
    Step 1 + 2: be signed in on the deployed site, and open the journey.

    Returns True when the password had to be typed. Opening the journey with
    the saved .ASPXAUTH session issues a fresh .mob-token-w by itself, so the
    form is only filled when the site actually shows it.
    """
    page.goto(f"{cfg.auth_base_url}/motor-journey/two-wheeler",
              wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if "/Account/Login" not in page.url and any(
            c["name"] == ".mob-token-w" for c in page.context.cookies(cfg.auth_base_url)):
        return False

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
    return True


def _log_in_by_hand(page: Page, cfg) -> None:
    """
    The live site: open its login page and wait for a person to log in.

    The live login can ask for an OTP, and a live password does not belong in
    a file, so the tool never types one. It waits until the journey opens
    with a .mob-token-w cookie, which is what every API call needs.
    """
    page.goto(f"{cfg.auth_base_url}/motor-journey/two-wheeler",
              wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if _has_journey_token(page, cfg) and "/Account/Login" not in page.url:
        return
    if getattr(page.context, "probus_headless", False):
        raise LoginFailed(
            "The saved live login has expired (or there is none), and this run "
            "has no visible browser to log in with.\n"
            "  Log in from the Test Studio (Live production -> Log in), or run "
            "once without --headless and log in by hand.")
    if "/Account/Login" not in page.url:
        page.goto(f"{cfg.auth_base_url}/Account/Login", wait_until="domcontentloaded")

    print("\n  " + "=" * 60)
    print(f"  PLEASE LOG IN BY HAND in the browser window ({_host(cfg.auth_base_url)})")
    print("  Type your username and password, and the OTP if it asks.")
    print(f"  The tool waits up to {MANUAL_LOGIN_SECONDS // 60} minutes, then "
          f"carries on by itself.")
    print("  (Started with --headless? Press Ctrl+C and run again without it.)")
    print("  " + "=" * 60 + "\n")

    deadline = time.monotonic() + MANUAL_LOGIN_SECONDS
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        # Logged in = no tab is on the login page any more. Never navigate
        # while the person may still be typing.
        try:
            urls = [p.url for p in page.context.pages]
        except Exception:
            continue
        if urls and not any("/Account/Login" in u for u in urls):
            break
    else:
        raise LoginFailed(
            f"Nobody logged in on {_host(cfg.auth_base_url)} within "
            f"{MANUAL_LOGIN_SECONDS // 60} minutes. Run again and log in in the "
            f"browser window when it opens.")

    # Opening the journey is what issues .mob-token-w.
    page.goto(f"{cfg.auth_base_url}/motor-journey/two-wheeler",
              wait_until="domcontentloaded")
    for _ in range(10):
        if _has_journey_token(page, cfg):
            return
        page.wait_for_timeout(1000)
    raise LoginFailed(
        "Logged in, but the live site never issued a .mob-token-w cookie when "
        "the two-wheeler journey opened. Check that this account can open "
        f"{cfg.auth_base_url}/motor-journey/two-wheeler by hand.")


def _has_journey_token(page: Page, cfg) -> bool:
    try:
        return any(c["name"] == ".mob-token-w"
                   for c in page.context.cookies(cfg.auth_base_url))
    except Exception:
        return False


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
    # The first try on localhost may have left the OLD token in 'buy-token',
    # and the app never replaces a 'buy-token' that is already there. Remove
    # it and reload, so the app copies the fresh cookie itself.
    try:
        page.evaluate("() => localStorage.removeItem('buy-token')")
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
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
    Does the saved session really work? Open the app and watch.

    IMPORTANT: we check that the app actually WORKS, not merely that the login
    dialog is absent. A stale .mob-token-w does not bring the dialog back; the
    app simply hangs on "Loading" forever, because its data calls are rejected
    and nothing re-prompts. So the answer is yes only when the journey's first
    field renders, and no the moment the app shows a login screen or its API
    answers 401/403 - without waiting out the whole probe.

    Check on /two-wheeler, NOT /dontknownumber: /dontknownumber sometimes hangs
    on "Loading" when the app's Firebase connection pool is full, while
    /two-wheeler still renders - and deciding a good session was dead is what
    got the account rate-limited by repeated logins.
    """
    rejected: list[int] = []

    def on_response(response) -> None:
        if response.status in (401, 403) and "/api/" in response.url.lower():
            rejected.append(response.status)

    page.on("response", on_response)
    try:
        try:
            page.goto(f"{cfg.base_url}/two-wheeler", wait_until="domcontentloaded")
        except Exception as exc:
            if "ERR_CONNECTION_REFUSED" in str(exc):
                # Nothing is listening. A token from the test site cannot fix
                # that, so do not go and fetch one.
                raise LoginFailed(
                    f"Nothing is running on {cfg.base_url} (connection refused).\n"
                    f"  Start the Angular app ('ng serve' in the Saarthi folder) "
                    f"and the API in Visual Studio, then run again.") from exc
            return False
        field = page.locator('input[placeholder*="MH-02"]')
        deadline = time.monotonic() + PROBE_SECONDS
        while time.monotonic() < deadline:
            if rejected or _login_dialog_showing(page):
                return False
            try:
                if field.first.is_visible():
                    return True
            except Exception:
                pass
            page.wait_for_timeout(400)
        return False
    except LoginFailed:
        raise
    except Exception:
        return False
    finally:
        page.remove_listener("response", on_response)


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


def _save_session(context: BrowserContext, cfg=None) -> None:
    path = _session_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))


class LoginFailed(RuntimeError):
    """Raised when we cannot get authenticated - a setup problem, not a test failure."""


class SessionRejected(RuntimeError):
    """
    The app threw us out in the middle of a journey ("Login Required").

    Seen 2026-10-05: a saved token that the quote API still accepted was
    refused by the buy API (api/v2/Client/MailQuotation: StatusCode 401,
    "Invalid user id"), and the app logged out. The probe in
    _existing_session_works only exercises the quote side, so it cannot see
    this coming. Whoever catches it calls forget_saved_session() and tries
    again: a fresh token from the test site was accepted at once.
    """


def forget_saved_session() -> None:
    """Make the next log_in() in this process fetch a fresh token."""
    global REUSE_SESSION
    REUSE_SESSION = False
