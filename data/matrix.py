"""
The quote matrix: which journeys to run, and why these ones.

THE PROBLEM
-----------
A two-wheeler quote - and a private car quote, on the same screens - depends
on nine choices:

    bike (make/model/variant)  RTO        registration year   policy type
    previous expiry status     previous policy type          previous insurer
    NCB                        claim made

With a handful of values each that is tens of thousands of combinations, at two
minutes a journey. Nobody can run that - and nobody needs to.

THE IDEA: PAIRWISE
------------------
Most bugs need only TWO choices to meet: "Third Party AND a 2014 bike",
"OD Only AND a claim". Very few need three or more at once. So instead of every
combination, we want every PAIR of values to appear together at least once.
That takes a few dozen journeys instead of tens of thousands.

The planner picks, again and again, the journey that tests the most pairs not
yet tested, until none are left - so the first journeys are always the most
valuable ones, and a short run (--max) still gets the best of it.

PLUS TWINS
----------
Some rules are only visible by COMPARING two quotes: Comprehensive must cost
more than Third Party; a higher NCB must cost less; an older bike must get a
lower IDV. A "twin" is the known-good journey with exactly ONE thing changed,
so any difference in price can only come from that one thing.

ONLY WHAT THE FORM REALLY OFFERS
--------------------------------
Rules read from Saarthi's tw-dont-know-number component (2026-09-30):
  * OD Only is offered for bikes up to 4 years old; Comprehensive up to 25.
  * The previous policy details only matter when the policy has not lapsed
    by more than 90 days.
  * "Third Party" as the previous type is offered for a new CP/TP policy,
    "OD Only" as the previous type only for a new OD policy.
  * NCB and "claim made" do not apply to a Third Party policy; NCB also does
    not apply after a claim, a lapse of 90+ days, or a previous TP policy.
A choice that does not apply is left out (shown as "-"), so the planner never
asks the form a question it would not show.

PRIVATE CARS (pc-dont-know-number, read and checked live 2026-10-05)
--------------------------------------------------------------------
The car screens are the same three screens with the same questions. What
differs is the age limits: Comprehensive only up to 14 years (a 15-year-old
car gets Third Party only), and OD Only up to 4 years - although a car's
bundled third-party cover lasts 3 years, not 5, so at 4 years the form offers
OD Only and then never lets Proceed be pressed (its TP expiry date can only be
in the past). The planner still tries that once in a while: the runner spots
the stuck Proceed, reports it, and the notebook stops planning it again for a
fortnight (`blocked`).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from datetime import date

from data.labscenarios import PRODUCTS
from pages.additional_details import AdditionalChoice
from pages.policy_details import PolicyChoice
from pages.vehicle_details import Vehicle

# The order is the order of the columns in every report.
DIMENSIONS = ("vehicle", "rto", "year", "policy", "previous", "prev_type",
              "prev_insurer", "ncb", "claim")

NICE = {"vehicle": "vehicle", "rto": "RTO", "year": "reg. year",
        "policy": "policy", "previous": "previous policy",
        "prev_type": "previous type", "prev_insurer": "previous insurer",
        "ncb": "NCB", "claim": "claim made"}

CP, TP, OD = "Comprehensive", "Third Party", "OD Only"
NOT_EXPIRED = "Not Expired"
WITHIN_90 = "Expired within 90 Days"
OVER_90 = "Expired more than 90 Days"
DONT_KNOW = "Don't remember"
PREVIOUS = (NOT_EXPIRED, WITHIN_90, OVER_90, DONT_KNOW)
REMEMBERED = (NOT_EXPIRED, WITHIN_90)

# NCB values the matrix tries. "default" = leave the app's own value, which it
# derives from the bike's age.
NCB_VALUES = ("default", "0%", "25%", "50%")
NCB_LADDER = (0, 20, 25, 35, 45, 50)

# Ages, not years, so the matrix never goes stale. Four years old is the
# baseline: the oldest bike that can still buy OD Only, so every policy type
# is available to it. 20 years old is the edge most insurers draw a line at.
AGES = (4, 1, 7, 12, 20)

# Cars: three years old is the standard quote, the oldest car whose OD Only
# journey works. 4 is the age the form offers OD Only but blocks it, 16 is
# past the 14-year Comprehensive limit (Third Party only).
CAR_AGES = (3, 1, 7, 4, 12, 16)


@dataclass(frozen=True)
class Rules:
    """What one product's form allows, and the ages the matrix tries."""
    product: str          # "bike" | "car"
    noun: str             # what the reports call one vehicle
    od_max_age: int       # the form offers OD Only up to this age
    cp_max_age: int       # ... and Comprehensive up to this one
    ages: tuple           # [0] = the standard quote's age, [2] = the IDV twin's
    sizes: tuple          # third-party price bands, smallest engine first


