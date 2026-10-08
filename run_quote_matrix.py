"""
Test every insurer's quote across many scenarios - and learn from each run.

    python run_quote_matrix.py --plan            # show the plan, open nothing
    python run_quote_matrix.py                   # run it (12 journeys, ~35 min)
    python run_quote_matrix.py --max 4           # a quick taste (~10 min)
    python run_quote_matrix.py --all --fast      # every pair, straight to the API (minutes)
    python run_quote_matrix.py --max 30          # a complete pairwise pass
    python run_quote_matrix.py --depth 1         # smoke: every value once
    python run_quote_matrix.py --all             # until every pair is covered
    python run_quote_matrix.py --depth 3 --all --fast   # every 3 choices together, via the API
    python run_quote_matrix.py --learn-vehicles  # re-read the portal's vehicles
    python -m core.quotenotes                    # what it has learned so far

    THE LIVE SITE - quotes only, for the insurers you name (log in by hand
    once when the browser asks; the session is then reused):
    python run_quote_matrix.py --target live --insurers BAJAJ,TATA,DIGIT,SBI,KOTAK --plan
    python run_quote_matrix.py --target live --insurers BAJAJ,TATA,DIGIT,SBI,KOTAK --max 4
    python -m core.quotenotes bike live

    PRIVATE CARS - the same, with --product car:
    python run_quote_matrix.py --product car --plan
    python run_quote_matrix.py --product car --max 4
    python -m core.quotenotes car

WHAT ONE RUN DOES
-----------------
1. Learns the portal's REAL bikes (or cars) and previous insurers from the
   app's own master data (first run, then monthly) - nothing is invented.
2. Plans the journeys (data/matrix.py): re-checks of last run's findings, the
   known-good baseline, "twins" (baseline with ONE thing changed), then
   pairwise journeys that test the most never-tested pairs of choices.
3. Drives each journey through screens 1-3 and reads every insurer's answer
   from the API - premium, GST, OD and TP parts, NCB, IDV, or the reason it
   said no (core/quotecapture.py).
4. Checks every quote, and compares twins (core/quotechecks.py).
5. Writes it all down (core/quotenotes.py) and reports: a grid of who priced
   what, the findings, the rules it has learned, what changed since last time.

It only ever QUOTES. It never presses Buy Now, so nothing is created at any
insurer, whatever the write ceiling says.

WHEN THE FORM ITSELF WILL NOT MOVE
    Some choices are offered and then blocked: Proceed stays grey with no
    message (a 4-year-old car on OD Only, found 2026-10-05). The journey is
    reported as a Bug with the field that holds it back, the rest of the run
    skips the same choice, and the notebook leaves it out of plans for 14
    days - then tries it once more, in case the portal was fixed.

EXIT CODES
    0  every check passed          1  at least one DEFECT
    3  guard rail refused          4  login failed
    5  nobody priced anything      6  the environment stopped the run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from config import settings
from core import (auth, backend, browser, console, health, matrixreport,
                  quotechecks, quotenotes, safety, ui, vehiclecatalog)
from core.quotecapture import QuoteCapture
from data import matrix
from data.labscenarios import PRODUCTS
from data.scenarios import discovered_rtos
from pages import routes
from pages.additional_details import AdditionalDetailsPage
from pages.policy_details import PolicyDetailsPage
from pages.quote_list import Failure, QuoteListPage
from pages.vehicle_details import VehicleDetailsPage

HARNESS_BUGS = (NameError, AttributeError, TypeError, ImportError, KeyError,
                IndexError)

# Give up on the whole run after this many journeys in a row are lost to the
# environment. By then the answer is "the environment is down", and more
# journeys only add load to a sick server.
MAX_ENVIRONMENT_FAILURES_IN_A_ROW = 3

# Screen 1's vehicle fields - the same names for bikes and cars.
BIKE_FIELDS = ("vehicleMake", "vehicleModel", "vehicleVariant")


@dataclass
class Journey:
    n: int
    scenario: matrix.Scenario
    # ok | incomplete | environment | form | blocked | skipped | error
    #   form     the form would not take a value (a vehicle not in its list)
    #   blocked  the form took everything and still kept Proceed grey
    #   skipped  not run: this run already saw the form block the same choice
    status: str = "not run"
    note: str = ""
    offers: dict = field(default_factory=dict)
    answers: list = field(default_factory=list)
    declines: list = field(default_factory=list)
    kinds: dict = field(default_factory=dict)
    silent: list = field(default_factory=list)
    sent: dict = field(default_factory=dict)
    cards: list = field(default_factory=list)
    quotation: str = ""
    qualified: list = field(default_factory=list)   # every code the portal named
    seconds: float = 0.0
    run_dir: Path | None = None
    findings: list = field(default_factory=list)
    changes: list = field(default_factory=list)


class StopRun(Exception):
    """Something that makes every further journey pointless."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ====================================================================== main

