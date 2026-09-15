"""
Learn real RTOs and vehicles by reading the portal's own dropdowns.

    python discover_masters.py                     # a sensible default sweep
    python discover_masters.py --states MH,GJ,DL   # only these states
    python discover_masters.py --deep              # also harvest models/variants

WHY
---
The first version of the scenario hunter varied only the year and the policy
type. RTO and vehicle stayed fixed at GJ-01 / Honda Activa in all eight
attempts - which is a poor way to hunt for an insurer, because WHERE the
vehicle is registered is one of the strongest reasons an insurer declines to
quote. A Maharashtra-focused insurer will never appear for a Gujarat RTO no
matter how many years you try.

The reason it was fixed is that RTO and vehicle values cannot be invented: they
have to match rows the plan master actually holds. But they do not need to be
invented - the portal's autocompletes ARE the plan master. Type a prefix, read
the list, and every value you get back is real by construction.

This is READ-ONLY and cheap: it stays on screen one and never requests a quote,
so it does not hit a single insurer API. Running it is far gentler on the
environment than one journey.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from config import settings
from core import auth, browser, console, safety, ui
from pages.vehicle_details import VehicleDetailsPage

CACHE = Path(__file__).resolve().parent / "data" / "master_cache.json"

# Indian state RTO prefixes worth sweeping. Ordered with the big insurance
# markets first, so a short run still covers the states most likely to unlock
# an insurer.
DEFAULT_STATES = ["MH", "GJ", "DL", "KA", "TN", "UP", "RJ", "WB", "TS", "HR"]

# A few common two-wheeler makes to probe for. We only ever KEEP what the app
# actually returns, so a make that does not exist simply yields nothing.
MAKE_PROBES = ["HON", "HER", "BAJ", "TVS", "YAM", "SUZ", "ROY"]


def sweep_rtos(page, states: list[str]) -> dict[str, list[str]]:
    """For each state prefix, collect the RTO codes the portal offers."""
    found: dict[str, list[str]] = {}
    for state in states:
        options: list[str] = []
        # Most states number their RTOs 01, 02, 03... so a couple of probes per
        # state finds real ones without enumerating all of them.
        for suffix in ("-01", "-02", "-03"):
            options += ui.options_for(page, VehicleDetailsPage.RTO, f"{state}{suffix}")
        unique = sorted(dict.fromkeys(options))
        if unique:
            found[state] = unique
            print(f"  {state}: {len(unique)} RTO(s)  e.g. {unique[0]}")
        else:
            print(f"  {state}: none")
    return found


def sweep_vehicles(page, rto: str, deep: bool) -> dict:
    """With an RTO selected, harvest the makes (and optionally models/variants)."""
    ui.autocomplete(page, VehicleDetailsPage.RTO, rto.split()[0], rto)
    page.wait_for_timeout(1200)

    makes: list[str] = []
    for probe in MAKE_PROBES:
        makes += ui.options_for(page, VehicleDetailsPage.MAKE, probe)
    makes = sorted(dict.fromkeys(makes))
    print(f"  makes under {rto}: {len(makes)}  {', '.join(makes[:8])}")

    result: dict = {"rto": rto, "makes": makes, "vehicles": []}
    if not deep or not makes:
        return result

    # Deep mode: walk one make down to real variants, so we end up with at least
    # one fully-valid vehicle for this RTO that a scenario can actually use.
    for make in makes[:3]:
        ui.autocomplete(page, VehicleDetailsPage.MAKE, make[:4], make)
        page.wait_for_timeout(1000)
        models = sorted(dict.fromkeys(ui.options_for(page, VehicleDetailsPage.MODEL, "")))
        if not models:
            continue
        model = models[0]
        ui.autocomplete(page, VehicleDetailsPage.MODEL, model[:4], model)
        page.wait_for_timeout(1000)
        variants = sorted(dict.fromkeys(
            ui.options_for(page, VehicleDetailsPage.VARIANT, "")))
        if variants:
            result["vehicles"].append(
                {"make": make, "model": model, "variant": variants[0]})
            print(f"    {make} / {model} / {variants[0]}")
    return result


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--states", default=",".join(DEFAULT_STATES))
    ap.add_argument("--deep", action="store_true",
                    help="also harvest models and variants (slower)")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--watch", dest="headless", action="store_false")
    args = ap.parse_args()

    cfg = settings.load(args.target)
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()]

    print(f"\nReading the portal's master data (read-only - no quotes requested)")
    print(f"Target : {cfg.name} ({cfg.base_url})")
    print(f"States : {', '.join(states)}\n")

    with browser.browser_session(cfg, headed=not args.headless) as (ctx, run_dir):
        try:
            page = auth.log_in(ctx, cfg)
            safety.verify_environment(page, cfg)
            safety.allow("read", cfg)      # genuinely read-only

            VehicleDetailsPage(page).open(cfg.base_url)
            print("RTOs by state:")
            rtos = sweep_rtos(page, states)

            vehicles_by_rto = {}
            if rtos:
                # Re-open the screen so the cascade starts clean before we pick.
                for state, options in list(rtos.items())[:3]:
                    VehicleDetailsPage(page).open(cfg.base_url)
                    print(f"\nVehicles available in {options[0]}:")
                    vehicles_by_rto[options[0]] = sweep_vehicles(
                        page, options[0], args.deep)

            payload = {
                "discovered_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "target": cfg.name,
                "rtos_by_state": rtos,
                "vehicles_by_rto": vehicles_by_rto,
            }
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

            total = sum(len(v) for v in rtos.values())
            print(f"\n{'=' * 62}")
            print(f"Found {total} real RTOs across {len(rtos)} states")
            print("=" * 62)
            print(f"\nSaved to data/{CACHE.name}")
            print("The scenario hunter will now vary RTO as well as year and")
            print("policy type - which is the dimension most likely to find an")
            print("insurer that only operates in certain states.\n")
            return 0

        except Exception as exc:
            print(f"\nERROR: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
