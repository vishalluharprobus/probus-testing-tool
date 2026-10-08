"""
Look at the quotes and say what is wrong with them.

"A price came back" is the weakest possible check. A quote can come back and
still be wrong: GST that is not 18%, own-damage premium on a Third Party
policy, a 35% NCB for a customer who just made a claim, a card showing a
different price from the one the insurer sent. This file checks the things a
careful underwriter would check, on every quote, automatically.

TWO KINDS OF CHECK
------------------
One journey at a time - things that must hold inside a single quote:
    sums         FinalPremium = NetPremium + GST, and GST = 18%
    parts        Third Party has no OD part; OD Only has no TP part;
                 Comprehensive has both
    TP price     the third-party part is the regulator's price for that
                 engine size (cars and bikes alike)
    IDV          present when there is own-damage cover, inside its own range
    NCB          none given when none was earned; given when it was; the same
                 NCB from every insurer
    screen       every priced insurer has a card, showing the price it sent
    request      what the form SENT matches what the tool CHOSE
    reasons      refusals that are our own code failing
    outliers     one insurer wildly off everybody else

Between journeys - only visible by comparing two quotes that differ in ONE
thing (the "twins" in data/matrix.py):
    Comprehensive > Third Party     OD Only < Comprehensive
    bigger NCB -> cheaper           older vehicle -> lower IDV
    claim made -> not cheaper       lapsed 90+ days -> not cheaper
    bigger engine -> TP not cheaper

HOW SURE
--------
DEFECT   the numbers break a rule that has no exceptions. Worth a ticket.
LOOK     odd enough for a human to look at, but there can be a reason.
Only sums, contradictions and "our code failed" are DEFECTs. Anything that
depends on how an insurer prices is a LOOK - the tool does not pretend to know
more than it does.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from data import matrix
from data.matrix import CP, OD, OVER_90, DONT_KNOW, TP, Scenario
from pages.quote_list import Failure

DEFECT, LOOK = "DEFECT", "LOOK"

GST_RATE = 0.18

# The one-year third-party premium is fixed by the government (MoRTH
# notification, in force since 1 June 2022) by engine size: (up to cc, Rs).
# Insurers send exactly this as NetTPPremium - Rs 714 for the 110cc Activa,
# Rs 2,804 for the 411cc Himalayan, every insurer, in the live captures of
# 2026-09-30 - with the owner-driver PA cover reported separately.
TP_RATES = {
    "bike": ((75, 538), (150, 714), (350, 1366), (None, 2804)),
    "car": ((1000, 2094), (1500, 3416), (None, 7897)),
}
# A CNG or LPG kit adds its own third-party charge.
GAS_KIT_TP = 60


def regulated_tp(product: str, cc: float | None, fuel: str = "") -> set[int]:
    """
    The third-party prices the regulator allows for this vehicle - empty when
    the rule cannot be applied: electric vehicles are priced by kW (not in
    the catalogue), hybrids get a discount, and an unknown engine size has no
    band.
    """
    fuel = (fuel or "").upper()
    if not cc or product not in TP_RATES or any(
            word in fuel for word in ("ELECTRIC", "HYBRID", "BATTERY")):
        return set()
    rate = next(rs for top, rs in TP_RATES[product] if top is None or cc <= top)
    if "CNG" in fuel or "LPG" in fuel:
        # Some insurers add the kit's charge, some do not, so both are
        # accepted rather than calling either one wrong.
        return {rate, rate + GAS_KIT_TP}
    return {rate}


@dataclass
class Finding:
    severity: str        # DEFECT | LOOK
    check: str           # short id, e.g. "gst"
    insurer: str
    title: str           # one plain sentence
    detail: str = ""
    journeys: tuple = ()  # journey numbers involved

    def line(self) -> str:
        where = ", ".join(f"#{j}" for j in self.journeys)
        return (f"{self.insurer:<12} {self.title}"
                + (f"  [{where}]" if where else ""))


def _rs(x) -> str:
    return f"Rs {x:,.0f}" if x is not None else "-"


def _close(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


# ============================================================ one journey

def check_journey(n: int, s: Scenario, offers: dict, answers: list,
                  declines: list, sent: dict, cards: list,
                  prev_codes: dict[str, str],
                  vehicle: dict | None = None) -> list[Finding]:
    """
    n          journey number, for the report
    offers     insurer -> PlanAnswer (the one-year, cheapest success)
    answers    every PlanAnswer, successes and failures
    declines   Decline list (who said no, why, and who decided)
    sent       the QualifiedCompany request body
    cards      Quote objects read from the screen
    prev_codes previous-insurer label -> CompanyCode
    vehicle    {"product", "cc", "fuel"} of this journey's vehicle, for the
               regulated third-party price; None skips that check
    """
    out: list[Finding] = []
    j = (n,)
    policy = s.get("policy")

    for code, a in sorted(offers.items()):
        # --- sums --------------------------------------------------------
        if a.net is not None and a.gst is not None and a.net > 0:
            expected = a.net * GST_RATE
            if not _close(a.gst, expected, max(3.0, a.net * 0.01)):
                out.append(Finding(DEFECT, "gst", code,
                    f"GST is {_rs(a.gst)} on a net premium of {_rs(a.net)} - "
                    f"18% would be {_rs(expected)}", journeys=j))
            if a.premium is not None and not _close(a.premium, a.net + a.gst, 2.0):
                out.append(Finding(DEFECT, "total", code,
                    f"final premium {_rs(a.premium)} is not net {_rs(a.net)} "
                    f"+ GST {_rs(a.gst)} = {_rs(a.net + a.gst)}", journeys=j))

        # --- parts by policy type ------------------------------------------
        od, tp, pa = a.od or 0, a.tp or 0, a.pa_cover or 0
        if policy == TP and od > 1:
            out.append(Finding(DEFECT, "tp-has-od", code,
                f"Third Party policy carries {_rs(od)} of OWN-DAMAGE premium",
                "A TP-only policy has no own-damage cover, so this part must "
                "be zero.", j))
        if policy == OD and tp - pa > 1:
            out.append(Finding(DEFECT, "od-has-tp", code,
                f"OD Only policy carries {_rs(tp - pa)} of THIRD-PARTY premium",
                "The customer already holds third-party cover; charging it "
                "again is double cover.", j))
        if policy == CP and a.od is not None and a.tp is not None \
                and (od <= 0 or tp <= 0):
            out.append(Finding(DEFECT, "cp-parts", code,
                f"Comprehensive quote is missing a part: OD {_rs(od)}, "
                f"TP {_rs(tp)}", journeys=j))

        # --- IDV -------------------------------------------------------------
        if policy in (CP, OD):
            if not a.idv:
                out.append(Finding(DEFECT, "idv-missing", code,
                    "no IDV on a policy that covers the vehicle itself", journeys=j))
            elif a.idv_min and a.idv_max and not (
                    a.idv_min - 1 <= a.idv <= a.idv_max + 1):
                out.append(Finding(DEFECT, "idv-range", code,
                    f"IDV {_rs(a.idv)} is outside its own range "
                    f"{_rs(a.idv_min)} - {_rs(a.idv_max)}", journeys=j))

        # --- NCB -------------------------------------------------------------
        if policy in (CP, OD):
            earned = _ncb_earned(s)
            # The discount can be written as a negative amount - size matters,
            # not sign.
            given = abs(a.ncb_percent or 0) > 0 or abs(a.ncb_discount or 0) > 0.5
            if earned is False and given:
                out.append(Finding(DEFECT, "ncb-unearned", code,
                    f"gives an NCB ({a.ncb_percent or 0:.0f}%, "
                    f"{_rs(a.ncb_discount)}) the customer did not earn",
                    f"Why none was earned: {_why_no_ncb(s)}. Money leak - "
                    f"the discount must be zero.", j))
            if earned is True and not given and a.ncb_percent is not None:
                out.append(Finding(LOOK, "ncb-missing", code,
                    "no NCB applied although the customer earned one",
                    f"previous NCB {matrix.ncb_number(s.get('ncb'), s.get('year'))}"
                    f"%, no claim, policy not lapsed", j))
            elif earned is True and given and a.ncb_percent is not None:
                # CurrentNCB is the NEW slab: previous 35% -> 45% on all four
                # insurers that priced the baseline live (2026-09-30).
                previous = matrix.ncb_number(s.get("ncb"), s.get("year")) or 0
                expected = next_ncb(previous)
                if abs(a.ncb_percent - expected) > 0.5:
                    out.append(Finding(LOOK, "ncb-slab", code,
                        f"NCB {a.ncb_percent:.0f}% - one claim-free year after "
                        f"{previous}% should give {expected}%", journeys=j))

    # --- NCB, all insurers together ----------------------------------------
    ncbs = {c: a.ncb_percent for c, a in offers.items()
            if a.ncb_percent is not None and policy in (CP, OD)}
    if len(set(ncbs.values())) > 1 and len(ncbs) >= 2:
        common = statistics.mode(ncbs.values())
        odd = {c: v for c, v in ncbs.items() if v != common}
        if len(odd) < len(ncbs):
            for c, v in sorted(odd.items()):
                out.append(Finding(LOOK, "ncb-disagree", c,
                    f"NCB {v:.0f}% while most insurers say {common:.0f}%",
                    "NCB belongs to the customer, not the insurer - every "
                    "insurer should apply the same slab.", j))

    # --- third-party part, all insurers together -----------------------------
    if policy in (CP, TP):
        parts = {c: (a.tp or 0) - (a.pa_cover or 0) for c, a in offers.items()
                 if a.tp}
        if len(parts) >= 3:
            low, high = min(parts.values()), max(parts.values())
            if low > 0 and high / low > 1.25:
                cheapest = min(parts, key=parts.get)
                dearest = max(parts, key=parts.get)
                out.append(Finding(LOOK, "tp-spread", "ALL",
                    f"third-party part ranges from {_rs(low)} ({cheapest}) to "
                    f"{_rs(high)} ({dearest})",
                    "The basic TP premium is fixed by the regulator for an "
                    "engine size, so it should be near-identical everywhere "
                    "(PA cover already taken out).", j))

    # --- third-party part against the regulator's price -----------------------
    vehicle = vehicle or {}
    allowed = regulated_tp(vehicle.get("product", ""), vehicle.get("cc"),
                           vehicle.get("fuel", ""))
    if policy in (CP, TP) and allowed:
        for code, a in sorted(offers.items()):
            if not a.tp:
                continue
            basic = a.tp - (a.pa_cover or 0)
            if not any(abs(basic - rate) <= 2 for rate in allowed):
                shown = " or ".join(_rs(r) for r in sorted(allowed))
                out.append(Finding(LOOK, "tp-rate", code,
                    f"third-party part is {_rs(basic)}; the regulated price for "
                    f"a {vehicle['cc']:.0f}cc {matrix.RULES[vehicle['product']].noun} "
                    f"is {shown}",
                    "One-year TP is fixed by the government by engine size "
                    "(owner-driver PA cover already taken out). A higher figure "
                    "can be an extra cover the insurer added on its own.", j))

    # --- outliers --------------------------------------------------------------
    prices = {c: a.premium for c, a in offers.items() if a.premium}
    if len(prices) >= 3:
        mid = statistics.median(prices.values())
        for c, p in sorted(prices.items()):
            if p > mid * 2.5 or p < mid * 0.4:
                out.append(Finding(LOOK, "outlier", c,
                    f"premium {_rs(p)} is far from the middle price {_rs(mid)}",
                    journeys=j))

    # --- refusals ----------------------------------------------------------------
    prev_code = prev_codes.get(s.get("prev_insurer") or "", "")
    for d in declines:
        low = d.reason.lower()
        if matrix.says_same_insurer(d.reason):
            if prev_code and not _same(d.insurer, prev_code):
                out.append(Finding(DEFECT, "same-insurer", d.insurer,
                    f"refused as 'same insurer', but the previous insurer was "
                    f"{prev_code}", d.reason, j))
            elif not prev_code:
                out.append(Finding(DEFECT, "same-insurer", d.insurer,
                    "refused as 'same insurer' on a journey with no previous "
                    "insurer", d.reason, j))
            continue
        if d.source == "silent":
            out.append(Finding(LOOK, "silent", d.insurer,
                "was asked for a price and never answered",
                "Slow or stuck - the page keeps a spinner for it.", j))
        elif d.source == "http":
            out.append(Finding(DEFECT, "http", d.insurer,
                f"the quote call itself failed: {d.reason}",
                "The request never produced an insurer answer - our API.", j))
        elif _other_vehicle_type(low):
            number = (sent.get("VehicleDetails") or {}).get("RegistrationNumber", "")
            out.append(Finding(DEFECT, "placeholder-number", d.insurer,
                f"refused: the registration number we sent"
                f"{f' ({number})' if number else ''} is a "
                f"{_other_vehicle_type(low)} in its records",
                "On the don't-know-number journey the app invents the number "
                "(RTO + '-AB-1111', tw- and pc-dont-know-number.component.ts), "
                "and that number is real. Every customer on this journey in "
                "this RTO hits it.", j))
        elif _is_our_defect(d.reason):
            out.append(Finding(DEFECT, "our-code", d.insurer,
                f"refused with an error from OUR integration: {d.reason[:90]}",
                Failure("", d.reason).meaning, j))
        elif "empty answer" in low or "never answered" in low:
            out.append(Finding(LOOK, "vanished", d.insurer,
                "was asked, but the page shows neither a card nor a reason",
                "Its answer was an empty list; the result page only lists an "
                "insurer as unavailable when it sends a failure.", j))
        elif low.startswith("status=") or low == "no reason given":
            out.append(Finding(LOOK, "no-reason", d.insurer,
                f"refused without any reason ({d.reason})",
                "The customer only sees 'Specified plan not available online "
                "for the mentioned Agent.'", j))

    # --- screen versus wire -----------------------------------------------------
    # cards is None for a journey sent straight to the API (fast mode): there
    # was no screen, so "no card on screen" would be a lie.
    if cards is not None:
        out += _screen_checks(j, offers, answers, cards)

    # --- what the form sent -----------------------------------------------------
    out += _request_checks(j, s, sent)
    return out


def _other_vehicle_type(reason_lower: str) -> str:
    """'... belongs to PrivateCar' -> 'private car'; '' when it is not that
    refusal. Bike journeys hear 'private car', car journeys 'two wheeler'."""
    for words, name in ((("belongs to privatecar", "belongs to private car"),
                         "private car"),
                        (("belongs to twowheeler", "belongs to two wheeler",
                          "belongs to two-wheeler"), "two-wheeler")):
        if any(w in reason_lower for w in words):
            return name
    return ""


def next_ncb(previous: int) -> int:
    """One claim-free year later: 0->20->25->35->45->50, then 50 stays."""
    ladder = matrix.NCB_LADDER
    return next((step for step in ladder if step > previous), ladder[-1])


def _same(code: str, other: str) -> bool:
    a, b = code.upper(), other.upper()
    return a == b or a in b or b in a


def _is_our_defect(reason: str) -> bool:
    """Our integration failing, by the portal's own categories
    (pages/quote_list.FAILURE_KINDS) plus raw .NET exception wording."""
    kind = Failure("", reason).kind
    if kind == "our-defect":
        return True
    if kind == "insurer-down":
        # "I/O error on POST request for https://htauth.preprod.bajajgeneral
        # .com ... SSLHandshakeException" names an exception, but it is the
        # insurer failing to reach its OWN server (live, 2026-10-05).
        return False
    low = reason.lower()
    return any(k in low for k in ("object reference", "nullreference",
                                  "index was outside", "exception"))


def _ncb_earned(s: Scenario) -> bool | None:
    """True = the customer earned an NCB; False = certainly none; None = unsure."""
    if s.get("claim") == "Yes":
        return False
    if s.get("previous") in (OVER_90, DONT_KNOW):
        return False
    if s.get("prev_type") == TP:
        return False
    if s.get("claim") == "No" and s.get("previous") in matrix.REMEMBERED:
        return True
    return None


def _why_no_ncb(s: Scenario) -> str:
    if s.get("claim") == "Yes":
        return "a claim was made on the expiring policy"
    if s.get("previous") == OVER_90:
        return "the previous policy lapsed more than 90 days ago"
    if s.get("previous") == DONT_KNOW:
        return "the previous policy is not known"
    if s.get("prev_type") == TP:
        return "the previous policy was Third Party only (no own damage, no NCB)"
    return "unknown"


def _screen_checks(j, offers, answers, cards) -> list[Finding]:
    """What the insurer SENT against what the customer SEES."""
    out: list[Finding] = []
    if not cards and not offers:
        return out
    shown: dict[str, list[int]] = {}
    for card in cards:
        if card.insurer and card.premium:
            shown.setdefault(card.insurer.upper(), []).append(card.premium)
    priced: dict[str, set[int]] = {}
    for a in answers:
        if a.ok and a.premium:
            priced.setdefault(a.insurer, set()).add(round(a.premium))

    for code in sorted(offers):
        cards_for = next((v for k, v in shown.items() if _same(k, code)), None)
        if cards_for is None:
            out.append(Finding(DEFECT, "no-card", code,
                f"priced at {_rs(offers[code].premium)} by the API, but no "
                f"card on screen - the customer cannot buy it", journeys=j))
            continue
        allowed = priced.get(code, set())
        for price in cards_for:
            if allowed and not any(abs(price - p) <= 1 for p in allowed):
                out.append(Finding(DEFECT, "card-price", code,
                    f"card shows {_rs(price)}, but the insurer quoted "
                    f"{', '.join(_rs(p) for p in sorted(allowed))}", journeys=j))
    for name in sorted(shown):
        if not any(_same(name, code) for code in priced):
            out.append(Finding(LOOK, "card-unpriced", name,
                "a card is on screen but no successful answer came back for it",
                journeys=j))
    return out


def _request_checks(j, s: Scenario, sent: dict) -> list[Finding]:
    """Did the form send what the tool chose? A mismatch means either the
    click did not take or the app dropped the value - both worth a look, and
    the twin comparisons below skip a journey that did not run as planned."""
    if not sent:
        return []
    out: list[Finding] = []

    def differ(what: str, chose, got) -> None:
        out.append(Finding(LOOK, "request", "FORM",
            f"{what}: chose {chose!r}, the request carried {got!r}",
            "Either the form did not take the choice, or the app changed it "
            "on the way. See the journey's screenshot.", j))

    policy = s.get("policy")
    tp_only = bool(sent.get("IsThirdPartyOnly"))
    od_only = bool(sent.get("IsODOnly"))
    if tp_only != (policy == TP) or od_only != (policy == OD):
        differ("policy type", policy,
               "Third Party" if tp_only else "OD Only" if od_only else "Comprehensive")

    status = str(sent.get("PrevPolicyExpiryStatus") or "")
    want = {matrix.NOT_EXPIRED: "1", matrix.WITHIN_90: "2"}.get(s.get("previous"), "")
    if "PrevPolicyExpiryStatus" in sent and status != want:
        differ("previous expiry status", s.get("previous"), status or "(blank)")

    prev = sent.get("PreviousPolicyDetails") or {}
    if isinstance(prev, dict) and prev and s.get("prev_type"):
        code = str(prev.get("PreviousPolicyType") or "")
        want_code = {CP: "1", TP: "2", OD: "3"}[s.get("prev_type")]
        if code and code != want_code:
            differ("previous policy type", s.get("prev_type"), code)
        claimed = prev.get("IsPreviousInsuranceClaimed")
        if s.get("claim") is not None and claimed is not None \
                and bool(claimed) != (s.get("claim") == "Yes"):
            differ("claim made", s.get("claim"), claimed)
        ncb = prev.get("PreviousNcbPercentage")
        want_ncb = matrix.ncb_number(s.get("ncb"), s.get("year"))
        if want_ncb is not None and ncb not in (None, "") \
                and str(ncb).rstrip("%") != str(want_ncb):
            differ("previous NCB", f"{want_ncb}%", f"{ncb}%")
    return out


def ran_as_planned(findings: list[Finding], n: int) -> bool:
    return not any(f.check == "request" and n in f.journeys for f in findings)


# ======================================================= between journeys

def compare(journeys: list, findings: list[Finding],
            bands: dict[str, str]) -> tuple[list[Finding], dict[str, str]]:
    """
    Check the rules that need two quotes, wherever two journeys in this run
    differ in exactly one thing.

    journeys: list of (n, Scenario, offers) or (n, Scenario, offers, declines)
    Returns (findings, relation -> "held" | "broken" | "not checked").
    """
    out: list[Finding] = []
    status: dict[str, str] = {}
    journeys = [tuple(j) + ((),) * (4 - len(j)) for j in journeys]
    out += _same_insurer_uneven(journeys)
    usable = [(n, s, o) for n, s, o, _ in journeys
              if o and ran_as_planned(findings, n)]

    def mark(relation: str, broken: bool) -> None:
        if broken:
            status[relation] = "broken"
        else:
            status.setdefault(relation, "held")

    for (na, a, oa), (nb, b, ob) in _pairs(usable):
        dim = _only_difference(a, b)
        if dim is None:
            continue
        both = sorted(set(oa) & set(ob))
        if not both:
            continue
        pair = (na, nb)

        if dim == "policy" and {a.get("policy"), b.get("policy")} == {CP, TP}:
            cp, tp = (oa, ob) if a.get("policy") == CP else (ob, oa)
            ncp, ntp = (na, nb) if a.get("policy") == CP else (nb, na)
            for c in both:
                broken = cp[c].premium <= tp[c].premium
                mark("cp-vs-tp", broken)
                if broken:
                    out.append(Finding(DEFECT, "cp-vs-tp", c,
                        f"Comprehensive {_rs(cp[c].premium)} is not dearer than "
                        f"Third Party {_rs(tp[c].premium)}",
                        "Comprehensive = own damage + third party, so it must "
                        "cost more.", (ncp, ntp)))
                if cp[c].tp and tp[c].tp and not _close(cp[c].tp, tp[c].tp, 5):
                    out.append(Finding(LOOK, "tp-part", c,
                        f"third-party part differs: {_rs(cp[c].tp)} inside "
                        f"Comprehensive, {_rs(tp[c].tp)} on its own", journeys=pair))

        elif dim == "policy" and {a.get("policy"), b.get("policy")} == {CP, OD}:
            cp, od = (oa, ob) if a.get("policy") == CP else (ob, oa)
            for c in both:
                broken = od[c].premium >= cp[c].premium
                mark("cp-vs-od", broken)
                if broken:
                    out.append(Finding(DEFECT, "cp-vs-od", c,
                        f"OD Only {_rs(od[c].premium)} is not cheaper than "
                        f"Comprehensive {_rs(cp[c].premium)}",
                        "OD Only leaves the third-party part out.", pair))

        elif dim == "ncb":
            hi, lo = (a, b) if _ncb(a) > _ncb(b) else (b, a)
            ohi, olo = (oa, ob) if hi is a else (ob, oa)
            for c in both:
                if ohi[c].premium > olo[c].premium:
                    mark("ncb", True)
                    out.append(Finding(DEFECT, "ncb", c,
                        f"NCB {_ncb(hi)}% costs {_rs(ohi[c].premium)}, MORE than "
                        f"NCB {_ncb(lo)}% at {_rs(olo[c].premium)}", journeys=pair))
                elif ohi[c].premium == olo[c].premium:
                    mark("ncb", True)
                    out.append(Finding(LOOK, "ncb", c,
                        f"NCB {_ncb(lo)}% and {_ncb(hi)}% give the same price "
                        f"{_rs(ohi[c].premium)} - the NCB may not reach this "
                        f"insurer", journeys=pair))
                else:
                    mark("ncb", False)

        elif dim == "year":
            old, new = (a, b) if int(a.get("year")) < int(b.get("year")) else (b, a)
            oold, onew = (oa, ob) if old is a else (ob, oa)
            for c in both:
                if not (oold[c].idv and onew[c].idv):
                    continue
                broken = oold[c].idv >= onew[c].idv
                mark("age-idv", broken)
                if broken:
                    out.append(Finding(DEFECT, "age-idv", c,
                        f"IDV did not drop with age: {old.get('year')} vehicle "
                        f"{_rs(oold[c].idv)}, {new.get('year')} vehicle "
                        f"{_rs(onew[c].idv)}", journeys=pair))

        elif dim == "claim":
            yes, no = (a, b) if a.get("claim") == "Yes" else (b, a)
            oyes, ono = (oa, ob) if yes is a else (ob, oa)
            for c in both:
                if oyes[c].premium < ono[c].premium:
                    mark("claim", True)
                    out.append(Finding(DEFECT, "claim", c,
                        f"after a claim the renewal is CHEAPER: "
                        f"{_rs(oyes[c].premium)} vs {_rs(ono[c].premium)}",
                        "A claim removes the NCB, so the price can only go up.",
                        pair))
                elif oyes[c].premium == ono[c].premium and (_ncb(no) or 0) > 0:
                    mark("claim", True)
                    out.append(Finding(LOOK, "claim", c,
                        f"claim or no claim, the price is {_rs(ono[c].premium)} "
                        f"- the {_ncb(no)}% NCB should have been lost",
                        journeys=pair))
                else:
                    mark("claim", False)

        elif dim == "previous" and OVER_90 in (a.get("previous"), b.get("previous")):
            lapsed, live = (a, b) if a.get("previous") == OVER_90 else (b, a)
            if live.get("previous") != matrix.NOT_EXPIRED:
                continue
            olap, olive = (oa, ob) if lapsed is a else (ob, oa)
            for c in both:
                broken = olap[c].premium < olive[c].premium
                mark("expired", broken)
                if broken:
                    out.append(Finding(LOOK, "expired", c,
                        f"a policy lapsed 90+ days is CHEAPER: "
                        f"{_rs(olap[c].premium)} vs {_rs(olive[c].premium)}",
                        "The NCB is lost after 90 days, so the price should not "
                        "fall.", pair))

        elif dim == "vehicle" and a.get("policy") == TP:
            order = {band: i for rules in matrix.RULES.values()
                     for i, band in enumerate(rules.sizes, start=1)}
            ba, bb = order.get(bands.get(a.get("vehicle"), "")), \
                order.get(bands.get(b.get("vehicle"), ""))
            if not ba or not bb or ba == bb:
                continue
            big, small = (oa, ob) if ba > bb else (ob, oa)
            for c in both:
                bt = (big[c].tp or big[c].premium) - (big[c].pa_cover or 0)
                st = (small[c].tp or small[c].premium) - (small[c].pa_cover or 0)
                broken = bt < st
                mark("cc-tp", broken)
                if broken:
                    out.append(Finding(DEFECT, "cc-tp", c,
                        f"a bigger engine gets a CHEAPER third-party price: "
                        f"{_rs(bt)} vs {_rs(st)}",
                        "Regulated TP rates rise with engine size.", pair))
    for relation in matrix.RELATIONS:
        status.setdefault(relation, "not checked")
    return out, status


def _same_insurer_uneven(journeys) -> list[Finding]:
    """
    The 'same insurer' rule should not depend on the policy type. Seen live
    2026-09-30: BAJAJ refused Comprehensive as 'same insurer' but priced
    Third Party, with BAJAJ as the previous insurer both times.
    """
    out, seen = [], set()
    for (na, a, oa, da), (nb, b, ob, db) in _pairs(journeys):
        if not a.get("prev_insurer") or a.get("prev_insurer") != b.get("prev_insurer"):
            continue
        for (n1, s1, d1), (n2, s2, o2) in (((na, a, da), (nb, b, ob)),
                                          ((nb, b, db), (na, a, oa))):
            for d in d1:
                if matrix.says_same_insurer(d.reason) and d.insurer in o2 \
                        and d.insurer not in seen:
                    seen.add(d.insurer)
                    out.append(Finding(LOOK, "same-insurer-uneven", d.insurer,
                        f"refused {s1.get('policy')} as 'same insurer' but priced "
                        f"{s2.get('policy')} - previous insurer "
                        f"{a.get('prev_insurer')} both times",
                        "Either the renewal rule is wrong for one policy type, "
                        "or it is missing for the other.",
                        tuple(sorted((n1, n2)))))
    return out


def _pairs(items):
    for i in range(len(items)):
        for k in range(i + 1, len(items)):
            yield items[i], items[k]


def _only_difference(a: Scenario, b: Scenario) -> str | None:
    """The single dimension two journeys differ in, or None.

    A choice that does not apply on one side (None) is not a difference -
    Third Party has no NCB, and that is part of being Third Party."""
    diff = [d for d, x, y in zip(matrix.DIMENSIONS, a.values, b.values)
            if x is not None and y is not None and x != y]
    if len(diff) != 1:
        return None
    dim = diff[0]
    # The NCB twin compares numbers; "default" and "35%" can be the same thing.
    if dim == "ncb" and _ncb(a) == _ncb(b):
        return None
    # A claim or a lapse also switches NCB off; that is part of the change.
    return dim


def _ncb(s: Scenario) -> int:
    return matrix.ncb_number(s.get("ncb"), s.get("year")) or 0