def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--product", choices=tuple(PRODUCTS), default="bike",
                    help="bike = two-wheeler (default), car = private car")
    ap.add_argument("--max", type=int, default=12,
                    help="most journeys to run (each takes ~2-3 minutes)")
    ap.add_argument("--depth", type=int, choices=(1, 2, 3), default=2,
                    help="2 = every PAIR of choices (default), 1 = every value "
                         "once, 3 = every THREE choices together (use --fast)")
    ap.add_argument("--all", action="store_true",
                    help="no journey limit: run until the depth is fully covered")
    ap.add_argument("--vehicles", "--bikes", dest="vehicles", type=int, default=4,
                    help="how many different vehicles (one per engine size)")
    ap.add_argument("--states", type=int, default=4,
                    help="how many RTO states (one RTO each)")
    ap.add_argument("--no-twins", action="store_true",
                    help="skip the before/after twin journeys")
    ap.add_argument("--pause", type=int, default=20,
                    help="seconds between journeys, to be kind to the environment")
    ap.add_argument("--fast", action="store_true",
                    help="one browser journey, then every journey straight to the API "
                         "- minutes instead of hours (core/fastsweep.py)")
    ap.add_argument("--workers", type=int, default=0,
                    help="fast mode: journeys at once (default 4; 2 on the live site)")
    ap.add_argument("--insurers", default="",
                    help="ask only these insurers, e.g. BAJAJ,TATA,DIGIT,SBI,KOTAK "
                         "(every other insurer's quote call is stopped)")
    # Pin any choice; whatever is not pinned is chosen for you, as before.
    # Each may be given more than once (--rto "GJ-01 Ahmedabad" --rto "MH-01 Mumbai").
    ap.add_argument("--vehicle", action="append", default=[],
                    help="a vehicle as MAKE|MODEL|VARIANT, exactly as the portal "
                         "lists it (the Test Studio fills this in)")
    ap.add_argument("--rto", action="append", default=[],
                    help="an RTO as the portal names it, e.g. 'GJ-01 Ahmedabad'")
    ap.add_argument("--policy", action="append", default=[],
                    choices=(matrix.CP, matrix.TP, matrix.OD))
    ap.add_argument("--year", action="append", default=[],
                    help="registration year, e.g. 2022")
    ap.add_argument("--previous", action="append", default=[],
                    choices=matrix.PREVIOUS, help="previous policy status")
    ap.add_argument("--ncb", action="append", default=[],
                    choices=matrix.NCB_VALUES)
    ap.add_argument("--claim", action="append", default=[], choices=("No", "Yes"))
    ap.add_argument("--plan", action="store_true",
                    help="print the plan and stop - no browser, no quotes")
    ap.add_argument("--learn-vehicles", "--learn-bikes", dest="learn_vehicles",
                    action="store_true",
                    help="re-read the portal's vehicles and insurers first")
    ap.add_argument("--report", action="store_true",
                    help="print what the notebook has learned and stop")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--slow", type=int, default=0)
    args = ap.parse_args()
    product = PRODUCTS[args.product]
    rules = matrix.RULES[product.name]
    if args.all:
        args.max = 100_000        # the planner stops once everything is covered
    args.only = tuple(dict.fromkeys(
        c.strip().upper() for c in args.insurers.split(",") if c.strip()))
    # Each product learns into its own notebook (reports/quote_matrix*.json);
    # the live site into its own (..._live.json).
    quotenotes.use(product.name, args.target)

    if args.report:
        print(quotenotes.report())
        return 0

    cfg = settings.load(args.target)
    if args.fast and not args.workers:
        args.workers = 2 if cfg.live else 4      # gentler on real insurers
    print(f"\nQUOTE MATRIX - {product.title} - {cfg.name} ({cfg.base_url})")
    print("Quotes only: nothing is bought, nothing is created at any insurer.")
    if cfg.live:
        print("THE LIVE SITE: each journey sends real quote requests to the "
              "insurers asked.")
    print()

    catalog = vehiclecatalog.load(product.name)
    known = {i.get("code") for i in catalog.get("insurers") or [] if i.get("code")}
    unknown = [c for c in args.only if known and c not in known]
    if unknown:
        print(f"Unknown insurer code(s): {', '.join(unknown)}.\n"
              f"The portal's codes are: {', '.join(sorted(known))}")
        return 2
    if cfg.live and not args.only:
        print("On the live site, name the insurers to ask, e.g.\n"
              "  --insurers BAJAJ,TATA,DIGIT,SBI,KOTAK\n"
              "(asking every insurer on every journey sends a lot of real "
              "requests).")
        return 2
    if args.only:
        print(f"Asking only: {', '.join(args.only)} - every other insurer's "
              f"quote call is stopped in the browser.\n")
    stale = vehiclecatalog.age_days(catalog) > 30
    if not args.plan and (args.learn_vehicles or not catalog or stale):
        why = ("asked to" if args.learn_vehicles else
               "first run" if not catalog else "last read over 30 days ago")
        print(f"Learning the portal's {rules.noun}s and insurers ({why}) - "
              f"read-only ...")
        if not _api_awake(cfg):
            return 6
        learned = learn_catalog(cfg, args, product)
        catalog = learned or catalog
    elif not catalog:
        proven = vehiclecatalog.PROVEN_BY_PRODUCT[product.name]
        print(f"  ({rules.noun}s not learned yet - this plan uses the "
              f"{proven.model.title()} only. Run once\n   without --plan and the "
              f"tool reads the portal's {rules.noun}s first.)\n")

    try:
        setup = build_setup(args, catalog, product.name)
    except PinError as exc:
        print(f"Cannot plan this run: {exc}")
        return 2
    blocked = quotenotes.blocked()
    journeys_plan, facts = matrix.plan(
        setup["opts"], budget=args.max, depth=args.depth,
        history_pairs=quotenotes.history_pairs(),
        relation_age=quotenotes.relation_age(),
        retests=[s for s in quotenotes.suspects()
                 if _fits(s, setup["opts"], product.name, blocked)],
        bands=setup["bands"],
        max_twins=0 if args.no_twins else (7 if args.max >= 20 else 4),
        product=product.name, blocked=blocked)
    if not journeys_plan:
        rules_ = matrix.RULES[product.name]
        print(f"Cannot plan this run: the chosen options leave no journey the form "
              f"allows.\n  Remember: OD Only is offered up to {rules_.od_max_age} "
              f"years, Comprehensive up to {rules_.cp_max_age} years, and NCB "
              f"needs a remembered, claim-free, non-Third-Party previous policy.")
        return 2
    print_plan(journeys_plan, facts, setup, args)
    if args.plan:
        return 0

    if not _api_awake(cfg):
        return 6
    if args.fast:
        return run_all_fast(cfg, args, journeys_plan, facts, setup)
    return run_all(cfg, args, journeys_plan, facts, setup)


