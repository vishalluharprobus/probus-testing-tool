"""
Guard rails.

The portal talks to real insurers. A quote costs nothing, but a proposal creates
a real record at the insurer, and the payment page is one click from real money.
Nothing here is about correctness - it is about making the dangerous things
impossible rather than merely discouraged.

Two rules:
  1. Prove the target is a test environment before doing anything.
  2. Never let a scenario go further than the target's write ceiling.
"""
from __future__ import annotations

from playwright.sync_api import Page

from config.settings import STAGING_MARKER, Target

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
    """
    if not cfg.require_staging_banner:
        return

    body = page.content()
    if STAGING_MARKER not in body:
        raise SafetyRefusal(
            f"Refusing to run against {cfg.base_url}.\n"
            f"Expected the staging banner ({STAGING_MARKER!r}) and did not find it.\n"
            f"Either this is not a test environment, or the banner was removed - "
            f"check by hand before overriding."
        )


def allow(step_needs: str, cfg: Target) -> None:
    """Called before any step that changes state. Raises rather than proceeding."""
    needed = WRITE_LEVELS.get(step_needs)
    ceiling = WRITE_LEVELS.get(cfg.write_ceiling)

    if needed is None:
        raise ValueError(f"Unknown write class {step_needs!r}")
    if ceiling is None:
        raise ValueError(f"Target {cfg.name} has an unknown write_ceiling")

    if needed > ceiling:
        raise SafetyRefusal(
            f"Step needs '{step_needs}' but target '{cfg.name}' allows only "
            f"'{cfg.write_ceiling}'.\n"
            f"Raise write_ceiling in config/settings.local.json if this is "
            f"genuinely intended - it is deliberately not the default."
        )


def assert_never_pays(page: Page) -> None:
    """
    A last line of defence on the payment screen.

    The payment page object exposes only read methods, so the harness has no way
    to submit a payment even if a scenario asked it to. This checks the other
    direction: that we have not accidentally landed past the payment page on a
    success screen, which would mean money moved without a human.
    """
    if page.get_by_text("Payment Successful", exact=False).count() > 0:
        raise SafetyRefusal(
            "Landed on a payment-success screen. The harness must never complete "
            "a payment - a human does that step. Stop and investigate."
        )
