"""
Remember which vehicle registration numbers the portal accepted.

WHY THIS EXISTS
---------------
Continuing past Vehicle Details makes the app call
carinfo/ValidateVehicleDetailsWithCompany, which looks the registration number
up in the RTO's records and compares THAT vehicle with the one being insured.
The harness used to invent a fresh number every run, so whether the step passed
was pure luck. The runs up to 2026-09-25 show it:

  accepted   GJ-01-XV-1549, GJ-01-NC-1113, GJ-01-EX-1530, GJ-01-FY-1605
  rejected   "Vehicle sub class is not matching with vehicle registration
              number data."          - the number belongs to, say, a car
             "Vehicle Cubic capacity is not matching ..." - a bigger bike
             "We are unable to retrieve vehicle details !!" - no such vehicle

Every one of those is a problem with the NUMBER, not with the journey, so the
cure is a different number - and the best different number is one that has
already worked.

So every answer is written down here, and the next number is chosen from what
worked before. The first time this runs it reads the answers already sitting in
reports/run-*/api-responses.log, so it starts out knowing every number the
harness has ever tried rather than learning from zero.

BIKES AND CARS ARE KEPT APART (2026-10-05)
------------------------------------------
A plate that passed for a bike is a bike's plate; offered on a car journey it
can only fail "sub class is not matching". So each product has its own pool
("numbers" for bikes, as before, "numbers_car" for cars), told apart by the
SubProduct the check itself sends (MTRTW / MTRPC). And the two pools teach
each other: a plate refused on a bike as "sub class is not matching" belongs
to some OTHER kind of vehicle - very often a car - so it is the first thing a
car journey tries when it has no proven car plate yet (and the other way
round).

Like kycnotes, this file holds OBSERVATIONS with dates, and never raises: a
notes file is not worth failing a run over.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import date
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
NOTES_FILE = REPORTS_DIR / "vehicle_numbers.json"

# Lower-case fragment of the endpoint that checks the number.
CHECK_URL = "validatevehicledetails"

# Plates skip I and O - too easily read as 1 and 0.
_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# Where each product's plates live in the file.
POOLS = {"bike": "numbers", "car": "numbers_car"}


def product_of(sub_product: str) -> str:
    """'MTRPC' (the check's SubProduct) -> 'car'; anything else -> 'bike'."""
    return "car" if "PC" in (sub_product or "").upper() else "bike"


def normalise(number: str) -> str:
    """'GJ-01-KS-1136', 'gj01ks1136' and 'GJ 01 KS 1136' are one number."""
    return re.sub(r"[^A-Z0-9]", "", (number or "").upper())


def read_answer(body) -> tuple[bool, str]:
    """
    (accepted, reason) from the check's JSON.

    The verdict is inside an HTTP 200: Response.success says yes or no, and the
    reason sits in Error - even on success, where it can read "By default true
    by system", meaning the check was skipped rather than passed.
    """
    if not isinstance(body, dict):
        return False, ""
    response = body.get("Response")
    ok = bool(response.get("success")) if isinstance(response, dict) else False
    reason = str(body.get("Error") or body.get("Message") or "").strip()
    return ok, reason


def number_problem(reason: str) -> bool:
    """Is this a rejection that a DIFFERENT registration number can fix?"""
    low = (reason or "").lower()
    return ("registration number data" in low
            or "unable to retrieve vehicle details" in low)


def _kind(ok: bool, reason: str) -> str:
    low = (reason or "").lower()
    if ok:
        # Skipped, not checked - says nothing about the RTO record.
        return "default_pass" if "by default" in low else "pass"
    if "unable to retrieve" in low:
        return "not_found"
    if "registration number data" in low:
        return "mismatch"
    return "other"


def _add(data: dict, insurer: str, number: str, ok: bool, reason: str,
         when: str, product: str = "bike") -> None:
    key = normalise(number)
    if not key:
        return
    entry = data.setdefault(POOLS[product], {}).setdefault(key, {
        "pass": 0, "default_pass": 0, "mismatch": 0, "not_found": 0,
        "other": 0, "passed_for": [], "last": ""})
    kind = _kind(ok, reason)
    entry[kind] = entry.get(kind, 0) + 1
    if "sub class" in (reason or "").lower():
        # A different KIND of vehicle - what the other pool wants to hear.
        entry["sub_class"] = entry.get("sub_class", 0) + 1
    insurer = (insurer or "").strip().upper()
    if kind == "pass" and insurer and insurer not in entry["passed_for"]:
        entry["passed_for"].append(insurer)
    entry["last"] = f"{when} {insurer}: {'accepted' if ok else reason}"[:160]


def _learn_from_reports() -> dict:
    """Build the notes from every check any earlier run recorded."""
    data: dict = {"numbers": {}}
    for log in sorted(REPORTS_DIR.glob("run-*/api-responses.log")):
        stamp = log.parent.name[4:12]                # run-YYYYMMDD-HHMMSS
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
            _add(data, sent.get("Company", ""), sent.get("RegistrationNumber", ""),
                 ok, reason, when, product_of(sent.get("SubProduct", "")))
    return data


def _save(data: dict) -> None:
    # Write a temporary file and swap it in, so a run killed mid-write can
    # never leave half a file behind.
    try:
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = NOTES_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True),
                        encoding="utf-8")
        os.replace(temp, NOTES_FILE)
    except Exception:
        pass


