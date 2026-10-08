"""
Remember which ADDRESSES each insurer accepts - by pincode and by city.

WHY THIS EXISTS
---------------
NATIONAL stops every proposal at Preview with

    "No district found matching your state, city & pincode combination."

The cause is on the server, in InsureBridge's TWNationalInsuranceRules:
PrepareCompanySpecificQuoteModelNew looks the COMMUNICATION address up in
NATIONAL's own city table -

    MotorData.GetCityMasterNIC(State, City, Pincode)
      -> sp_get_nic_city_master(22, 922001, <pincode>)    Gujarat / AHMEDABAD

WHAT THE RUNS TAUGHT
--------------------
First guess: the pincode. 380009 was refused 16 times out of 16, so the harness
tried other Ahmedabad pincodes - 380001, 380006, 380015, 380013, 380007 - and
NATIONAL refused every one (run 20260925-153045). ZUNO accepted 380009.

So it is not the pincode, it is the CITY: NATIONAL's table has no usable row
for Ahmedabad at all. Trying a sixth Ahmedabad pincode would be the definition
of doing the same thing and expecting a different answer.

HOW IT DECIDES NOW - an escalation ladder, cheapest change first:
  0. change nothing without evidence - an address nobody refused is kept
  1. a pincode this insurer has ACCEPTED before (any city)
  2. another pincode in the same city - only while that city is still
     plausible for this insurer
  3. a different CITY, same state first (cities the insurer's dropdown does
     not even list are ruled out on the page), then one big metro as a probe
  4. two whole cities refused and none ever accepted: a VERDICT - the rows
     are missing on the server. From then on runs keep the customer's own
     address and stop at the first refusal, in seconds; the day the backend
     adds the rows, that address passes and the verdict clears by itself.

The server confirms it is data, not a crash: InsureBridge logs any SQL error
in GetCityMasterNIC to LogFile/Data/Error, and there was none on 2026-09-25.

A city counts as "refused" after two refusals with no acceptance - or after ONE
once this insurer has already shown that its refusals are per-city (a whole
city refused before). That is the pattern the Ahmedabad runs revealed, and it
turns five wasted round trips into one.

Everything is remembered in reports/address_pincodes.json, built on first use
from the answers already in reports/run-*/api-responses.log.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
NOTES_FILE = REPORTS_DIR / "address_pincodes.json"

# The request that carries the address to the insurer at Preview.
CHECK_URL = "companyspecificquotation"

# Test addresses to fall back on, in order of preference. Each city lists the
# names insurers' dropdowns may use for it (NATIONAL writes "AHMEDABAD", others
# "Ahmedabad"; Bengaluru is still "BANGALORE" in plenty of master data), its
# state, and real pincodes for its central post offices. Only used to pick what
# to TRY next - the insurer's answer is the only thing that counts.
CITIES: dict[str, dict] = {
    "AHMEDABAD": {"state": "GUJARAT", "names": ("AHMEDABAD",),
                  "pincodes": ("380009", "380001", "380006", "380015", "380013",
                               "380007", "380054", "380051", "380052", "380061")},
    "VADODARA":  {"state": "GUJARAT", "names": ("VADODARA", "BARODA"),
                  "pincodes": ("390001", "390007", "390020")},
    "SURAT":     {"state": "GUJARAT", "names": ("SURAT",),
                  "pincodes": ("395003", "395001", "395007")},
    "RAJKOT":    {"state": "GUJARAT", "names": ("RAJKOT",),
                  "pincodes": ("360001", "360005")},
    "MUMBAI":    {"state": "MAHARASHTRA", "names": ("MUMBAI", "GREATER MUMBAI", "BOMBAY"),
                  "pincodes": ("400001", "400051", "400069")},
    "DELHI":     {"state": "DELHI", "names": ("NEW DELHI", "DELHI"),
                  "pincodes": ("110001", "110011", "110017")},
    "BENGALURU": {"state": "KARNATAKA", "names": ("BENGALURU", "BANGALORE"),
                  "pincodes": ("560001", "560025", "560034")},
    "CHENNAI":   {"state": "TAMIL NADU", "names": ("CHENNAI", "MADRAS"),
                  "pincodes": ("600001", "600002", "600017")},
    "KOLKATA":   {"state": "WEST BENGAL", "names": ("KOLKATA", "CALCUTTA"),
                  "pincodes": ("700001", "700016", "700091")},
    "PUNE":      {"state": "MAHARASHTRA", "names": ("PUNE",),
                  "pincodes": ("411001", "411004")},
    "HYDERABAD": {"state": "TELANGANA", "names": ("HYDERABAD",),
                  "pincodes": ("500001", "500003")},
    "JAIPUR":    {"state": "RAJASTHAN", "names": ("JAIPUR",),
                  "pincodes": ("302001", "302005")},
    "LUCKNOW":   {"state": "UTTAR PRADESH", "names": ("LUCKNOW",),
                  "pincodes": ("226001", "226010")},
}

# The metros, tried in this order once the home state has nothing left.
METRO_ORDER = ("MUMBAI", "DELHI", "BENGALURU", "CHENNAI", "KOLKATA", "PUNE",
               "HYDERABAD", "JAIPUR", "LUCKNOW")


def address_problem(reason: str) -> bool:
    """Is this a refusal that a different address can fix?"""
    low = (reason or "").lower()
    return "district" in low or ("pincode" in low and "combination" in low)


def city_of(pincode: str) -> str:
    """Which of our known cities a pincode belongs to, or ''."""
    for city, info in CITIES.items():
        if pincode in info["pincodes"]:
            return city
    return ""


def canonical_city(name: str) -> str:
    """'Ahmedabad', 'AHMEDABAD', 'Bangalore' -> our key, or the name upper-cased."""
    upper = (name or "").strip().upper()
    for city, info in CITIES.items():
        if upper == city or upper in info["names"]:
            return city
    return upper


# ------------------------------------------------------------------ storage

def _read() -> dict | None:
    """The notes, or None if the file exists but cannot be read right now
    (Windows briefly locking it, say) - a signal NOT to write over it."""
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            return None
    except ValueError:
        pass                            # damaged - rebuilt below
    # First run, deleted, or damaged: rebuild from the run logs.
    data = _learn_from_reports()
    _save(data)
    return data


def _load() -> dict:
    return _read() or {}


def _save(data: dict) -> None:
    # Temporary file then swap, so a killed run never leaves half a file.
    try:
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = NOTES_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True),
                        encoding="utf-8")
        os.replace(temp, NOTES_FILE)
    except Exception:
        pass


def _add(data: dict, insurer: str, pincode: str, city: str, ok: bool,
         reason: str, when: str) -> None:
    insurer = (insurer or "").strip().upper()
    pincode = re.sub(r"\D", "", pincode or "")
    if not insurer or len(pincode) != 6:
        return
    entry = data.setdefault(insurer, {}).setdefault(
        pincode, {"pass": 0, "fail": 0, "last": "", "city": ""})
    entry["pass" if ok else "fail"] += 1
    entry["city"] = canonical_city(city) or entry.get("city") or city_of(pincode)
    # Keep the reason readable: toasts arrive with their close glyph attached.
    reason = re.sub(r"^[^\w]+", "", reason or "").strip()
    entry["last"] = f"{when}: {'accepted' if ok else reason}"[:160]


def _learn_from_reports() -> dict:
    """Build the notes from every Preview answer an earlier run recorded."""
    data: dict = {}
    for log in sorted(REPORTS_DIR.glob("run-*/api-responses.log")):
        stamp = log.parent.name[4:12]
        when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in text.split("\n=== [")[1:]:
            if CHECK_URL not in block.split("\n", 1)[0].lower():
                continue
            found = re.search(r"--- sent:\n(.*?)\n--- received:\n(.*?)\n",
                              block + "\n", re.S)
            if not found:
                continue
            try:
                sent = json.loads(found.group(1))
                ok, reason = read_answer(json.loads(found.group(2)))
            except ValueError:
                continue
            # Only answers ABOUT the address teach us anything about it.
            if ok or address_problem(reason):
                comm = sent.get("CommunicationAddressDetails") or {}
                _add(data, sent.get("CompanyCode", ""), str(comm.get("Pincode", "")),
                     str(comm.get("CityName") or comm.get("City") or ""),
                     ok, reason, when)
    return data


def read_answer(body) -> tuple[bool, str]:
    """(accepted, reason) from the Preview request's JSON answer."""
    if not isinstance(body, dict):
        return False, ""
    response = body.get("Response") if isinstance(body.get("Response"), dict) else {}
    reason = str(response.get("ErrorMessage") or body.get("Error") or "").strip()
    failed = str(response.get("Status", "")).lower() in ("error", "failed", "failure")
    return (not failed and not reason), reason


