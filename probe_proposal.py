"""
Probe: what happens after clicking "Buy Now" on one insurer's quote?

Maps the proposal flow WITHOUT submitting anything. It clicks Buy Now, then
dumps each screen it finds and stops. Nothing is saved at the insurer by
looking at a form - only by submitting it, which this script never does.

    python probe_proposal.py --insurer ZUNO
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

from config import settings
from core import auth, browser, console, safety, ui
from map_journey import DUMP_JS, dump
from pages.additional_details import AdditionalChoice, AdditionalDetailsPage
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.quote_list import QuoteListPage
from pages.vehicle_details import Vehicle, VehicleDetailsPage

HONDA_ACTIVA = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
                       "3G (110 CC) (PETROL)", "2022")


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--insurer", default="ZUNO")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    cfg = settings.load()
    captured = {}

    with browser.browser_session(cfg, headed=not args.headless) as (context, run_dir):
        page = None
        try:
            page = auth.log_in(context, cfg)
            safety.verify_environment(page, cfg)
            print("  logged in")

            VehicleDetailsPage(page).open(cfg.base_url).fill(HONDA_ACTIVA).proceed()
            page.wait_for_timeout(3000)
            PolicyDetailsPage(page).fill(PolicyChoice()).proceed()
            page.wait_for_timeout(3000)
            AdditionalDetailsPage(page).fill(AdditionalChoice()).proceed()
            print("  reached quote list, waiting for insurers ...")

            quotes_page = QuoteListPage(page).wait_until_loaded()
            quotes = quotes_page.quotes()
            print(f"  {len(quotes)} quotes: "
                  f"{', '.join(f'{q.insurer}={q.premium}' for q in quotes)}")

            target = next((q for q in quotes
                           if args.insurer.upper() in q.insurer.upper()), None)
            if target is None:
                print(f"\n  {args.insurer} did not return a quote this run, so there "
                      f"is no Buy Now button to click.\n  Available: "
                      f"{', '.join(q.insurer for q in quotes)}")
                return 5

            print(f"\n  clicking Buy Now on {target.insurer} (Rs {target.premium}) ...")
            clicked = _click_buy_now(page, target.insurer)
            if not clicked:
                print("  could not find that card's Buy Now button")
                return 1

            page.wait_for_timeout(9000)
            captured["after-buy-now"] = dump(page, f"AFTER BUY NOW ({target.insurer})")

            # Walk a couple more screens if the form offers a way forward, so we
            # can see the shape of the proposal flow. We never submit.
            for n in (2, 3):
                if not _advance(page):
                    break
                page.wait_for_timeout(6000)
                captured[f"proposal-{n}"] = dump(page, f"PROPOSAL SCREEN {n}")

            (run_dir / "proposal-map.json").write_text(
                json.dumps(captured, indent=2), encoding="utf-8")
            print(f"\nSaved: {run_dir / 'proposal-map.json'}")
            print("NOTE: nothing was submitted - forms were only inspected.")
            return 0

        except Exception as exc:
            print(f"\nERROR: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            if page:
                browser.capture_failure(page, run_dir, "proposal-probe")
            return 1


def _click_buy_now(page, insurer: str) -> bool:
    """Click Buy Now on the card belonging to one insurer."""
    cards = page.locator(".plan-card")
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            alts = card.locator("img").evaluate_all(
                "els => els.map(e => e.alt).filter(Boolean)")
        except Exception:
            continue
        if any(insurer.upper() in (a or "").upper() for a in alts):
            card.locator(".buy-now-btn").first.click(timeout=15_000)
            return True
    return False


def _advance(page) -> bool:
    """Click whatever moves the proposal forward, if anything is enabled."""
    # "Confirm" first: clicking Buy Now opens a confirmation dialog rather than
    # navigating straight on, and that dialog is the real gate into the proposal.
    for label in ("Confirm", "Proceed", "Continue", "Next", "Save & Continue"):
        btn = page.get_by_role("button", name=label).first
        try:
            if btn.count() and btn.is_enabled(timeout=2500):
                print(f"    clicking '{label}' ...")
                btn.click(timeout=10_000)
                return True
        except Exception:
            continue
    print("    no enabled forward button - stopping here (form needs data)")
    return False


if __name__ == "__main__":
    sys.exit(main())
