"""
Learn the portal's real bikes (and cars) and insurers, then pick a useful spread.

WHERE THE LIST COMES FROM
-------------------------
Make, model and variant are NOT safe to invent (data/scenarios.py explains
why), and they do not need to be: the app downloads the whole two-wheeler
catalogue when it starts (Saarthi core/services/master-data.service.ts:40-80):

    GET /api/Motor/TWManufacturer                    every make
    GET /api/Motor/AllModel?subProductId=1           every model, with MakeId
    GET /api/Motor/AllVariant?subProductId=1         every variant, with ModelId
    GET /api/Motor/PreviousInsurerForPrivatecarJson  every previous insurer

Private cars have their own lists (loadAllPCData, same file):

    GET /api/Motor/PCManufacturer
    GET /api/Motor/AllModel?subProductId=2           note: same path as bikes,
    GET /api/Motor/AllVariant?subProductId=2         only the number differs

So we listen while the app loads and keep what it received. Every bike in
data/vehicle_catalog.json (cars: data/vehicle_catalog_car.json) is real by
construction. The ids are kept too: the quote request names the vehicle by
VariantCode (= VariantId), and the insurer lab changes vehicles by it. The dropdown shows a variant
as "<VariantName> (<FuelType>)" - "3G (110 CC) (PETROL)" - and that is the text
the tool picks.

SOME VEHICLES CANNOT BE CHOSEN AT ALL (found 2026-10-05)
--------------------------------------------------------
The form looks a make up BY NAME and keeps the first match, then does the
same for the model inside that make (pc- and tw-dont-know-number
onChngMake / onChngModel: `filter(x => x.Name === value)` then `[0]`). The
car list has HYUNDAI, TATA and MG (MORRIS GARAGES) twice each, with the
electric models filed under the second copy - so the Nexon EV, Nexon EV Max,
Tigor EV, Kona and ZS EV can never be picked on the form. build() writes the
reason into each such row (`hidden_by`), pick() never chooses them, and
hidden() lists them, so the matrix reports it instead of wasting journeys.

WHY A SPREAD OF ENGINE SIZES
----------------------------
The third-party premium is set by the regulator by ENGINE SIZE (up to 75cc,
75-150cc, 150-350cc, over 350cc; electric bikes by kW). Five scooters of 110cc
test one price band five times. One bike per band tests four - and lets the
checker confirm that a bigger engine never gets a cheaper third-party price.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from pages.vehicle_details import Vehicle

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CATALOG_FILE = DATA_DIR / "vehicle_catalog.json"
CATALOG_FILES = {"bike": CATALOG_FILE, "car": DATA_DIR / "vehicle_catalog_car.json"}

# path?query, lower case. The query matters: bikes and cars share AllModel and
# AllVariant and differ only in subProductId.
LISTS_BY_PRODUCT = {
    "bike": {"makes": "/api/motor/twmanufacturer",
             "models": "/api/motor/allmodel?subproductid=1",
             "variants": "/api/motor/allvariant?subproductid=1",
             "insurers": "/api/motor/previousinsurerforprivatecarjson"},
    "car": {"makes": "/api/motor/pcmanufacturer",
            "models": "/api/motor/allmodel?subproductid=2",
            "variants": "/api/motor/allvariant?subproductid=2",
            "insurers": "/api/motor/previousinsurerforprivatecarjson"},
}
LISTS = LISTS_BY_PRODUCT["bike"]


def _matches(url: str, needle: str) -> bool:
    low = url.lower()
    path, _, query = low.partition("?")
    want_path, _, want_query = needle.partition("?")
    return path.endswith(want_path) and (not want_query or want_query in query)

# The combination every runner already uses, proven on this environment. It is
# always the matrix's first vehicle, so a healthy run has a healthy baseline.
PROVEN = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
                 "3G (110 CC) (PETROL)", "2022")

# The car the insurer lab captured its template with (2026-09-30), filled
# again by the live form probe on 2026-10-05. The year is only a default -
# the matrix sets it per journey.
PROVEN_CAR = Vehicle("GJ-01", "GJ-01 Ahmedabad", "MARUTI", "NEW SWIFT",
                     "1.2 ZXI AMT (1197 CC) (PETROL)", "2023")
PROVEN_BY_PRODUCT = {"bike": PROVEN, "car": PROVEN_CAR}
PROVEN_BAND = {"bike": "up to 150cc", "car": "1000-1500cc"}

BANDS = ("up to 150cc", "150-350cc", "over 350cc", "electric")

# Popular bikes to prefer inside each band, when the catalogue has them. Only
# a preference: anything picked must exist in the catalogue.
FAVOURITES = {
    "up to 150cc": [("HONDA", "ACTIVA"), ("HERO", "SPLENDOR"), ("TVS", "JUPITER"),
                    ("SUZUKI", "ACCESS"), ("HONDA", "SHINE")],
    "150-350cc": [("ROYAL ENFIELD", "CLASSIC"), ("BAJAJ", "PULSAR"),
                  ("YAMAHA", "FZ"), ("TVS", "APACHE"), ("ROYAL ENFIELD", "BULLET")],
    "over 350cc": [("ROYAL ENFIELD", "HIMALAYAN"), ("ROYAL ENFIELD", "INTERCEPTOR"),
                   ("ROYAL ENFIELD", "CONTINENTAL"), ("KTM", "390"), ("BAJAJ", "DOMINAR")],
    "electric": [("ATHER", "450"), ("TVS", "IQUBE"), ("HERO ELECTRIC", ""),
                 ("OLA", "S1"), ("BAJAJ", "CHETAK")],
}

# Popular cars per band - all present in the learned catalogue (2026-10-05).
CAR_FAVOURITES = {
    "up to 1000cc": [("MARUTI", "WAGON R"), ("MARUTI", "ALTO"), ("MARUTI", "CELERIO")],
    "1000-1500cc": [("HYUNDAI", "I20"), ("MARUTI", "BALENO"), ("HONDA", "CITY"),
                    ("KIA", "SELTOS")],
    "over 1500cc": [("MAHINDRA", "SCORPIO"), ("TOYOTA", "INNOVA"),
                    ("MAHINDRA", "XUV500"), ("TOYOTA", "FORTUNER")],
    "electric": [("TATA", "NEXON"), ("MG", "ZS EV")],
}
FAVOURITES_BY_PRODUCT = {"bike": FAVOURITES, "car": CAR_FAVOURITES}

# Cars also differ by FUEL in ways insurers price: a CNG kit carries its own
# third-party charge, and diesel is most of the catalogue. So the small car
# is a CNG one and the big one a diesel, when the catalogue has them - one
# run then covers petrol, CNG, diesel and electric.
FUEL_WANTED = {"car": {"up to 1000cc": "CNG", "over 1500cc": "DIESEL"}}


class MasterListener:
    """
    Attach to a context before the app loads; keeps the four lists.

    On the local target the login happens on the deployed test site first,
    and that app downloads ITS lists too. The target's own API is the one the
    journeys will use, so its answers replace any earlier ones.
    """

    def __init__(self, prefer_hosts=(), product: str = "bike"):
        self.raw: dict[str, list] = {}
        self.source: dict[str, str] = {}
        self.prefer = {h for h in prefer_hosts if h}
        self.lists = LISTS_BY_PRODUCT[product]

    @classmethod
    def attach(cls, context, prefer_hosts=(), product: str = "bike") -> "MasterListener":
        listener = cls(prefer_hosts, product)
        context.on("response", listener._on_response)
        return listener

    def _on_response(self, response) -> None:
        try:
            host = urlparse(response.url).hostname or ""
            for name, needle in self.lists.items():
                if not _matches(response.url, needle):
                    continue
                if name in self.raw and self.source.get(name) in self.prefer:
                    continue                   # already have the target's own
                body = json.loads(response.text() or "{}")
                rows = body.get("Response") if isinstance(body, dict) else body
                if isinstance(rows, list) and rows:
                    self.raw[name] = rows
                    self.source[name] = host
        except Exception:
            pass

    @property
    def complete(self) -> bool:
        """All four lists - from the target's own API when we know it."""
        if not all(name in self.raw for name in self.lists):
            return False
        return not self.prefer or all(self.source.get(n) in self.prefer
                                      for n in self.lists)