def record(insurer: str, pincode: str, city: str, ok: bool,
           reason: str = "") -> None:
    """Add one answer from the live run. Never raises."""
    try:
        data = _read()
        if data is None:
            return                      # unreadable right now - keep what is there
        _add(data, insurer, pincode, city, ok, reason, date.today().isoformat())
        _save(data)
    except Exception:
        pass


# Cities an insurer's dropdown does not even list, kept apart from the pincode
# answers under a key no insurer code can collide with.
_NOT_LISTED = "_not_listed"


def record_not_offered(insurer: str, city: str) -> None:
    """This insurer's city list has no such city. Never raises."""
    try:
        data = _read()
        if data is None:
            return
        listed = data.setdefault(_NOT_LISTED, {}).setdefault(
            (insurer or "").strip().upper(), [])
        city = canonical_city(city)
        if city and city not in listed:
            listed.append(city)
            _save(data)
    except Exception:
        pass


def not_offered(insurer: str) -> list[str]:
    return list(_load().get(_NOT_LISTED, {}).get((insurer or "").strip().upper(), []))


# ---------------------------------------------------------------- reasoning

def _notes(insurer: str) -> dict:
    return _load().get((insurer or "").strip().upper(), {})


def _entry_city(pincode: str, entry: dict) -> str:
    return entry.get("city") or city_of(pincode)