RULES = {
    "bike": Rules("bike", "bike", PRODUCTS["bike"].od_max_age,
                  PRODUCTS["bike"].cp_max_age, AGES,
                  ("up to 150cc", "150-350cc", "over 350cc")),
    "car": Rules("car", "car", PRODUCTS["car"].od_max_age,
                 PRODUCTS["car"].cp_max_age, CAR_AGES,
                 ("up to 1000cc", "1000-1500cc", "over 1500cc")),
}


# The portal's variant list carries no launch year, so nothing stops the
# planner from asking for a 2014 Nexon EV - a car that did not exist. Electric
# cars and bikes on sale in India date from about 2019, so an electric
# vehicle is never planned older than this.
EV_MAX_AGE = 7

# How insurers word "you cannot renew with the insurer you already hold".
# Local and test servers say "same insurer"; the live BAJAJ says "Same company
# Renewal is not allowed." (2026-10-07) - matching only the first hid a real
# uneven-rule finding on the live site.
SAME_INSURER_WORDS = ("same insurer", "same company")


def says_same_insurer(reason: str) -> bool:
    low = (reason or "").lower()
    return any(words in low for words in SAME_INSURER_WORDS)


def block_key(policy: str, age: int) -> str:
    """How the notebook names a choice the form will not let through."""
    return f"{policy}|{age}"


def this_year() -> int:
    return date.today().year


def age_of(year: str) -> int:
    return this_year() - int(year)


def ncb_by_age(age: int) -> int:
    """The app's own default (component.ts:1177-1196): 0,20,25,35,45,50."""
    return NCB_LADDER[max(0, min(age - 1, len(NCB_LADDER) - 1))]


def ncb_number(ncb: str | None, year: str) -> int | None:
    """The NCB actually in play, as a number."""
    if ncb is None:
        return None
    if ncb == "default":
        return ncb_by_age(age_of(year))
    return int(ncb.rstrip("%"))