def _api_awake(cfg) -> bool:
    api = backend.check(cfg.api_url)
    if not api:
        print("=" * 62)
        print("THE APP'S BACK END IS NOT ANSWERING - nothing was run")
        print("=" * 62)
        print(f"\n  {api.detail}\n")
    return bool(api)


def _fits(s: matrix.Scenario, opts: dict, product: str = "bike",
          blocked: frozenset = frozenset()) -> bool:
    """Can last run's suspect run again with today's choices and rules?"""
    if not all(v is None or v in opts[d] for d, v in zip(matrix.DIMENSIONS, s.values)):
        return False
    return matrix.normalise(dict(zip(matrix.DIMENSIONS, s.values)), product,
                            blocked) is not None


def build_setup(args, catalog: dict, product: str = "bike") -> dict:
    """Pick the vehicles, states and previous insurers this run will use.
    Raises PinError when a pinned choice does not exist."""
    pinned = getattr(args, "vehicle", None) or []
    if pinned:
        picked = [_pinned_vehicle(text, catalog, product) for text in pinned]
    else:
        picked = vehiclecatalog.pick(catalog, max(1, args.vehicles),
                                     avoid=quotenotes.bad_bikes(),
                                     tried=quotenotes.tried_bikes(), product=product)
    vehicles, bands, facts = {}, {}, {}
    for vehicle, band in picked:
        label = vehiclecatalog.label(vehicle)
        vehicles[label] = vehicle
        bands[label] = band
        row = vehiclecatalog.row_for(vehicle, catalog)
        # What the regulated third-party price check needs to know.
        facts[label] = {"product": product, "band": band, "cc": row.get("cc"),
                        "fuel": row.get("fuel", "")}

    home = vehiclecatalog.PROVEN_BY_PRODUCT[product].rto
    rtos = discovered_rtos() or [home]
    rtos = [home] + [r for r in rtos if r != home]
    rtos = rtos[:max(1, args.states)]
    if getattr(args, "rto", None):
        rtos = list(dict.fromkeys(r.strip() for r in args.rto if r.strip()))
        bad = [r for r in rtos if not re.match(r"^[A-Z]{2}-\d{1,2}[A-Z]?\s+\S", r)]
        if bad:
            raise PinError(f"RTO {bad[0]!r} is not in the portal's form "
                           f"'GJ-01 Ahmedabad' (code, space, city).")

    prev = vehiclecatalog.previous_insurers(catalog, getattr(args, "only", ()))
    insurers = {code: (name, search) for name, search, code in prev}
    prev_codes = {code: code for _, _, code in prev}
    company_ids = {i["code"]: i.get("id") for i in catalog.get("insurers") or []
                   if i.get("code")}

    return {"product": product, "vehicles": vehicles, "bands": bands,
            "facts": facts, "insurers": insurers, "prev_codes": prev_codes,
            "company_ids": company_ids,
            "only": tuple(getattr(args, "only", ())),
            # Vehicles in the portal's own list that its form can never show.
            "hidden": vehiclecatalog.hidden(catalog),
            "opts": _pin(matrix.options(list(vehicles), rtos, list(insurers), product),
                         args)}


class PinError(ValueError):
    """A pinned choice that does not exist - said plainly, before any browser."""


def _pinned_vehicle(text: str, catalog: dict, product: str):
    """'MAKE|MODEL|VARIANT' (or the same with spaces) -> (Vehicle, band)."""
    rows = catalog.get("vehicles") or []
    want = text.strip().upper()
    hit = next((r for r in rows if vehiclecatalog.key_of(r) == want), None)
    if hit is None:
        spaced = want.replace("|", " ")
        hit = next((r for r in rows
                    if f"{r['make']} {r['model']} {r['variant']}".upper() == spaced), None)
    if hit is None:
        raise PinError(f"vehicle {text!r} is not in the portal's {product} list "
                       f"(data/{vehiclecatalog.CATALOG_FILES[product].name}).")
    if hit.get("hidden_by"):
        raise PinError(f"vehicle {text!r} can never be picked on the form: "
                       f"{hit['hidden_by']}")
    return vehiclecatalog._as_vehicle(hit, product), hit.get("band") or "unknown"


def _pin(opts: dict, args) -> dict:
    """Replace each dimension the run pinned with just the pinned values."""
    for dim, name in (("policy", "policy"), ("year", "year"),
                      ("previous", "previous"), ("ncb", "ncb"), ("claim", "claim")):
        values = [v for v in getattr(args, name, None) or [] if v]
        if not values:
            continue
        if dim == "year":
            bad = [y for y in values if not (y.isdigit() and 1990 <= int(y) <= matrix.this_year())]
            if bad:
                raise PinError(f"registration year {bad[0]!r} is not a year this "
                               f"form accepts (1990-{matrix.this_year()}).")
        opts[dim] = list(dict.fromkeys(values))
    return opts


