"""
Run a two-wheeler quote journey and report on each insurer.

    python run_quote_test.py
    python run_quote_test.py --addons "Zero Depreciation Cover,Consumables"
    python run_quote_test.py --headless          # for scheduled runs

The report distinguishes three outcomes per insurer, because collapsing them
into pass/fail is what makes a suite untrustworthy:

    PASS       a premium came back
    NO QUOTE   the insurer is not in the results - it was not offered for this
               vehicle/RTO/date, or it failed upstream. NOT a code defect, and
               not a pass either.
    NO PRICE   a card is present but carries no premium - the interesting case,
               and usually a real problem
"""
from __future__ import annotations

import argparse
import sys
import traceback

from config import settings
from core import auth, browser, console, health, safety, ui
from pages.additional_details import AdditionalChoice, AdditionalDetailsPage
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.quote_list import QuoteListPage
from pages.vehicle_details import Vehicle, VehicleDetailsPage

HONDA_ACTIVA = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
                       "3G (110 CC) (PETROL)", "2022")

# The insurers this team cares about first. Names are matched loosely against
# the logo alt text, because the portal writes them in its own style
# (e.g. "NATIONAL", "BAJAJ ALLIANZ").
WATCHED = ["BAJAJ", "ICICI", "ZUNO", "TATA", "DIGIT"]


def rupees(n: int | None) -> str:
    return f"Rs {n:,}" if n else "-"


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--addons", default="", help="comma-separated add-on names")
    ap.add_argument("--watch", default=",".join(WATCHED))
    ap.add_argument("--slow", type=int, default=0,
                    help="milliseconds to pause between actions so a human can "
                         "follow along, e.g. --slow 500. Ignored when headless.")
    args = ap.parse_args()

    cfg = settings.load(args.target)
    watched = [w.strip().upper() for w in args.watch.split(",") if w.strip()]
    addons = [a.strip() for a in args.addons.split(",") if a.strip()]

    print(f"\nTarget : {cfg.name} ({cfg.base_url})")
    print(f"Vehicle: {HONDA_ACTIVA.make} {HONDA_ACTIVA.model} "
          f"{HONDA_ACTIVA.variant}, {HONDA_ACTIVA.rto}, {HONDA_ACTIVA.registration_year}")
    print(f"Add-ons: {', '.join(addons) if addons else 'none'}\n")

    with browser.browser_session(cfg, headed=not args.headless,
                                 slow_mo_ms=args.slow) as (context, run_dir):
        page = None
        # Watch from before the first navigation - a hung login page is exactly
        # the case we most need explained.
        watcher = health.Watcher.for_context(context)
        try:
            page = auth.log_in(context, cfg)
            safety.verify_environment(page, cfg)
            safety.allow("quote", cfg)
            print("  logged in, staging confirmed")

            VehicleDetailsPage(page).open(cfg.base_url).fill(HONDA_ACTIVA).proceed()
            print("  screen 1  vehicle details      done")

            PolicyDetailsPage(page).wait_until_loaded().fill(
                PolicyChoice()).proceed()
            print("  screen 2  policy details       done")

            extra = AdditionalDetailsPage(page).wait_until_loaded()
            filled = extra.prefilled()
            print(f"  screen 3  app pre-filled       "
                  f"expiry={filled.get('policy_expiry')} ncb={filled.get('ncb')}")
            extra.fill(AdditionalChoice()).proceed()
            print("  screen 3  additional details   done")

            print("  waiting for insurer quotes ...")
            quote_page = QuoteListPage(page).wait_until_loaded()

            if addons:
                quote_page.select_add_ons(addons)
                print(f"  add-ons applied, re-priced")

            quotes = quote_page.quotes()
            failures = quote_page.unavailable()
            return report(quotes, failures, watched, addons, run_dir)

        except safety.SafetyRefusal as exc:
            print(f"\nREFUSED (guard rail working as designed):\n  {exc}\n")
            return 3
        except ui.LookupTimedOut as exc:
            print("\n" + "=" * 62)
            print("MASTER-DATA LOOKUP FAILED - usually a busy environment")
            print("=" * 62)
            print(f"\n  {exc}\n")
            return 6
        except ui.PageStuckLoading as exc:
            print("\n" + "=" * 62)
            print("APP DID NOT LOAD - this is not a test failure")
            print("=" * 62)
            print(f"\n  {exc}\n")
            print(f"  Trace: {run_dir / 'trace.zip'}\n")
            return 6
        except auth.LoginFailed as exc:
            print(f"\nLOGIN FAILED (setup problem):\n  {exc}\n")
            return 4
        except Exception as exc:
            # Before blaming the app, ask what the browser actually reported.
            # A hung page is usually infrastructure, and saying so saves someone
            # a morning of hunting a regression that was never there.
            # A bug in THIS tool must never be dressed up as an environment
            # problem. These exception types can only come from our own code,
            # and hiding one behind "Firebase is busy" would send someone
            # chasing an infrastructure ghost while the real fix is one line here.
            HARNESS_BUGS = (NameError, AttributeError, TypeError,
                            ImportError, KeyError, IndexError)

            diag = watcher.diagnose(page) if watcher else None
            if diag and diag.is_environment_problem and not isinstance(exc, HARNESS_BUGS):
                print("\n" + "=" * 62)
                print("ENVIRONMENT PROBLEM - this is not a test failure")
                print("=" * 62)
                print(f"\n  {diag.explanation}\n")
                if diag.console_errors:
                    print("  What the browser reported:")
                    for line in diag.console_errors[:3]:
                        print(f"    - {line[:160]}")
                print(f"\n  The step that timed out: {type(exc).__name__}")
                print(f"  Trace: {run_dir / 'trace.zip'}\n")
                return 6

            print(f"\nERROR: {type(exc).__name__}: {exc}\n")
            traceback.print_exc()
            if page:
                print(f"\nScreenshot: {browser.capture_failure(page, run_dir, 'error')}")
                print(f"Trace     : {run_dir / 'trace.zip'}")
            return 1