def _read() -> dict | None:
    """The notes, or None if the file exists but cannot be read right now
    (Windows briefly locking it, say) - a signal NOT to write over it."""
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("numbers"), dict):
            return data
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            return None
    except ValueError:
        pass                            # damaged - rebuilt below
    # First run, deleted, or damaged: rebuild from the run logs, which hold
    # every answer the app ever gave, live ones included.
    data = _learn_from_reports()
    _save(data)
    return data


def _load() -> dict:
    return _read() or {"numbers": {}}


def record(insurer: str, number: str, ok: bool, reason: str = "",
           product: str = "bike") -> None:
    """Add one answer from the live run. Never raises."""
    try:
        data = _read()
        if data is None:
            return                      # unreadable right now - keep what is there
        _add(data, insurer, number, ok, reason, date.today().isoformat(), product)
        _save(data)
    except Exception:
        pass


def _failures(entry: dict) -> int:
    # "Unable to retrieve vehicle details" sometimes comes back in half a
    # second - the lookup service failing, not the vehicle missing. It only
    # counts against a number that has never passed, so one bad-service run
    # cannot condemn the whole proven pool.
    not_found = entry.get("not_found", 0) if not entry.get("pass") else 0
    return entry.get("mismatch", 0) + not_found


def _bad(entry: dict) -> bool:
    # The check is not perfectly consistent: run 20260915-152839 sent
    # GJ-01-EX-1530 twice with identical data and was told "Cubic capacity is
    # not matching", then "success". So one rejection does not condemn a
    # number that has also passed - only more rejections than passes do.
    return _failures(entry) > entry.get("pass", 0)


def fresh(prefix: str, taken: set[str]) -> str:
    """A new random number for this RTO that has never been tried."""
    rng = random.Random()
    key = ""
    for _ in range(500):
        key = (f"{prefix}{rng.choice(_LETTERS)}{rng.choice(_LETTERS)}"
               f"{rng.randint(1, 9999):04d}")
        if key not in taken:
            break
    return key


def choose(rto: str, insurer: str = "", avoid=(),
           product: str = "bike") -> tuple[str, str]:
    """
    The best registration number to try next, and why it was picked.

    Best first, from this product's own pool:
      1. numbers the RTO check really ACCEPTED - this insurer's first
      2. numbers only waved through "by default" - not rejected, not proven
      3. numbers the OTHER product was refused as "sub class is not matching"
         - another kind of vehicle, quite possibly this kind
      4. a brand-new random number that nobody has tried yet

    A number rejected more often than it passed is never offered again, and
    neither is anything in `avoid` - the numbers already tried in this run.
    """
    code = re.match(r"\s*([A-Za-z]{2})[-\s]?(\d{1,2})", rto or "")
    prefix = f"{code.group(1).upper()}{int(code.group(2)):02d}" if code else "GJ01"
    skip = {normalise(n) for n in avoid}
    data = _load()
    numbers = data.get(POOLS[product], {})
    insurer = (insurer or "").strip().upper()

    usable = [(key, entry) for key, entry in numbers.items()
              if key.startswith(prefix) and key not in skip and not _bad(entry)]

    proven = [(k, e) for k, e in usable if e.get("pass")]
    if proven:
        # Cleanest record first; this insurer's own passes only break ties.
        # The check compares the RTO record, not anything insurer-specific,
        # so a clean number beats a mixed one that this insurer once took.
        proven.sort(key=lambda ke: (ke[1].get("pass", 0) - _failures(ke[1]),
                                    insurer in ke[1].get("passed_for", []),
                                    ke[1].get("pass", 0)), reverse=True)
        key, entry = proven[0]
        return key, (f"accepted before for "
                     f"{', '.join(entry.get('passed_for', [])) or 'another insurer'}")

    lenient = [(k, e) for k, e in usable
               if e.get("default_pass") and not _failures(e)]
    if lenient:
        return lenient[0][0], "let through before ('by default'), never rejected"

    other = "car" if product == "bike" else "bike"
    hints = [k for k, e in data.get(POOLS[other], {}).items()
             if k.startswith(prefix) and k not in skip and k not in numbers
             and not e.get("pass")
             and (e.get("sub_class") or "sub class" in e.get("last", "").lower())]
    if hints:
        return hints[0], (f"refused for a {other} as 'sub class is not matching' "
                          f"- another kind of vehicle, maybe a {product}")

    return (fresh(prefix, set(numbers) | skip),
            "new random number - no proven one left to reuse")


def known(product: str = "bike") -> str:
    """One line for a run report: how much the notes know."""
    numbers = _load().get(POOLS[product], {})
    good = sum(1 for e in numbers.values() if e.get("pass") and not _bad(e))
    bad = sum(1 for e in numbers.values() if _bad(e))
    return f"{good} proven, {bad} rejected, {len(numbers)} tried in total"