@dataclass(frozen=True)
class Scenario:
    """One journey. A value of None means 'does not apply to this journey'."""
    values: tuple                   # one entry per DIMENSIONS, in order
    kind: str = "pairwise"          # retest | baseline | twin | pairwise
    why: str = ""                   # one line: why this journey is in the plan
    relation: str = ""              # twin only: which rule it checks

    def get(self, dim: str):
        return self.values[DIMENSIONS.index(dim)]

    def with_(self, **changes) -> "Scenario":
        vals = list(self.values)
        for dim, value in changes.items():
            vals[DIMENSIONS.index(dim)] = value
        return replace(self, values=tuple(vals))

    @property
    def key(self) -> str:
        """Stable across runs - how the notebook recognises a journey again."""
        return " | ".join("-" if v is None else str(v) for v in self.values)

    def pairs(self) -> frozenset:
        """Every pair of (dimension, value) this journey tests."""
        live = [(d, v) for d, v in zip(DIMENSIONS, self.values) if v is not None]
        return frozenset(itertools.combinations(live, 2))

    def short(self, vehicles: dict[str, Vehicle] | None = None) -> str:
        """One readable line, leaving out what does not apply."""
        bike = self.get("vehicle")
        if vehicles and bike in vehicles:
            v = vehicles[bike]
            bike = f"{v.make} {v.model} {v.variant}"
        bits = [bike, self.get("rto").split()[0], self.get("year"),
                self.get("policy")]
        previous = self.get("previous")
        if previous == DONT_KNOW:
            bits.append("prev: not remembered")
        elif previous == OVER_90:
            bits.append("prev: lapsed 90+ days")
        else:
            prev = f"prev: {previous.lower()}"
            if self.get("prev_type"):
                prev += f", {self.get('prev_type')}"
            if self.get("prev_insurer"):
                prev += f", {self.get('prev_insurer')}"
            bits.append(prev)
        ncb = self.get("ncb")
        if ncb is not None:
            shown = ncb_number(ncb, self.get("year"))
            bits.append(f"NCB {shown}%" + (" (app default)" if ncb == "default" else ""))
        if self.get("claim") is not None:
            bits.append(f"claim {self.get('claim')}")
        return " · ".join(str(b) for b in bits)


# ------------------------------------------------------------------ the rules

def normalise(raw: dict, product: str = "bike",
              blocked: frozenset = frozenset(),
              bands: dict | None = None) -> dict | None:
    """
    Apply the form's rules to one combination.

    Returns the combination with every choice that does not apply set to None,
    or None if the form would not allow it at all. `blocked` holds the
    policy-and-age choices a run found the form offers but will not let past
    (block_key), so they are not planned again until the lesson is re-checked.
    `bands` (vehicle -> price band) keeps electric vehicles young (EV_MAX_AGE).
    """
    rules = RULES[product]
    s = dict(raw)
    age = age_of(s["year"])
    policy = s["policy"]
    if policy == OD and age > rules.od_max_age:
        return None                       # OD Only: bikes and cars up to 4 years
    if policy == CP and age > rules.cp_max_age:
        return None                       # Comprehensive: bikes 25, cars 14
    if block_key(policy, age) in blocked:
        return None
    if (bands or {}).get(s["vehicle"]) == "electric" and age > EV_MAX_AGE:
        return None

    remembered = s["previous"] in REMEMBERED
    if not remembered:
        s["prev_type"] = None
        s["prev_insurer"] = None
    else:
        allowed = (CP, OD) if policy == OD else (CP, TP)
        if s["prev_type"] not in allowed:
            return None

    # Claim: asked for CP/OD when the previous policy is remembered; for OD it
    # is also asked when nothing is remembered (html:765-801). Never for TP -
    # it is on screen, but a TP premium has no NCB for a claim to take away.
    if policy == TP:
        s["claim"] = None
    elif not (remembered or (policy == OD and s["previous"] == DONT_KNOW)):
        s["claim"] = None
    elif s["claim"] is None:
        return None

    # NCB: only when it can apply at all.
    ncb_applies = (policy != TP and remembered and s["prev_type"] != TP
                   and s["claim"] == "No")
    if not ncb_applies:
        s["ncb"] = None
    else:
        if s["ncb"] is None:
            return None
        if s["ncb"] != "default":
            # No more NCB than the vehicle can have earned: one step per
            # claim-free year. 50% on a one-year-old bike is not a scenario,
            # it is a typo, and it would make every insurer look wrong.
            if int(s["ncb"].rstrip("%")) > ncb_by_age(age):
                return None
            if int(s["ncb"].rstrip("%")) == ncb_by_age(age):
                s["ncb"] = "default"     # same thing as leaving it alone
    return s


def _scenario(values: dict, **kw) -> Scenario:
    return Scenario(values=tuple(values[d] for d in DIMENSIONS), **kw)


