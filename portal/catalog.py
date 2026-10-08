"""
What the pickers offer: insurers, vehicles (MMV) and RTOs - all real, all from
the portal's own master data, never invented.

    vehicles  data/vehicle_catalog.json / vehicle_catalog_car.json, learned by
              run_quote_matrix.py from the app's own downloads
    insurers  the same files (the portal's previous-insurer list)
    RTOs      api/Motor/RTOcityJson - a public read-only list of 1,426 RTOs,
              fetched once from the test site's API and kept for 30 days in
              data/rto_catalog.json
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

from core import vehiclecatalog
from data import scenarios

ROOT = Path(__file__).resolve().parent.parent
RTO_FILE = ROOT / "data" / "rto_catalog.json"
RTO_SOURCE = "https://testapi.probusinsurance.com/api/Motor/RTOcityJson"
RTO_MAX_AGE_DAYS = 30

# Quick picks for the company chips. "Top 5" is the admin panel's Company
# wise FTD-MTD report (MTD premium), 2026-10-07.
PRESETS = [
    {"id": "top5", "label": "Top 5 by premium",
     "codes": ["BAJAJ", "TATA", "DIGIT", "SBI", "KOTAK"]},
    {"id": "top10", "label": "Top 10 by premium",
     "codes": ["BAJAJ", "TATA", "DIGIT", "SBI", "KOTAK", "ZUNO", "LIBERTYVGI",
               "ICICI", "MAGMA", "HDFCERGO"]},
]

# Names people use, for the chips (the portal's names are the legal ones).
SHORT_NAMES = {
    "BAJAJ": "Bajaj Allianz", "BHARTI": "Bharti AXA", "CHOLAMANDLAM": "Chola MS",
    "FUTURE": "Future Generali", "DIGIT": "Go Digit", "HDFCERGO": "HDFC ERGO",
    "ICICI": "ICICI Lombard", "IFFCOTOKIO": "IFFCO Tokio", "KGID": "KGID",
    "LTGIC": "L&T General", "LIBERTYVGI": "Liberty", "MAGMA": "Magma HDI",
    "NATIONAL": "National", "RAHEJAQBE": "Raheja QBE", "RELIANCE": "Reliance",
    "ROYALSUNDRAM": "Royal Sundaram", "SBI": "SBI General", "SHRIRAM": "Shriram",
    "TATA": "TATA AIG", "NEWINDIA": "New India", "ORIENTAL": "Oriental",
    "UNITED": "United India", "UNIVERSALSOMPO": "Universal Sompo", "ZUNO": "Zuno",
    "KOTAK": "Zurich Kotak",
}

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, path: Path, load):
    """Re-read a file only when it changed on disk."""
    stamp = path.stat().st_mtime if path.exists() else 0.0
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (stamp, value)
    return value


# ------------------------------------------------------------------ insurers

def insurers() -> list[dict]:
    cat = vehiclecatalog.load("bike") or vehiclecatalog.load("car") or {}
    out = []
    for row in cat.get("insurers") or []:
        code = str(row.get("code") or "").upper()
        if not code:
            continue
        out.append({"code": code, "id": row.get("id"), "name": row.get("name", ""),
                    "short": SHORT_NAMES.get(code, row.get("name", code).title())})
    return sorted(out, key=lambda r: r["short"].upper())


# ------------------------------------------------------------------ vehicles

def _vehicle_rows(product: str) -> list[dict]:
    path = vehiclecatalog.CATALOG_FILES[product]

    def load():
        rows = (vehiclecatalog.load(product) or {}).get("vehicles") or []
        out = []
        for r in rows:
            out.append({
                "key": vehiclecatalog.key_of(r),
                "make": r["make"], "model": r["model"], "variant": r["variant"],
                "band": r.get("band") or "", "fuel": r.get("fuel") or "",
                "cc": r.get("cc"), "top": bool(r.get("top")),
                "hidden": r.get("hidden_by") or "",
                "_text": f"{r['make']} {r['model']} {r['variant']}".upper(),
            })
        return out
    return _cached(f"vehicles-{product}", path, load)


def vehicles(product: str, q: str = "", limit: int = 40) -> dict:
    rows = _vehicle_rows(product)
    words = [w for w in re.split(r"\s+", q.upper().strip()) if w]
    if words:
        hits = [r for r in rows if all(_word_fits(w, r) for w in words)]
        first = words[0]
        hits.sort(key=lambda r: (bool(r["hidden"]),
                                 not r["make"].upper().startswith(first),
                                 not r["top"], r["make"], r["model"], r["variant"]))
    else:
        # Nothing typed yet: popular ones, taking turns across engine bands
        # so the first screen shows small, big and electric side by side.
        by_band: dict[str, list[dict]] = {}
        for r in rows:
            if r["top"] and not r["hidden"]:
                by_band.setdefault(r["band"], []).append(r)
        lanes = [sorted(v, key=lambda r: (r["make"], r["model"], r["variant"]))
                 for _, v in sorted(by_band.items())]
        hits = [lane[i] for i in range(max(map(len, lanes), default=0))
                for lane in lanes if i < len(lane)]
    return {"total": len(rows), "count": len(hits),
            "items": [{k: v for k, v in r.items() if k != "_text"}
                      for r in hits[:limit]]}


def _word_fits(word: str, row: dict) -> bool:
    """A typed word matches the text - or, when it is an engine size, a
    variant within 15 cc of it: people type "classic 350", the portal lists
    the Classic as 346 CC."""
    if word in row["_text"]:
        return True
    if word.isdigit() and int(word) >= 50 and row.get("cc"):
        return abs(float(row["cc"]) - int(word)) <= 15
    return False


def vehicle(product: str, key: str) -> dict | None:
    key = key.upper()
    return next(({k: v for k, v in r.items() if k != "_text"}
                 for r in _vehicle_rows(product) if r["key"] == key), None)


# ---------------------------------------------------------------------- RTOs

def _fetch_rtos() -> list[dict]:
    request = urllib.request.Request(RTO_SOURCE, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    rows = body.get("Response") if isinstance(body, dict) else body
    out = []
    for r in rows or []:
        name = str(r.get("Name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "code": name.split()[0], "city": r.get("CityName") or "",
                    "id": str(r.get("Id") or ""), "top": bool(r.get("IsTopCity")),
                    "zone": r.get("Zone") or ""})
    return out


def refresh_rtos(force: bool = False) -> None:
    """Fetch the RTO list when missing or a month old. Never raises - the
    pickers fall back to the few RTOs the tool already knows."""
    fresh = RTO_FILE.exists() and (time.time() - RTO_FILE.stat().st_mtime
                                   < RTO_MAX_AGE_DAYS * 86400)
    if fresh and not force:
        return
    try:
        rows = _fetch_rtos()
    except Exception:
        return
    if len(rows) > 100:
        RTO_FILE.write_text(json.dumps({"fetched": time.strftime("%Y-%m-%d"),
                                        "source": RTO_SOURCE, "rtos": rows}),
                            encoding="utf-8")


def _rto_rows() -> list[dict]:
    def load():
        try:
            rows = json.loads(RTO_FILE.read_text(encoding="utf-8"))["rtos"]
        except Exception:
            rows = [{"name": n, "code": n.split()[0], "city": " ".join(n.split()[1:]),
                     "id": "", "top": True, "zone": ""}
                    for n in (scenarios.discovered_rtos() or ["GJ-01 Ahmedabad"])]
        for r in rows:
            r["_text"] = f"{r['name']} {r['city']}".upper()
        return sorted(rows, key=lambda r: r["name"])
    return _cached("rtos", RTO_FILE, load)


def rtos(q: str = "", limit: int = 40) -> dict:
    rows = _rto_rows()
    text = q.upper().strip().replace(" ", "-", 1) if re.match(r"^[A-Za-z]{2}\s?\d", q.strip()) \
        else q.upper().strip()
    if text:
        hits = [r for r in rows if text in r["_text"] or text.replace("-", "") in
                r["code"].replace("-", "")]
        hits.sort(key=lambda r: (not r["code"].startswith(text),
                                 not r["city"].upper().startswith(text), r["name"]))
    else:
        hits = [r for r in rows if r["top"]]
    return {"total": len(rows), "count": len(hits),
            "items": [{k: v for k, v in r.items() if k != "_text"} for r in hits[:limit]]}