def print_plan(plan, facts, setup, args) -> None:
    rules = matrix.RULES[setup["product"]]
    print("THE CHOICES")
    for label, band in setup["bands"].items():
        print(f"  {rules.noun:<9} {label}   ({band or 'size unknown'})")
    print(f"  RTOs      {', '.join(setup['opts']['rto'])}")
    print(f"  years     {', '.join(setup['opts']['year'])}")
    limits = {matrix.CP: f" (up to {rules.cp_max_age} years)",
              matrix.OD: f" (up to {rules.od_max_age} years)"}
    opts = setup["opts"]
    print(f"  policy    {', '.join(p + limits.get(p, '') for p in opts['policy'])}")
    print(f"  previous  {', '.join(opts['previous'])}")
    print(f"  prev. ins {', '.join(setup['insurers'])} (rotated, so each gets a "
          f"fair turn at a renewal)")
    ncb = ", ".join("app default" if n == "default" else n for n in opts["ncb"])
    print(f"  NCB       {ncb} (never more than the {rules.noun}'s age allows)")
    print(f"  claim     {', '.join(opts['claim'])}")
    for line in setup.get("hidden", []):
        print(f"  NOT PICKABLE  {line}")
    for key in sorted(quotenotes.blocked()):
        policy, _, age = key.partition("|")
        print(f"  left out  {policy} for a {age}-year-old {rules.noun}: the form "
              f"blocked it last time (re-checked after "
              f"{quotenotes.BLOCK_RECHECK_DAYS} days)")

    unit = matrix.UNIT_WORDS[args.depth][1]
    if getattr(args, "fast", False):
        # ~1.5 min to log in (and capture a request, once a week), then ~15 s
        # a journey, `workers` at a time.
        minutes = round(1.5 + len(plan) * 0.25 / max(1, args.workers or 4))
        print(f"\nTHE PLAN - {len(plan)} journeys (~{minutes // 60:.0f} h "
              f"{minutes % 60:.0f} min, fast mode: straight to the API, "
              f"{args.workers or 4} at a time)")
    else:
        minutes = len(plan) * (2.6 + args.pause / 60)
        print(f"\nTHE PLAN - {len(plan)} journeys "
              f"(~{minutes // 60:.0f} h {minutes % 60:.0f} min, {args.pause}s pause between)")
    for n, s in enumerate(plan, 1):
        print(f"  {n:>2}. [{s.kind:<8}] {s.short(setup['vehicles'])}")
        print(f"      {'':<10} {s.why}")
    print(f"\n  Coverage: this plan tests {facts['this_run']} of {facts['wanted']} "
          f"{unit}.")
    if args.depth == 2:
        print(f"  With earlier runs      : {facts['with_this_run']} of "
              f"{facts['wanted']} pairs (before this run: {facts['ever_before']}).")
        print(f"  A complete pass takes about {facts['full_plan_size']} journeys; "
              f"each run continues where the last one stopped.")
    print()


# ========================================================= vehicles & insurers

def learn_catalog(cfg, args, product) -> dict:
    """One read-only visit to screen 1, listening to the app's master data."""
    hosts = {urlparse(u).hostname for u in (cfg.api_url, cfg.base_url) if u}
    noun = matrix.RULES[product.name].noun
    proven = vehiclecatalog.PROVEN_BY_PRODUCT[product.name].model.title()
    with browser.browser_session(cfg, headed=not args.headless) as (context, _):
        listener = vehiclecatalog.MasterListener.attach(context, hosts, product.name)
        try:
            page = auth.log_in(context, cfg)
            safety.verify_environment(page, cfg)
            safety.allow("read", cfg)
            VehicleDetailsPage(page).open(cfg.base_url, product.name)
            waited = 0
            while not listener.complete and waited < 25_000:
                page.wait_for_timeout(1000)
                waited += 1000
        except Exception as exc:                       # noqa: BLE001
            print(f"  could not read the {noun}s ({type(exc).__name__}: "
                  f"{str(exc)[:80]}) - carrying on with the proven {proven}")
            return {}
    if not listener.raw.get("variants"):
        print(f"  the app's {noun} list never arrived - carrying on with the "
              f"proven {proven}")
        return {}
    catalog = vehiclecatalog.build(listener.raw, cfg.name, product.name)
    vehiclecatalog.save(catalog, product.name)
    bands: dict[str, int] = {}
    for row in catalog["vehicles"]:
        bands[row["band"] or "unknown"] = bands.get(row["band"] or "unknown", 0) + 1
    print(f"  learned {len(catalog['vehicles'])} variants "
          f"({', '.join(f'{b} {n}' for b, n in sorted(bands.items()))}) and "
          f"{len(catalog['insurers'])} insurers -> "
          f"data/{vehiclecatalog.CATALOG_FILES[product.name].name}\n")
    return catalog


# ================================================================== the run

