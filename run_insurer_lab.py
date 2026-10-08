"""
Test ONE insurer's integration through every scenario - in minutes - and learn.

    python run_insurer_lab.py --insurer NATIONAL --product car
    python run_insurer_lab.py --insurer NATIONAL --product bike
    python run_insurer_lab.py --insurer NATIONAL --product car --plan     # what it would try
    python run_insurer_lab.py --insurer NATIONAL --product car --report   # what it has learned
    python run_insurer_lab.py --insurer NATIONAL --product car --recheck  # re-test every rule
    python run_insurer_lab.py --insurer NATIONAL --product bike --journeys 5  # 5 to payment

FOR WHOM
--------
The developer building (or fixing) one insurer's integration. The quote matrix
(run_quote_matrix.py) asks every insurer a few questions; this asks ONE insurer
every question: every vehicle type, policy type, expiry, NCB, claim, owner
change, every add-on alone and by vehicle age, every accessory amount, PA
cover, voluntary excess, discount and IDV - about 100-150 quotes.

HOW IT IS FAST
--------------
One browser journey, the first time only, captures a real request (the
"template") - with every OTHER insurer's call blocked. After that each
scenario is two direct API calls for this insurer alone, a few seconds each,
three at a time. Later sessions reuse the template: login (~3s on localhost)
and straight to the API.

HOW IT LEARNS (reports/insurer_lab/<INSURER>-<product>.json)
------------------------------------------------------------
Every refusal is read and pinned on the one thing that changed:
    "Electrical accessories should not be more than 10000"
        -> rule: electrical at most 10,000 (and the lab checks 10,000 passes)
    no message, but 25,000 refused and 5,000 accepted
        -> it searches 15,000, 10,000, 12,000 ... and finds the line itself
    Zero Dep accepted but missing from the quote at 7 years old
        -> rule: silently leaves out Zero Dep when the vehicle is 6+ years old
Next session obeys every rule - it sends 10,000, not 25,000 - and re-checks
each rule once a week in case the insurer changed. Our own code failing, an
outage or a bug is NEVER learned as a rule: it is reported, every time, until
it is fixed.

AFTER THE QUOTE: KYC, COMPANY SPECIFIC, PROPOSAL, PAYMENT
--------------------------------------------------------
Every quote that priced then goes on through the buy journey in the browser
(core/labjourney.py): KYC, the company-specific quotation at Preview, the
proposal at Proceed, and the payment page. Each stage is written down as
Success or Error, with the reason. It stops ON the payment page - nothing
there is touched and nothing is paid. A journey is about 3 minutes, so

    --journeys 5     take only the first 5 priced quotes on (a quick look)
    --journeys 0     quotes only - nothing is proposed

How far it may go is still the target's write_ceiling (config/settings.local
.json): "quote" - no journeys, "proposal" - up to Preview, "payment" - all the
way to the payment page.

THE EXCEL (results.xlsx, next to report.html)
---------------------------------------------
One row per scenario: company id, sub product, segment, the scenario, then
Quote / KYC / Company Specific / Proposal / Payment, a Status (Success or
Failure) and, for a Failure, the reason. Row 1 is the standard quote every
other row changes one thing from. A Summary sheet counts each stage and groups
the reasons.

EXIT CODES  0 all good · 1 a DEFECT · 4 login failed · 5 the standard quote
            never priced · 6 the environment is down
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import html
import json
import re
import sys
import threading
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from config import settings
from core import (auth, backend, browser, console, labchecks, labexcel,
                  labjourney, labrules, safety, vehiclecatalog)
from core.labcapture import JourneyTap
from core.labchecks import Outcome
from core.labclient import ApiRefused, ApiReplay, Template
from core.quotechecks import DEFECT, LOOK, Finding
from data import labscenarios as ls
from pages.additional_details import AdditionalChoice, AdditionalDetailsPage
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.vehicle_details import Vehicle, VehicleDetailsPage

TEMPLATES = labrules.LAB_DIR / "templates"
TEMPLATE_MAX_AGE_DAYS = 7
BOUNDARY_CALLS = 4          # most extra calls spent finding one limit

# Popular cars, preferred for the baseline when the catalogue has them.
CAR_BASELINES = (("MARUTI", "SWIFT"), ("HYUNDAI", "I20"), ("MARUTI", "BALENO"),
                 ("HONDA", "CITY"), ("TATA", "NEXON"), ("HYUNDAI", "VENUE"))


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser(description="Test one insurer through every scenario.")
    ap.add_argument("--insurer", required=True, help="e.g. NATIONAL, ICICI, ZUNO")
    ap.add_argument("--product", choices=("car", "bike"), default="car")
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--max", type=int, default=160, help="most quotes to ask for")
    ap.add_argument("--workers", type=int, default=3,
                    help="quotes in flight at once (be kind to the insurer's UAT)")
    ap.add_argument("--vehicles", type=int, default=12,
                    help="how many different MMVs to try, spread by size and fuel")
    ap.add_argument("--vehicle", default="",
                    help="baseline vehicle, e.g. 'MARUTI SWIFT' (default: a popular one)")
    ap.add_argument("--rto", default="GJ-01 Ahmedabad")
    ap.add_argument("--no-combos", action="store_true",
                    help="only change one thing at a time")
    ap.add_argument("--recheck", action="store_true",
                    help="re-test every learned rule now, not just the week-old ones")
    ap.add_argument("--refresh-template", action="store_true",
                    help="drive the browser again to capture a fresh request")
    ap.add_argument("--journeys", type=int, default=-1, metavar="N",
                    help="how many priced quotes to take on through KYC, company "
                         "specific, proposal and payment, ~3 minutes each "
                         "(default: all of them; 0 = quotes only)")
    ap.add_argument("--plan", action="store_true", help="show the plan and stop")
    ap.add_argument("--report", action="store_true", help="show what has been learned")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    insurer = args.insurer.upper()
    product = ls.PRODUCTS[args.product]
    book = labrules.Notebook(insurer, product.name)

    if args.report:
        print(notebook_report(book))
        return 0

    cfg = settings.load(args.target)
    print(f"\nINSURER LAB - {insurer} {product.name} on {cfg.name} ({cfg.base_url})")
    if args.journeys == 0 or not safety.permits("proposal", cfg):
        print("Quotes only, this insurer only. Nothing is bought or proposed.\n")
    else:
        print("Quotes for this insurer only, then each one that prices goes on to the "
              "payment page\n(KYC, company specific, proposal). Nothing is paid.\n")

    saved = load_template(product, args.rto, cfg.name)
    if args.plan:
        return show_plan(book, product, saved, args)

    # A busy API (still working through a slow insurer call from an earlier
    # run) answers again within a minute or so - so ask a few times before
    # calling it down.
    health = backend.check(cfg.api_url)
    for _ in range(4):
        if health:
            break
        print("  the app's API is not answering yet - asking again in 20s ...")
        time.sleep(20)
        health = backend.check(cfg.api_url)
    if not health:
        print(f"THE APP'S BACK END IS NOT ANSWERING - nothing was run\n"
              f"  {health.detail}\n")
        return 6

    clock = time.monotonic()
    try:
        session = open_session(cfg, product, insurer, args, saved)
    except auth.LoginFailed as exc:
        print(f"\nLOGIN FAILED:\n  {exc}\n")
        return 4
    except safety.SafetyRefusal as exc:
        print(f"\nREFUSED (guard rail):\n  {exc}\n")
        return 3
    if session is None:
        return 6
    print(f"  ready in {time.monotonic() - clock:.0f}s\n")
    lab = Lab(cfg, product, insurer, args, book, session)
    return lab.run()


# ================================================================ templates

def template_path(product: ls.Product, rto: str, target: str = "local") -> Path:
    # One file per target: a request captured on localhost names
    # localhost:53339 as its API, and must never be replayed for the test site.
    prefix = "" if target == "local" else f"{target}-"
    return TEMPLATES / f"{prefix}{product.name}-{rto.split()[0]}.json"


def load_template(product: ls.Product, rto: str, target: str = "local") -> dict:
    try:
        return json.loads(template_path(product, rto, target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def template_fresh(saved: dict, cfg=None) -> bool:
    # Captured against another API (a file copied over, or an older layout)?
    # Then it is not ours to replay, however new it is.
    if cfg is not None and cfg.api_url and saved.get("api_base"):
        if urlparse(saved["api_base"]).hostname != urlparse(cfg.api_url).hostname:
            return False
    try:
        when = datetime.strptime(saved.get("captured", ""), "%Y-%m-%d %H:%M")
        return (datetime.now() - when).days < TEMPLATE_MAX_AGE_DAYS
    except ValueError:
        return False


def save_template(product: ls.Product, rto: str, data: dict,
                  target: str = "local") -> None:
    path = template_path(product, rto, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


# ================================================================== session

def open_session(cfg, product, insurer, args, saved, trace: bool = True) -> dict | None:
    """
    Log in and get what the API calls need: the app's auth header, the
    template, the catalogue and the add-on list. The browser drives a journey
    only when there is no fresh template.
    """
    need_journey = args.refresh_template or not template_fresh(saved, cfg)
    catalog = vehiclecatalog.load(product.name)
    catalog_ok = catalog and all(r.get("variant_id") is not None
                                 for r in catalog.get("vehicles", [])[:5])
    # On localhost the login may visit the test site first, so only OUR API's
    # calls count. Elsewhere the API has its own host (testapi.*), so any.
    hosts = ({urlparse(u).hostname for u in (cfg.api_url, cfg.base_url) if u}
             if cfg.is_local else set())

    # trace=False for the quote sweep's fast mode: a ~100 MB trace of a
    # login nobody will open made closing the browser take minutes.
    with browser.browser_session(cfg, headed=not args.headless,
                                 trace=trace) as (context, run_dir):
        tap = JourneyTap(insurer, product.segment, hosts).attach(context)
        masters = vehiclecatalog.MasterListener.attach(context, hosts, product.name)
        page = auth.log_in(context, cfg)
        safety.verify_environment(page, cfg)
        safety.allow("quote", cfg)

        VehicleDetailsPage(page).open(cfg.base_url, product.name)
        waited = 0
        while (not masters.complete or not tap.headers) and waited < 25_000:
            page.wait_for_timeout(1000)
            waited += 1000
        if not catalog_ok and masters.raw.get("variants"):
            catalog = vehiclecatalog.build(masters.raw, cfg.name, product.name)
            vehiclecatalog.save(catalog, product.name)
            print(f"  learned {len(catalog['vehicles'])} {product.name} variants "
                  f"and {len(catalog['insurers'])} insurers from the app itself")
        if not tap.headers:
            print("  the app made no API call to borrow its login from - is the "
                  "API running?")
            return None

        if need_journey:
            baseline = pick_baseline(catalog, product, args)
            if baseline is None:
                print("  no vehicle to start from - the catalogue is empty")
                return None
            print(f"  capturing a real request: one journey with "
                  f"{baseline['make']} {baseline['model']} {baseline['variant']}, "
                  f"every other insurer blocked ...")
            drive_capture(page, cfg, product, baseline, args.rto, insurer,
                          catalog.get("insurers", []))
            waited = 0
            while not tap.done and waited < 120_000:
                page.wait_for_timeout(1000)
                waited += 1000
            template = tap.template(product.name,
                                    f"{baseline['make']} {baseline['model']} "
                                    f"{baseline['variant']} / {args.rto}",
                                    datetime.now().strftime("%Y-%m-%d %H:%M"))
            if template is None:
                shot = browser.capture_failure(page, run_dir, "lab-capture")
                print(f"  the journey never sent a quote request - see {shot}")
                return None
            saved = {**template.to_json(), "vehicle": baseline,
                     "insurers": catalog.get("insurers", [])}
            save_template(product, args.rto, saved, cfg.name)
            print(f"  template saved ({len(tap.blocked)} other insurers' calls "
                  f"blocked) - next sessions skip this step")
        else:
            print(f"  using the request captured {saved.get('captured')} "
                  f"({saved.get('label')})")

    template = Template.from_json(saved)
    return {"template": template, "headers": tap.headers, "catalog": catalog,
            "vehicle": saved.get("vehicle") or {},
            "insurers": saved.get("insurers") or catalog.get("insurers", [])}


def pick_baseline(catalog: dict, product: ls.Product, args) -> dict | None:
    rows = catalog.get("vehicles") or []
    if product.name == "bike" and not args.vehicle:
        p = vehiclecatalog.PROVEN
        hit = next((r for r in rows if r["make"] == p.make and r["model"] == p.model
                    and r["variant"] == p.variant), None)
        return hit or {"make": p.make, "model": p.model, "variant": p.variant,
                       "fuel": "PETROL", "band": "up to 150cc"}
    wanted = args.vehicle.upper().split()
    pool = [r for r in rows if r.get("fuel", "").upper() == "PETROL"] or rows
    if wanted:
        hit = [r for r in rows if all(w in f"{r['make']} {r['model']} {r['variant']}".upper()
                                      for w in wanted)]
        if hit:
            return sorted(hit, key=lambda r: (not r.get("top"), r["variant"]))[0]
    for make, model in CAR_BASELINES:
        hit = [r for r in pool if r["make"].upper().startswith(make)
               and model in r["model"].upper() and r.get("band") == "1000-1500cc"]
        if hit:
            return sorted(hit, key=lambda r: (not r.get("top"), r["variant"]))[0]
    return sorted(pool, key=lambda r: (not r.get("top"), r["make"]))[0] if pool else None


def drive_capture(page, cfg, product, row, rto, insurer, insurers) -> None:
    """The one browser journey: the baseline, through screens 1-3."""
    vehicle = Vehicle(rto.split()[0], rto, row["make"], row["model"], row["variant"],
                      str(date.today().year - ls.BASE_AGE))
    VehicleDetailsPage(page).fill(vehicle).proceed()
    choice = PolicyChoice()
    if insurer.startswith("BAJAJ"):
        # The previous insurer must not be the one under test.
        other = next((i for i in insurers if "ICICI" in (i.get("name") or "").upper()),
                     None) or next((i for i in insurers if not
                                    (i.get("name") or "").upper().startswith("BAJAJ")), None)
        if other:
            choice = replace(choice, previous_insurer=other["name"],
                             previous_insurer_search=other["name"].split()[0])
    PolicyDetailsPage(page).wait_until_loaded().fill(choice).proceed()
    AdditionalDetailsPage(page).wait_until_loaded().fill(AdditionalChoice()).proceed()


# ====================================================================== lab

class Lab:
    def __init__(self, cfg, product, insurer, args, book, session):
        self.cfg, self.product, self.insurer, self.args = cfg, product, insurer, args
        self.book: labrules.Notebook = book
        self.template: Template = session["template"]
        self.api = ApiReplay(session["headers"])
        self.catalog = session["catalog"]
        self.base_vehicle = session["vehicle"]
        self.prev_insurer = self._previous_insurer(session["insurers"])
        self.addons: dict[str, str] = {}
        self.outcomes: list[Outcome] = []
        self.new_rules: list[labrules.Rule] = []
        self.dropped_rules: list[str] = []
        self.skipped: list[tuple[ls.Scenario, labrules.Rule]] = []
        self.n = 0
        self._lock = threading.Lock()
        self.rows = {vehiclecatalog.key_of(r): r for r in self.catalog.get("vehicles", [])}
        self.bands = {k: r.get("band", "") for k, r in self.rows.items()}
        self.fuel = str(self.base_vehicle.get("fuel") or "PETROL")
        self.company = self._company(session["insurers"])
        self.insurer_codes = [i.get("code") for i in session["insurers"] if i.get("code")]
        self.folder = (labrules.LAB_DIR /
                       f"{insurer}-{product.name}-{datetime.now():%Y%m%d-%H%M%S}")
        # Why journey stages are "Not run" for EVERY row that has them - said
        # once, in the Excel's heading, instead of on each row.
        self.journey_why = ""
        # Portal bugs the buy journey proved (labjourney.Journeys.bugs).
        self.portal_bugs: list[str] = []
        # Set when our own server stopped answering and journeys had to stop.
        self.journey_stopped = ""

    # ----------------------------------------------------------------- helpers
    def _company(self, insurers: list) -> dict:
        """The insurer under test as the portal's own list has it (id, name)."""
        code = lambda i: str(i.get("code") or "").upper()
        return (next((i for i in insurers if code(i) == self.insurer), None)
                or next((i for i in insurers if code(i) and (code(i) in self.insurer
                                                             or self.insurer in code(i))),
                        None) or {})

    def _previous_insurer(self, insurers: list) -> dict:
        """Never the insurer under test: NATIONAL answers an empty list, with
        no message, when told it is the previous insurer (InsureBridge
        PrivateCarAgent.cs:4136)."""
        for want in ("BAJAJ", "ICICI", "HDFC", "TATA"):
            if want == self.insurer:
                continue
            row = next((i for i in insurers if want in (i.get("name") or "").upper()), None)
            if row:
                return {"Id": row.get("id"), "CompanyCode": row.get("code"),
                        "CShortName": row.get("short"), "Name": row.get("name")}
        prev = self.template.qualify.get("PrevPolicyInsurer") or {}
        return {k: prev.get(k) for k in ("Id", "CompanyCode", "CShortName", "Name")}

    def body(self, s: ls.Scenario, recalc: dict | None) -> dict:
        vehicle = self.rows.get(s.state.vehicle) if s.state.vehicle else None
        return ls.build_body(self.template.qualify, self.product, s.state,
                             prev_insurer=self.prev_insurer, vehicle=vehicle,
                             recalc=recalc if ls.is_recalc(s) else None)

    def ask(self, s: ls.Scenario, recalc: dict | None = None,
            count: bool = True) -> Outcome:
        n = 0
        if count:
            with self._lock:
                self.n += 1
                n = self.n
        try:
            reply = self.api.quote(self.template, self.body(s, recalc), self.insurer)
        except ApiRefused as exc:
            if exc.status != 401:
                raise
            self._refresh_login()
            reply = self.api.quote(self.template, self.body(s, recalc), self.insurer)
        o = Outcome(n, s, "priced", seconds=reply.seconds, quotation=reply.quotation,
                    items=reply.items, others=reply.others, not_listed=reply.not_listed,
                    sub_product=reply.sub_product)
        if reply.http_error:
            o.status, o.reason = "error", reply.http_error
            o.kind = "insurer-down" if "timed out" in reply.http_error.lower() else "our-defect"
        elif not reply.qualified:
            o.status, o.reason = "not-asked", reply.decline
            # Our API's own form validation ("Association name is required.")
            # means the REQUEST was incomplete - a fault in what was sent,
            # never a rule about the insurer.
            o.kind = ("request-refused" if reply.decline.startswith("request refused:")
                      else "probus-rule")
        else:
            o.best, o.raw = labchecks.best_of(reply.items, self.insurer)
            if o.best is None:
                failed = [i for i in reply.items if i.get("Status") != "Success"
                          or i.get("ErrorMessage")]
                if failed:
                    o.status = "refused"
                    o.reason = str(failed[0].get("ErrorMessage") or
                                   f"Status={failed[0].get('Status')}").strip()
                    o.kind = labrules.kind_of(o.reason)
                else:
                    o.status, o.kind = "empty", "unknown"
                    o.reason = "no price and no message (an empty answer)"
        return o

    def _refresh_login(self) -> None:
        print("  the login token expired mid-session - logging in again ...")
        with browser.browser_session(self.cfg, headed=False) as (context, _):
            hosts = ({urlparse(u).hostname for u in (self.cfg.api_url,
                                                     self.cfg.base_url) if u}
                     if self.cfg.is_local else set())
            tap = JourneyTap(self.insurer, self.product.segment, hosts).attach(context)
            page = auth.log_in(context, self.cfg)
            VehicleDetailsPage(page).open(self.cfg.base_url, self.product.name)
            waited = 0
            while not tap.headers and waited < 20_000:
                page.wait_for_timeout(1000)
                waited += 1000
        self.api = ApiReplay(tap.headers)

    def say(self, o: Outcome) -> None:
        word = {"priced": f"Rs {o.best.premium:,.0f}" if o.best else "",
                "refused": "REFUSED", "not-asked": "NOT ASKED", "empty": "EMPTY",
                "error": "ERROR", "skipped": "skipped"}[o.status]
        extra = "" if o.status == "priced" else f"  {o.reason[:90]}"
        if o.silent:
            extra += f"  (silently {o.silent})"
        print(f"  {o.n:>3}. {o.seconds:>4.0f}s  {word:<11} "
              f"{o.scenario.label(self.addons)[:62]:<62}{extra}")

    # -------------------------------------------------------------------- run
    def run(self) -> int:
        started = time.monotonic()
        try:
            self.addons = self._addon_list()
            saved = load_template(self.product, self.args.rto, self.cfg.name)
            if saved:
                save_template(self.product, self.args.rto, {**saved, "addons": self.addons},
                              self.cfg.name)
            print(f"  {len(self.addons)} add-ons on offer: "
                  f"{', '.join(list(self.addons.values())[:8])}"
                  f"{' ...' if len(self.addons) > 8 else ''}")
        except ApiRefused as exc:
            print(f"  could not read the add-on list ({exc}) - carrying on without add-ons")

        base = self._baseline()
        if base is None or base.best is None:
            return self.finish(base, started, stopped=True)

        recalc_seed = ls.recalc_details(base.items, base.quotation)
        idv_range = (int(base.best.idv_min or 0), int(base.best.idv_max or 0)) \
            if base.best.idv_min and base.best.idv_max else None
        vehicles = self._vehicle_spread()

        plan = ls.one_at_a_time(self.product, self.addons, self.fuel, vehicles, idv_range)[1:]
        if not self.args.no_combos:
            plan += ls.combinations(self.product, self.addons, self.fuel)
        plan = self._obey_rules(plan)[: max(0, self.args.max - 1)]
        print(f"\n  {len(plan)} scenarios to ask ({len(self.skipped)} skipped - a "
              f"learned rule says they will be refused)\n")

        self._run_batch([s for s in plan if not ls.is_recalc(s)], None)
        self._run_batch([s for s in plan if ls.is_recalc(s)], recalc_seed)
        self._retry_outages(recalc_seed)
        self._learn(recalc_seed)

        by_state = {}
        for o in self.outcomes:
            if o.status == "priced":
                by_state.setdefault(o.scenario.state, o)
        for o in self.outcomes:
            if o.status == "priced":
                bare = replace(o.scenario.state, covers=(), addons=(), idv=None, cpa=False)
                labchecks.judge(o, base, self.addons, self.insurer, by_state.get(bare))
        self._journeys(base)
        return self.finish(base, started)

    def _addon_list(self) -> dict[str, str]:
        answer = self.api.get(self.template.api_base + "AddOn")
        rows = answer.get("Response") or []
        return {str(r.get("Id")): str(r.get("Name") or r.get("Id")) for r in rows
                if r.get("Id") is not None}

    def _baseline(self) -> Outcome | None:
        """Ask the standard quote; if it does not price, change the likely cause."""
        print("  standard quote:")
        base = self.ask(ls.Scenario(ls.State(), (), phase="baseline"))
        self.say(base)
        # A timeout or outage says nothing about the quote: the local API and
        # the insurer's UAT both have slow spells. Ask again before giving up.
        for _ in range(2):
            if base.status != "error":
                break
            print("  that looks like an outage, not an answer - asking again in 15s ...")
            time.sleep(15)
            n = base.n
            base = self.ask(ls.Scenario(ls.State(), (), phase="baseline"), count=False)
            base.n = n                  # the same row, asked again
            self.say(base)
        self.outcomes.append(base)
        if base.best:
            return base
        # "MMV is not mapped" or a refusal of the vehicle itself: another car.
        # Not even LISTED (no plan, no decline) is different: that is the
        # insurer's plan not being set up here, and no vehicle changes it -
        # but trying three costs under a second, and proves it.
        if base.status in ("not-asked", "refused", "empty"):
            for row in self._vehicle_spread()[:3]:
                trial = self.ask(ls.Scenario(replace(ls.State(), vehicle=row["key"]), (),
                                             why="standard quote on another vehicle",
                                             phase="baseline"))
                self.say(trial)
                self.outcomes.append(trial)
                if trial.best:
                    print(f"  standard quote moved to {row['make']} {row['model']} - "
                          f"the first vehicle was refused: {base.reason[:80]}")
                    self.base_vehicle = row
                    self.fuel = str(row.get("fuel") or self.fuel)
                    self.template = replace(self.template, qualify=ls.build_body(
                        self.template.qualify, self.product, ls.State(),
                        prev_insurer=self.prev_insurer, vehicle=row))
                    trial.scenario = ls.Scenario(ls.State(), (), phase="baseline")
                    return trial
        return base

    def _vehicle_spread(self) -> list[dict]:
        """
        Different MMVs: one per (size band, fuel) in turn, popular first, a
        new make whenever there is one. Vehicles a rule says our portal will
        not send (e.g. "MMV is not mapped.") are left out until their weekly
        re-check, so each session widens the coverage instead of repeating it.
        """
        base_key = (vehiclecatalog.key_of(self.base_vehicle)
                    if self.base_vehicle.get("make") else "")
        groups: dict[tuple, list] = {}
        for r in self.catalog.get("vehicles", []):
            if r.get("variant_id") is None:
                continue
            key = vehiclecatalog.key_of(r)
            rule = self.book.blocks("vehicle", key, {})
            if key == base_key or (rule and not rule.due_for_recheck()):
                continue
            groups.setdefault((r.get("band") or "?", (r.get("fuel") or "?").upper()),
                              []).append(dict(r, key=key))
        for g in groups.values():
            g.sort(key=lambda r: (not r.get("top"), r["make"], r["model"], r["variant"]))
        out, makes = [], set()
        while len(out) < self.args.vehicles and any(groups.values()):
            for key in sorted(groups):
                g = groups[key]
                if not g:
                    continue
                pick = next((r for r in g if r["make"] not in makes), g[0])
                g.remove(pick)
                out.append(pick)
                makes.add(pick["make"])
                if len(out) >= self.args.vehicles:
                    break
        return out

    def _obey_rules(self, plan: list[ls.Scenario]) -> list[ls.Scenario]:
        """Drop or adjust scenarios a learned rule says will be refused."""
        out = []
        seen = set()
        for s in plan:
            if s.key in seen:
                continue
            seen.add(s.key)
            ctx = s.state.context()
            # "All add-ons" means all the add-ons this insurer offers HERE:
            # leave out the ones a rule says it refuses or drops at this age.
            if len(s.state.addons) > 1:
                keep = tuple(a for a in s.state.addons
                             if not self.book.blocks("addon", a, ctx))
                if len(keep) < len(s.state.addons):
                    if not keep:
                        continue
                    s = replace(s, state=replace(s.state, addons=keep))
            ctx["idv"] = s.state.idv
            blocked = None
            for dim, value in s.changes:
                if dim == "year":
                    continue
                rule = self.book.blocks(dim, value, ctx)
                if rule:
                    blocked = rule
                    break
            if blocked and (self.args.recheck or blocked.due_for_recheck()):
                out.append(replace(s, phase="recheck",
                                   why=f"re-check rule: {blocked.sentence()}"))
            elif blocked:
                self.skipped.append((s, blocked))
            else:
                out.append(s)
        # Learned limits are the best values to test: exactly on the line.
        for dim in labrules.NUMERIC:
            for limit in self.book.boundary_values(dim):
                cover = dim if dim in ls.COVERS or dim in ls.DISCOUNTS else None
                if cover and ls.offered(self.product, ls.State(), dim, self.fuel):
                    state = replace(ls.State(), covers=((dim, int(limit)),))
                    on_line = ls.Scenario(state, ((dim, int(limit)),),
                                          "exactly on the learned limit - must pass",
                                          phase="boundary")
                    out = [x for x in out if x.key != on_line.key]
                    out.insert(0, on_line)
        return out

    def _run_batch(self, scenarios: list[ls.Scenario], recalc_seed: dict | None) -> None:
        if not scenarios:
            return
        if recalc_seed:
            print("  add-ons, covers, discounts and IDV (as a Re-Calculate, like the "
                  "filter on the quote page):")
            # Each worker re-calculates its OWN quotation, so parallel
            # scenarios never overwrite each other's saved quote.
            seeds = [recalc_seed]
            for _ in range(max(0, self.args.workers - 1)):
                extra = self.ask(ls.Scenario(ls.State(), (), phase="baseline",
                                             why="a quotation for another worker"),
                                 count=False)
                if extra.best:
                    seeds.append(ls.recalc_details(extra.items, extra.quotation))
            lanes = [scenarios[i::len(seeds)] for i in range(len(seeds))]

            def lane(i):
                return [self.ask(s, seeds[i]) for s in lanes[i]]
            with futures.ThreadPoolExecutor(len(seeds)) as pool:
                done = [o for results in pool.map(lane, range(len(seeds)))
                        for o in results]
            for o in sorted(done, key=lambda o: o.n):
                self.outcomes.append(o)
                self.say(o)
        else:
            print("  vehicle, policy, previous policy, NCB, claim, customer:")
            with futures.ThreadPoolExecutor(max(1, self.args.workers)) as pool:
                for o in pool.map(self.ask, scenarios):
                    self.outcomes.append(o)
                    self.say(o)

    def _retry_outages(self, recalc_seed) -> None:
        down = [o for o in self.outcomes if o.kind == "insurer-down"]
        if not down:
            return
        print(f"\n  {len(down)} answered like an outage - asking once more ...")
        for o in down:
            again = self.ask(o.scenario, recalc_seed)
            self.say(again)
            if again.status != "error":
                self.outcomes[self.outcomes.index(o)] = again

    # ------------------------------------------------------------------ learn
    def _learn(self, recalc_seed) -> None:
        """Turn refusals into rules, find numeric limits, spot age limits."""
        refused = [o for o in self.outcomes if o.scenario.changes and o.status in
                   ("refused", "not-asked") and o.kind in ("company-validation",
                                                            "probus-rule")]
        # A rule re-checked and now accepted: the insurer changed. Forget it.
        for o in self.outcomes:
            if o.scenario.phase == "recheck" and o.status == "priced":
                ctx = o.scenario.state.context()
                for dim, value in o.scenario.changes:
                    rule = self.book.blocks(dim, value, ctx)
                    if rule:
                        self.book.drop_rule(rule.key)
                        self.dropped_rules.append(rule.sentence())
            elif o.scenario.phase == "recheck":
                for dim, value in o.scenario.changes:
                    rule = self.book.blocks(dim, value, o.scenario.state.context())
                    if rule:
                        self.book.touch_rule(rule.key)

        single = [o for o in refused if len(o.scenario.changes) == 1]
        for o in single:
            dim, value = o.scenario.changes[0]
            if dim in ("year", "baseline") or dim in labrules.NUMERIC:
                continue            # ages and amounts are handled below
            rule = labrules.rule_from_refusal(dim, value, o.reason)
            rule.source = "our portal" if o.kind == "probus-rule" else "insurer"
            if rule.kind == "refused":
                rule.when = {}
                if dim == "vehicle":
                    row = self.rows.get(str(value), {})
                    rule.label = f"{row.get('make', '')} {row.get('model', '')} " \
                                 f"{row.get('variant', '')}".strip() or str(value)
                elif dim == "addon":
                    rule.label = self.addons.get(str(value), str(value))
            stored, new = self.book.add_rule(rule)
            if new:
                self.new_rules.append(stored)

        # Amounts: one rule per cover, from the stated limit or a search.
        for dim in sorted(labrules.NUMERIC - {"idv"}):
            rows = sorted(((o.scenario.value, o) for o in self.outcomes
                           if len(o.scenario.changes) == 1 and o.scenario.dimension == dim
                           and isinstance(o.scenario.value, (int, float))
                           and not isinstance(o.scenario.value, bool)),
                          key=lambda row: (row[0], row[1].n))
            passed = [v for v, o in rows if o.status == "priced"]
            failed = [(v, o) for v, o in rows if o.status in ("refused", "not-asked")
                      and o.kind in ("company-validation", "probus-rule")]
            if not failed:
                continue
            first_fail, o = failed[0]
            low = max([v for v in passed if v < first_fail], default=0)
            stated = labrules.read_limit(o.reason)
            if stated.high is not None and stated.high < first_fail:
                self._confirm_limit(dim, stated.high, low, o.reason, recalc_seed)
            else:
                self._search_limit(dim, low, first_fail, o.reason, recalc_seed)

        # Add-ons by age: refused or silently left out from some age on.
        for addon_id in self.addons:
            self._age_limit(addon_id)
        self.book.save()

    def _one(self, dim, amount, recalc_seed, why) -> Outcome:
        state = replace(ls.State(), covers=((dim, amount),))
        o = self.ask(ls.Scenario(state, ((dim, amount),), why, phase="boundary"),
                     recalc_seed)
        self.outcomes.append(o)
        self.say(o)
        return o

    def _confirm_limit(self, dim, limit, low, message, recalc_seed) -> None:
        """The message states the limit: check it is true on both sides."""
        print(f"\n  checking the stated limit for {ls.NICE[dim]}: {limit:,.0f}")
        at = self._one(dim, int(limit), recalc_seed, "exactly on the stated limit")
        if at.status != "priced":
            # The limit itself is refused - the real line is lower.
            self._search_limit(dim, low, int(limit), at.reason or message, recalc_seed)
            return
        over = self._one(dim, int(limit) + 1, recalc_seed, "one rupee over the limit")
        rule, new = self.book.add_rule(labrules.Rule(
            dim, "max", high=float(limit), message=message,
            confirmed=over.status in ("refused", "not-asked")))
        if new:
            self.new_rules.append(rule)

    def _search_limit(self, dim, low, high, message, recalc_seed) -> None:
        print(f"\n  {ls.NICE[dim]}: {low:,.0f} passed, {high:,.0f} refused, with no "
              f"number in the message - finding the line ...")
        for _ in range(BOUNDARY_CALLS):
            if high - low <= max(500, high * 0.05):
                break
            mid = int(labrules.nice(low, high))
            o = self._one(dim, mid, recalc_seed, "halving the gap to find the limit")
            if o.status == "priced":
                low = mid
            elif o.status == "refused" and o.kind == "company-validation":
                high = mid
                message = o.reason
            else:
                break
        rule, new = self.book.add_rule(labrules.Rule(
            dim, "max", high=float(low), message=f"{message} (found: {low:,.0f} "
                                                  f"passes, {high:,.0f} refused)"))
        if new:
            self.new_rules.append(rule)

    def _age_limit(self, addon_id: str) -> None:
        name = self.addons.get(addon_id, addon_id)
        by_age: dict[int, str] = {}
        for o in self.outcomes:
            dims = dict(o.scenario.changes)
            if dims.get("addon") != addon_id or set(dims) - {"addon", "year"}:
                continue
            if o.status == "priced":
                applied = labchecks.addon_applied(o.raw, name)
                by_age[o.scenario.state.age] = "dropped" if applied is False else "ok"
            elif o.status == "refused" and o.kind == "company-validation":
                by_age[o.scenario.state.age] = "refused"
        bad = sorted(a for a, v in by_age.items() if v != "ok")
        good = sorted(a for a, v in by_age.items() if v == "ok")
        if not bad or not good:
            return
        start = min(bad)
        if any(a >= start for a in good):
            return                      # not a clean age limit
        kind = "silent" if all(by_age[a] == "dropped" for a in bad) else "refused"
        rule, new = self.book.add_rule(labrules.Rule(
            "addon", kind, value=addon_id, when={"age_min": start}, label=name,
            message=f"{name}: fine up to {max(good)} years, "
                    f"{'left out without a word' if kind == 'silent' else 'refused'} "
                    f"from {start} years"))
        if new:
            self.new_rules.append(rule)

    # --------------------------------------------------------------- journeys
    def _journeys(self, base: Outcome) -> None:
        """
        Take each priced quote on through KYC, company specific, proposal and
        payment - one browser journey each, in row order (core/labjourney.py).
        """
        priced = sorted((o for o in self.outcomes if o.status == "priced"),
                        key=lambda o: o.n)
        limit = getattr(self.args, "journeys", 0)
        self.journey_why = self._why_no_journeys(limit)
        if self.journey_why or not priced:
            for o in priced:
                labjourney.mark_not_run(o.stages, self.journey_why)
            if self.journey_why and limit != 0:
                print(f"\n  BUY JOURNEY NOT RUN: {self.journey_why}")
            return
        todo = priced if limit < 0 else self._spread(priced, limit)
        if len(todo) < len(priced):
            self.journey_why = (f"{len(todo)} of the {len(priced)} priced quotes were "
                                f"taken on, one of each kind first (--journeys {limit})")
            for o in priced[len(todo):]:
                labjourney.mark_not_run(o.stages, self.journey_why)

        print(f"\n  BUY JOURNEY: {len(todo)} priced quote(s) go on through KYC, "
              f"company specific, proposal and payment")
        print(f"  One browser journey each, about {len(todo) * labjourney.MINUTES_EACH} "
              f"minutes in all. It stops ON the payment page - nothing is paid.")
        print("  Do not click in the browser while it runs. The Excel is saved after "
              "every journey.\n")
        runner = labjourney.Journeys(self.cfg, self.product, self.insurer,
                                     self.insurer_codes, self.args.rto, self.api,
                                     headed=not self.args.headless)
        try:
            with runner.open():
                for i, o in enumerate(todo, start=1):
                    print(f"  {o.n:>3}. journey {i}/{len(todo)}  "
                          f"{o.scenario.label(self.addons)[:80]}")
                    down = self._wait_for_environment()
                    if down:
                        self._stop_journeys(todo[i - 1:], down)
                        break
                    self.api = runner.api           # the browser's live login
                    quotation, request, why_not = self._journey_request(o)
                    if why_not:
                        labjourney.mark_not_run(o.stages, why_not)
                        print(f"       not run: {why_not}")
                        continue
                    j = runner.drive(o.n, quotation, request,
                                     o.sub_product or self.product.sub_product)
                    failed = labjourney.verdict(j.stages)[0] != labjourney.SUCCESS
                    if failed and self._environment_down():
                        # Our own server stopped answering mid-journey: that
                        # says nothing about the insurer. Wait, then redo it.
                        print("       that failure came from our own server not "
                              "answering, not from the insurer")
                        down = self._wait_for_environment()
                        if down:
                            self._keep_journey(o, j)
                            o.journey_notes.append(f"failed while {down}")
                            self._stop_journeys(todo[i:], down)
                            self._save_progress(base)
                            break
                        print("       driving the same quote again ...")
                        first = j
                        j = runner.drive(o.n, quotation, request,
                                         o.sub_product or self.product.sub_product)
                        j.notes.insert(0, f"first try was lost to our server not "
                                          f"answering ({labjourney.verdict(first.stages)[1][:80]})")
                    self._keep_journey(o, j)
                    self._save_progress(base)
        except auth.LoginFailed as exc:
            print(f"\n  LOGIN FAILED for the buy journey - the rest are not run:\n  {exc}")
        except safety.SafetyRefusal as exc:
            print(f"\n  REFUSED (guard rail) - the buy journey stopped:\n  {exc}")
        finally:
            self.portal_bugs = list(getattr(runner, "bugs", []))
        for o in todo:
            labjourney.mark_not_run(o.stages, "the buy journey stopped before this one")

    def _environment_down(self) -> str:
        """Which of the portal's own APIs is not answering right now, or ''."""
        for name, url in (("quote API", self.cfg.api_url),
                          ("buy API", getattr(self.cfg, "buy_url", ""))):
            if url and not backend.check(url):
                return f"the portal's {name} ({url}) is not answering"
        return ""

    def _wait_for_environment(self, limit_s: int = 300) -> str:
        """'' once both APIs answer; otherwise why not, after up to limit_s."""
        down = self._environment_down()
        waited = 0
        if down:
            print(f"       {down} - waiting up to {limit_s // 60} minutes for it to "
                  f"recover ...")
        while down and waited < limit_s:
            time.sleep(20)
            waited += 20
            down = self._environment_down()
        if waited and not down:
            print(f"       it is answering again (after {waited}s)")
        return down

    def _stop_journeys(self, rest: list, down: str) -> None:
        why = (f"not run - {down} (it did not recover in 5 minutes; restart it in "
               f"Visual Studio and run again)")
        print(f"\n  BUY JOURNEY STOPPED: {down}.")
        print("  The rows left say 'Not run' - nothing is guessed. Restart that API "
              "in Visual Studio and run the same command again.\n")
        self.journey_stopped = down
        for o in rest:
            labjourney.mark_not_run(o.stages, why)

    @staticmethod
    def _spread(priced: list, limit: int) -> list:
        """
        The `limit` journeys that cover the most ground: the standard quote,
        then one of each KIND of change (policy type, NCB, claim, add-on ...)
        in turn - not the first rows, which are all vehicle ages.
        """
        groups: dict[str, list] = {}
        for o in priced:
            kind = "+".join(d for d, _ in o.scenario.changes) or "standard"
            groups.setdefault(kind, []).append(o)
        picked: list = []
        while len(picked) < limit and any(groups.values()):
            for kind in list(groups):
                if groups[kind] and len(picked) < limit:
                    picked.append(groups[kind].pop(0))
        return sorted(picked, key=lambda o: o.n)

    def _why_no_journeys(self, limit: int) -> str:
        if limit == 0:
            return "quotes only (--journeys 0)"
        if not safety.permits("proposal", self.cfg):
            return (f"quotes only - write_ceiling is '{self.cfg.write_ceiling}' "
                    f"(\"proposal\" or \"payment\" in config/settings.local.json "
                    f"lets it go on)")
        if not getattr(self.cfg, "buy_url", ""):
            return f"quotes only - no buy_url for the '{self.cfg.name}' target"
        return ""

    def _journey_request(self, o: Outcome) -> tuple[str, dict, str]:
        """
        (quotation, request, why not) to open this row's quote in the browser.

        A plain scenario has its own quotation. An add-on, cover or IDV one is
        a Re-Calculate of a shared quotation, so it is asked again on a fresh
        one of its own first - two journeys must never buy the same quotation.
        """
        s = o.scenario
        if not ls.is_recalc(s):
            if not o.quotation:
                return "", {}, "the quote has no quotation number"
            return o.quotation, self.body(s, None), ""
        try:
            fresh = self.ask(ls.Scenario(ls.State(), (), phase="baseline",
                                         why="a quotation for the journey"), count=False)
            if not fresh.best:
                return "", {}, f"a fresh quotation for it did not price ({fresh.reason})"
            seed = ls.recalc_details(fresh.items, fresh.quotation)
            again = self.ask(s, seed, count=False)
        except Exception as exc:
            return "", {}, f"could not ask it again on a fresh quotation ({exc})"
        if not again.best:
            return "", {}, f"asked again on a fresh quotation, it did not price ({again.reason})"
        return fresh.quotation, self.body(s, seed), ""

    def _keep_journey(self, o: Outcome, j: labjourney.Journey) -> None:
        o.stages.update(j.stages)
        o.proposal_no = j.proposal_no
        o.journey_seconds = j.seconds
        o.journey_notes = list(j.notes)
        if j.premium_on_screen and o.best and abs(j.premium_on_screen - o.best.premium) > 5:
            o.journey_notes.append(f"the quote page showed Rs {j.premium_on_screen:,}, "
                                   f"the API quote was Rs {o.best.premium:,.0f}")
        # The amount the customer is finally asked to pay must be the price
        # they were quoted.
        if j.payment_amount and o.best:
            if abs(j.payment_amount - o.best.premium) > 2:
                o.journey_notes.append(
                    f"Bug: the payment page asks Rs {j.payment_amount:,.2f} but the quote "
                    f"was Rs {o.best.premium:,.2f}")
            else:
                o.journey_notes.append(f"payment page asks Rs {j.payment_amount:,.2f} - "
                                       f"the quoted price")
        cells = "  ".join(f"{labjourney.NAMES[k]}: {s.result}" for k, s in j.stages.items())
        print(f"       {cells}  ({j.seconds:.0f}s)")
        status, reason = labjourney.verdict(j.stages)
        if status != labjourney.SUCCESS:
            print(f"       {reason[:160]}")
            if j.evidence:
                print(f"       evidence: {j.evidence}")

    def _save_progress(self, base: Outcome) -> None:
        """The Excel after every journey, so a long run stopped early keeps its rows."""
        try:
            write_files(self, base, [], [])
        except Exception as exc:
            print(f"       (could not update the Excel just now: {exc})")

    # ----------------------------------------------------------------- report
    def finish(self, base: Outcome | None, started: float, stopped: bool = False) -> int:
        findings = [f for o in self.outcomes for f in o.findings]
        if not stopped:
            findings += labchecks.across(self.outcomes, self.insurer, self.bands)
        for o in self.outcomes:
            if o.status == "priced" or not o.scenario.changes and o is not base:
                continue
            if o.kind == "our-defect":
                findings.append(Finding(DEFECT, "our-code", self.insurer,
                    f"{o.scenario.label(self.addons)}: our code failed - {o.reason[:110]}",
                    journeys=(o.n,)))
            elif o.status == "empty":
                findings.append(Finding(LOOK, "empty", self.insurer,
                    f"{o.scenario.label(self.addons)}: no price and no message",
                    "The customer sees nothing for this insurer and is not told why.",
                    (o.n,)))
            elif o.kind == "request-refused":
                findings.append(Finding(LOOK, "request-refused", self.insurer,
                    f"{o.scenario.label(self.addons)}: our API refused the request "
                    f"itself - {o.reason[:100]}",
                    "A field the screen would have filled is missing. A gap in "
                    "the lab's request, or a field the API requires silently.",
                    (o.n,)))
            elif o.kind == "unknown" and o.status == "refused":
                findings.append(Finding(LOOK, "no-reason", self.insurer,
                    f"{o.scenario.label(self.addons)}: refused without a reason "
                    f"({o.reason})", journeys=(o.n,)))
        for o in self.outcomes:
            failed = next(((k, s) for k, s in o.stages.items()
                           if s.result == labjourney.ERROR), None)
            if failed:
                findings.append(Finding(LOOK, "journey", self.insurer,
                    f"{o.scenario.label(self.addons)}: quoted, but "
                    f"{labjourney.NAMES[failed[0]]} failed - {failed[1].reason[:110]}",
                    "The customer is shown a price and cannot buy it.", (o.n,)))
        first = min((o.n for o in self.outcomes if o.journey_seconds), default=0)
        for bug in self.portal_bugs:
            findings.append(Finding(DEFECT, "portal", self.insurer,
                bug.split(". ", 1)[0], bug, (first,)))
        changes = self._compare_with_last_session()
        self.book.add_session({
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "quotes": len(self.outcomes),
            "priced": sum(o.status == "priced" for o in self.outcomes),
            "defects": sum(f.severity == DEFECT for f in findings),
            "new_rules": len(self.new_rules),
            "journeys": sum(bool(o.journey_seconds) for o in self.outcomes),
            "to_payment": sum(o.stages.get("payment", labjourney.Stage()).result
                              == labjourney.SUCCESS for o in self.outcomes)})
        for o in self.outcomes:
            self.book.remember(o.scenario.key, {
                "status": o.status, "premium": o.best.premium if o.best else None,
                "reason": o.reason[:160]})
        self.book.save()
        paths = write_files(self, base, findings, changes)
        print_report(self, base, findings, changes, started, paths, stopped)
        if stopped:
            return 5
        return 1 if any(f.severity == DEFECT for f in findings) else 0

    def _compare_with_last_session(self) -> list[str]:
        out = []
        for o in self.outcomes:
            before = self.book.last(o.scenario.key)
            if not before or before.get("last") == date.today().isoformat():
                continue
            if before.get("status") == "priced" and o.status != "priced":
                out.append(f"{o.scenario.label(self.addons)}: priced on "
                           f"{before['last']}, now {o.status.upper()} - {o.reason[:70]}")
            elif before.get("status") != "priced" and o.status == "priced":
                out.append(f"{o.scenario.label(self.addons)}: was {before.get('status')}, "
                           f"now priced (Rs {o.best.premium:,.0f})")
            elif o.best and before.get("premium"):
                old = before["premium"]
                if abs(o.best.premium - old) / old > 0.25:
                    out.append(f"{o.scenario.label(self.addons)}: price moved "
                               f"Rs {old:,.0f} -> Rs {o.best.premium:,.0f}")
        return out