def options(vehicles: list[str], rtos: list[str], insurers: list[str],
            product: str = "bike") -> dict:
    return {
        "vehicle": vehicles,
        "rto": rtos,
        "year": [str(this_year() - a) for a in RULES[product].ages],
        "policy": [CP, TP, OD],
        "previous": list(PREVIOUS),
        "prev_type": [CP, TP, OD],
        "prev_insurer": insurers,
        "ncb": list(NCB_VALUES),
        "claim": ["No", "Yes"],
    }


def every_valid(opts: dict, product: str = "bike",
                blocked: frozenset = frozenset(),
                bands: dict | None = None) -> list[Scenario]:
    """Every combination the form allows, with duplicates folded together."""
    seen: dict[tuple, Scenario] = {}
    for combo in itertools.product(*(opts[d] for d in DIMENSIONS)):
        s = normalise(dict(zip(DIMENSIONS, combo)), product, blocked, bands)
        if s is None:
            continue
        values = tuple(s[d] for d in DIMENSIONS)
        seen.setdefault(values, Scenario(values=values))
    return list(seen.values())


def baseline(opts: dict, product: str = "bike") -> Scenario | None:
    """The known-good journey every runner uses: the Activa (cars: the
    Swift), Ahmedabad, 4 years old (cars: 3), Comprehensive, renewing an
    unexpired Comprehensive policy.

    When a run pins a choice (--policy, --year, ... or the Test Studio),
    the pinned value replaces the usual one. None when the pinned choices
    leave no journey the form allows."""
    def prefer(dim: str, usual: str) -> str:
        return usual if usual in opts[dim] else opts[dim][0]
    values = normalise({
        "vehicle": opts["vehicle"][0], "rto": opts["rto"][0],
        "year": prefer("year", str(this_year() - RULES[product].ages[0])),
        "policy": prefer("policy", CP),
        "previous": prefer("previous", NOT_EXPIRED), "prev_type": prefer("prev_type", CP),
        "prev_insurer": opts["prev_insurer"][0], "ncb": prefer("ncb", "default"),
        "claim": prefer("claim", "No")}, product)
    if values is None:
        return None
    return _scenario(values, kind="baseline",
        why="known-good journey - proves the environment is healthy, and is "
            "the 'before' for every twin")


def fits(s: Scenario, opts: dict) -> bool:
    """Does every choice this journey makes belong to the run's options?
    A twin changes one thing - to a value a pinned run may have ruled out."""
    return all(v is None or v in opts[d] for d, v in zip(DIMENSIONS, s.values))


# The twins, in order of value. Each checks one rule by changing one thing.
RELATIONS = {
    "cp-vs-tp": "Comprehensive must cost MORE than Third Party (it includes it)",
    "cp-vs-od": "OD Only must cost LESS than Comprehensive (it leaves TP out)",
    "ncb": "a bigger NCB must make Comprehensive CHEAPER",
    "age-idv": "an older vehicle must get a LOWER IDV",
    "claim": "a claim must NOT make the renewal cheaper (the NCB is lost)",
    "expired": "a policy lapsed 90+ days must NOT be cheaper (NCB is lost)",
    "cc-tp": "a bigger engine must NOT get a cheaper third-party price",
}


