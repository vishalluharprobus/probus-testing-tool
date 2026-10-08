"""
Remember every quote matrix run, and learn from it.

WHAT IT REMEMBERS (reports/quote_matrix.json; cars: quote_matrix_car.json)
---------------------------------------------------------------------------
  pairs      how often each pair of choices has been tested, over all runs.
             The planner spends a short run on pairs never tested before, so
             coverage GROWS run after run instead of repeating itself.
  scenarios  the last result of every journey, by its stable key. The next
             time the same journey runs, a change is visible: an insurer that
             quoted last week and refuses today is a regression, not news.
  insurers   per insurer, per choice ("policy=OD Only", "year=2014"...): how
             often it quoted and how often it refused, and the reasons.
  relations  when each twin rule ("Comprehensive > Third Party") was last
             checked, so the twins rotate and none is forgotten.
  suspects   journeys that produced a finding, to re-run first next time: a
             finding that happens twice is a bug, once may be a blip.
  bad_bikes  vehicles the form would not accept, so they are not picked
             again (the name is from before cars; cars have their own file).
  form_blocks  choices the form OFFERS but will not let past - Proceed stays
             grey (a 4-year-old car on OD Only, 2026-10-05). Not planned
             again for BLOCK_RECHECK_DAYS, then tried once more in case the
             portal was fixed.

WHAT IT LEARNS
--------------
The point of the tallies is to reason at the right level. "ICICI refused 3
journeys" is noise. "ICICI refused all 3 journeys with OD Only and quoted 5 of
5 without it" is a RULE - ICICI does not sell OD Only here - and it is stated
once, as a rule, instead of as three separate failures. Refusals caused by an
outage ("server is down", timeouts) never count towards a rule: an outage is a
fact about Tuesday, not about the insurer.

Like the other notebooks, this never raises into a run.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from data import matrix
from pages.quote_list import Failure

REPORTS = Path(__file__).resolve().parent.parent / "reports"
FILES = {"bike": REPORTS / "quote_matrix.json",
         "car": REPORTS / "quote_matrix_car.json"}
NOTES_FILE = FILES["bike"]

# How long a "the form blocks this" lesson holds before it is re-checked.
BLOCK_RECHECK_DAYS = 14


def use(product: str, target: str = "") -> None:
    """Point this module at one product's notebook. A run tests one product,
    so what a car run learns never mixes with what bikes taught. The live site
    keeps its own notebooks too (quote_matrix_live.json, ..._car_live.json):
    live prices and test-site prices are not the same evidence."""
    global NOTES_FILE
    NOTES_FILE = FILES[product]
    if target == "live":
        NOTES_FILE = NOTES_FILE.with_name(f"{NOTES_FILE.stem}_live.json")

# Refusal kinds that say nothing about what the insurer sells.
OUTAGE_KINDS = ("insurer-down", "http", "silent")

# How many journeys of evidence before a pattern is called a rule.
# Three, not two: matrix journeys change several choices at once, so two
# refusals that share a value are too often a coincidence (a 12-journey run on
# 2026-09-30 "learned" that BAJAJ refuses previous insurer ICICI from two).
# The insurer lab (run_insurer_lab.py) proves rules one change at a time.
RULE_MIN_REFUSALS = 3


def _empty() -> dict:
    return {"runs": [], "pairs": {}, "scenarios": {}, "insurers": {},
            "relations": {}, "suspects": [], "bad_bikes": {}, "form_blocks": {}}


def _read() -> dict | None:
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _empty()
            base.update(data)
            return base
    except FileNotFoundError:
        return _empty()
    except OSError:
        return None                     # locked right now - do not overwrite
    except ValueError:
        pass
    return _empty()


def load() -> dict:
    return _read() or _empty()


def _save(data: dict) -> None:
    try:
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = NOTES_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        # Windows refuses to replace a file that antivirus or the search
        # indexer has open for a moment; a few short retries keep the lesson
        # instead of silently dropping it (a flaky test showed it, 2026-10-07).
        for attempt in range(6):
            try:
                os.replace(temp, NOTES_FILE)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        pass


# ---------------------------------------------------------------- for the plan

def history_pairs() -> dict[str, int]:
    return dict(load().get("pairs", {}))


def relation_age() -> dict[str, int]:
    """Days since each twin rule was last checked (missing = never)."""
    out = {}
    for relation, entry in load().get("relations", {}).items():
        try:
            last = date.fromisoformat(entry.get("last", "")[:10])
            out[relation] = (date.today() - last).days
        except ValueError:
            continue
    return out


def suspects() -> list[matrix.Scenario]:
    out = []
    for row in load().get("suspects", [])[:2]:
        try:
            values = tuple(row["values"])
            if len(values) == len(matrix.DIMENSIONS):
                out.append(matrix.Scenario(
                    values=values, why=f"re-check: {row.get('why', 'finding last run')}"))
        except (KeyError, TypeError):
            continue
    return out


def bad_bikes() -> set[str]:
    return set(load().get("bad_bikes", {}))


def tried_bikes() -> set[str]:
    """Every bike a journey has used, so new runs can prefer new bikes."""
    bikes = set()
    for key in load().get("scenarios", {}):
        bikes.add(key.split(" | ", 1)[0].upper())
    return bikes


def last_result(key: str) -> dict:
    return load().get("scenarios", {}).get(key, {})


def blocked(today: date | None = None) -> frozenset:
    """The form blocks learned within BLOCK_RECHECK_DAYS, as block keys
    ("OD Only|4") for matrix.plan(blocked=...)."""
    today = today or date.today()
    out = set()
    for key, entry in load().get("form_blocks", {}).items():
        try:
            when = date.fromisoformat(str(entry.get("last", ""))[:10])
        except ValueError:
            continue
        if (today - when).days < BLOCK_RECHECK_DAYS:
            out.add(key)
    return frozenset(out)


def record_block(key: str, why: str) -> None:
    """The form would not let this choice past (see blocked())."""
    try:
        data = _read()
        if data is None:
            return
        entry = data["form_blocks"].setdefault(key, {"seen": 0})
        entry["seen"] = entry.get("seen", 0) + 1
        entry["last"] = date.today().isoformat()
        entry["why"] = why[:200]
        _save(data)
    except Exception:
        pass


def record_unblock(key: str) -> None:
    """A re-check got through: the portal was fixed, forget the lesson."""
    try:
        data = _read()
        if data is not None and data["form_blocks"].pop(key, None) is not None:
            _save(data)
    except Exception:
        pass


# ------------------------------------------------------------------ recording

def record_journey(s: matrix.Scenario, offers: dict, declines: list,
                   kinds: dict[str, str], quotation: str) -> list[str]:
    """
    Write one finished journey down. Returns the regression lines: changes
    since the last time this exact journey ran.

    kinds: insurer -> failure kind (probus-rule, insurer-down, our-defect...)
    """
    changes: list[str] = []
    try:
        data = _read()
        if data is None:
            return changes
        today = date.today().isoformat()

        before = data["scenarios"].get(s.key, {})
        old_offers = before.get("offers", {})
        for code, old in sorted(old_offers.items()):
            if code in offers:
                new = offers[code].premium
                if old and new and abs(new - old) / old > 0.25:
                    changes.append(f"{code}: price moved {old:,.0f} -> {new:,.0f} "
                                   f"({(new - old) / old:+.0%}) since "
                                   f"{before.get('last', 'last time')}")
            elif code in kinds and kinds[code] not in OUTAGE_KINDS:
                reason = next((d.reason for d in declines if d.insurer == code), "")
                changes.append(f"{code}: quoted Rs {old:,.0f} on "
                               f"{before.get('last', 'last time')}, REFUSES now: "
                               f"{reason[:70]}")
        for code in sorted(set(offers) - set(old_offers)):
            if before and code in before.get("declines", {}):
                changes.append(f"{code}: refused on {before.get('last')}, "
                               f"quotes now (Rs {offers[code].premium:,.0f})")

        data["scenarios"][s.key] = {
            "last": today, "values": list(s.values), "quotation": quotation,
            "runs": before.get("runs", 0) + 1,
            "offers": {c: a.premium for c, a in offers.items()},
            "declines": {d.insurer: kinds.get(d.insurer, d.source) for d in declines},
        }

        for unit in s.pairs():
            k = matrix.pair_key(unit)
            data["pairs"][k] = data["pairs"].get(k, 0) + 1

        live = [(d, v) for d, v in zip(matrix.DIMENSIONS, s.values) if v is not None]
        asked = set(offers) | {d.insurer for d in declines}
        for code in asked:
            entry = data["insurers"].setdefault(code, {"asked": 0, "quoted": 0,
                                                       "dims": {}, "reasons": {}})
            entry["asked"] += 1
            quoted = code in offers
            kind = kinds.get(code, "")
            reason = next((d.reason for d in declines if d.insurer == code), "")
            if quoted:
                entry["quoted"] += 1
            else:
                entry["reasons"][reason[:80]] = entry["reasons"].get(reason[:80], 0) + 1
            if not quoted and kind in OUTAGE_KINDS:
                continue                # an outage teaches nothing about rules
            if not quoted and "same insurer" in reason.lower():
                # Explained by the previous insurer, not by the policy type or
                # the bike - counting it would teach "BAJAJ never quotes
                # Comprehensive" from journeys where BAJAJ was the previous one.
                continue
            for dim, value in live:
                cell = entry["dims"].setdefault(f"{dim}={value}",
                                                {"quoted": 0, "refused": 0})
                cell["quoted" if quoted else "refused"] += 1
        _save(data)
    except Exception:
        pass
    return changes


def record_run(journeys: int, findings: list, relations: dict[str, str],
               suspects_: list[tuple[matrix.Scenario, str]],
               refused_bikes: dict[str, str]) -> None:
    try:
        data = _read()
        if data is None:
            return
        today = date.today().isoformat()
        data["runs"] = (data["runs"] + [{
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "journeys": journeys,
            "defects": sum(1 for f in findings if f.severity == "DEFECT"),
            "looks": sum(1 for f in findings if f.severity == "LOOK"),
        }])[-40:]
        for relation, state in relations.items():
            if state == "not checked":
                continue
            entry = data["relations"].setdefault(relation, {"checked": 0, "broken": 0})
            entry["checked"] += 1
            entry["broken"] += state == "broken"
            entry["last"] = f"{today} {state}"
        data["suspects"] = [{"values": list(s.values), "why": why[:120]}
                            for s, why in suspects_][:4]
        for bike, why in refused_bikes.items():
            data["bad_bikes"][bike.upper()] = f"{today}: {why[:120]}"
        _save(data)
    except Exception:
        pass


# ------------------------------------------------------------------- learning

def rules(insurer: str | None = None) -> list[tuple[str, str]]:
    """
    What the evidence says each insurer does, as (insurer, sentence).

    A rule needs: RULE_MIN_REFUSALS or more refusals for one value, not one
    quote for that value, and at least one quote for another value of the
    same choice. "Never quotes anything" is its own, stronger sentence.
    """
    out: list[tuple[str, str]] = []
    data = load()
    for code, entry in sorted(data.get("insurers", {}).items()):
        if insurer and code != insurer.upper():
            continue
        asked, quoted = entry.get("asked", 0), entry.get("quoted", 0)
        if asked >= 3 and quoted == 0:
            top = max(entry.get("reasons", {"no reason given": 1}).items(),
                      key=lambda kv: kv[1])[0]
            kind = Failure("", top).kind
            if kind == "insurer-down":
                out.append((code, f"never quoted in {asked} journeys - its service "
                                  f"was down: '{top}'. An outage, not a rule."))
            elif kind == "our-defect":
                out.append((code, f"never quoted in {asked} journeys - OUR code "
                                  f"fails for it: '{top}'. Fix that first."))
            else:
                out.append((code, f"never quoted in {asked} journeys - usually "
                                  f"'{top}'. Likely not set up for this broker/"
                                  f"product, or refuses everything we send."))
            continue
        dims = entry.get("dims", {})
        by_dim: dict[str, dict[str, dict]] = {}
        for cell, tally in dims.items():
            dim, value = cell.split("=", 1)
            by_dim.setdefault(dim, {})[value] = tally
        suspects: list[tuple[str, str, int, str]] = []
        for dim, values in by_dim.items():
            quoted_somewhere = any(t["quoted"] for t in values.values())
            if not quoted_somewhere:
                continue
            for value, tally in sorted(values.items()):
                if tally["quoted"] == 0 and tally["refused"] >= RULE_MIN_REFUSALS:
                    others = ", ".join(v for v, t in sorted(values.items())
                                       if t["quoted"])[:60]
                    suspects.append((dim, value, tally["refused"], others))
        prove = f"run_insurer_lab.py --insurer {code} --product {product()}"
        # Choices refused the same number of times were, as a rule, the SAME
        # journeys: the standard quote and its twins share RTO, vehicle, year
        # and previous insurer. Four "rules" from one set of journeys is one
        # rule with four suspects (CHOLAMANDLAM cars, 2026-10-05), and it is
        # said once, as that.
        by_count: dict[int, list[tuple]] = {}
        for item in suspects:
            by_count.setdefault(item[2], []).append(item)
        for count, group in sorted(by_count.items(), reverse=True):
            if len(group) == 1:
                dim, value, refused, others = group[0]
                out.append((code, f"seems not to quote when {matrix.NICE[dim]} = "
                                  f"{value} (refused {refused} of {refused}); quotes "
                                  f"for {others}. Prove it: {prove}"))
                continue
            which = "; ".join(f"{matrix.NICE[d]} = {v}" for d, v, _, _ in group)
            out.append((code, f"refused all {count} journeys that had {which} - "
                              f"these always came together, so ONE of them is the "
                              f"reason. Find which: {prove}"))
    return out


def product() -> str:
    """Which product's notebook this module is pointed at (see use())."""
    return next((name for name, path in FILES.items() if path == NOTES_FILE), "bike")