def run_all(cfg, args, plan, facts, setup) -> int:
    started = datetime.now()
    tag = "" if setup["product"] == "bike" else f"{setup['product']}-"
    tag += "live-" if cfg.live else ""
    folder = browser.REPORTS / f"matrix-{tag}{started:%Y%m%d-%H%M%S}"
    journeys: list[Journey] = []
    in_a_row = 0
    refused_bikes: dict[str, str] = {}
    blocked_now: dict[str, int] = {}      # block key -> the journey that found it
    stop_code = None

    for n, s in enumerate(plan, 1):
        print(f"[{n:>2}/{len(plan)}] {s.why}")
        print(f"        {s.short(setup['vehicles'])}")
        block = matrix.block_key(s.get("policy"), matrix.age_of(s.get("year")))
        if s.get("vehicle") in refused_bikes:
            j = Journey(n, s, status="skipped",
                        note="not run: the form already refused this vehicle twice "
                             "in this run")
            journeys.append(j)
            print(f"        SKIPPED - {j.note}\n")
            continue
        if block in blocked_now:
            # The same choice already stopped the form in this run - running
            # it again would only repeat what #n proved.
            j = Journey(n, s, status="skipped",
                        note=f"not run: journey #{blocked_now[block]} already showed "
                             f"the form blocks {s.get('policy')} at this age")
            journeys.append(j)
            print(f"        SKIPPED - {j.note}\n")
            continue
        try:
            j = run_journey(cfg, args, n, s, setup)
            if j.status in ("environment", "form"):
                print(f"        {j.status} problem: {j.note}")
                print("        trying once more in 30s (a fresh login) ...")
                time.sleep(30)
                first = j
                j = run_journey(cfg, args, n, s, setup)
                if j.status == "form" and first.status == "form":
                    bike = s.get("vehicle")
                    if any(f in j.note for f in BIKE_FIELDS):
                        refused_bikes[bike] = j.note
                        print(f"        the form refused this vehicle twice - it "
                              f"will not be picked again")
        except StopRun as stop:
            print(f"\n{stop}\n")
            stop_code = stop.code
            break
        journeys.append(j)
        if j.status == "blocked":
            blocked_now[block] = n
            quotenotes.record_block(block, j.note)
        elif j.status in ("ok", "incomplete"):
            quotenotes.record_unblock(block)     # a re-check that got through
        analyse(j, setup)
        print_journey(j)

        in_a_row = in_a_row + 1 if j.status == "environment" else 0
        if in_a_row >= MAX_ENVIRONMENT_FAILURES_IN_A_ROW:
            print(f"\n  {in_a_row} journeys in a row lost to the environment. "
                  f"Stopping here:\n  the answer today is 'the environment is "
                  f"down', and more journeys only add load.\n")
            stop_code = 6
            break
        if n < len(plan):
            time.sleep(args.pause)

    return finish(journeys, facts, setup, folder, refused_bikes, stop_code)


def run_all_fast(cfg, args, plan, facts, setup) -> int:
    """Fast mode: one login (and a captured request), then every journey
    straight to the API, `args.workers` at a time (core/fastsweep.py)."""
    import concurrent.futures as futures
    from core.fastsweep import FastSweep, TokenExpired

    started = datetime.now()
    tag = "" if setup["product"] == "bike" else f"{setup['product']}-"
    tag += "live-" if cfg.live else ""
    folder = browser.REPORTS / f"matrix-{tag}{started:%Y%m%d-%H%M%S}"
    evidence = folder / "journeys"
    evidence.mkdir(parents=True, exist_ok=True)

    sweep = FastSweep(cfg, setup["product"], setup, args.only, headless=args.headless)
    try:
        sweep.open()
    except safety.SafetyRefusal as exc:
        print(f"\nREFUSED (guard rail working as designed):\n  {exc}\n")
        return 3
    except auth.LoginFailed as exc:
        print(f"\nLOGIN FAILED (setup problem):\n  {exc}\n")
        return 4
    except Exception as exc:                           # noqa: BLE001
        print(f"\nCOULD NOT START FAST MODE: {exc}\n"
              f"  Browser mode still works: run again without --fast.\n")
        return 6
    print(f"  ready - {len(plan)} journeys, {args.workers} at a time\n")

    def one(n: int, s: matrix.Scenario) -> Journey:
        j = Journey(n, s, cards=None)
        for attempt in (1, 2):
            r = sweep.run(s)
            if r.status != "environment" or attempt == 2:
                break
            time.sleep(5)                        # one retry for a busy API
        j.status, j.note, j.seconds = r.status, r.note, r.seconds
        j.offers, j.answers, j.declines = r.offers, r.answers, r.declines
        j.silent, j.sent, j.quotation, j.qualified = r.silent, r.sent, r.quotation, r.qualified
        j.kinds = {d.insurer: _kind(d) for d in j.declines}
        try:
            (evidence / f"journey-{n:03d}.json").write_text(
                json.dumps({"scenario": dict(zip(matrix.DIMENSIONS, s.values)),
                            **r.evidence}, indent=1, default=str), encoding="utf-8")
        except Exception:
            pass
        return j

    journeys: list[Journey] = []
    stop_code = None
    in_a_row = 0
    pool = futures.ThreadPoolExecutor(max(1, args.workers))
    jobs = {pool.submit(one, n, s): n for n, s in enumerate(plan, 1)}
    try:
        for done in futures.as_completed(jobs):
            try:
                j = done.result()
            except TokenExpired:
                print("\nLOGIN EXPIRED: the API no longer accepts the borrowed login. "
                      "Run again - the next login fetches a fresh one.\n")
                stop_code = 4
                break
            except HARNESS_BUGS:
                traceback.print_exc()
                print("A BUG IN THE TEST TOOL (not the portal) - see above. Stopping.")
                stop_code = 1
                break
            journeys.append(j)
            # Learn in the order they finish; the notebook is one file.
            analyse(j, setup)
            print(f"[{len(journeys):>3}/{len(plan)}] #{j.n} {j.scenario.short(setup['vehicles'])}")
            print_journey(j)
            in_a_row = in_a_row + 1 if j.status == "environment" else 0
            if in_a_row >= MAX_ENVIRONMENT_FAILURES_IN_A_ROW + 2:
                print(f"\n  {in_a_row} journeys in a row lost to the environment - "
                      f"stopping: the API is struggling, more calls only add load.\n")
                stop_code = 6
                break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    journeys.sort(key=lambda j: j.n)
    took = (datetime.now() - started).total_seconds()
    print(f"\n  fast mode: {len(journeys)} journeys in {took / 60:.1f} min "
          f"(browser mode would take about {len(plan) * 3:.0f} min)\n")
    return finish(journeys, facts, setup, folder, {}, stop_code)