# =================================================================== output

def print_report(lab: Lab, base, findings, changes, started, paths, stopped) -> None:
    outs = lab.outcomes
    priced = sum(o.status == "priced" for o in outs)
    secs = time.monotonic() - started
    print("\n" + "=" * 74)
    print(f"INSURER LAB  {lab.insurer} {lab.product.name}   {len(outs)} quotes in "
          f"{secs / 60:.1f} min ({secs / max(1, len(outs)):.1f}s each) · {priced} priced")
    print("=" * 74)
    if base is not None:
        if base.best:
            b = base.best
            print(f"\n  STANDARD QUOTE  Rs {b.premium:,.0f}  (OD {b.od or 0:,.0f} · TP "
                  f"{b.tp or 0:,.0f} · IDV {b.idv or 0:,.0f} · NCB {b.ncb_percent or 0:.0f}%)"
                  f"  quotation {base.quotation}")
        else:
            print(f"\n  THE STANDARD QUOTE DID NOT PRICE: {base.status} - {base.reason}")
    baselines = [o for o in outs if not o.scenario.changes]
    if stopped and baselines and all(o.not_listed for o in baselines):
        asked = sorted({c for o in baselines for c in o.others})
        print(f"\n  WHY: our server (InsureBridge QualifiedCompany) did not put "
              f"{lab.insurer} on its list")
        print(f"  for ANY of the {len(baselines)} vehicles tried - and did not decline "
              f"it either.")
        print(f"  It would only ask: {', '.join(asked) or 'nobody'}.")
        print(f"\n  That means {lab.insurer} {lab.product.name} has no ACTIVE PLAN for "
              f"our broker in this")
        print("  environment's database (the plan list comes from the stored "
              "procedure")
        print("  sp_motor_show_plans). It is set-up data, not a code bug and not "
              "the test.")
        print("\n  To fix, ask whoever owns the local database to check, for "
              f"{lab.insurer} {lab.product.name}:")
        print("    1. the plan exists and is active")
        print("    2. it is mapped to our broker (PIBL) and to this product")
        print(f"    3. the RTO ({lab.args.rto}) and the vehicle make are mapped")
        print("  Then run the same command again - nothing else needs changing.")
        if asked:
            print(f"\n  Meanwhile the lab works for any insurer on that list, e.g.:")
            print(f"    python run_insurer_lab.py --insurer {asked[0]} "
                  f"--product {lab.product.name}\n")
    elif stopped:
        print("\n  Nothing else can be compared without a standard quote that prices. Fix the")
        print("  reason above (or pick another vehicle with --vehicle) and run again.\n")

    groups: dict[str, list[Outcome]] = {}
    combos = [o for o in outs if len(o.scenario.changes) > 1]
    for o in outs:
        if len(o.scenario.changes) == 1:
            groups.setdefault(o.scenario.dimension, []).append(o)
    if groups:
        print("\n  WHAT EACH CHOICE DID (one change at a time, against the standard quote)")
        base_p = base.best.premium if base is not None and base.best else None
        for dim, rows in groups.items():
            cells = []
            for o in rows[:12]:
                v = o.scenario.label(lab.addons).split(" = ", 1)[-1].split(" + ")[0][:18]
                if o.status == "priced" and base_p:
                    d = o.best.premium - base_p
                    cells.append(f"{v} {'+' if d >= 0 else ''}{d:,.0f}"
                                 + (" (silent)" if o.silent else ""))
                else:
                    cells.append(f"{v} {o.status.upper()}")
            print(f"    {ls.NICE.get(dim, dim):<24} " + " | ".join(cells)[:180])
    if combos:
        print(f"    {'combinations':<24} {len(combos)} asked: "
              f"{sum(o.status == 'priced' for o in combos)} priced, "
              f"{sum(o.status != 'priced' for o in combos)} not, "
              f"{sum(bool(o.silent) for o in combos)} silently incomplete")

    ran = [o for o in outs if o.journey_seconds]
    if ran:
        print(f"\n  BUY JOURNEY ({len(ran)} quotes taken on in the browser)")
        for key in labjourney.JOURNEY:
            got = [o.stages.get(key, labjourney.Stage()).result for o in ran]
            print(f"    {labjourney.NAMES[key]:<18} {got.count(labjourney.SUCCESS):>3} "
                  f"success · {got.count(labjourney.ERROR):>3} error · "
                  f"{got.count(labjourney.NOT_REACHED) + got.count(labjourney.NOT_RUN):>3} "
                  f"not reached")
    elif lab.journey_why:
        print(f"\n  BUY JOURNEY: not run - {lab.journey_why}")

    for severity, heading in ((DEFECT, "DEFECTS - worth a ticket"),
                              (LOOK, "WORTH A LOOK")):
        groups: dict[str, list[Finding]] = {}
        for f in findings:
            if f.severity == severity:
                # Same finding, different amounts -> one line; a different
                # add-on or cover -> its own line.
                shape = re.sub(r"Rs -?[\d,]+|\d[\d,]*", "#", f.title)
                groups.setdefault(f"{f.check}|{shape}", []).append(f)
        print(f"\n  {heading} ({len(groups)})")
        for items in groups.values():
            where = ", ".join(f"#{n}" for f in items for n in f.journeys[:1])
            more = f"  (+{len(items) - 1} more like it)" if len(items) > 1 else ""
            print(f"    {items[0].title}{more}  [{where}]")
            if items[0].detail:
                print(f"      {items[0].detail[:130]}")
        if not groups:
            print("    none")

    print("\n  COMPANY-SIDE RULES (learned; obeyed in the next session)")
    rules = lab.book.rules()
    for r in rules or []:
        tag = "NEW " if any(r.key == n.key for n in lab.new_rules) else "    "
        print(f"    {tag}{r.sentence()}  [{r.source}]")
        if r.message:
            print(f"         said: {r.message[:110]}")
    if not rules:
        print("    none yet - nothing was refused")
    for text in lab.dropped_rules:
        print(f"    FORGOTTEN (the insurer now accepts it): {text}")
    if lab.skipped:
        print(f"\n  SKIPPED {len(lab.skipped)} scenarios a rule says will be refused "
              f"(re-checked weekly, or now with --recheck)")
    unmapped = [o for o in outs if o.scenario.dimension == "vehicle"
                and o.status == "not-asked"]
    if unmapped:
        print(f"\n  VEHICLES OUR PORTAL DID NOT SEND TO {lab.insurer} ({len(unmapped)})")
        for o in unmapped:
            row = lab.rows.get(o.scenario.value, {})
            print(f"    {row.get('make', '')} {row.get('model', '')} "
                  f"{row.get('variant', '')}: {o.reason}")
    if changes:
        print("\n  CHANGED SINCE THE LAST SESSION")
        for line in changes:
            print(f"    {line}")
    print(f"\n  Report  : {paths['html']}")
    print(f"  Excel   : {paths.get('xlsx') or paths['csv']}")
    print(f"  Notebook: {lab.book.path}\n")


