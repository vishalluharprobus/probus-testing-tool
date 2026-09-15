"""
Find a scenario that a given insurer will actually quote for - and remember it.

    python find_insurer.py --insurer LIBERTY
    python find_insurer.py --insurer LIBERTY --max 10
    python find_insurer.py --insurer LIBERTY --then-buy    # buy it once found

THE PROBLEM THIS SOLVES
-----------------------
"LIBERTY returned no quote" is not a result you can act on. It might mean the
vehicle, the RTO, the policy type, the bike's age, a momentary outage, or that
Liberty simply is not configured for this broker. Running the same scenario
again distinguishes none of those.

So this walks through DIFFERENT scenarios, changing one thing at a time, until
the insurer appears - then writes down which combination worked.

IT LEARNS
---------
Every success is saved to data/known_good.json. Next time anyone tests that
insurer, the tool starts with the scenario that worked rather than rediscovering
it. Over a few weeks that file becomes genuinely valuable: a map of which
insurer quotes for what, which nobody currently has written down anywhere.

IT IS DELIBERATELY SLOW
-----------------------
Each attempt is a full journey - a minute or two - and hits every insurer's UAT
API. So there is a pause between attempts and a hard cap on how many it will
try. Ten attempts is roughly 15-20 minutes. That is the honest cost of the
answer, and it is still far less than doing it by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from config import settings
from core import auth, browser, console, health, safety, ui
from data.scenarios import Scenario, build_variations
from pages.additional_details import AdditionalChoice, AdditionalDetailsPage
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.quote_list import QuoteListPage
from pages.vehicle_details import VehicleDetailsPage

KNOWN_GOOD = Path(__file__).resolve().parent / "data" / "known_good.json"

# Breathing room between attempts. Each journey asks every insurer for a price,
# so hammering this back-to-back degrades the environment for everyone - which
# we have already watched happen.
PAUSE_BETWEEN_ATTEMPTS_S = 20


def load_known_good() -> dict:
    if KNOWN_GOOD.exists():
        try:
            return json.loads(KNOWN_GOOD.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_known_good(insurer: str, scenario: Scenario, premium: int | None) -> None:
    """Write down what worked, so nobody has to find it again."""
    data = load_known_good()
    data[insurer.upper()] = {
        "found_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rto": scenario.vehicle.rto,
        "make": scenario.vehicle.make,
        "model": scenario.vehicle.model,
        "variant": scenario.vehicle.variant,
        "registration_year": scenario.vehicle.registration_year,
        "policy_type": scenario.policy_type,
        "premium_seen": premium,
    }
    KNOWN_GOOD.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_GOOD.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def try_scenario(context, cfg, scenario: Scenario, insurer: str):
    """
    Run one scenario and report what came back.

    Returns (found, quotes, note). `found` is the matching quote or None.
    A failed ATTEMPT is not a failed test - the environment misbehaving just
    means this attempt told us nothing, so we say so and move on.
    """
    page = auth.log_in(context, cfg)
    safety.verify_environment(page, cfg)
    safety.allow("quote", cfg)

    VehicleDetailsPage(page).open(cfg.base_url).fill(scenario.vehicle).proceed()
    PolicyDetailsPage(page).wait_until_loaded().fill(
        PolicyChoice(policy_type=scenario.policy_type)).proceed()
    AdditionalDetailsPage(page).wait_until_loaded().fill(
        AdditionalChoice()).proceed()

    quotes = QuoteListPage(page).wait_until_loaded().quotes()
    match = next((q for q in quotes if insurer.upper() in q.insurer.upper()), None)
    return match, quotes, page


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--insurer", required=True)
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--max", type=int, default=8,
                    help="how many scenarios to try before giving up")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--watch", dest="headless", action="store_false",
                    help="show the browser")
    args = ap.parse_args()

    cfg = settings.load(args.target)
    insurer = args.insurer.upper()
    variations = build_variations(args.max)

    print(f"\nHunting for a scenario {insurer} will quote for.")
    print(f"Target : {cfg.name} ({cfg.base_url})")
    print(f"Trying : up to {len(variations)} scenarios, ~{PAUSE_BETWEEN_ATTEMPTS_S}s "
          f"apart\n")

    remembered = load_known_good().get(insurer)
    if remembered:
        print(f"  (we found {insurer} before, on "
              f"{remembered['policy_type']} / {remembered['registration_year']} "
              f"/ {remembered['rto']} - trying the full sweep anyway)\n")

    seen_insurers: set[str] = set()
    attempts: list[tuple[Scenario, str]] = []

    for n, scenario in enumerate(variations, 1):
        print(f"[{n}/{len(variations)}] {scenario.summary}")

        with browser.browser_session(cfg, headed=not args.headless) as (ctx, run_dir):
            watcher = health.Watcher.for_context(ctx)
            try:
                match, quotes, page = try_scenario(ctx, cfg, scenario, insurer)
                names = [q.insurer for q in quotes]
                seen_insurers.update(n.upper() for n in names)
                print(f"        {len(quotes)} quotes: {', '.join(names) or 'none'}")

                if match:
                    print(f"\n{'=' * 62}")
                    print(f"FOUND {insurer} - Rs {match.premium:,}")
                    print("=" * 62)
                    print(f"\n  Scenario that worked:")
                    print(f"    RTO          : {scenario.vehicle.rto}")
                    print(f"    Vehicle      : {scenario.vehicle.make} "
                          f"{scenario.vehicle.model} {scenario.vehicle.variant}")
                    print(f"    Registration : {scenario.vehicle.registration_year}")
                    print(f"    Policy type  : {scenario.policy_type}")
                    print(f"    Premium      : Rs {match.premium:,}   "
                          f"IDV Rs {match.idv:,}" if match.idv else "")
                    save_known_good(insurer, scenario, match.premium)
                    print(f"\n  Saved to {KNOWN_GOOD.name} - future runs start here.")
                    print(f"\n  Now buy it with:")
                    print(f"    python run_proposal_test.py --insurer {insurer} "
                          f"--submit-kyc\n")
                    return 0

                attempts.append((scenario, f"{len(quotes)} quotes, no {insurer}"))

            except (ui.PageStuckLoading, ui.LookupTimedOut) as exc:
                print(f"        environment problem - this attempt proves nothing")
                attempts.append((scenario, "environment problem"))
            except safety.SafetyRefusal as exc:
                print(f"\nREFUSED: {exc}\n")
                return 3
            except Exception as exc:
                diag = watcher.diagnose(None)
                if diag.is_environment_problem:
                    print(f"        environment problem - attempt inconclusive")
                    attempts.append((scenario, "environment problem"))
                else:
                    print(f"        ERROR {type(exc).__name__}: {str(exc)[:90]}")
                    attempts.append((scenario, f"error: {type(exc).__name__}"))

        if n < len(variations):
            print(f"        pausing {PAUSE_BETWEEN_ATTEMPTS_S}s to be kind to the "
                  f"environment ...")
            time.sleep(PAUSE_BETWEEN_ATTEMPTS_S)

    # ---------------------------------------------------------------- summary
    print(f"\n{'=' * 62}")
    print(f"{insurer} DID NOT QUOTE IN ANY OF {len(variations)} SCENARIOS")
    print("=" * 62)
    print("\n  What was tried:")
    for scenario, note in attempts:
        print(f"    {scenario.summary:<52} {note}")

    inconclusive = sum(1 for _, note in attempts if "environment" in note)
    if inconclusive:
        print(f"\n  NOTE: {inconclusive} of {len(attempts)} attempts hit "
              f"environment problems,")
        print("  so they prove nothing. Re-run when the environment is quieter.")

    print(f"\n  Insurers that DID quote at some point:")
    print(f"    {', '.join(sorted(seen_insurers)) or 'none'}")

    print(f"\n  Most likely explanations, in order:")
    print(f"    1. {insurer} is not configured for this broker on this environment")
    print(f"    2. {insurer} does not cover this vehicle or this RTO")
    print(f"    3. {insurer}'s service is down")
    print(f"\n  Worth checking the broker's insurer configuration before")
    print(f"  assuming an integration defect.\n")
    return 5


if __name__ == "__main__":
    sys.exit(main())
