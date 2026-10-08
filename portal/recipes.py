"""
From what a developer picked in the Studio to the exact command a runner gets.

Every test type is one of the command-line tools this repository already has,
so a run from the browser and a run from the terminal are the same run. The
command is shown in the Studio, so anyone can copy it and run it by hand.

Rules decided HERE, before anything starts (the runners and core/safety.py
check again - this is the friendly early "no", not the guard rail):
  * the live site takes the quote sweep only, and only with named companies
  * an insurer deep-dive needs exactly one company
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from data import matrix

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

PRODUCTS = {"bike": "Two-wheeler", "car": "Private car"}

TARGETS = {
    "local": {"label": "Local dev", "blurb": "The Angular app and API on the host "
              "computer (localhost:4200 / :53339)"},
    "testsite": {"label": "Test site", "blurb": "test.probusinsurance.com - always "
                 "up, shared with the team"},
    "live": {"label": "Live production", "blurb": "buy.probusinsurance.com - real "
             "insurers, quotes only"},
}

# How thorough a quote sweep is: one value of the matrix runner's knobs each.
LEVELS = {
    "smoke": {"label": "One quote", "args": ["--max", "1", "--no-twins"],
              "blurb": "The standard journey once - is everything alive?"},
    "quick": {"label": "Quick", "args": ["--depth", "1", "--all"],
              "blurb": "Every choice at least once"},
    "standard": {"label": "Standard", "args": ["--max", "12"],
                 "blurb": "12 well-chosen journeys"},
    "full": {"label": "Full", "args": ["--all"],
             "blurb": "Every PAIR of choices meets at least once"},
    "deep": {"label": "Deep", "args": ["--depth", "3", "--all"],
             "blurb": "Every THREE choices together - the deepest check"},
}

# How a quote sweep reaches the quotes (core/fastsweep.py). Fast is the
# default: 12 journeys took 1.8 minutes instead of ~36 (2026-10-07).
SPEEDS = {
    "fast": {"label": "Fast", "args": ["--fast"],
             "blurb": "One real browser journey, then straight to the API - "
                      "minutes, not hours. Same checks and reports."},
    "browser": {"label": "Real browser", "args": [],
                "blurb": "Clicks every screen like a customer - ~3 min a journey. "
                         "Also catches form problems (a stuck Proceed, a missing card)."},
}

TESTS = {
    "sweep": {
        "label": "Quote sweep", "script": "run_quote_matrix.py",
        "blurb": "Many scenarios across many companies, up to the quote list. "
                 "Checks every premium, GST, NCB and IDV - and learns.",
        "targets": ("local", "testsite", "live"),
        "uses": ("insurers", "vehicles", "rtos", "policy", "level"),
    },
    "lab": {
        "label": "Insurer deep-dive", "script": "run_insurer_lab.py",
        "blurb": "ONE company, ~150 quotes straight to its API: every add-on, "
                 "NCB, IDV and accessory - and the rules it learns.",
        "targets": ("local", "testsite"),
        "uses": ("insurer", "vehicle", "rto", "journeys"),
    },
    "journey": {
        "label": "Buy journey", "script": "run_proposal_test.py",
        "blurb": "One company from quote through KYC and the proposal form - "
                 "never pays.",
        "targets": ("local", "testsite"),
        "uses": ("insurer", "stage"),
    },
    "best": {
        "label": "Best vehicle finder", "script": "find_best_vehicle.py",
        "blurb": "Which city and vehicle get prices from the MOST companies - "
                 "the best place to start testing.",
        "targets": ("local", "testsite"),
        "uses": (),
    },
}

STAGES = {
    "kyc-check": {"label": "Fill KYC, don't submit", "args": []},
    "kyc-submit": {"label": "Submit KYC", "args": ["--submit-kyc"]},
    "proposal": {"label": "Up to the proposal Preview", "args": ["--proposal"]},
}

MAX_VEHICLES = 6
MAX_RTOS = 8
RTO_PATTERN = re.compile(r"^[A-Z]{2}-\d{1,2}[A-Z]?\s+\S")


class Invalid(ValueError):
    """A choice that cannot run - said in a sentence the Studio shows."""


@dataclass
class Recipe:
    test: str
    target: str
    product: str
    command: list[str]
    title: str
    lock: str                     # runs sharing a lock never run at once
    plan_command: list[str] | None = None
    summary: dict = field(default_factory=dict)


def _clean_list(values, upper: bool = False) -> list[str]:
    out = []
    for v in values or []:
        v = str(v).strip()
        if v:
            out.append(v.upper() if upper else v)
    return list(dict.fromkeys(out))


def build(form: dict, known_insurers: set[str] | None = None) -> Recipe:
    """Validate a Studio form and turn it into a Recipe. Raises Invalid."""
    test = form.get("test") or "sweep"
    target = form.get("target") or "testsite"
    product = form.get("product") or "bike"
    if test not in TESTS:
        raise Invalid(f"Unknown test {test!r}.")
    if target not in settings.TARGETS:
        raise Invalid(f"Unknown server {target!r}.")
    if product not in PRODUCTS:
        raise Invalid(f"Unknown product {product!r}.")
    spec = TESTS[test]
    if target not in spec["targets"]:
        raise Invalid(f"{spec['label']} cannot run on {TARGETS[target]['label']}: "
                      f"the live site is quotes only. Pick the Quote sweep, or "
                      f"the Test site.")

    insurers = _clean_list(form.get("insurers"), upper=True)
    unknown = [c for c in insurers if known_insurers and c not in known_insurers]
    if unknown:
        raise Invalid(f"Unknown company code {unknown[0]!r}.")
    vehicles = _clean_list(form.get("vehicles"))
    rtos = _clean_list(form.get("rtos"))
    for r in rtos:
        if not RTO_PATTERN.match(r):
            raise Invalid(f"RTO {r!r} is not in the portal's form 'GJ-01 Ahmedabad'.")

    head = [PYTHON, "-u", spec["script"]]
    common = ["--target", target, "--product", product]
    watch = bool(form.get("watch"))
    tail = [] if watch else ["--headless"]
    who = TARGETS[target]["label"]
    noun = PRODUCTS[product]

    if test == "sweep":
        if target == "live" and not insurers:
            raise Invalid("On the live site, pick the companies to ask - each journey "
                          "sends a real quote request to every company asked.")
        if len(vehicles) > MAX_VEHICLES:
            raise Invalid(f"At most {MAX_VEHICLES} vehicles in one sweep.")
        if len(rtos) > MAX_RTOS:
            raise Invalid(f"At most {MAX_RTOS} RTOs in one sweep.")
        level = form.get("level") or "standard"
        if level not in LEVELS:
            raise Invalid(f"Unknown level {level!r}.")
        args = list(common)
        if insurers:
            args += ["--insurers", ",".join(insurers)]
        for v in vehicles:
            args += ["--vehicle", v]
        for r in rtos:
            args += ["--rto", r]
        pins = _policy_pins(form)
        args += pins
        args += LEVELS[level]["args"]
        speed = form.get("speed") or "fast"
        if speed not in SPEEDS:
            raise Invalid(f"Unknown speed {speed!r}.")
        args += SPEEDS[speed]["args"]
        command = head + args + tail
        plan = head + args + ["--plan"]
        companies = ", ".join(insurers) if insurers else "every company"
        title = (f"{noun} quote sweep · {LEVELS[level]['label']}"
                 f"{' · fast' if speed == 'fast' else ''} · {companies}")
        lock = f"sweep-{product}-{'live' if target == 'live' else 'shared'}"
        return Recipe(test, target, product, command, title, lock, plan,
                      {"level": level, "speed": speed, "insurers": insurers, "vehicles": vehicles,
                       "rtos": rtos, "server": who})

    if test == "lab":
        if len(insurers) != 1:
            raise Invalid("Insurer deep-dive tests ONE company - pick exactly one.")
        args = ["--insurer", insurers[0]] + common
        if len(vehicles) > 1 or len(rtos) > 1:
            raise Invalid("Insurer deep-dive starts from one vehicle and one RTO.")
        if vehicles:
            args += ["--vehicle", vehicles[0].replace("|", " ")]
        if rtos:
            args += ["--rto", rtos[0]]
        # How many priced quotes go on through KYC to the payment page. The
        # runner's own default is ALL of them, so "nothing chosen" must say 0.
        journeys = str(form.get("journeys") or "0")
        if journeys == "all":
            pass
        elif journeys.isdigit() and 0 <= int(journeys) <= 10:
            args += ["--journeys", journeys]
        else:
            raise Invalid(f"Journeys to payment must be 0-10 or 'all', not {journeys!r}.")
        title = f"{noun} deep-dive · {insurers[0]}"
        return Recipe(test, target, product, head + args + tail, title,
                      f"lab-{insurers[0]}-{product}",
                      summary={"insurers": insurers, "vehicles": vehicles,
                               "rtos": rtos, "server": who})

    if test == "journey":
        if len(insurers) > 1:
            raise Invalid("A buy journey drives one company - pick one, or none "
                          "to let the tool choose.")
        stage = form.get("stage") or "kyc-check"
        if stage not in STAGES:
            raise Invalid(f"Unknown stage {stage!r}.")
        insurer = insurers[0] if insurers else "AUTO"
        args = ["--insurer", insurer] + common + STAGES[stage]["args"]
        title = f"{noun} buy journey · {insurer} · {STAGES[stage]['label']}"
        return Recipe(test, target, product, head + args + tail, title,
                      f"journey-{target}",
                      summary={"insurers": [insurer], "stage": stage, "server": who})

    # best
    args = list(common)
    title = f"{noun} best vehicle finder"
    return Recipe(test, target, product, head + args + tail, title,
                  f"best-{target}-{product}", summary={"server": who})


def _policy_pins(form: dict) -> list[str]:
    out = []
    allowed = {"policy": (matrix.CP, matrix.TP, matrix.OD),
               "previous": matrix.PREVIOUS, "ncb": matrix.NCB_VALUES,
               "claim": ("No", "Yes")}
    for name, flag in (("policy", "--policy"), ("years", "--year"),
                       ("previous", "--previous"), ("ncb", "--ncb"),
                       ("claim", "--claim")):
        for value in _clean_list(form.get(name)):
            if name in allowed and value not in allowed[name]:
                raise Invalid(f"{value!r} is not a {name} the form offers.")
            if name == "years" and not (value.isdigit()
                                        and 1990 <= int(value) <= matrix.this_year()):
                raise Invalid(f"{value!r} is not a registration year.")
            out += [flag, value]
    return out


def shown(command: list[str]) -> str:
    """The command as someone would type it in the project folder."""
    def q(part: str) -> str:
        return f'"{part}"' if re.search(r"[\s|()&]", part) else part
    parts = ["venv\\Scripts\\python"] + [p for p in command[1:] if p != "-u"]
    return " ".join(q(p) for p in parts)


# ---------------------------------------------------------- the plan preview

PLAN_LINE = re.compile(r"THE PLAN - (\d+) journeys \(~(\d+) h (\d+) min")
COVERAGE = re.compile(r"Coverage: this plan tests (\d+) of (\d+) (\w+)")
JOURNEY = re.compile(r"^\s+(\d+)\. \[(\w+)\s*\] (.+)$")


def read_plan(output: str) -> dict:
    """The parts of `run_quote_matrix.py --plan` the Studio shows."""
    found = PLAN_LINE.search(output)
    if not found:
        reason = next((line.strip() for line in output.splitlines()
                       if line.startswith(("Cannot plan", "Unknown insurer",
                                           "On the live site"))), "")
        detail = output.strip().splitlines()[-3:] if output.strip() else []
        return {"ok": False, "error": reason or " ".join(detail)[:400]
                or "the planner gave no plan"}
    journeys, hours, minutes = map(int, found.groups())
    cover = COVERAGE.search(output)
    lines = output.splitlines()
    items = []
    for i, line in enumerate(lines):
        m = JOURNEY.match(line)
        if m:
            why = lines[i + 1].strip() if i + 1 < len(lines) else ""
            items.append({"n": int(m.group(1)), "kind": m.group(2),
                          "text": m.group(3).strip(), "why": why})
    choices = {}
    in_choices = False
    for line in lines:
        if line.startswith("THE CHOICES"):
            in_choices = True
            continue
        if in_choices:
            if not line.strip() or line.startswith("THE PLAN"):
                break
            key, _, value = line.strip().partition("  ")
            choices.setdefault(key.strip(), []).append(value.strip())
    return {"ok": True, "journeys": journeys, "minutes": hours * 60 + minutes,
            "covered": int(cover.group(1)) if cover else None,
            "wanted": int(cover.group(2)) if cover else None,
            "unit": cover.group(3) if cover else "",
            "items": items[:60], "choices": choices}


# ------------------------------------------------------------ the outcome

OUTCOMES = {
    "sweep": {0: ("passed", "Every check passed"), 1: ("bugs", "Bugs found"),
              2: ("failed", "Could not plan"), 3: ("failed", "Refused by a guard rail"),
              4: ("failed", "Login failed"), 5: ("failed", "Nobody gave a price"),
              6: ("failed", "The environment is down")},
}
OUTCOMES["lab"] = OUTCOMES["sweep"]


def outcome(test: str, code: int | None) -> tuple[str, str]:
    """(status, words) for a finished run's exit code."""
    if code is None:
        return "failed", "Stopped unexpectedly"
    table = OUTCOMES.get(test, {})
    if code in table:
        return table[code]
    if code == 0:
        return "passed", "Finished"
    if code == 3:
        return "failed", "Refused by a guard rail"
    if code == 4:
        return "failed", "Login failed"
    return "bugs" if code == 1 else "failed", f"Finished with problems (exit {code})"