def city_record(insurer: str) -> dict[str, dict[str, int]]:
    """{city: {"pass": n, "fail": n, "pincodes": n}} for this insurer."""
    out: dict[str, dict[str, int]] = {}
    for pincode, entry in _notes(insurer).items():
        row = out.setdefault(_entry_city(pincode, entry) or "?",
                             {"pass": 0, "fail": 0, "pincodes": 0})
        row["pass"] += entry.get("pass", 0)
        row["fail"] += entry.get("fail", 0)
        row["pincodes"] += 1
    return out


def refused_cities(insurer: str) -> list[str]:
    """
    Cities this insurer has turned down as a whole.

    Two refusals and no acceptance - or just one, once this insurer has shown
    that it refuses by city (another city already refused outright). Counted
    per PINCODE, not per request, so one stubborn pincode retried sixteen times
    still needs a second pincode before the city itself is condemned.
    """
    record_ = city_record(insurer)
    notes = _notes(insurer)

    def failed_pincodes(city: str) -> int:
        return sum(1 for p, e in notes.items()
                   if _entry_city(p, e) == city and e.get("fail") and not e.get("pass"))

    def dead(city: str, threshold: int) -> bool:
        return record_[city]["pass"] == 0 and failed_pincodes(city) >= threshold

    firm = [c for c in record_ if c != "?" and dead(c, 2)]
    if firm:
        return sorted(set(firm) | {c for c in record_ if c != "?" and dead(c, 1)})
    return []


def _bad(entry: dict) -> bool:
    return entry.get("fail", 0) > entry.get("pass", 0)