# What the form's control names mean, for a sentence about a stuck Proceed.
FIELD_NAMES = {"tpPolicyExpDate": "TP Policy Expiry Date",
               "tpPolicyInsurer": "TP Policy Insurer",
               "policyExpDate": "Policy Expiry Date",
               "purchaseDate": "Registration Date",
               "manufactYear": "Manufacturing Year", "prevInsurer": "Previous Insurer",
               "ncb": "NCB", "claim": "Claim made", "ownerChange": "Owner changed"}


def _blocked(j: Journey, page, run_dir, screen: str, invalid: list,
             noun: str) -> Journey:
    """The form took every answer and still will not let Proceed be pressed."""
    s = j.scenario
    age = matrix.age_of(s.get("year"))
    fields = ", ".join(f"{FIELD_NAMES.get(name, name)}"
                       + (f" (it holds {value!r})" if value else "")
                       for name, value in invalid)
    j.status = "blocked"
    j.note = (f"{screen}: Proceed stays disabled for {s.get('policy')} on a "
              f"{age}-year-old {noun} - the form marks {fields} invalid and shows "
              f"no message")
    j.findings.append(quotechecks.Finding(
        quotechecks.DEFECT, "form-block", "FORM",
        f"the form offers {s.get('policy')} for a {age}-year-old {noun}, then "
        f"never lets Proceed be pressed: {fields} cannot be made valid",
        "A customer who picks this is stuck with a grey button and no reason. "
        "Either the choice should not be offered at this age, or the field's "
        "allowed dates are wrong.", (j.n,)))
    browser.capture(page, run_dir, f"journey-{j.n:02d}-blocked")
    return j


def run_journey(cfg, args, n: int, s: matrix.Scenario, setup) -> Journey:
    j = Journey(n, s)
    clock = time.monotonic()
    vehicle, policy, extra = matrix.choices(s, setup["vehicles"], setup["insurers"])
    product = PRODUCTS[setup["product"]]
    noun = matrix.RULES[product.name].noun

    with browser.browser_session(cfg, headed=not args.headless,
                                 slow_mo_ms=args.slow) as (context, run_dir):
        j.run_dir = run_dir
        watcher = health.Watcher.for_context(context)
        capture = QuoteCapture.attach(context, product.segment, args.only)
        page = None
        stage = "logging in"
        try:
            page = auth.log_in(context, cfg)
            safety.verify_environment(page, cfg)
            safety.allow("quote", cfg)

            stage = f"screen 1 ({noun})"
            VehicleDetailsPage(page).open(cfg.base_url, product.name).fill(vehicle).proceed()

            stage = "screen 2 (policy)"
            screen2 = PolicyDetailsPage(page).wait_until_loaded()
            if not screen2.offers_policy_type(policy.policy_type):
                j.status = "form"
                j.note = (f"the form does not offer {policy.policy_type} for a "
                          f"{vehicle.registration_year} {noun}")
                return j
            screen2.fill(policy)
            invalid = ui.proceed_blocked(page)
            if invalid:
                return _blocked(j, page, run_dir, "screen 2", invalid, noun)
            screen2.proceed()

            stage = "screen 3 (details)"
            screen3 = AdditionalDetailsPage(page).wait_until_loaded()
            if extra.ncb_percent and not screen3.ncb_offered():
                j.note = f"NCB {extra.ncb_percent} not offered - left as it was"
                extra.ncb_percent = None
            screen3.fill(extra)
            invalid = ui.proceed_blocked(page)
            if invalid:
                return _blocked(j, page, run_dir, "screen 3", invalid, noun)
            screen3.proceed()

            stage = "waiting for quotes"
            page.wait_for_url(lambda u: routes.on(u, "result"), timeout=60_000)
            finished = capture.wait_until_complete(page)
            if not capture.started:
                j.status = "environment"
                j.note = "the quote request never went out (QualifiedCompany)"
                return j

            j.offers = capture.best_offers()
            j.answers = capture.answers
            j.declines = capture.declines()
            j.silent = capture.silent()
            j.kinds = {d.insurer: _kind(d) for d in j.declines}
            j.sent = capture.sent
            j.quotation = capture.quotation_no
            j.qualified = capture.qualified_all
            j.cards = _read_cards(page, j.offers)
            j.status = "ok" if finished else "incomplete"
            if not finished:
                j.note = (f"gave up waiting; never answered: "
                          f"{', '.join(j.silent) or '?'}")
            browser.capture(page, run_dir, f"journey-{n:02d}")
            _save_evidence(run_dir, j, capture)
            return j

        except safety.SafetyRefusal as exc:
            raise StopRun(3, f"REFUSED (guard rail working as designed):\n  {exc}")
        except auth.LoginFailed as exc:
            raise StopRun(4, f"LOGIN FAILED (setup problem):\n  {exc}")
        except ui.LookupTimedOut as exc:
            field_name = next((f for f in BIKE_FIELDS if f"'{f}'" in str(exc)), "")
            j.status = "form" if field_name else "environment"
            j.note = (f"{field_name} never offered this vehicle's value" if field_name
                      else f"master-data lookup came back empty at {stage}")
            return j
        except ui.PageStuckLoading:
            j.status, j.note = "environment", f"the app stuck on 'Loading' at {stage}"
            return j
        except HARNESS_BUGS:
            # A bug in THIS tool repeats on every journey - stop and say so.
            traceback.print_exc()
            raise StopRun(1, "A BUG IN THE TEST TOOL (not the portal) - see the "
                             "traceback above. Stopping.")
        except Exception as exc:                       # noqa: BLE001
            diag = watcher.diagnose(page) if watcher else None
            if diag and diag.is_environment_problem:
                j.status, j.note = "environment", diag.explanation.split("\n")[0]
            else:
                j.status = "error"
                j.note = f"{stage}: {type(exc).__name__}: {str(exc).splitlines()[0][:110]}"
                if page:
                    browser.capture_failure(page, run_dir, f"journey-{n:02d}")
            return j
        finally:
            j.seconds = round(time.monotonic() - clock)


