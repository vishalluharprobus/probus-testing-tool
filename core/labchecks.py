"""
Judge one insurer's answers: against the rules of the sum, and against the baseline.

Every scenario changes one thing from the baseline, so its answer can be
compared with the baseline's answer and the difference explained:

    asked for Zero Dep       -> the add-on must be in the quote and cost money
    electrical Rs 25,000     -> ElecAccessoriesPremium > 0, total goes UP
    voluntary excess 15,000  -> VoluntaryDiscount > 0, total goes DOWN
    IDV Rs 3,00,000          -> the quote's IDV IS 3,00,000
    a claim was made         -> no NCB, and never cheaper than without one

The most useful findings are the SILENT ones: the insurer answers "Success",
the customer sees a price, and what they asked for is simply not in it -
NATIONAL drops Return to Invoice after 3 years and Zero Dep after 5 without a
word (InsureBridge PCNationalInsuranceRules.cs:2553-2606), and clamps a custom
IDV to its own range without saying so (:703-734). No error message will ever
report those; only a comparison can.

Same two levels as the quote matrix: DEFECT (a rule without exceptions is
broken) and LOOK (odd, and worth a human's eyes).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.quotecapture import PlanAnswer, answer_items
from core.quotechecks import DEFECT, LOOK, Finding, GST_RATE
from data import labscenarios as ls

# Add-on name words -> (ApplicableAddonDetails key, premium field). The keys
# and fields are Saarthi's (addon-list.util.ts:16-38, pc-result.ts:2357-2431).
ADDON_FIELDS = (
    (("zero", "nil dep", "depreciation"), "IsZeroDepreciation", "ZeroDepPremium"),
    (("consumable",), "IsConsumables", "CostOfConsumablesPremium"),
    (("ncb protect", "ncb protection"), "IsNCBProtection", "NcbProtectorPremium"),
    (("engine",), "IsEngineProtector", "EngineProtectorPremium"),
    (("invoice", "rti"), "IsReturnToInvoice", "InvoicePriceCoverPremium"),
    (("key",), "IsLossOfKey", "KeyReplacementPremium"),
    (("personal belong",), "IsLossOfPersonalBelonging", "LossOfPersonalBelongingPremium"),
    (("road side", "roadside", "rsa"), "IsRoadSideAssistance", "RSAPremium"),
    (("tyre", "tire"), "IsTyreCover", "TyreCoverPremium"),
    (("emergency",), "IsEmergencyCover", "EmergencyAssistancePremium"),
    (("hydrostatic",), "IsHydrostaticLockCover", "HydrostaticLockCoverPremium"),
    (("hospital",), "IsHospitalCashCover", "HospitalCashCoverPremium"),
    (("passenger assist",), "IsPassengerAssistcover", "PassengerAssistCoverPremium"),
)


def _rs(x) -> str:
    return f"Rs {x:,.0f}" if x is not None else "-"


def _f(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Outcome:
    """What one scenario came back with, for the chosen insurer."""
    n: int
    scenario: ls.Scenario
    status: str                     # priced | refused | not-asked | empty | error | skipped
    reason: str = ""
    kind: str = ""                  # company-validation | our-defect | probus-rule ...
    best: PlanAnswer | None = None
    raw: dict = field(default_factory=dict)      # the best item, as received
    items: list = field(default_factory=list)
    seconds: float = 0.0
    quotation: str = ""
    findings: list = field(default_factory=list)
    silent: str = ""                # what was silently not done, if anything
    others: list = field(default_factory=list)   # insurers our server did ask
    not_listed: bool = False        # insurer absent from plans and declines
    sub_product: str = ""           # SubProductCode the API answered, e.g. MTRTW
    # The buy journey after the quote, stage key -> labjourney.Stage
    # (kyc, company_specific, proposal, payment). Empty until it runs.
    stages: dict = field(default_factory=dict)
    proposal_no: str = ""
    journey_notes: list = field(default_factory=list)
    journey_seconds: float = 0.0


def best_of(items: list, insurer: str) -> tuple[PlanAnswer | None, dict]:
    """The one-year, cheapest successful answer, and its raw item."""
    pairs = [(a, raw) for a, raw in zip(answer_items(items, insurer), items)
             if a.ok and a.premium]
    if not pairs:
        return None, {}

    def tenure(a: PlanAnswer) -> int:
        digits = "".join(ch for ch in (a.years or "1") if ch.isdigit())
        return int(digits or 1)
    return min(pairs, key=lambda p: (tenure(p[0]), p[0].premium))


def _breakup(raw: dict, name: str) -> float:
    b = raw.get("PremiumBreakUpDetails") or {}
    return _f(b.get(name, raw.get(name)))


def addon_applied(raw: dict, name: str) -> bool | None:
    """Did this add-on make it into the quote? None = cannot tell."""
    low = (name or "").lower()
    for words, key, premium in ADDON_FIELDS:
        if any(w in low for w in words):
            status = str((raw.get("ApplicableAddonDetails") or {}).get(key, "")).lower()
            if status in ("applicable", "included"):
                return True
            if _breakup(raw, premium) > 0:
                return True
            if status in ("notapplicable", "not applicable"):
                return False
            return None
    listed = str((raw.get("PremiumBreakUpDetails") or {}).get(
        "ApplicableCompanyAddonList") or "").lower()
    if listed:
        return any(part.strip() and part.strip() in listed
                   for part in low.split())
    return None


def judge(o: Outcome, base: Outcome | None, addon_names: dict[str, str],
          insurer: str, ref: Outcome | None = None) -> None:
    """
    Fill o.findings (and o.silent) for one priced scenario.

    `base` is the baseline; `ref` is the same scenario WITHOUT its add-ons and
    covers ("OD Only" for "OD Only + electrical 10,000"). Add-ons and covers
    are judged against `ref` - judging them against the Comprehensive
    baseline would blame the accessory for what OD Only did.
    """
    a, raw, s = o.best, o.raw, o.scenario.state
    if a is None:
        return
    j = (o.n,)
    out = o.findings

    # --- a price nobody can pay ----------------------------------------------
    if a.premium is not None and a.premium <= 0:
        out.append(Finding(DEFECT, "premium-not-positive", insurer,
            f"the final premium is {_rs(a.premium)} - zero or NEGATIVE",
            "Something is being subtracted that should be added.", j))

    # --- the sum ------------------------------------------------------------
    if a.net and a.gst is not None and a.net > 0:
        if abs(a.gst - a.net * GST_RATE) > max(3.0, a.net * 0.01):
            out.append(Finding(DEFECT, "gst", insurer,
                f"GST {_rs(a.gst)} on net {_rs(a.net)} is not 18%", journeys=j))
        if a.premium and abs(a.premium - (a.net + a.gst)) > 2:
            out.append(Finding(DEFECT, "total", insurer,
                f"final {_rs(a.premium)} is not net + GST = {_rs(a.net + a.gst)}",
                journeys=j))

    # --- the parts a policy type may have -------------------------------------
    od, tp = a.od or 0, a.tp or 0
    if s.policy == ls.TP and od > 1:
        out.append(Finding(DEFECT, "tp-has-od", insurer,
            f"Third Party quote carries {_rs(od)} of own-damage premium", journeys=j))
    if s.policy == ls.OD and tp - (a.pa_cover or 0) > 1:
        out.append(Finding(DEFECT, "od-has-tp", insurer,
            f"OD Only quote carries {_rs(tp)} of third-party premium", journeys=j))
    if s.policy in (ls.CP, ls.OD):
        if not a.idv:
            out.append(Finding(DEFECT, "idv-missing", insurer,
                "no IDV although the vehicle itself is covered", journeys=j))
        elif a.idv_min and a.idv_max and not (a.idv_min - 1 <= a.idv <= a.idv_max + 1) \
                and not s.idv:
            out.append(Finding(DEFECT, "idv-range", insurer,
                f"IDV {_rs(a.idv)} outside its own range {_rs(a.idv_min)}-{_rs(a.idv_max)}",
                journeys=j))

    # --- NCB --------------------------------------------------------------------
    given = abs(a.ncb_percent or 0) > 0 or abs(a.ncb_discount or 0) > 0.5
    no_ncb_reason = ("a claim was made" if s.claim else
                     "the policy lapsed or is unknown" if s.previous in (ls.OVER_90, ls.DONT_KNOW) else
                     "the previous policy was Third Party" if s.prev_type == ls.TP else
                     "the owner changed" if s.owner_changed else "")
    if s.policy != ls.TP and no_ncb_reason and given:
        out.append(Finding(DEFECT, "ncb-unearned", insurer,
            f"gives an NCB ({a.ncb_percent or 0:.0f}%) although {no_ncb_reason}",
            journeys=j))

    # --- organisation: no compulsory PA for a company ---------------------------
    if s.customer == "Organization" and (a.pa_cover or 0) > 0:
        out.append(Finding(DEFECT, "cpa-organisation", insurer,
            f"charges compulsory PA ({_rs(a.pa_cover)}) to a company-owned vehicle",
            journeys=j))

    if base is None or base.best is None or not o.scenario.changes:
        return
    dims = dict(o.scenario.changes)
    extras = {"addon", "idv", "cpa"} | set(ls.COVERS) | set(ls.DISCOUNTS)
    if set(dims) & extras and set(dims) - extras:
        # A combination such as "OD Only + electrical": the fair comparison
        # is "OD Only" on its own.
        base = ref if ref is not None and ref.best is not None else None
    if base is None:
        return                  # nothing fair to compare with
    b = base.best
    delta = (a.premium or 0) - (b.premium or 0)

    # --- one thing changed: did the answer move the right way? -----------------
    if "addon" in dims and s.policy != ls.TP:
        ids = [str(x) for x in s.addons]
        dropped = [addon_names.get(i, i) for i in ids
                   if addon_applied(raw, addon_names.get(i, i)) is False]
        unknown = all(addon_applied(raw, addon_names.get(i, i)) is None for i in ids)
        if dropped:
            o.silent = f"dropped {', '.join(dropped)}"
            out.append(Finding(LOOK, "addon-dropped", insurer,
                f"asked for {', '.join(dropped)}; the quote came back WITHOUT it, "
                f"and no message said so", _age_note(s), j))
        elif unknown and (a.addon or 0) <= (b.addon or 0) and delta <= 0:
            o.silent = "add-on not priced"
            out.append(Finding(LOOK, "addon-unpriced", insurer,
                f"asked for {', '.join(addon_names.get(i, i) for i in ids)}; "
                f"price did not move ({_rs(a.premium)})", _age_note(s), j))

    for name, value in s.covers:
        spec = ls.COVERS.get(name) or ls.DISCOUNTS.get(name)
        if not spec:
            continue
        _, _, _, field_name, direction = spec
        part = abs(_breakup(raw, field_name))
        nice = ls.NICE[name]
        if direction == "adds":
            if part == 0 and delta <= 0:
                o.silent = f"{nice} ignored"
                out.append(Finding(LOOK, "cover-ignored", insurer,
                    f"{nice} {_value(value)} accepted, but not priced - "
                    f"{field_name} is 0 and the total did not rise", journeys=j))
            elif delta < 0:
                out.append(Finding(DEFECT, "cover-cheaper", insurer,
                    f"adding {nice} {_value(value)} made the quote CHEAPER "
                    f"({_rs(b.premium)} -> {_rs(a.premium)})", journeys=j))
        else:
            if delta > 0:
                out.append(Finding(DEFECT, "discount-dearer", insurer,
                    f"{nice} {_value(value)} made the quote DEARER "
                    f"({_rs(b.premium)} -> {_rs(a.premium)})", journeys=j))
            elif part == 0 and delta == 0:
                o.silent = f"{nice} had no effect"
                out.append(Finding(LOOK, "discount-ignored", insurer,
                    f"{nice} {_value(value)} changed nothing - {field_name} is 0 and "
                    f"the price is the same", "It may not be sent to the insurer at "
                    "all (NATIONAL: VoluntaryExcessAmount is never sent, InsureBridge "
                    "PCNationalInsuranceRules)." if name == "voluntary_deductible" else "",
                    j))

    if s.idv and a.idv and b.idv and abs(s.idv - b.idv) <= 1 \
            and abs((a.premium or 0) - (b.premium or 0)) > 1:
        out.append(Finding(LOOK, "idv-same-price-differs", insurer,
            f"choosing IDV {_rs(s.idv)} - the SAME as the automatic IDV - changed "
            f"the price {_rs(b.premium)} -> {_rs(a.premium)}",
            "Custom IDV and automatic IDV should price the same vehicle the same.",
            j))
    if s.idv and a.idv:
        inside = b.idv_min and b.idv_max and b.idv_min <= s.idv <= b.idv_max
        if inside and abs(a.idv - s.idv) > 1:
            out.append(Finding(DEFECT, "idv-not-honoured", insurer,
                f"asked for IDV {_rs(s.idv)}, the quote has {_rs(a.idv)}", journeys=j))
        elif not inside and abs(a.idv - s.idv) > 1:
            o.silent = f"IDV {_rs(s.idv)} changed to {_rs(a.idv)}"
            out.append(Finding(LOOK, "idv-clamped", insurer,
                f"IDV {_rs(s.idv)} is outside {_rs(b.idv_min)}-{_rs(b.idv_max)}; "
                f"the quote quietly used {_rs(a.idv)} instead of refusing",
                journeys=j))

    if s.cpa and (a.pa_cover or 0) <= 0:
        out.append(Finding(LOOK, "cpa-missing", insurer,
            "asked for compulsory PA owner-driver cover; PACoverToOwnDriver is 0",
            journeys=j))

    if "policy" in dims and len(dims) == 1:
        if s.policy == ls.TP and delta >= 0:
            out.append(Finding(DEFECT, "tp-not-cheaper", insurer,
                f"Third Party {_rs(a.premium)} is not cheaper than Comprehensive "
                f"{_rs(b.premium)}", journeys=j))
        if s.policy == ls.OD and delta >= 0:
            out.append(Finding(DEFECT, "od-not-cheaper", insurer,
                f"OD Only {_rs(a.premium)} is not cheaper than Comprehensive "
                f"{_rs(b.premium)}", journeys=j))
    if "ncb" in dims and len(dims) == 1:
        more = s.ncb_value > _ncb_of(base)
        if more and delta > 0 or (not more and delta < 0):
            out.append(Finding(DEFECT, "ncb-direction", insurer,
                f"NCB {s.ncb_value}% costs {_rs(a.premium)}, baseline NCB "
                f"{_ncb_of(base)}% costs {_rs(b.premium)} - wrong way round",
                journeys=j))
        elif delta == 0:
            out.append(Finding(LOOK, "ncb-no-effect", insurer,
                f"NCB {s.ncb_value}% gives the same price as {_ncb_of(base)}%",
                journeys=j))
    if "claim" in dims and len(dims) == 1 and delta < 0:
        out.append(Finding(DEFECT, "claim-cheaper", insurer,
            f"after a claim the quote is CHEAPER ({_rs(b.premium)} -> {_rs(a.premium)})",
            journeys=j))
    if "year" in dims and len(dims) == 1 and a.idv and b.idv:
        older = s.age > base.scenario.state.age
        if (older and a.idv >= b.idv) or (not older and a.idv <= b.idv):
            out.append(Finding(DEFECT, "age-idv", insurer,
                f"{s.age}-year-old vehicle has IDV {_rs(a.idv)}, the "
                f"{base.scenario.state.age}-year-old baseline {_rs(b.idv)}", journeys=j))


def _ncb_of(o: Outcome) -> int:
    return o.scenario.state.ncb_value


def _value(v) -> str:
    return "" if v is True else _rs(v) if isinstance(v, (int, float)) else str(v)


def _age_note(s: ls.State) -> str:
    return f"vehicle {s.age} years old ({s.year})"


def across(outcomes: list[Outcome], insurer: str,
           bands: dict[str, str]) -> list[Finding]:
    """Checks that need several scenarios: bigger amounts cost more; vehicles
    in the same engine band pay the same regulated third-party price."""
    out: list[Finding] = []
    priced = [o for o in outcomes if o.best and len(o.scenario.changes) == 1]
    for name in list(ls.COVERS) + list(ls.DISCOUNTS):
        rows = sorted(((o.scenario.value, o) for o in priced
                       if o.scenario.dimension == name
                       and isinstance(o.scenario.value, (int, float))
                       and not isinstance(o.scenario.value, bool)),
                      key=lambda row: (row[0], row[1].n))
        for (v1, o1), (v2, o2) in zip(rows, rows[1:]):
            p1, p2 = o1.best.premium, o2.best.premium
            adds = (ls.COVERS.get(name) or ls.DISCOUNTS.get(name))[4] == "adds"
            if (adds and p2 < p1) or (not adds and p2 > p1):
                out.append(Finding(DEFECT, "amount-order", insurer,
                    f"{ls.NICE[name]} {_rs(v2)} costs {_rs(p2)}, {_rs(v1)} costs "
                    f"{_rs(p1)} - more cover should never cost less",
                    journeys=(o1.n, o2.n)))
    idv_rows = sorted((o for o in priced if o.scenario.dimension == "idv"
                       and o.best.idv and o.best.od is not None),
                      key=lambda o: (o.best.idv, o.n))
    for o1, o2 in zip(idv_rows, idv_rows[1:]):
        if o2.best.idv > o1.best.idv * 1.05 and o2.best.od <= o1.best.od:
            out.append(Finding(LOOK, "idv-od-flat", insurer,
                f"IDV {_rs(o1.best.idv)} -> {_rs(o2.best.idv)}, but the own-damage "
                f"premium stayed {_rs(o1.best.od)} -> {_rs(o2.best.od)}",
                "Own-damage premium is a rate on the IDV; more IDV should cost more.",
                (o1.n, o2.n)))
    by_band: dict[str, list[Outcome]] = {}
    for o in priced:
        if o.scenario.dimension == "vehicle" and o.raw:
            band = bands.get(o.scenario.value, "")
            if band and band != "electric":
                by_band.setdefault(band, []).append(o)
    for band, rows in by_band.items():
        basic = {o.n: _breakup(o.raw, "BasicThirdPartyLiability") for o in rows}
        values = {v for v in basic.values() if v}
        if len(values) > 1:
            out.append(Finding(LOOK, "tp-band", insurer,
                f"vehicles in the {band} band pay different basic TP: "
                f"{', '.join(_rs(v) for v in sorted(values))}",
                "Basic third-party premium is set by the regulator per engine band.",
                tuple(sorted(basic))))
    return out