def report_rows(lab: Lab) -> list[dict]:
    """One row per scenario, exactly as the Excel and the CSV show it."""
    def r2(x):
        # Two decimals: 566.34, not the float noise 566.3399999999999.
        return "" if x is None else round(x, 2)

    company_id = str(lab.company.get("id") or "")
    rows = []
    for o in sorted(lab.outcomes, key=lambda o: o.n):
        stages = {"quote": labjourney.quote_stage(o.status, o.reason)}
        for key in labjourney.JOURNEY:
            stages[key] = (labjourney.Stage(labjourney.NOT_REACHED)
                           if stages["quote"].result == labjourney.ERROR
                           else o.stages.get(key) or labjourney.Stage(labjourney.NOT_RUN))
        status, reason = labjourney.verdict(stages)
        notes = [f"{'Bug' if f.severity == DEFECT else 'Worth a look'}: {f.title}"
                 for f in o.findings]
        if o.silent and not o.findings:
            notes.append(f"silently: {o.silent}")
        notes += o.journey_notes
        # Why a stage was not run - unless it is the same for every row, which
        # the Excel's heading already says.
        notes += sorted({s.reason for s in stages.values()
                         if s.result == labjourney.NOT_RUN and s.reason
                         and s.reason != lab.journey_why})
        policy = o.scenario.state.policy
        sub = o.sub_product or lab.product.sub_product
        a = o.best
        rows.append({
            "n": o.n, "company_id": company_id, "company": lab.insurer,
            "sub_product": f"{sub} - {lab.product.title}" if sub else lab.product.title,
            "segment": f"{ls.SEGMENTS.get(policy, '?')} - {policy}",
            "scenario": o.scenario.label(lab.addons),
            **{key: stage.result for key, stage in stages.items()},
            "status": status, "reason": reason, "notes": "; ".join(notes),
            "premium": r2(a.premium) if a else "", "od": r2(a.od) if a else "",
            "tp": r2(a.tp) if a else "", "idv": r2(a.idv) if a else "",
            "ncb": r2(a.ncb_percent) if a else "",
            "quotation": o.quotation, "proposal_no": o.proposal_no,
            "seconds": round(o.seconds + o.journey_seconds, 1)})
    return rows


