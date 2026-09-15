"""
Scenario variations, for finding a combination an insurer will actually quote.

WHY THIS EXISTS
---------------
"LIBERTY returned no quote" can mean at least six different things:

    1. Liberty does not cover this vehicle
    2. Liberty does not operate in this RTO / state
    3. Liberty only offers Third Party for this case, not Comprehensive
    4. The bike's age is outside their band
    5. Liberty's service was down at that moment
    6. Liberty is not configured for this broker at all

Retrying the SAME scenario only distinguishes (5) from the rest. Varying the
scenario distinguishes all of them - and, more usefully, finds a combination
that does work so the insurer can actually be tested.

WHAT WE VARY, AND WHAT WE DO NOT
--------------------------------
Policy type and registration year are safe to vary freely: they are choices on
the form, not references to master data.

Make/model/variant/RTO are NOT safe to invent. They must match rows the plan
master actually holds, and a wrong guess fails at screen one with an empty
dropdown rather than telling us anything about the insurer. So those come from
a list of combinations known to exist - add to it as the team confirms more.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from pages.vehicle_details import Vehicle

# ---------------------------------------------------------------------------
# Vehicles confirmed to exist in the plan master. ADD TO THIS LIST as the team
# confirms more - each one multiplies what the hunter can try.
# ---------------------------------------------------------------------------
KNOWN_VEHICLES: list[Vehicle] = [
    Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
            "3G (110 CC) (PETROL)", "2022"),
]

# Registration years worth trying. Vehicle age changes which insurers bid:
# some decline bikes over a certain age, some decline brand-new ones.
YEARS = ["2022", "2020", "2018", "2023"]

# Policy types. Several insurers offer Third Party where they decline
# Comprehensive, so this is often what unlocks a missing insurer.
POLICY_TYPES = ["Comprehensive", "Third Party"]


@dataclass(frozen=True)
class Scenario:
    """One complete combination to try."""
    vehicle: Vehicle
    policy_type: str
    label: str

    @property
    def summary(self) -> str:
        return (f"{self.vehicle.make} {self.vehicle.model} "
                f"{self.vehicle.registration_year}, {self.vehicle.rto}, "
                f"{self.policy_type}")


CACHE = Path(__file__).resolve().parent / "master_cache.json"


def discovered_rtos() -> list[str]:
    """
    Real RTOs harvested from the portal by discover_masters.py.

    Empty until that has been run, in which case we fall back to whatever the
    known vehicles carry - the hunt still works, just across fewer states.
    """
    if not CACHE.exists():
        return []
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return []

    # One RTO per state is enough. Insurers decline by STATE, not by individual
    # RTO office, so trying MH-01, MH-02 and MH-03 costs three journeys to learn
    # the same thing one journey would have told us.
    return [options[0] for options in data.get("rtos_by_state", {}).values() if options]


def build_variations(max_count: int = 10) -> list[Scenario]:
    """
    Build the scenarios to try, most-likely-to-matter FIRST.

    The order is the whole point. Each attempt costs a minute or two on a shared
    environment, so the dimension most likely to unlock a missing insurer has to
    come first - and that is the RTO.

    An insurer that only writes business in Maharashtra will never appear for a
    Gujarat RTO no matter how many years or policy types you try. The first
    version of this file varied only year and policy type, kept RTO fixed at
    GJ-01 for all eight attempts, and therefore could not have found such an
    insurer even in principle.

    So: vary RTO first, then policy type, then year.
    """
    out: list[Scenario] = []
    seen: set[tuple] = set()

    def add(vehicle: Vehicle, rto: str, year: str, policy: str) -> bool:
        """Add one scenario. Returns False once we have enough."""
        key = (rto, vehicle.make, vehicle.model, year, policy)
        if key in seen:
            return True
        seen.add(key)
        out.append(Scenario(
            vehicle=replace(vehicle, rto=rto, rto_type=rto.split()[0],
                            registration_year=year),
            policy_type=policy,
            label=f"{rto.split()[0]}-{policy[:4]}-{year}",
        ))
        return len(out) < max_count

    base = KNOWN_VEHICLES[0]
    base_year, base_policy = base.registration_year, POLICY_TYPES[0]
    rtos = discovered_rtos() or [base.rto]
    other_rtos = [r for r in rtos if r != base.rto]

    # PHASE 1 - the known-good baseline. Proves the run itself is healthy, so a
    # later empty result means something about the insurer rather than the setup.
    if not add(base, base.rto, base_year, base_policy):
        return out

    # PHASE 2 - every other STATE, holding everything else steady.
    # This is the big one. An insurer that only writes business in Maharashtra
    # is invisible from a Gujarat RTO however many years you try, so states come
    # before every other dimension.
    for rto in other_rtos:
        if not add(base, rto, base_year, base_policy):
            return out

    # PHASE 3 - policy type across those same states. Several insurers offer
    # Third Party where they decline Comprehensive.
    for policy in POLICY_TYPES[1:]:
        for rto in [base.rto] + other_rtos:
            if not add(base, rto, base_year, policy):
                return out

    # PHASE 4 - vehicle age, last. It matters least: an insurer that declines
    # every year in a state usually is not in that state at all.
    for year in [y for y in YEARS if y != base_year]:
        for rto in [base.rto] + other_rtos:
            if not add(base, rto, year, base_policy):
                return out

    # PHASE 5 - any other known vehicles, in the baseline state.
    for vehicle in KNOWN_VEHICLES[1:]:
        for rto in [vehicle.rto] + other_rtos:
            if not add(vehicle, rto, vehicle.registration_year, base_policy):
                return out

    return out
