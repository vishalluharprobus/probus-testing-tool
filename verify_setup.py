"""
Smoke test: prove the plumbing works end to end.

Logs in (including the localhost token carry), checks the staging banner, fills
screen 1 with the team's Honda Activa data, and steps to screen 2. Stops there -
it creates nothing and submits nothing.

    python verify_setup.py                 # watch it run
    python verify_setup.py --headless      # no window
    python verify_setup.py --target testsite
"""
from __future__ import annotations

import argparse
import sys
import traceback

from config import settings
from core import auth, browser, console, safety
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.vehicle_details import Vehicle, VehicleDetailsPage

# The team's standing test vehicle.
HONDA_ACTIVA = Vehicle(
    rto_type="GJ-01",
    rto="GJ-01 Ahmedabad",
    make="HONDA",
    model="ACTIVA",
    variant="3G (110 CC) (PETROL)",
    registration_year="2022",
)


def step(n: str, msg: str) -> None:
    print(f"  [{n}] {msg}", flush=True)


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--slow", type=int, default=0,
                    help="ms between actions, e.g. --slow 300 to watch it")
    args = ap.parse_args()

    cfg = settings.load(args.target)
    print(f"\nTarget      : {cfg.name}  ({cfg.base_url})")
    print(f"Login via   : {cfg.auth_base_url}")
    print(f"Token carry : {'yes' if cfg.needs_token_carry else 'no'}")
    print(f"Write limit : {cfg.write_ceiling}\n")

    with browser.browser_session(cfg, headed=not args.headless,
                                 slow_mo_ms=args.slow) as (context, run_dir):
        page = None
        try:
            step("1/6", "Logging in ...")
            page = auth.log_in(context, cfg)
            step("1/6", "Logged in.")

            step("2/6", "Checking this is a staging environment ...")
            safety.verify_environment(page, cfg)
            step("2/6", "Staging banner found - safe to continue.")

            step("3/6", "Opening Vehicle Details ...")
            vehicle_page = VehicleDetailsPage(page).open(cfg.base_url)
            step("3/6", "Screen 1 is showing.")

            step("4/6", "Filling vehicle details (cascading, ~5 lookups) ...")
            safety.allow("read", cfg)
            vehicle_page.fill(HONDA_ACTIVA)
            step("4/6", "All five fields accepted.")

            step("5/6", "Clicking Proceed ...")
            vehicle_page.proceed()
            page.wait_for_timeout(3000)

            step("6/6", "Checking we reached Policy Details ...")
            policy_page = PolicyDetailsPage(page)
            if policy_page.is_showing():
                step("6/6", "Screen 2 reached. Policy type options are visible.")
            else:
                print("\n  Screen 2 did not appear as expected.")
                print(f"  URL now: {page.url}")
                browser.capture_failure(page, run_dir, "screen2-missing")
                return 2

            print(f"\nPASSED - login, safety check and screen 1 all work.")
            print(f"Video and trace: {run_dir}")
            print(f"Open the trace with:  python -m playwright show-trace "
                  f"\"{run_dir / 'trace.zip'}\"\n")
            return 0

        except safety.SafetyRefusal as exc:
            print(f"\nREFUSED (this is the guard rail working, not a bug):\n  {exc}\n")
            return 3
        except auth.LoginFailed as exc:
            print(f"\nLOGIN FAILED (setup problem, not a test failure):\n  {exc}\n")
            return 4
        except Exception as exc:
            print(f"\nERROR: {type(exc).__name__}: {exc}\n")
            traceback.print_exc()
            if page:
                shot = browser.capture_failure(page, run_dir, "error")
                print(f"\nScreenshot: {shot}")
                print(f"Trace     : {run_dir / 'trace.zip'}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