def _save(path: Path, write) -> Path:
    """write(path), or - when the file is open in Excel right now - a copy
    next to it, so a locked file never costs a run its results."""
    try:
        write(path)
        return path
    except PermissionError:
        copy = path.with_name(f"{path.stem}-latest{path.suffix}")
        write(copy)
        return copy


def write_files(lab: Lab, base, findings, changes) -> dict[str, Path]:
    folder = lab.folder
    folder.mkdir(parents=True, exist_ok=True)
    rows = report_rows(lab)
    done = sum(r["status"] == labjourney.SUCCESS for r in rows)
    vehicle = " ".join(str(lab.base_vehicle.get(k) or "") for k in
                       ("make", "model", "variant")).strip() or lab.template.label
    title = f"Insurer lab - {lab.insurer} {lab.product.title}"
    subtitle = (f"{len(rows)} scenarios · {done} Success · {len(rows) - done} Failure · "
                f"{vehicle} / {lab.args.rto} · {lab.cfg.name} · "
                f"{datetime.now():%d %b %Y %H:%M}")
    if lab.journey_why:
        subtitle += f" · KYC to Payment: {lab.journey_why}"
    facts = [("Insurer", f"{lab.insurer}  {lab.company.get('name') or ''}".strip()),
             ("Company ID", str(lab.company.get("id") or "-")),
             ("Product", f"{lab.product.title} ({lab.product.sub_product})"),
             ("Environment", f"{lab.cfg.name}  {getattr(lab.cfg, 'base_url', '')}".strip()),
             ("Vehicle / RTO", f"{vehicle} / {lab.args.rto}"),
             ("Standard quote", f"{ls.STANDARD} - every other row changes one thing "
                                f"from it"),
             ("Run", datetime.now().strftime("%d %b %Y %H:%M")),
             ("Scenarios", f"{len(rows)}: {done} Success, {len(rows) - done} Failure"),
             ("Buy journey", lab.journey_why or
              "KYC, company specific, proposal and payment, in the browser - it stops "
              "ON the payment page, nothing is paid"),
             *(("Portal bug found", bug) for bug in lab.portal_bugs),
             *((("Buy journey stopped", f"{lab.journey_stopped} - the rows after it "
                 f"say Not run"),) if lab.journey_stopped else ())]

    def csv_file(path: Path) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow([c.heading for c in labexcel.COLUMNS])
            for row in rows:
                w.writerow([row.get(c.key, "") for c in labexcel.COLUMNS])

    paths = {"csv": _save(folder / "results.csv", csv_file)}
    written = []
    xlsx = _save(folder / "results.xlsx",
                 lambda p: written.append(labexcel.write(p, title, subtitle, rows, facts)))
    if all(written):
        paths["xlsx"] = xlsx            # without openpyxl the CSV is the spreadsheet

    base_p = base.best.premium if base is not None and base.best else None
    e = html.escape
    look = {labjourney.SUCCESS: "ok", "Failure": "bad", labjourney.ERROR: "bad",
            labjourney.NOT_REACHED: "m", labjourney.NOT_RUN: "m"}
    stage_keys = [key for key, _ in labjourney.STAGES]
    trs = []
    for r in rows:
        premium = "" if r["premium"] == "" else f"{r['premium']:,.0f}"
        trs.append(
            f"<tr><td>{r['n']}</td><td>{e(r['segment'])}</td><td>{e(r['scenario'])}</td>"
            + "".join(f"<td class='{look.get(r[k], '')}'>{e(r[k])}</td>" for k in stage_keys)
            + f"<td class='{look.get(r['status'], '')}'><b>{e(r['status'])}</b></td>"
            f"<td>{premium}</td><td class='bad'>{e(r['reason'])}</td>"
            f"<td class='m'>{e(r['notes'])}</td></tr>")
    rows_html = "".join(trs)
    fl = "".join(f"<li><b class='{f.severity}'>{f.severity}</b> #{f.journeys[0] if f.journeys else ''} "
                 f"{e(f.title)}<div class='m'>{e(f.detail)}</div></li>" for f in findings) \
        or "<li>None - every check passed.</li>"
    rl = "".join(f"<li>{e(r.sentence())} <span class='m'>[{e(r.source)}] "
                 f"{e(r.message)}</span></li>" for r in lab.book.rules()) or "<li>None yet.</li>"
    ch = "".join(f"<li>{e(c)}</li>" for c in changes) or "<li>Nothing changed.</li>"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Insurer Lab</title><style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--m:#6b6a64;--line:#e4e1d8;--ok:#17603a;--bad:#9b1c14;--warn:#8a5a00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171716;--fg:#ecebe6;--m:#a3a19a;--line:#34332f;