def twins(base: Scenario, opts: dict, bands: dict[str, str],
          product: str = "bike") -> list[Scenario]:
    rules = RULES[product]
    out: list[Scenario] = []

    def twin(relation: str, why: str, **change) -> None:
        values = dict(zip(DIMENSIONS, base.values))
        values.update(change)
        # Fill what the change newly makes applicable, then re-apply the rules.
        for dim, fallback in (("claim", "No"), ("ncb", "default"),
                              ("prev_type", CP),
                              ("prev_insurer", opts["prev_insurer"][0])):
            if values[dim] is None:
                values[dim] = fallback
        s = normalise(values, product)
        if s is not None:
            out.append(_scenario(s, kind="twin", why=why, relation=relation))

    twin("cp-vs-tp", "twin: same as baseline but Third Party", policy=TP)
    twin("cp-vs-od", "twin: same as baseline but OD Only", policy=OD)
    twin("ncb", "twin: same as baseline but NCB 0%", ncb="0%")
    twin("age-idv", f"twin: same as baseline but a {rules.ages[2]}-year-old "
                    f"{rules.noun}", year=str(this_year() - rules.ages[2]))
    twin("claim", "twin: same as baseline but a claim was made", claim="Yes")
    twin("expired", "twin: same as baseline but the policy lapsed 90+ days ago",
         previous=OVER_90)
    # Engine size: the Third Party twin again, on the biggest engine we have
    # that is in a higher price band than the standard quote's vehicle.
    size = {band: i for i, band in enumerate(rules.sizes, start=1)}
    own = size.get(bands.get(base.get("vehicle"), ""), 1)
    bigger = sorted((v for v in opts["vehicle"][1:] if size.get(bands.get(v), 0) > own),
                    key=lambda v: -size[bands[v]])
    if bigger:
        twin("cc-tp", f"twin: the Third Party twin on a {bands[bigger[0]]} "
                      f"{rules.noun}", policy=TP, vehicle=bigger[0])
    return out


# ------------------------------------------------------------------- planning

def plan(opts: dict, *, budget: int, depth: int = 2,
         history_pairs: dict | None = None, relation_age: dict | None = None,
         retests: list[Scenario] = (), bands: dict[str, str] | None = None,
         max_twins: int = 4, product: str = "bike",
         blocked: frozenset = frozenset()) -> tuple[list[Scenario], dict]:
    """
    Choose up to `budget` journeys, most valuable first.

        1. re-checks of anything suspicious last time (at most 2)
        2. the baseline
        3. twins - the rules checked longest ago come first (at most max_twins)
        4. pairwise: the journey covering the most untested pairs, repeatedly

    history_pairs: how often each pair was tested on EARLIER runs. A pair
    never tested before is worth 1; tested before, 0.3; three times or more,
    0.1. So a short run spends its budget on new ground, and run after run the
    coverage grows instead of repeating itself.

    depth=1 asks only that every single VALUE appears once (a quick smoke
    run); depth=2 asks for every PAIR; depth=3 for every THREE values together
    (about five times the journeys - an overnight run).

    product picks the form's rules (RULES); blocked leaves out what the
    notebook learned the form will not let through.

    Returns (journeys, coverage facts for the report).
    """
    history_pairs = history_pairs or {}
    relation_age = relation_age or {}
    pool = every_valid(opts, product, blocked, bands)
    wanted = _targets(pool, depth)

    chosen: list[Scenario] = []
    covered: set = set()

    def take(s: Scenario) -> bool:
        if len(chosen) >= budget or any(c.values == s.values for c in chosen):
            return False
        chosen.append(s)
        covered.update(_units(s, depth))
        return True

    for s in list(retests)[:2]:
        take(replace(s, kind="retest",
                     why=s.why or "re-check: something looked wrong here last run"))
    base = baseline(opts, product)
    if base is not None:
        take(base)
    pending = [t for t in (twins(base, opts, bands or {}, product) if base else [])
               if fits(t, opts)
               and normalise(dict(zip(DIMENSIONS, t.values)), product, blocked)]
    # Least recently checked first; never checked counts as oldest.
    pending.sort(key=lambda t: -relation_age.get(t.relation, 10_000))
    # cc-tp compares against the Third Party twin, so it needs that one in.
    taken_twins = 0
    for t in pending:
        if taken_twins >= max_twins:
            break
        if t.relation == "cc-tp" and not any(c.relation == "cp-vs-tp" for c in chosen):
            continue
        if take(t):
            taken_twins += 1

    def weight(unit) -> float:
        seen = history_pairs.get(pair_key(unit), 0) if depth == 2 else 0
        return 1.0 if seen == 0 else 0.3 if seen < 3 else 0.1

    units_of = {s.values: _units(s, depth) for s in pool}
    while len(chosen) < budget:
        best, best_score = None, 0.0
        for s in pool:
            fresh = units_of[s.values] - covered
            if not fresh:
                continue
            score = sum(weight(u) for u in fresh)
            if score > best_score:
                best, best_score = s, score
        if best is None:
            break
        new = len(units_of[best.values] - covered)
        take(replace(best, kind="pairwise",
                     why=f"tests {new} new {UNIT_WORDS[depth][new != 1]}"))

    ever = {k for k, n in history_pairs.items() if n} if depth == 2 else set()
    facts = {
        "depth": depth,
        "wanted": len(wanted),
        "this_run": len(covered & wanted),
        "ever_before": len({pair_key(u) for u in wanted} & ever),
        "with_this_run": len({pair_key(u) for u in wanted} & (
            ever | {pair_key(u) for u in covered})),
        "full_plan_size": _full_plan_size(pool, wanted, depth),
    }
    return chosen, facts