# ------------------------------------------------------------------ building

def _cc(variant: dict) -> float | None:
    """Engine size from whichever field carries it, else from the name."""
    for key, value in variant.items():
        low = key.lower()
        if "cubic" in low or low in ("cc", "engincc", "enginecc"):
            try:
                number = float(str(value).replace(",", ""))
                if number > 0:
                    return number
            except ValueError:
                pass
    found = re.search(r"(\d{2,4})\s*CC", str(variant.get("VariantName", "")), re.I)
    return float(found.group(1)) if found else None


# Regulated third-party price bands. Cars: up to 1000cc, 1000-1500cc, above.
CAR_BANDS = ("up to 1000cc", "1000-1500cc", "over 1500cc", "electric")


def band_of(cc: float | None, fuel: str, product: str = "bike") -> str:
    if "ELECTRIC" in (fuel or "").upper() or "BATTERY" in (fuel or "").upper():
        return "electric"
    if cc is None:
        return ""
    if product == "car":
        return ("up to 1000cc" if cc <= 1000 else
                "1000-1500cc" if cc <= 1500 else "over 1500cc")
    if cc <= 150:
        return "up to 150cc"
    if cc <= 350:
        return "150-350cc"
    return "over 350cc"


def _top(row: dict) -> bool:
    return row.get("IsTop") in (True, "True", "true", 1, "1")