def _kind(d) -> str:
    if d.source in ("probus-rule", "http", "silent"):
        return d.source
    return Failure(d.insurer, d.reason).kind


def _read_cards(page, offers: dict) -> list:
    """Read the cards, and look again if a priced insurer has none yet -
    the page re-sorts after the last answer, and a too-early read would
    report a card as missing when it was only late."""
    quote_page = QuoteListPage(page)
    cards = quote_page.quotes()
    for _ in range(3):
        shown = {c.insurer.upper() for c in cards}
        if all(any(code in name or name in code for name in shown) for code in offers):
            break
        page.wait_for_timeout(4000)
        cards = quote_page.quotes()
    return cards


def _save_evidence(run_dir: Path, j: Journey, capture: QuoteCapture) -> None:
    try:
        (run_dir / "quote-capture.json").write_text(json.dumps({
            "scenario": dict(zip(matrix.DIMENSIONS, j.scenario.values)),
            "quotation": capture.quotation_no,
            "qualified_all": capture.qualified_all,
            # The fields the checks compare, not the whole request.
            "sent": {k: capture.sent.get(k) for k in (
                "IsThirdPartyOnly", "IsODOnly", "PrevPolicyExpiryStatus",
                "IsBreakingCase", "PreviousPolicyDetails", "PolicyType")}
            | {"VehicleDetails": {"RegistrationNumber": (
                capture.sent.get("VehicleDetails") or {}).get("RegistrationNumber")}},
            "planned": capture.planned,
            "answers": [a.as_dict() for a in capture.answers],
            "declines": [d.__dict__ for d in j.declines],
            "cards": [{"insurer": c.insurer, "premium": c.premium, "idv": c.idv}
                      for c in j.cards],
        }, indent=1), encoding="utf-8")
    except Exception:
        pass


def analyse(j: Journey, setup) -> None:
    if j.status not in ("ok", "incomplete"):
        return
    j.findings = quotechecks.check_journey(
        j.n, j.scenario, j.offers, j.answers, j.declines, j.sent, j.cards,
        setup["prev_codes"], setup.get("facts", {}).get(j.scenario.get("vehicle")))
    j.changes = quotenotes.record_journey(j.scenario, j.offers, j.declines,
                                          j.kinds, j.quotation)
    for line in j.changes:
        if "REFUSES now" in line:
            code = line.split(":", 1)[0]
            j.findings.append(quotechecks.Finding(
                quotechecks.LOOK, "regression", code,
                "quoted this exact journey last time, refuses it now",
                line, (j.n,)))


def print_journey(j: Journey) -> None:
    if j.status == "blocked":
        print(f"        FORM BLOCKED - {j.note}")
        print(f"        (a Bug: listed at the end; the rest of this run skips "
              f"the same choice)\n")
        return
    if j.status not in ("ok", "incomplete"):
        print(f"        NOT RUN ({j.status}): {j.note}\n")
        return
    asked = len(set(j.offers) | {d.insurer for d in j.declines})
    print(f"        {asked} insurers asked · {len(j.offers)} priced · "
          f"{len(j.declines)} said no · {j.seconds:.0f}s"
          + (f" · {j.quotation}" if j.quotation else ""))
    if j.offers:
        low = min(j.offers.values(), key=lambda a: a.premium)
        high = max(j.offers.values(), key=lambda a: a.premium)
        print(f"        cheapest {low.insurer} Rs {low.premium:,.0f} · dearest "
              f"{high.insurer} Rs {high.premium:,.0f}")
    if j.note:
        print(f"        note: {j.note}")
    defects = sum(1 for f in j.findings if f.severity == quotechecks.DEFECT)
    looks = len(j.findings) - defects
    if j.findings:
        print(f"        {defects} DEFECT · {looks} LOOK  (listed at the end)")
    for line in j.changes:
        print(f"        changed: {line}")
    print()


# ================================================================ the report

def _never_listed(ran: list[Journey], asked) -> list:
    """An asked insurer the portal left off its list on EVERY journey. Either
    it is switched off for this product, or its code here is not the code the
    portal uses - the codes the portal did use are listed to tell which."""
    out = []
    if not ran or not asked:
        return out
    named = {code for j in ran for code in j.qualified}
    others = sorted(named - set(asked))
    for code in asked:
        if code in named:
            continue
        out.append(quotechecks.Finding(
            quotechecks.LOOK, "never-listed", code,
            f"the portal never asked {code} in any of {len(ran)} journeys - no "
            f"plan and no reason",
            "Either it is switched off for this product on this site, or the "
            "portal uses another code for it. Companies the portal did name: "
            + (", ".join(others) or "none"),
            tuple(j.n for j in ran)))
    return out