--ok:#8fdcaa;--bad:#ffb4ab;--warn:#f5c86b}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1180px;margin:0 auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
.m{{color:var(--m);font-size:12px}}.DEFECT{{color:var(--bad)}}.LOOK{{color:var(--warn)}}
.wrap{{overflow-x:auto}}</style></head><body><main>
<h1>Insurer lab: {e(lab.insurer)} {e(lab.product.title)}</h1>
<p class="m">{len(rows)} scenarios · {done} Success · {len(rows) - done} Failure ·
company id {e(str(lab.company.get('id') or '-'))} · {e(lab.product.sub_product)} ·
{datetime.now():%Y-%m-%d %H:%M} ·
standard quote {'' if not base_p else f'Rs {base_p:,.0f}'} ({e(ls.STANDARD)})
{'' if not lab.journey_why else f' · KYC to Payment: {e(lab.journey_why)}'}</p>
<h2>Findings</h2><ul>{fl}</ul>
<h2>Company-side rules learned</h2><ul>{rl}</ul>
<h2>Changed since last session</h2><ul>{ch}</ul>
<h2>Every scenario</h2><div class="wrap"><table><tr><th>#</th><th>segment</th>
<th>scenario</th>{''.join(f'<th>{e(name)}</th>' for _, name in labjourney.STAGES)}
<th>status</th><th>premium</th><th>reason</th><th>notes</th></tr>{rows_html}</table></div>
</main></body></html>"""
    paths["html"] = _save(folder / "report.html",
                          lambda p: p.write_text(page, encoding="utf-8"))
    return paths


def show_plan(book, product, saved, args) -> int:
    addons = saved.get("addons") or {}
    fuel = str((saved.get("vehicle") or {}).get("fuel") or "PETROL")
    plan = ls.one_at_a_time(product, addons, fuel, [{"key": f"vehicle {i + 1}", "band": "",
                                                     "fuel": ""} for i in range(args.vehicles)],
                            (200000, 300000))
    if not args.no_combos:
        plan += ls.combinations(product, addons, fuel)
    per_dim: dict[str, int] = {}
    for s in plan:
        per_dim[s.dimension] = per_dim.get(s.dimension, 0) + 1
    print("THE PLAN (one thing at a time, then the combinations insurers restrict)")
    for dim, count in per_dim.items():
        print(f"  {ls.NICE.get(dim, dim):<28} {count}")
    rules = book.rules()
    print(f"\n  {len(plan)} quotes planned, at most --max {args.max}. At ~3 at a time "
          f"that is about {len(plan) * 6 / max(1, args.workers) / 60:.0f} minutes.")
    going_on = len(plan) if args.journeys < 0 else min(args.journeys, len(plan))
    print("  Then each quote that prices goes on through KYC, company specific, "
          "proposal and payment"
          + (f" - up to {going_on}, about {going_on * labjourney.MINUTES_EACH} minutes "
             f"more (--journeys N to change)." if going_on else
             " - not this time (--journeys 0)."))
    print(f"  Add-ons: {'read from the API at the start of the run' if not addons else len(addons)}")
    print(f"  Template: {'captured ' + saved['captured'] if saved else 'none yet - the first run drives one browser journey to capture it'}")
    print(f"  Learned rules obeyed: {len(rules)}")
    for r in rules:
        print(f"    {r.sentence()}")
    print()
    return 0


def notebook_report(book: labrules.Notebook) -> str:
    lines = [f"INSURER LAB NOTEBOOK - {book.insurer} {book.product}",
             f"  file: {book.path}"]
    sessions = book.data.get("sessions", [])
    lines.append(f"  sessions: {len(sessions)}"
                 + (f" (last {sessions[-1]['at']}: {sessions[-1]['quotes']} quotes, "
                    f"{sessions[-1]['priced']} priced, {sessions[-1]['defects']} defects)"
                    if sessions else ""))
    lines.append("\n  RULES IT OBEYS")
    for r in book.rules():
        lines.append(f"    {r.sentence()}  [{r.source}; learned {r.learned}, "
                     f"seen {r.evidence}x{', boundary confirmed' if r.confirmed else ''}]")
        if r.message:
            lines.append(f"      said: {r.message[:120]}")
    if not book.rules():
        lines.append("    none yet")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