def build(raw: dict[str, list], target: str, product: str = "bike") -> dict:
    """Turn the four raw lists into the catalogue file's shape."""
    makes = {m.get("Id"): m for m in raw.get("makes", [])}
    models = {m.get("Id"): m for m in raw.get("models", [])}
    # Every id behind each name, in the app's own list order: the form lands
    # on the first (see "SOME VEHICLES CANNOT BE CHOSEN AT ALL").
    make_ids: dict[str, list] = {}
    for m in raw.get("makes", []):
        make_ids.setdefault(_name(m), []).append(m.get("Id"))
    model_ids: dict[tuple, list] = {}
    for m in raw.get("models", []):
        model_ids.setdefault((m.get("MakeId"), _name(m)), []).append(m.get("Id"))
    vehicles = []
    for variant in raw.get("variants", []):
        model = models.get(variant.get("ModelId"))
        make = makes.get(model.get("MakeId")) if model else None
        if not model or not make:
            continue
        fuel = str(variant.get("FuelType") or "").strip()
        name = str(variant.get("VariantName") or "").strip()
        if not name:
            continue
        cc = _cc(variant)
        vehicles.append({
            "make": str(make.get("Name") or "").strip(),
            "model": str(model.get("Name") or "").strip(),
            # Exactly the text the dropdown shows (master-data.service.ts:67).
            "variant": f"{name} ({fuel})" if fuel else name,
            "cc": cc,
            "fuel": fuel,
            "band": band_of(cc, fuel, product),
            "top": _top(make) or _top(model) or _top(variant),
            # What the quote request carries (VehicleDetails.VariantCode etc.)
            "make_id": make.get("Id"),
            "model_id": model.get("Id"),
            "variant_id": variant.get("VariantId", variant.get("Id")),
            "variant_name": name,
            # Why the form can never show this one; "" when it can.
            "hidden_by": _hidden_by(make, model, make_ids, model_ids),
        })
    insurers = [{"name": str(i.get("Name") or "").strip(),
                 "code": str(i.get("CompanyCode") or i.get("CShortName") or "").upper(),
                 "id": i.get("Id"), "short": i.get("CShortName")}
                for i in raw.get("insurers", []) if i.get("Name")]
    return {"learned_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "target": target, "product": product, "vehicles": vehicles,
            "insurers": insurers}


def _name(row: dict) -> str:
    return str(row.get("Name") or "").strip().upper()