def finish(journeys, facts, setup, folder, refused_bikes, stop_code) -> int:
    ran = [j for j in journeys if j.status in ("ok", "incomplete")]
    findings = [f for j in ran for f in j.findings]
    # A form that will not move is a finding too, though no quote came of it.
    findings += [f for j in journeys if j.status == "blocked" for f in j.findings]
    noun = matrix.RULES[setup["product"]].noun
    findings += [quotechecks.Finding(
        quotechecks.DEFECT, "hidden-vehicles", "FORM",
        f"no customer can quote these {noun}s on the form: {line}",
        "The form finds the make (then the model) by NAME and keeps the first "
        "match - pc/tw-dont-know-number onChngMake/onChngModel. Fix the master "
        "data (one name, one id) or look the item up by id.")
        for line in setup.get("hidden", [])]
    twin_findings, relations = quotechecks.compare(
        [(j.n, j.scenario, j.offers, j.declines) for j in ran], findings,
        setup["bands"])
    findings += twin_findings
    findings += _never_listed(ran, setup.get("only") or ())

    # Journeys with a finding are re-checked first next time.
    suspects = []
    for j in ran:
        worst = next((f for f in j.findings if f.severity == quotechecks.DEFECT), None)
        if worst:
            suspects.append((j.scenario, f"{worst.insurer} {worst.title}"[:110]))
    quotenotes.record_run(len(ran), findings, relations, suspects, refused_bikes)

    insurers = sorted({code for j in ran for code in
                       set(j.offers) | set(j.kinds) | set(j.silent)},
                      key=lambda c: (-sum(c in j.offers for j in ran), c))
    rules = [r for r in quotenotes.rules() if r[0] in insurers]
    changes = [f"#{j.n} {c}" for j in ran for c in j.changes]

    print("=" * 72)
    print(f"QUOTE MATRIX RESULTS ({PRODUCTS[setup['product']].title})   "
          f"{len(ran)} of {len(journeys)} journeys ran · {len(insurers)} insurers")
    print("=" * 72)
    if not ran:
        print("\n  No journey reached the quote list, so there is nothing to judge.")
        for f in findings:
            print(f"  Bug: {f.title}")
        if not findings:
            print("  That is the environment, not an insurer - see the notes above.")
        print()
        return stop_code or (1 if findings else 6)

    # --- the grid --------------------------------------------------------
    print(f"\n  {'':<14}" + "".join(f"{'#' + str(j.n):>4}" for j in ran))
    for code in insurers:
        print(f"  {code[:13]:<14}" + "".join(f"{matrixreport.cell(j, code):>4}"
                                             for j in ran))
    print("\n  " + "   ".join(f"{k} {v.split(' (')[0]}" for k, v in
                              matrixreport.LEGEND.items()))

    print("\n  THE JOURNEYS")
    for j in journeys:
        state = "" if j.status == "ok" else f"  [{j.status}]"
        print(f"  #{j.n:<3} {j.scenario.kind:<8} {j.scenario.short(setup['vehicles'])}"
              f"{state}")

    # --- findings ----------------------------------------------------------
    lines = matrixreport.grouped_lines(findings)
    defects = [l for l in lines if l[0] == quotechecks.DEFECT]
    looks = [l for l in lines if l[0] == quotechecks.LOOK]
    print(f"\n  DEFECTS - worth a ticket ({len(defects)})")
    for _, line, detail in defects or [("", "none", "")]:
        print(f"    {line}")
        if detail:
            print(f"      {detail[:140]}")
    print(f"\n  WORTH A LOOK ({len(looks)})")
    for _, line, detail in looks or [("", "none", "")]:
        print(f"    {line}")

    print("\n  TWIN RULES (checked by comparing two journeys)")
    for relation, text in matrix.RELATIONS.items():
        print(f"    {relations.get(relation, 'not checked'):<12} {text}")

    print("\n  WHAT THE EVIDENCE SAYS (all runs so far)")
    for code, text in rules or [("", "not enough evidence yet - rules appear "
                                     "after a few runs")]:
        print(f"    {code:<12} {text}")

    if changes:
        print("\n  CHANGED SINCE THE LAST TIME THESE JOURNEYS RAN")
        for line in changes:
            print(f"    {line}")

    slow = {}
    for j in ran:
        for a in j.answers:
            if a.seconds:
                slow.setdefault(a.insurer, []).append(a.seconds)
    if slow:
        avg = sorted(((sum(v) / len(v), k) for k, v in slow.items()), reverse=True)
        print("\n  SLOWEST INSURERS (average seconds to answer)")
        print("    " + " · ".join(f"{k} {s:.0f}s" for s, k in avg[:6]))

    print(f"\n  COVERAGE  this run {facts['this_run']} of {facts['wanted']} "
          f"{matrix.UNIT_WORDS[facts['depth']][1]}; with earlier runs "
          f"{facts['with_this_run']} of {facts['wanted']}.")

    paths = matrixreport.write(folder, journeys, insurers, findings, relations,
                               rules, changes, facts, setup["vehicles"],
                               setup["product"], setup.get("company_ids"))
    tail = "" if setup["product"] == "bike" else f" {setup['product']}"
    print(f"\n  Report   : {paths['html']}")
    print(f"  Excel    : {paths.get('xlsx') or paths['csv']}")
    print(f"  Notebook : {quotenotes.NOTES_FILE}  (python -m core.quotenotes{tail})")
    print(f"  Each journey's video, trace and screenshot: reports/run-*\n")

    if stop_code:
        return stop_code
    if defects:
        return 1
    if not any(j.offers for j in ran):
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