def coverage_line(wanted_pairs: set[str]) -> str:
    pairs = load().get("pairs", {})
    done = sum(1 for p in wanted_pairs if pairs.get(p))
    return f"{done} of {len(wanted_pairs)} pairs tested at least once, over all runs"


def report() -> str:
    """Everything the notebook knows, for `python -m core.quotenotes`."""
    data = load()
    if not data.get("runs"):
        return (f"No {product()} quote matrix runs yet - run: python "
                f"run_quote_matrix.py --product {product()}")
    lines = [f"Runs so far: {len(data['runs'])} (last {data['runs'][-1]['at']})",
             f"Journeys remembered: {len(data['scenarios'])}",
             f"Pairs tested at least once: {sum(1 for n in data['pairs'].values() if n)}",
             "", "INSURERS"]
    for code, entry in sorted(data["insurers"].items()):
        lines.append(f"  {code:<14} quoted {entry['quoted']} of {entry['asked']}")
    learned = rules()
    lines += ["", "WHAT THE EVIDENCE SAYS"]
    lines += [f"  {code:<14} {text}" for code, text in learned] or ["  nothing yet"]
    lines += ["", "TWIN RULES"]
    for relation, text in matrix.RELATIONS.items():
        entry = data["relations"].get(relation)
        state = (f"checked {entry['checked']}x, broken {entry['broken']}x, "
                 f"last {entry.get('last', '?')}") if entry else "never checked"
        lines.append(f"  {text}\n      {state}")
    if data.get("bad_bikes"):
        lines += ["", "VEHICLES THE FORM REFUSED (not picked again)"]
        lines += [f"  {b}: {why}" for b, why in sorted(data["bad_bikes"].items())]
    if data.get("form_blocks"):
        lines += ["", f"CHOICES THE FORM OFFERS BUT BLOCKS (re-checked after "
                      f"{BLOCK_RECHECK_DAYS} days)"]
        for key, entry in sorted(data["form_blocks"].items()):
            policy, _, age = key.partition("|")
            lines.append(f"  {policy}, {age} years old: {entry.get('why', '')} "
                         f"(last {entry.get('last', '?')}, seen {entry.get('seen', 1)}x)")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    # python -m core.quotenotes             bikes
    # python -m core.quotenotes car         cars
    # python -m core.quotenotes bike live   what the LIVE site taught
    words = sys.argv[1:]
    use(next((w for w in words if w in FILES), "bike"),
        "live" if "live" in words else "")
    print(report())
