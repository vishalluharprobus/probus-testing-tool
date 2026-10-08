"""
Find the car (or bike) and RTO city that gets a price from the MOST insurers.

    python find_best_vehicle.py --product car --target testsite
    python find_best_vehicle.py --product bike --target testsite --cities 10

WHY
---
Which insurers are even asked depends mostly on the RTO city (on the test site
GJ-01 Ahmedabad asks 3 car insurers, MH-01 Mumbai asks 5), and which of those
then price depends on the vehicle. Testing one company "with a good vehicle"
starts with knowing which vehicle and city that is - so this measures it,
through the API, in a few minutes instead of a journey per guess.

HOW (quotes only - nothing is proposed or bought)
-------------------------------------------------
  0. Log in and borrow one real request, exactly as run_insurer_lab.py does
     (the same saved template, per target).
  A. For ~16 big cities: one QualifiedCompany call each - WHO would be asked.
     Cheap: no insurer is called.
  B. For the best few cities x a handful of popular vehicles: QualifiedCompany,
     then every company's own quote call - WHO actually prices.
  The winner is the combination with the most companies priced. It is saved to
  reports/best_vehicle-<target>-<product>.json and printed as the exact
  run_insurer_lab.py command to use next.

The previous insurer is one that is NOT on the plan list, so no company is
left out for "cannot renew itself".
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import sys
from argparse import Namespace
from dataclasses import replace
from datetime import datetime

import run_insurer_lab as lab
from config import settings
from core import auth, backend, console, labchecks, safety
from core.labclient import ApiRefused, ApiReplay, plan_body
from data import labscenarios as ls

# Big, spread-out RTOs. Matched by prefix against the app's own RTO list.
CITIES = ("MH-01", "MH-02", "MH-12", "DL-01", "DL-03", "KA-01", "KA-03",
          "TN-01", "TS-09", "WB-01", "GJ-01", "GJ-05", "RJ-14", "UP-32",
          "HR-26", "PB-10")

# Popular vehicles, (make, model) - the first visible petrol/diesel variant.
CARS = (("MARUTI", "SWIFT"), ("MARUTI", "BALENO"), ("HYUNDAI", "I20"),
        ("HYUNDAI", "CRETA"), ("HONDA", "CITY"), ("TOYOTA", "INNOVA"),
        ("MAHINDRA", "SCORPIO"))
BIKES = (("HONDA", "ACTIVA"), ("HERO", "SPLENDOR"), ("BAJAJ", "PULSAR"),
         ("TVS", "JUPITER"), ("ROYAL ENFIELD", "CLASSIC"), ("YAMAHA", "FZ"))

# Insurers that are rarely integrated on the test server - a safe "previous
# insurer" that keeps every plan-list company in the running.
PREVIOUS = ("NEW INDIA", "ORIENTAL", "UNITED INDIA", "NATIONAL")


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--product", choices=("car", "bike"), default="car")
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--cities", type=int, default=3,
                    help="how many of the best cities to price in step B")
    ap.add_argument("--seed-insurer", default="SHRIRAM",
                    help="an insurer GJ-01 asks, used once to borrow a real request")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    product = ls.PRODUCTS[args.product]
    cfg = settings.load(args.target)
    print(f"\nBEST VEHICLE FINDER - {product.name} on {cfg.name} ({cfg.base_url})")
    print("Quotes only: nothing is proposed or bought.\n")
    health = backend.check(cfg.api_url)
    if not health:
        print(f"THE APP'S BACK END IS NOT ANSWERING\n  {health.detail}")
        return 6

    rto = "GJ-01 Ahmedabad"
    saved = lab.load_template(product, rto, cfg.name)
    session_args = Namespace(refresh_template=False, headless=args.headless,
                             rto=rto, vehicle="")
    try:
        session = lab.open_session(cfg, product, args.seed_insurer.upper(),
                                   session_args, saved)
    except (auth.LoginFailed, safety.SafetyRefusal) as exc:
        print(f"\nSTOPPED: {exc}")
        return 4
    if session is None:
        return 6
    template, catalog = session["template"], session["catalog"]
    api = ApiReplay(session["headers"])
    previous = _previous(session["insurers"])

    cities = _cities(api, cfg)
    print(f"  {len(cities)} cities to check; previous insurer "
          f"{previous.get('Name')}\n")

    # ---------------------------------------------------------------- step A
    print("STEP A - who is ASKED in each city (no insurer is called)")
    base = ls.build_body(template.qualify, product, ls.State(), prev_insurer=previous)
    asked: dict[str, list[str]] = {}
    with futures.ThreadPoolExecutor(3) as pool:
        jobs = {pool.submit(_qualify, api, template, _in_city(base, product, c)): c
                for c in cities}
        for job in futures.as_completed(jobs):
            city = jobs[job]
            plans, _ = job.result()
            asked[city["Name"]] = sorted({p.get("CompanyCode") for p in plans} - {None})
    for name, codes in sorted(asked.items(), key=lambda kv: -len(kv[1])):
        print(f"  {name:<24} {len(codes):>2} asked  {', '.join(codes)}")

    best_cities = [c for c in sorted(cities, key=lambda c: -len(asked[c["Name"]]))
                   if asked[c["Name"]]][:args.cities]
    if not best_cities:
        print("\nNo city got any company asked - nothing to price.")
        return 5

    # ---------------------------------------------------------------- step B
    vehicles = _vehicles(catalog, CARS if product.name == "car" else BIKES)
    print(f"\nSTEP B - who actually PRICES: {len(best_cities)} cities x "
          f"{len(vehicles)} vehicles, every company asked")
    results = []
    for city in best_cities:
        for row in vehicles:
            body = ls.build_body(template.qualify, product, ls.State(),
                                 prev_insurer=previous, vehicle=row)
            body = _in_city(body, product, city)
            priced, refused = _price_all(api, template, body)
            label = f"{row['make']} {row['model']} {row['variant']}"
            results.append({"city": city["Name"], "vehicle": label, "row": row,
                            "priced": priced, "refused": refused})
            print(f"  {city['Name'][:16]:<16} {label[:46]:<46} "
                  f"{len(priced):>2} priced  {', '.join(sorted(priced))}")

    best = max(results, key=lambda r: (len(r["priced"]), -len(r["refused"])))
    out = {"found": datetime.now().strftime("%Y-%m-%d %H:%M"), "target": cfg.name,
           "product": product.name, "age_years": ls.BASE_AGE,
           "policy": "Comprehensive, previous policy not expired, no claim",
           "previous_insurer": previous.get("Name"), "best": best,
           "asked_by_city": asked, "all": results}
    path = lab.labrules.LAB_DIR.parent / f"best_vehicle-{cfg.name}-{product.name}.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print("\nBEST")
    print(f"  {best['vehicle']}  in  {best['city']}")
    print(f"  priced by {len(best['priced'])}: "
          + ", ".join(f"{k} Rs {v:,.0f}" for k, v in sorted(best["priced"].items())))
    for k, v in sorted(best["refused"].items()):
        print(f"  said no  {k}: {v[:90]}")
    print(f"\n  saved -> {path}")
    model = f"{best['row']['make']} {best['row']['model']}"
    print(f"  next: python run_insurer_lab.py --insurer <COMPANY> --product "
          f"{product.name} --target {cfg.name} --rto \"{best['city']}\" "
          f"--vehicle \"{model} {best['row']['variant'].split(' (')[0]}\"\n")
    return 0


def _previous(insurers: list) -> dict:
    for want in PREVIOUS:
        row = next((i for i in insurers if want in (i.get("name") or "").upper()), None)
        if row:
            return {"Id": row.get("id"), "CompanyCode": row.get("code"),
                    "CShortName": row.get("short"), "Name": row.get("name")}
    return {}


def _cities(api: ApiReplay, cfg) -> list[dict]:
    rows = api.get(cfg.api_url.rstrip("/") + "/api/Motor/RTOcityJson").get("Response") or []
    picked = []
    for prefix in CITIES:
        hit = next((r for r in rows if str(r.get("Name", "")).startswith(prefix + " ")), None)
        if hit:
            picked.append(hit)
    return picked


# Moved to data/labscenarios.py (the quote sweep's fast mode uses it too).
_in_city = ls.in_city


def _qualify(api: ApiReplay, template, body: dict) -> tuple[list, dict]:
    try:
        answer = api.post(template.api_base + "QualifiedCompany", body)
    except ApiRefused:
        return [], {}
    data = answer.get("Response") or {}
    return list(data.get("QualifiedPlanList") or []), answer


def _price_all(api: ApiReplay, template, body: dict) -> tuple[dict, dict]:
    """Every company's price for one request: {code: premium}, {code: reason}."""
    plans, answer = _qualify(api, template, body)
    priced: dict[str, float] = {}
    refused: dict[str, str] = {}
    data = answer.get("Response") or {} if answer else {}
    for d in data.get("APIDeclineDetails") or []:
        refused[str(d.get("CompanyCode"))] = f"our rules: {d.get('ErrorMessage')}"

    def one(plan):
        code = str(plan.get("CompanyCode"))
        try:
            got = api.post(template.api_base + code, plan_body(template, body, answer, plan))
        except ApiRefused as exc:
            return code, None, str(exc)
        items = got.get("Response") or []
        items = items if isinstance(items, list) else [items]
        best, _ = labchecks.best_of(items, code)
        if best is not None:
            return code, best.premium, ""
        why = next((str(i.get("ErrorMessage")) for i in items if i.get("ErrorMessage")),
                   str(got.get("Error") or "no price and no message"))
        return code, None, why

    with futures.ThreadPoolExecutor(4) as pool:
        for code, premium, why in pool.map(one, plans):
            if premium:
                priced[code] = min(premium, priced.get(code, premium))
            elif code not in priced:
                refused[code] = why
    for code in priced:
        refused.pop(code, None)
    return priced, refused


def _vehicles(catalog: dict, wanted) -> list[dict]:
    rows = [r for r in catalog.get("vehicles") or []
            if not r.get("hidden_by") and r.get("variant_id") is not None
            and str(r.get("fuel", "")).upper() in ("PETROL", "DIESEL")]
    picked = []
    for make, model in wanted:
        hit = sorted((r for r in rows if r["make"].upper().startswith(make)
                      and model in r["model"].upper()),
                     key=lambda r: (not r.get("top"), r["variant"]))
        if hit:
            picked.append(hit[0])
    return picked


if __name__ == "__main__":
    sys.exit(main())