def _city_order(home: str) -> list[str]:
    """Home city, then the rest of its state, then the metros."""
    home = canonical_city(home)
    state = CITIES.get(home, {}).get("state", "")
    same_state = [c for c, i in CITIES.items() if i["state"] == state and c != home]
    order = ([home] if home in CITIES else []) + same_state + list(METRO_ORDER)
    return list(dict.fromkeys(order))


def choose(insurer: str, city: str, current: str = "",
           avoid=(), skip_cities=()) -> tuple[str, str, str]:
    """
    The address to use next: (pincode, city key, why) - or ("", "", "") to
    keep the current one, or ("", "", reason) when nothing is left to try.

    `avoid` holds pincodes refused in this run; `skip_cities` holds cities this
    run has learned are refused (or whose names the insurer's dropdown lacks).
    """
    insurer = (insurer or "").strip().upper()
    notes = _notes(insurer)
    skip = set(avoid)
    dead = (set(refused_cities(insurer)) | set(not_offered(insurer))
            | {canonical_city(c) for c in skip_cities})
    current_entry = notes.get(current, {})
    current_city = _entry_city(current, current_entry) or canonical_city(city)

    # Change NOTHING without evidence. An empty box is the filler's job (it
    # types the customer's own pincode), and a pincode nobody has refused - in
    # a city nobody has refused - is kept exactly as it is.
    if not current:
        return "", "", ""
    if current not in skip and current_entry.get("pass") and not _bad(current_entry):
        return "", "", ""
    if current not in skip and not current_entry.get("fail") \
            and current_city not in dead:
        return "", "", ""

    # 1. The current address IS refused: a pincode this insurer accepted before.
    proven = [(p, e) for p, e in notes.items()
              if p not in skip and e.get("pass") and not _bad(e)
              and _entry_city(p, e) not in dead]
    if proven:
        proven.sort(key=lambda pe: pe[1]["pass"] - pe[1].get("fail", 0), reverse=True)
        pincode, entry = proven[0]
        return pincode, _entry_city(pincode, entry), f"{insurer} accepted it before"

    # 2 & 3. Walk the ladder: same city while it is still plausible, then
    # other cities. A refused city is skipped whole - that is the lesson.
    for candidate in _city_order(city):
        if candidate in dead:
            continue
        for pincode in CITIES[candidate]["pincodes"]:
            entry = notes.get(pincode, {})
            if pincode in skip or pincode == current or _bad(entry):
                continue
            if candidate == current_city:
                why = f"another {candidate.title()} pincode"
            else:
                refused_whole = sorted(set(refused_cities(insurer)))
                why = (f"{insurer} refused "
                       f"{', '.join(c.title() for c in refused_whole) or current_city.title()}"
                       f" - trying {candidate.title()}")
            return pincode, candidate, why

    return "", "", (f"{insurer} refused every address tried: "
                    f"{summary(insurer)}")


def summary(insurer: str) -> str:
    """One line: what this insurer accepted and refused, city by city."""
    record_ = city_record(insurer)
    parts = []
    for city, row in sorted(record_.items()):
        verdict = "OK" if row["pass"] else "refused"
        parts.append(f"{city.title()} {verdict} ({row['pincodes']} pincode"
                     f"{'s' if row['pincodes'] != 1 else ''})")
    missing = not_offered(insurer)
    if missing:
        parts.append("not in its list: " + ", ".join(c.title() for c in missing))
    return "; ".join(parts) or "nothing tried yet"


def looks_hopeless(insurer: str) -> bool:
    """
    Two whole cities refused - the home city and one probe elsewhere - and
    nothing ever accepted: a data problem on the server, not in the test.

    Two is enough, and more only wastes runs: NATIONAL's lookup reads a table
    (sp_get_nic_city_master), and when neither Ahmedabad nor a completely
    different city has a row, walking the rest of India proves nothing new.
    It clears itself the moment any address is accepted - which the
    customer's own address will be, once the backend adds the missing rows.
    """
    notes = _notes(insurer)
    return (len(refused_cities(insurer)) >= 2
            and not any(e.get("pass") for e in notes.values()))