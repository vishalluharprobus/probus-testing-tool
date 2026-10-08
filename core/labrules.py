"""
Learn an insurer's own validation rules from its error messages - and obey them.

THE IDEA
--------
While an integration is being built, the insurer refuses things all the time:

    "Electrical accessories value should not be more than 10000"
    "Zero depreciation is not available for vehicles older than 5 years"
    "IDV should be between 245000 and 330000"

Each of those is a COMPANY-SIDE VALIDATION: the insurer's rule, not our bug.
Sending 25,000 of electrical accessories again tomorrow teaches nothing and
wastes a call. So every refusal is read, turned into a rule, remembered per
insurer + product (reports/insurer_lab/<INSURER>-<product>.json), and the next
session respects it:

    numbers   a learned maximum becomes the value tried - the boundary is the
              most useful place to test (10,000 must pass, 10,001 must fail)
    choices   a refused choice ("Zero Dep on a 7-year-old car") is not sent
              again; it is re-checked once a week in case the insurer changed

HOW A REFUSAL IS PINNED ON THE RIGHT THING
------------------------------------------
The lab changes ONE thing at a time from a baseline that works. So when a
scenario fails, the one thing changed is the cause - no guessing. The message
text is then read for a limit ("not more than 10000", "maximum Rs. 10,000",
"between 1000 and 10000", "20% of IDV"). When the message gives no number,
the lab finds the limit itself by halving the gap between a value that passed
and one that failed.

What is NOT a company validation - and is never learned as a rule:
    our own code failing   (null values, exceptions, JObject parse errors)
    an outage              (server down, timeouts)
    our own decline rules  (Probus refused before asking the insurer)
Those are reported as what they are.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from pages.quote_list import Failure

LAB_DIR = Path(__file__).resolve().parent.parent / "reports" / "insurer_lab"

# A refused choice is tried again after this many days, in case the insurer
# changed its mind. One call a week per rule is cheap; never checking again is
# how a suite ends up testing last year's rules.
RECHECK_DAYS = 7

# Words in a message -> the lab dimension it is about. Order matters: the more
# specific phrase first ("non electrical" before "electrical").
FIELD_WORDS = (
    (r"non[\s-]*electric", "non_electrical"),
    (r"electric(al)?\s*access", "electrical"),
    (r"\b(cng|lpg|bi[\s-]*fuel|fuel\s*kit)\b", "bifuel_kit"),
    (r"unnamed|passenger", "pa_passenger"),
    (r"paid\s*driver", "paid_driver"),
    (r"voluntary|deductible", "voluntary_deductible"),
    (r"\bidv\b|insured\s*declared", "idv"),
    (r"zero\s*dep|nil\s*dep|depreciation", "addon"),
    (r"road\s*side|\brsa\b|consumable|engine\s*protect|return\s*to\s*invoice|"
     r"\brti\b|key\s*(replace|protect)|tyre|ncb\s*protect|add[\s-]*on|cover",
     "addon"),
    (r"vehicle\s*age|years?\s*old|older\s*than|age\s*of", "year"),
    (r"cubic|\bcc\b|seating", "vehicle"),
    (r"\brto\b|registration\s*(city|state)|location", "rto"),
    (r"\bncb\b|no\s*claim", "ncb"),
)

_MONEY = r"(?:rs\.?|inr|₹)?\s*([\d][\d,]*(?:\.\d+)?)"

MAX_PATTERNS = (
    rf"(?:not|cannot|can\s*not|should\s*not|must\s*not|shouldn't)\s+(?:be\s+)?"
    rf"(?:more|greater|higher|exceed\w*|above)\s+(?:than\s+)?(?:rs\.?\s*)?{_MONEY}",
    rf"(?:maximum|max\.?|upto|up\s*to|less\s*than\s*or\s*equal\s*to|<=|limit\s*(?:is|of))"
    rf"\s*(?:of|is|allowed|value|amount|:)?\s*(?:is\s*)?{_MONEY}",
    # "Maximum electrical accessories allowed is 25000" - words in between.
    rf"(?:maximum|max\.?)[^\d.;]{{0,60}}?{_MONEY}",
    rf"(?:should|must)\s+be\s+(?:less|lower|below)\s+(?:than\s+)?{_MONEY}",
)
MIN_PATTERNS = (
    rf"(?:not|cannot|should\s*not|must\s*not)\s+(?:be\s+)?(?:less|lower|below)\s+"
    rf"(?:than\s+)?{_MONEY}",
    rf"(?:minimum|min\.?|at\s*least|greater\s*than\s*or\s*equal\s*to|>=)\s*"
    rf"(?:of|is|value|amount|:)?\s*(?:is\s*)?{_MONEY}",
    rf"(?:minimum|min\.?)[^\d.;]{{0,60}}?{_MONEY}",
)
RANGE_PATTERN = rf"between\s*{_MONEY}\s*(?:and|to|-)\s*{_MONEY}"
PERCENT_OF_IDV = r"(\d+(?:\.\d+)?)\s*%\s*of\s*(?:the\s*)?(?:vehicle\s*)?idv"


def _n(text: str) -> float:
    return float(text.replace(",", ""))


@dataclass
class Limit:
    low: float | None = None
    high: float | None = None
    pct_of_idv: float | None = None

    def __bool__(self) -> bool:
        return any(v is not None for v in (self.low, self.high, self.pct_of_idv))


def read_limit(message: str) -> Limit:
    """Pull a numeric limit out of a validation message, if it states one."""
    low = (message or "").lower()
    found = Limit()
    pct = re.search(PERCENT_OF_IDV, low)
    if pct:
        found.pct_of_idv = float(pct.group(1))
    rng = re.search(RANGE_PATTERN, low)
    if rng:
        a, b = sorted((_n(rng.group(1)), _n(rng.group(2))))
        found.low, found.high = a, b
        return found
    for pattern in MAX_PATTERNS:
        m = re.search(pattern, low)
        if m and not (found.pct_of_idv and _n(m.group(1)) == found.pct_of_idv):
            found.high = _n(m.group(1))
            break
    for pattern in MIN_PATTERNS:
        m = re.search(pattern, low)
        if m:
            found.low = _n(m.group(1))
            break
    return found


def field_of(message: str) -> str:
    """Which dimension a message talks about, or '' when it does not say."""
    low = (message or "").lower()
    for pattern, dim in FIELD_WORDS:
        if re.search(pattern, low):
            return dim
    return ""


def kind_of(message: str, source: str = "insurer") -> str:
    """
    company-validation | our-defect | insurer-down | probus-rule | unknown

    Only company-validation is ever learned as a rule.
    """
    if source == "probus-rule":
        return "probus-rule"
    if source in ("http", "silent"):
        return "our-defect" if source == "http" else "insurer-down"
    k = Failure("", message).kind
    if k in ("our-defect", "insurer-down"):
        return k
    low = (message or "").lower()
    if any(w in low for w in ("exception", "object reference", "jsonreader",
                              "unexpected character", "stack trace")):
        return "our-defect"
    if not low or low.startswith("status=") or "empty answer" in low:
        return "unknown"
    return "company-validation"


# ------------------------------------------------------------------ the rules

@dataclass
class Rule:
    """One thing the insurer refuses, as learned from its own answers."""
    dimension: str                 # e.g. "electrical", "addon", "year"
    kind: str                      # "max" | "min" | "range" | "refused" | "silent"
    value: str = ""                # for "refused": the value refused
    low: float | None = None
    high: float | None = None
    pct_of_idv: float | None = None
    when: dict = field(default_factory=dict)   # e.g. {"year": "2019"}
    message: str = ""
    learned: str = ""
    last_checked: str = ""
    evidence: int = 1
    confirmed: bool = False        # boundary verified by two calls
    # Who draws the line: "insurer" (its own validation) or "our portal"
    # (InsureBridge's decline rules - "MMV is not mapped.", "Addon is not
    # allowed."). Both are obeyed; only the first is the insurer's business.
    source: str = "insurer"
    label: str = ""                # what to call `value` in a report

    @property
    def key(self) -> str:
        cond = ",".join(f"{k}={v}" for k, v in sorted(self.when.items()))
        return f"{self.dimension}|{self.kind}|{self.value}|{cond}"

    def sentence(self) -> str:
        what = self.dimension.replace("_", " ")
        cond = (" when " + ", ".join(_condition_text(k, v)
                                     for k, v in sorted(self.when.items()))
                if self.when else "")
        shown = self.label or self.value
        if self.kind == "refused":
            return f"refuses {what} = {shown}{cond}"
        if self.kind == "silent":
            return f"silently leaves out {what} = {shown}{cond} (no message)"
        if self.pct_of_idv is not None:
            return f"{what} at most {self.pct_of_idv:g}% of IDV{cond}"
        if self.kind == "range":
            return f"{what} must be between {self.low:,.0f} and {self.high:,.0f}{cond}"
        if self.kind == "max":
            return f"{what} at most {self.high:,.0f}{cond}"
        return f"{what} at least {self.low:,.0f}{cond}"

    def due_for_recheck(self) -> bool:
        try:
            last = date.fromisoformat((self.last_checked or self.learned)[:10])
        except ValueError:
            return True
        return (date.today() - last).days >= RECHECK_DAYS


class Notebook:
    """Everything the lab knows about one insurer + product."""

    def __init__(self, insurer: str, product: str, folder: Path | None = None):
        self.insurer = insurer.upper()
        self.product = product
        self.path = (folder or LAB_DIR) / f"{self.insurer}-{product}.json"
        self.data = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("rules", [])
                data.setdefault("results", {})
                data.setdefault("sessions", [])
                return data
        except (OSError, ValueError):
            pass
        return {"insurer": self.insurer, "product": self.product,
                "rules": [], "results": {}, "sessions": []}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, indent=1, sort_keys=True),
                            encoding="utf-8")
            os.replace(temp, self.path)
        except Exception:
            pass

    # --- rules ----------------------------------------------------------
    def rules(self) -> list[Rule]:
        out = []
        for row in self.data.get("rules", []):
            try:
                out.append(Rule(**row))
            except TypeError:
                continue
        return out

    def add_rule(self, rule: Rule) -> tuple[Rule, bool]:
        """Store a rule; returns (rule as stored, True if it is new)."""
        today = date.today().isoformat()
        rules = self.rules()
        for i, old in enumerate(rules):
            if old.key == rule.key:
                old.evidence += 1
                old.last_checked = today
                old.message = rule.message or old.message
                for attr in ("low", "high", "pct_of_idv"):
                    if getattr(rule, attr) is not None:
                        setattr(old, attr, getattr(rule, attr))
                old.confirmed = old.confirmed or rule.confirmed
                rules[i] = old
                self.data["rules"] = [asdict(r) for r in rules]
                return old, False
        rule.learned = rule.learned or today
        rule.last_checked = today
        rules.append(rule)
        self.data["rules"] = [asdict(r) for r in rules]
        return rule, True

    def drop_rule(self, key: str) -> None:
        """The insurer accepted what a rule said it refuses - forget it."""
        self.data["rules"] = [asdict(r) for r in self.rules() if r.key != key]

    def touch_rule(self, key: str) -> None:
        rules = self.rules()
        for r in rules:
            if r.key == key:
                r.last_checked = date.today().isoformat()
        self.data["rules"] = [asdict(r) for r in rules]

    # --- obeying the rules ------------------------------------------------
    def blocks(self, dimension: str, value, context: dict) -> Rule | None:
        """
        The rule that says this value will be refused, or None.

        A numeric value above a learned maximum (or below a minimum) is
        blocked; so is a refused choice whose conditions match.
        """
        for rule in self.rules():
            if rule.dimension != dimension:
                continue
            if rule.when and not conditions_hold(rule.when, context):
                continue
            if rule.kind in ("refused", "silent") and str(value) == rule.value:
                return rule
            number = _as_number(value)
            if number is None:
                continue
            if rule.pct_of_idv is not None and context.get("idv"):
                if number > context["idv"] * rule.pct_of_idv / 100:
                    return rule
            if rule.high is not None and number > rule.high:
                return rule
            if rule.low is not None and number < rule.low:
                return rule
        return None

    def boundary_values(self, dimension: str) -> list[float]:
        """Learned limits for a numeric dimension - the best values to test."""
        out = []
        for rule in self.rules():
            if rule.dimension == dimension and not rule.when:
                out += [v for v in (rule.low, rule.high) if v is not None]
        return out

    # --- results ------------------------------------------------------------
    def last(self, key: str) -> dict:
        return self.data["results"].get(key, {})

    def remember(self, key: str, row: dict) -> None:
        row = dict(row, last=date.today().isoformat())
        self.data["results"][key] = row

    def add_session(self, summary: dict) -> None:
        self.data["sessions"] = (self.data["sessions"] + [summary])[-30:]


def conditions_hold(when: dict, context: dict) -> bool:
    """{"age_min": 6} holds for age 6 and above; plain keys must be equal."""
    for key, want in when.items():
        if key.endswith("_min") or key.endswith("_max"):
            have = _as_number(context.get(key[:-4]))
            if have is None:
                return False
            if key.endswith("_min") and have < float(want):
                return False
            if key.endswith("_max") and have > float(want):
                return False
        elif str(context.get(key)) != str(want):
            return False
    return True


def _condition_text(key: str, value) -> str:
    if key.endswith("_min"):
        return f"{key[:-4].replace('_', ' ')} is {value} or more"
    if key.endswith("_max"):
        return f"{key[:-4].replace('_', ' ')} is {value} or less"
    return f"{key.replace('_', ' ')} = {value}"


def _as_number(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def rule_from_refusal(dimension: str, value, message: str,
                      context: dict | None = None) -> Rule:
    """
    Turn one refusal, caused by changing `dimension` to `value`, into a rule.

    The message's own number wins when it states one. Otherwise a numeric
    value is recorded as a provisional maximum just below what was refused
    (the boundary search then narrows it), and a choice is recorded as refused.
    """
    limit = read_limit(message)
    number = _as_number(value)
    # The lab changed ONE thing, so that thing is the cause. The message's own
    # wording only decides when the caller does not know (dimension "").
    dim = dimension or field_of(message)
    if limit and number is not None:
        if limit.pct_of_idv is not None:
            return Rule(dim, "max", pct_of_idv=limit.pct_of_idv, message=message)
        if limit.low is not None and limit.high is not None:
            return Rule(dim, "range", low=limit.low, high=limit.high, message=message)
        if limit.high is not None:
            return Rule(dim, "max", high=limit.high, message=message)
        return Rule(dim, "min", low=limit.low, message=message)
    if number is not None and dimension in NUMERIC:
        # No number in the message: refused at `value`, so the limit is below.
        return Rule(dimension, "max", high=number - 1, message=message)
    return Rule(dimension, "refused", value=str(value), message=message,
                when=dict(context or {}))


# Dimensions whose values are amounts, so limits make sense.
NUMERIC = {"electrical", "non_electrical", "bifuel_kit", "pa_passenger",
           "paid_driver", "voluntary_deductible", "idv"}


def nice(low: float, high: float) -> float:
    """
    A round number near the middle of two amounts, for the boundary search:
    5,000 and 25,000 -> 15,000; 10,000 and 15,000 -> 12,000.
    """
    mid = (low + high) / 2
    gap = (high - low) / 4
    step = 1.0
    for candidate in (10000, 5000, 1000, 500, 100, 50, 10, 5, 1):
        if candidate <= gap:
            step = float(candidate)
            break
    rounded = round(mid / step) * step
    return float(rounded) if low < rounded < high else float(int(mid))