def _hidden_by(make: dict, model: dict, make_ids: dict, model_ids: dict) -> str:
    ids = make_ids.get(_name(make), [])
    if ids and ids[0] != make.get("Id"):
        return (f"make {_name(make)} is listed {len(ids)} times (ids "
                f"{', '.join(map(str, ids))}) and the form only uses the first")
    ids = model_ids.get((make.get("Id"), _name(model)), [])
    if ids and ids[0] != model.get("Id"):
        return (f"model {_name(model)} is listed {len(ids)} times under "
                f"{_name(make)} (ids {', '.join(map(str, ids))}) and the form only "
                f"uses the first")
    return ""


def hidden(catalog: dict) -> list[str]:
    """One sentence per reason some vehicles can never be chosen on the form."""
    groups: dict[str, list[dict]] = {}
    for row in catalog.get("vehicles") or []:
        if row.get("hidden_by"):
            groups.setdefault(row["hidden_by"], []).append(row)
    return [f"{why}, so {len(rows)} variant{'s' if len(rows) != 1 else ''} can "
            f"never be chosen: {', '.join(sorted({r['model'] for r in rows}))}"
            for why, rows in sorted(groups.items())]


def save(catalog: dict, product: str = "bike") -> None:
    target = CATALOG_FILES[product]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(catalog, indent=1), encoding="utf-8")
        os.replace(temp, target)
    except Exception:
        pass


