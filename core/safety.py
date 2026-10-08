"""
Guard rails.

The portal talks to real insurers. A quote costs nothing, but a proposal creates
a real record at the insurer, and the payment page is one click from real money.
Nothing here is about correctness - it is about making the dangerous things
impossible rather than merely discouraged.

Two rules:
  1. Prove the target is a test environment before doing anything.
  2. Never let a scenario go further than the target's write ceiling.

The one exception to rule 1 is the "live" target, which exists to QUOTE on
the real site (asked for 2026-10-07). It proves the opposite - that it really
is on the live host - and rule 2 is hard-wired for it: quotes only, whatever
any settings file says.
"""
from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page

from config.settings import (KNOWN_LIVE_HOSTS, KNOWN_TEST_HOSTS, LIVE_API_HOSTS,
                             STAGING_MARKER, Target)

# Ordered least to most dangerous. A step declares what it needs; the target
# declares how far it allows. Higher than the ceiling = refused before the click.
WRITE_LEVELS = {"read": 0, "quote": 1, "proposal": 2, "payment": 3}


class SafetyRefusal(RuntimeError):
    """Raised instead of doing something the target does not permit."""


def verify_environment(page: Page, cfg: Target) -> None:
    """
    Confirm we are on a staging build before any test runs.

    The portal renders a PRE-LIVE banner on non-production builds. It is the only
    positive signal available to us: we cannot inspect which insurer endpoints
    the server is wired to, because those live in database rows and compile-time
    constants the browser never sees. So this banner is the evidence, and a
    missing banner is a refusal rather than a warning.

    A build without the banner (the deployed test site) is not let off: it has
    to pass _verify_test_host instead.
    """
    if getattr(cfg, "live", False):
        _verify_live_quotes_only(page, cfg)
        return
    if not cfg.require_staging_banner:
        _verify_test_host(page, cfg)
        return

    body = page.content()
    if STAGING_MARKER not in body:
        raise SafetyRefusal(
            f"Refusing to run against {cfg.base_url}.\n"
            f"Expected the staging banner ({STAGING_MARKER!r}) and did not find it.\n"
            f"Either this is not a test environment, or the banner was removed - "
            f"check by hand before overriding."
        )


def _verify_test_host(page: Page, cfg: Target) -> None:
    """
    The proof for a build that shows no banner. Two checks, both on what the
    browser actually sees rather than on our own config:

      1. the page is really on a known test host - so a redirect, or a
         base_url edited to the live site, is refused;
      2. nothing the app has fetched so far came from the live back end.
    """
    host = urlparse(page.url).hostname or ""
    if host not in KNOWN_TEST_HOSTS:
        raise SafetyRefusal(
            f"Refusing to run against {cfg.base_url}.\n"
            f"This target shows no staging banner, so it must be on a known test "
            f"host ({', '.join(sorted(KNOWN_TEST_HOSTS))}) - but the page is on "
            f"{host or page.url!r}.")

    live = sorted(_fetched_hosts(page) & LIVE_API_HOSTS)
    if live:
        raise SafetyRefusal(
            f"Refusing to run against {cfg.base_url}.\n"
            f"The address is a test host, but the app on it is calling the LIVE "
            f"back end ({', '.join(live)}). Tell the developers - the test site "
            f"should only call testapi.probusinsurance.com.")


def _verify_live_quotes_only(page: Page, cfg: Target) -> None:
    """The live target: on the live journey host, and quotes only."""
    if cfg.write_ceiling != "quote":
        raise SafetyRefusal(
            f"Refusing: the live target must be quotes only, but its write "
            f"ceiling says {cfg.write_ceiling!r}.")
    host = urlparse(page.url).hostname or ""
    if host not in KNOWN_LIVE_HOSTS:
        raise SafetyRefusal(
            f"Refusing to run the live target on {host or page.url!r}: expected "
            f"{', '.join(sorted(KNOWN_LIVE_HOSTS))}. A redirect or an edited "
            f"address - check by hand.")


def _fetched_hosts(page: Page) -> set[str]:
    """Every host the page has fetched from so far (the browser's own record)."""
    try:
        urls = page.evaluate(
            "() => performance.getEntriesByType('resource').map(e => e.name)")
    except Exception:
        return set()       # cannot tell - the host check above still stands
    return {urlparse(u).hostname or "" for u in urls or []}


def allow(step_needs: str, cfg: Target) -> None:
    """Called before any step that changes state. Raises rather than proceeding."""
    needed = WRITE_LEVELS.get(step_needs)
    ceiling = WRITE_LEVELS.get(cfg.write_ceiling)

    if needed is None:
        raise ValueError(f"Unknown write class {step_needs!r}")
    if ceiling is None:
        raise ValueError(f"Target {cfg.name} has an unknown write_ceiling")

    if getattr(cfg, "live", False) and needed > WRITE_LEVELS["quote"]:
        raise SafetyRefusal(
            f"Step needs '{step_needs}', but the LIVE site is quotes only. "
            f"That is fixed in code (config/settings.py, core/safety.py) - no "
            f"setting raises it.")
    if needed > ceiling:
        key = ("write_ceiling" if getattr(cfg, "is_local", True)
               else f"{cfg.name}_write_ceiling")
        raise SafetyRefusal(
            f"Step needs '{step_needs}' but target '{cfg.name}' allows only "
            f"'{cfg.write_ceiling}'.\n"
            f"Raise {key} in config/settings.local.json if this is "
            f"genuinely intended - it is deliberately not the default."
        )


def permits(step_needs: str, cfg: Target) -> bool:
    """Would allow() let this step run? For choosing where a run stops."""
    try:
        allow(step_needs, cfg)
        return True
    except SafetyRefusal:
        return False


def assert_never_pays(page: Page) -> None:
    """
    A last line of defence on the payment screen.

    The harness can go as far as the payment page but has no method that does
    anything on it, so it has no way to submit a payment even if a scenario
    asked it to. This checks the other
    direction: that we have not accidentally landed past the payment page on a
    success screen, which would mean money moved without a human.
    """
    if page.get_by_text("Payment Successful", exact=False).count() > 0:
        raise SafetyRefusal(
            "Landed on a payment-success screen. The harness must never complete "
            "a payment - a human does that step. Stop and investigate."
        )