def report(quotes, failures, watched, addons, run_dir) -> int:
    print(f"\n{'=' * 62}")
    print(f"QUOTE RESULTS      {len(quotes)} card(s) returned")
    print("=" * 62)

    if quotes:
        print(f"\n  {'INSURER':<18} {'PREMIUM':>10}  {'IDV':>10}   PLAN")
        print(f"  {'-' * 18} {'-' * 10}  {'-' * 10}   {'-' * 22}")
        for q in sorted(quotes, key=lambda x: x.premium or 0):
            flag = "" if q.ok else "   <-- no price"
            print(f"  {q.insurer:<18} {rupees(q.premium):>10}  {rupees(q.idv):>10}   "
                  f"{q.plan_name[:22]}{flag}")

    # Per-insurer verdicts for the ones this team is watching.
    print(f"\n  WATCHED INSURERS")
    print(f"  {'-' * 44}")
    found = {q.insurer.upper(): q for q in quotes}
    passed = missing = no_price = 0

    by_failure = {f.insurer.upper(): f for f in failures}

    for name in watched:
        match = next((q for code, q in found.items() if name in code), None)
        if match is None:
            # The portal usually says WHY. Print that instead of a shrug.
            why = next((f for code, f in by_failure.items() if name in code), None)
            if why:
                print(f"  {name:<14} NO QUOTE    {why.reason[:44]}")
            else:
                print(f"  {name:<14} NO QUOTE    no reason given by the portal")
            missing += 1
        elif match.ok:
            print(f"  {name:<14} PASS        {rupees(match.premium)}")
            passed += 1
        else:
            print(f"  {name:<14} NO PRICE    card shown but premium missing")
            no_price += 1

    print(f"\n  {passed} passed | {missing} no quote | {no_price} no price")

    # Split the failures into ours and theirs. This is the difference between a
    # ticket for this team and a note about somebody else's service - and it is
    # the whole reason for reading the reason text rather than counting absences.
    ours = [f for f in failures if f.is_our_problem]
    if ours:
        print(f"\n  DEFECTS IN OUR INTEGRATION ({len(ours)}) - worth raising:")
        for f in ours:
            print(f"    {f.insurer:<14} {f.reason[:56]}")
    others = [f for f in failures if not f.is_our_problem]
    if others:
        print(f"\n  Not ours ({len(others)}):")
        for f in others:
            print(f"    {f.insurer:<14} [{f.kind}] {f.reason[:44]}")
    if addons:
        print(f"  add-ons applied: {', '.join(addons)}")
    print(f"\n  Video and trace: {run_dir}")
    print(f"  View the run:    python -m playwright show-trace \"{run_dir / 'trace.zip'}\"\n")

    # Exit code 5 = nothing failed in code, but no watched insurer returned.
    # A pipeline should treat that differently from a real regression.
    if no_price:
        return 1
    if passed == 0:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