def load(product: str = "bike") -> dict:
    try:
        data = json.loads(CATALOG_FILES[product].read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("vehicles"):
            return data
    except Exception:
        pass
    return {}


def age_days(catalog: dict) -> float:
    try:
        when = datetime.strptime(catalog.get("learned_on", ""), "%Y-%m-%d %H:%M")
        return (datetime.now() - when).total_seconds() / 86400
    except ValueError:
        return 9999.0


# ------------------------------------------------------------------- picking

def _as_vehicle(row: dict, product: str = "bike") -> Vehicle:
    proven = PROVEN_BY_PRODUCT[product]
    return Vehicle(proven.rto_type, proven.rto, row["make"], row["model"],
                   row["variant"], proven.registration_year)


def pick(catalog: dict, count: int, avoid: set[str] = frozenset(),
         tried: set[str] = frozenset(),
         product: str = "bike") -> list[tuple[Vehicle, str]]:
    """
    Up to `count` vehicles, one per engine band, as (vehicle, band).

    The proven Activa (cars: the Swift) comes first. Then one per band - a
    favourite if the catalogue has one, else a popular (IsTop) one -
    preferring vehicles this tool has NOT tried before, so each run widens
    the coverage. `avoid` holds vehicles the form refused earlier ("never
    offered in the dropdown"). Both sets may hold either spelling - the
    notebook writes "HONDA ACTIVA 3G (...)", the catalogue "HONDA|ACTIVA|3G
    (...)". Until 2026-10-05 only the second was compared, so a refused or
    already-tried bike never matched and was picked again.
    """
    rows = catalog.get("vehicles") or []
    proven = PROVEN_BY_PRODUCT[product]
    home = PROVEN_BAND[product]
    favourites = FAVOURITES_BY_PRODUCT[product]
    fuel_wanted = FUEL_WANTED.get(product, {})
    avoid = {str(n).upper() for n in avoid or ()}
    tried = {str(n).upper() for n in tried or ()}
    chosen: list[tuple[Vehicle, str]] = [(proven, home)]
    seen = {key(proven)}
    for band in (BANDS if product == "bike" else CAR_BANDS):
        if len(chosen) >= count:
            break
        if band == home:
            continue                        # the proven vehicle covers it
        pool = [r for r in rows if r.get("band") == band and not r.get("hidden_by")
                and key_of(r) not in seen and not _named(r, avoid)]
        if not pool:
            continue

        def rank(r: dict) -> tuple:
            fav = next((i for i, (mk, md) in enumerate(favourites.get(band, []))
                        if r["make"].upper().startswith(mk)
                        and md.upper() in r["model"].upper()), 99)
            fuel = fuel_wanted.get(band, "")
            return (_named(r, tried),
                    bool(fuel) and fuel not in str(r.get("fuel", "")).upper(),
                    fav, not r.get("top"), r["make"], r["model"], r["variant"])
        best = min(pool, key=rank)
        chosen.append((_as_vehicle(best, product), band))
        seen.add(key_of(best))
    # Still short (small catalogue, or count above the band count): add more
    # popular ones from any band, untried first.
    extras = sorted((r for r in rows if key_of(r) not in seen
                     and not r.get("hidden_by") and not _named(r, avoid)),
                    key=lambda r: (_named(r, tried), not r.get("top"),
                                   r["make"], r["model"], r["variant"]))
    for row in extras:
        if len(chosen) >= count:
            break
        chosen.append((_as_vehicle(row, product), row.get("band") or "unknown"))
        seen.add(key_of(row))
    return chosen[:count]


def key(vehicle: Vehicle) -> str:
    return f"{vehicle.make}|{vehicle.model}|{vehicle.variant}".upper()


def key_of(row: dict) -> str:
    return f"{row['make']}|{row['model']}|{row['variant']}".upper()


def label(vehicle: Vehicle) -> str:
    """How the matrix and its notebook name a vehicle."""
    return f"{vehicle.make} {vehicle.model} {vehicle.variant}"


def _named(row: dict, names: set[str]) -> bool:
    """Is this row in `names`, under either spelling?"""
    return (key_of(row) in names
            or f"{row['make']} {row['model']} {row['variant']}".upper() in names)


def row_for(vehicle: Vehicle, catalog: dict) -> dict:
    """The catalogue row behind a vehicle: engine size, fuel, ids."""
    for row in catalog.get("vehicles") or []:
        if key_of(row) == key(vehicle):
            return row
    return {}


def band_for(vehicle: Vehicle, catalog: dict, product: str = "bike") -> str:
    row = row_for(vehicle, catalog)
    if row:
        return row.get("band") or ""
    return band_of(_cc({"VariantName": vehicle.variant}), vehicle.variant, product)


def previous_insurers(catalog: dict, asked=()) -> list[tuple[str, str, str]]:
    """
    Two previous insurers to rotate, as (display name, search text, code).

    Why two: an insurer cannot renew a policy it is told it already holds
    ("Policy can not issue with same insurer"), so with one fixed previous
    insurer that insurer is NEVER tested on a renewal. Rotating between two
    gives both a fair turn - and lets the checker catch the rule firing for the
    wrong insurer.

    asked (the run's --insurers): first an insurer NOT asked, so the standard
    quote is never refused as a same-company renewal (the first live run,
    2026-10-07, lost BAJAJ on every journey to a BAJAJ previous policy) - then
    each asked insurer once, so each one's same-company rule is tested too.
    """
    known = catalog.get("insurers") or []
    if asked:
        return _previous_for(known, [str(c).upper() for c in asked])
    bajaj = next((i for i in known if i["name"].upper().startswith("BAJAJ")), None)
    out = [("BAJAJ ALLIANZ GENERAL INSURANCE CO. LTD.", "BAJAJ",
            (bajaj or {}).get("code") or "BAJAJ")]
    for wanted in ("ICICI", "HDFC", "TATA", "NEW INDIA"):
        row = next((i for i in known if wanted in i["name"].upper()
                    and "BAJAJ" not in i["name"].upper()), None)
        if row:
            code = row.get("code") or wanted
            out.append((row["name"], wanted, code))
            break
    return out


# Neutral previous insurers, best first: big, always in the list, and rarely
# among the insurers a run asks.
NEUTRAL_PREVIOUS = ("ICICI", "HDFCERGO", "NEWINDIA", "RELIANCE", "UNITED", "BAJAJ")


def _previous_for(known: list[dict], asked: list[str]) -> list[tuple[str, str, str]]:
    by_code = {str(i.get("code") or "").upper(): i for i in known if i.get("name")}

    def entry(code: str):
        row = by_code.get(code)
        if not row:
            return None
        name = row["name"]
        # Type something the dropdown will match: the code when the name
        # contains it (TATA, KOTAK, SBI), else the name's first word.
        search = code if code in name.upper() else name.split()[0]
        return (name, search, row.get("code") or code)

    out = []
    neutral = next((c for c in NEUTRAL_PREVIOUS if c not in asked and c in by_code), None)
    for code in [neutral, *asked]:
        e = entry(code) if code else None
        if e and e not in out:
            out.append(e)
    return out