UNIT_WORDS = {1: ("value", "values"), 2: ("pair", "pairs"), 3: ("triple", "triples")}


def _units(s: Scenario, depth: int) -> frozenset:
    """What one journey covers: every single value (depth 1), every pair of
    values (2) or every three values together (3)."""
    live = [(d, v) for d, v in zip(DIMENSIONS, s.values) if v is not None]
    return frozenset(itertools.combinations(live, depth))


def _targets(pool: list[Scenario], depth: int) -> set:
    out: set = set()
    for s in pool:
        out |= _units(s, depth)
    return out


def _full_plan_size(pool: list[Scenario], wanted: set, depth: int) -> int:
    """How many journeys a complete pass would take - said in the report so
    nobody mistakes a budget-limited run for full coverage."""
    covered: set = set()
    count = 0
    units_of = {s.values: _units(s, depth) for s in pool}
    while covered < wanted:
        best = max(pool, key=lambda s: len(units_of[s.values] - covered))
        gained = units_of[best.values] - covered
        if not gained:
            break
        covered |= gained
        count += 1
    return count


def pair_key(unit) -> str:
    """A pair (or any group of values) as one string, for the notebook."""
    return " & ".join(f"{d}={v}" for d, v in unit)


# --------------------------------------------------------- to the page objects

def choices(s: Scenario, vehicles: dict[str, Vehicle],
            insurers: dict[str, tuple[str, str]]):
    """
    Turn a scenario into what the three screens need.

    vehicles: label -> Vehicle (without RTO/year; those come from the scenario)
    insurers: previous-insurer label -> (exact dropdown text, text to type)
    """
    base = vehicles[s.get("vehicle")]
    rto = s.get("rto")
    vehicle = Vehicle(rto.split()[0], rto, base.make, base.model, base.variant,
                      s.get("year"))

    previous = s.get("previous")
    prev_name, prev_search = insurers.get(s.get("prev_insurer") or "", ("", ""))
    policy = PolicyChoice(
        policy_type=s.get("policy"),
        remembers_previous=previous != DONT_KNOW,
        previous_expiry_status=previous if previous != DONT_KNOW else NOT_EXPIRED,
        previous_insurer=prev_name or PolicyChoice.previous_insurer,
        previous_insurer_search=prev_search or PolicyChoice.previous_insurer_search,
        previous_policy_type=s.get("prev_type") or CP,
    )

    ncb = s.get("ncb")
    extra = AdditionalChoice(
        claim_made=s.get("claim") == "Yes",
        ncb_percent=None if ncb in (None, "default") else ncb,
    )
    if s.get("policy") == OD:
        # The third-party cover's insurer. Any real one will do; the previous
        # insurer is the natural choice (the bundle usually came from them).
        first_name, first_search = next(iter(insurers.values()))
        extra.tp_insurer = prev_name or first_name
        extra.tp_insurer_search = prev_search or first_search
    return vehicle, policy, extra
