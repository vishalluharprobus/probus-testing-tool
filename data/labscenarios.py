"""
Every scenario the insurer lab tries, and exactly how each one changes the request.

HOW A SCENARIO IS BUILT
-----------------------
A scenario is the BASELINE (a quote that works) with ONE thing changed:

    baseline                    4-year-old vehicle, Comprehensive, previous
                                policy not expired, no claim, the app's NCB,
                                no add-ons, no covers, automatic IDV
    + electrical = 25000        -> CoverageDetails.IsElectricalAccessories =
                                   true, ElectricalAccessoryAmount = 25000

Changing one thing at a time is what makes the lab's conclusions safe: when
the answer changes, only that one thing can have changed it.

After that come COMBINATIONS insurers are known to restrict - add-ons by
vehicle age (NATIONAL keeps Return to Invoice only up to 3 years and Zero Dep
up to 5, InsureBridge PCNationalInsuranceRules.cs:2553-2606), OD Only with
add-ons and accessories, Third Party with the covers that still apply to it.

WHERE THE FIELD NAMES COME FROM
-------------------------------
Saarthi's own screens and filter (pc-dont-know-number, pc-quotation-filter,
tw-quotation-filter ProcessQuote) and InsureBridge's request model
(CoverageDetails.cs, DiscountDetails, PCQuoteRequest.cs), read 2026-09-30.
The request is always rebuilt from the whole state, never patched bit by
bit, so one scenario can never leak into the next.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

CP, TP, OD = "Comprehensive", "Third Party", "OD Only"
NOT_EXPIRED = "Not Expired"
WITHIN_90 = "Expired within 90 Days"
OVER_90 = "Expired more than 90 Days"
DONT_KNOW = "Don't remember"

NCB_LADDER = (0, 20, 25, 35, 45, 50)
BASE_AGE = 4
FMT = "%d %b %Y"               # the app's date format, "30 Sep 2026"


@dataclass(frozen=True)
class Product:
    name: str                  # "bike" | "car"
    segment: str               # API path segment, lower case
    page: str                  # Saarthi route segment
    quote_key: str             # "TWQuotation" | "PCQuotation"
    od_max_age: int            # OD Only offered up to this age
    cp_max_age: int            # Comprehensive offered up to this age
    tp_bundle_years: int       # length of the long-term TP cover
    ages: tuple                # ages to try, one at a time
    voluntary: tuple           # voluntary excess options in the UI
    pa_options: tuple          # PA sum insured options in the UI
    sub_product: str = ""      # SubProductCode the portal uses (tw-proposal-otp:873)
    title: str = ""            # what the business calls it


PRODUCTS = {
    # tw-dont-know-number.component.ts:1198-1212; tw-quotation-filter.html
    "bike": Product("bike", "twowheeler", "two-wheeler", "TWQuotation",
                    od_max_age=4, cp_max_age=25, tp_bundle_years=5,
                    ages=(1, 2, 3, 5, 7, 10, 15, 20),
                    voluntary=(500, 750, 1000, 1500, 3000),
                    pa_options=(10000, 50000, 100000),
                    sub_product="MTRTW", title="Two Wheeler"),
    # pc-dont-know-number.component.ts:929-943; pc-quotation-filter.html
    "car": Product("car", "privatecar", "private-car", "PCQuotation",
                   od_max_age=4, cp_max_age=14, tp_bundle_years=3,
                   ages=(1, 2, 3, 5, 7, 10, 14),
                   voluntary=(2500, 5000, 7500, 15000),
                   pa_options=(10000, 50000, 100000, 200000),
                   sub_product="MTRPC", title="Private Car"),
}

# Amounts to try for each cover. Wide on purpose: the point is to find where
# the insurer draws its line, and the notebook remembers it afterwards.
AMOUNTS = {
    "electrical": (5000, 10000, 25000, 50000, 100000),
    "non_electrical": (5000, 10000, 25000, 50000),
    "bifuel_kit": (10000, 25000, 50000),
}

# cover -> (flag field, amount field, amount as text?, PremiumBreakUpDetails
#           field that proves it was priced, "adds" or "takes off" premium)
COVERS = {
    "electrical": ("IsElectricalAccessories", "ElectricalAccessoryAmount", False,
                   "ElecAccessoriesPremium", "adds"),
    "non_electrical": ("IsNonElectricalAccessories", "NonElectricalAccessoryAmount",
                       False, "NonElecAccessoriesPremium", "adds"),
    "bifuel_kit": ("IsBiFuelKit", "BiFuelKitValue", False, "CNGLPGKitPremium", "adds"),
    "pa_passenger": ("IsPACoverUnnamedPerson", "UnNamedSumInsured", True,
                     "PACoverToUnNamedPerson", "adds"),
    "paid_driver": ("IsPACoverPaidDriver", "PaidDriverSumInsured", True,
                    "PAToPaidDriver", "adds"),
    "ll_paid_driver": ("IsLegalLiablityPaidDriver", "NoOfLLPaidDriver", True,
                       "LLToPaidDriver", "adds"),
    "fibre_glass": ("IsFiberGlassFuelTank", "", False, "FiberGlassTankPremium", "adds"),
}
DISCOUNTS = {
    "voluntary_deductible": ("IsVoluntaryExcess", "VoluntaryExcessAmount", True,
                             "VoluntaryDiscount", "takes off"),
    "anti_theft": ("IsAntiTheftDevice", "", False, "AntiTheftDiscount", "takes off"),
    "aai": ("IsMemberOfAutomobileAssociation", "", False, "AAIDiscount", "takes off"),
    "tppd": ("IsTPPDRestrictedto6000", "", False, "RestrictLiability", "takes off"),
}
# Which covers the UI offers for which policy type (pc-quotation-filter.html).
HIDDEN_FOR_TP = {"electrical", "non_electrical", "fibre_glass", "anti_theft", "aai",
                 "addon", "idv"}
HIDDEN_FOR_OD = {"pa_passenger", "ll_paid_driver", "cpa"}

# The portal's segment number for each policy type
# (tw-result.component.ts:1437-1443, pc-result.component.ts:1748-1754).
SEGMENTS = {CP: 1, OD: 2, TP: 3}

# What every scenario starts from - it changes only what its label says.
STANDARD = (f"{CP}, {BASE_AGE} years old, previous policy not expired, no claim, "
            f"no add-ons")

NICE = {
    "baseline": "standard quote", "year": "vehicle age", "policy": "policy type",
    "previous": "previous policy", "prev_type": "previous policy type",
    "ncb": "NCB", "claim": "claim made", "customer": "customer type",
    "owner_changed": "owner changed", "addon": "add-on", "electrical":
    "electrical accessories", "non_electrical": "non-electrical accessories",
    "bifuel_kit": "CNG/LPG kit", "pa_passenger": "PA unnamed passengers",
    "paid_driver": "PA paid driver", "ll_paid_driver": "LL paid driver",
    "fibre_glass": "fibre-glass tank", "voluntary_deductible": "voluntary excess",
    "anti_theft": "anti-theft device", "aai": "AAI membership",
    "tppd": "TPPD restricted to 6000", "idv": "IDV", "cpa": "compulsory PA owner",
    "vehicle": "vehicle (MMV)",
}


def ncb_for_age(age: int) -> int:
    return NCB_LADDER[max(0, min(age - 1, len(NCB_LADDER) - 1))]


# ====================================================================== state

@dataclass(frozen=True)
class State:
    """Everything a quote depends on. The baseline is State() with defaults."""
    age: int = BASE_AGE
    policy: str = CP
    previous: str = NOT_EXPIRED
    prev_type: str = CP
    ncb: int | None = None               # None = the app's own default
    claim: bool = False
    customer: str = "Individual"
    owner_changed: bool = False
    addons: tuple = ()                   # add-on ids
    covers: tuple = ()                   # ((name, amount), ...)
    idv: int | None = None
    cpa: bool = False
    vehicle: str = ""                    # catalogue key; "" = the template's

    @property
    def year(self) -> int:
        return date.today().year - self.age

    @property
    def ncb_value(self) -> int:
        """The NCB the request carries, after the form's own rules."""
        if self.policy == TP or self.claim or self.prev_type == TP \
                or self.previous not in (NOT_EXPIRED, WITHIN_90):
            return 0
        return ncb_for_age(self.age) if self.ncb is None else self.ncb

    def context(self) -> dict:
        """What a learned rule may be conditioned on."""
        return {"age": self.age, "policy": self.policy, "previous": self.previous,
                "customer": self.customer}


@dataclass(frozen=True)
class Scenario:
    """One quote the lab sends. `changes` says what differs from the baseline."""
    state: State
    changes: tuple                 # ((dimension, value), ...) ; () = baseline
    why: str = ""
    phase: str = "one-at-a-time"   # baseline | one-at-a-time | combination | boundary | recheck

    @property
    def key(self) -> str:
        if not self.changes:
            return "baseline"
        return " & ".join(f"{d}={v}" for d, v in self.changes)

    @property
    def dimension(self) -> str:
        return self.changes[0][0] if self.changes else "baseline"

    @property
    def value(self):
        return self.changes[0][1] if self.changes else ""

    def label(self, addon_names: dict | None = None) -> str:
        if not self.changes:
            on = f" on {self.state.vehicle.replace('|', ' ')}" if self.state.vehicle else ""
            return f"standard quote{on} ({STANDARD})"
        out = []
        for dim, value in self.changes:
            shown = value
            if value is True:
                shown = "yes"
            elif dim == "addon":
                shown = ("all add-ons" if value == "all" else
                         (addon_names or {}).get(str(value), value))
            elif dim == "vehicle":
                shown = str(value).replace("|", " ")
            elif dim == "ll_paid_driver":
                shown = f"{value} driver{'s' if value != 1 else ''}"
            elif isinstance(value, (int, float)) and dim not in ("age", "year", "ncb"):
                shown = f"Rs {value:,.0f}"
            elif dim == "year":
                shown = f"{value} years old ({date.today().year - int(value)})"
            elif dim == "ncb":
                shown = f"{value}%"
            out.append(f"{NICE.get(dim, dim)} = {shown}")
        return " + ".join(out)


# ============================================================ building bodies

def build_body(template: dict, product: Product, s: State, *,
               prev_insurer: dict, vehicle: dict | None = None,
               recalc: dict | None = None) -> dict:
    """
    The QualifiedCompany body for one state, from the captured template.

    prev_insurer: {"Id", "CompanyCode", "CShortName", "Name"} of the previous
                  insurer (never the insurer under test - see Lab).
    vehicle:      a catalogue row, or None to keep the template's vehicle.
    recalc:       {"QuotationNumber", "CompanyIdvDetails", "CompanyReferanceDetails"}
                  for add-on / cover / IDV scenarios, which the app only ever
                  sends as a Re-Calculate of an existing quote.
    """
    b = copy.deepcopy(template)
    q = b.get(product.quote_key)
    if not isinstance(q, dict):
        q = {}
    today = date.today()

    # --- the vehicle's age: registration, purchase and manufacture dates -----
    anchor = {NOT_EXPIRED: today, WITHIN_90: today - timedelta(days=50)}.get(
        s.previous, today - timedelta(days=90))
    registered = date(s.year, anchor.month, 1)
    vd = b.setdefault("VehicleDetails", {})
    for key in ("RegistrationDate", "PurchaseDate", "ManufaturingDate"):
        vd[key] = registered.strftime(FMT)
    q["RegistrationYear"] = s.year

    # --- the vehicle itself (MMV) --------------------------------------------
    if vehicle:
        _set_vehicle(b, vehicle)

    # --- policy type -----------------------------------------------------------
    b["IsThirdPartyOnly"] = s.policy == TP
    b["IsODOnly"] = s.policy == OD
    q["IsThirdPartyOnly"], q["IsODOnly"] = b["IsThirdPartyOnly"], b["IsODOnly"]
    if s.policy == OD:
        tp_end = _safe_date(registered.year + product.tp_bundle_years,
                            today.month, today.day)
        b["PreviousTPPolicyDetails"] = {
            "InsurerCode": prev_insurer.get("Id"),
            "InsurerName": prev_insurer.get("Name"),
            "PolicyEndDate": tp_end.strftime(FMT),
            "PolicyNumber": "TP12456789"}
    else:
        b.pop("PreviousTPPolicyDetails", None)

    # --- customer and owner -------------------------------------------------------
    b["CustomerType"] = s.customer
    b["OrganizationName"] = "Lab Test Org Pvt Ltd" if s.customer == "Organization" else ""
    b["IsOwnerChanged"] = s.owner_changed
    # Existing PA cover: false for Organization and OD (pc-dont-know-number
    # .ts:417-422); ticking "I need a Compulsory PA Owner Driver Cover" also
    # makes it false (pc-result.component.ts:290-314).
    b["IsExistingPACover"] = not (s.customer == "Organization" or s.policy == OD or s.cpa)

    # --- previous policy -------------------------------------------------------
    remembered = s.previous in (NOT_EXPIRED, WITHIN_90)
    b["IsBreakingCase"] = s.previous != NOT_EXPIRED
    b["DontKnowPreviousInsurer"] = not remembered
    b["PreviousPolicyDetailsRequired"] = remembered
    b["PrevPolicyExpiryStatus"] = {NOT_EXPIRED: "1", WITHIN_90: "2"}.get(s.previous, "")
    q["PrevPolicyExpiryStatus"] = b["PrevPolicyExpiryStatus"]
    q["DontKnowPreviousInsurer"] = b["DontKnowPreviousInsurer"]
    if remembered:
        ppd = b.get("PreviousPolicyDetails")
        ppd = dict(ppd) if isinstance(ppd, dict) else {
            "PolicyNumber": "123456789", "InsurerName": ""}
        ppd["PolicyEndDate"] = anchor.strftime(FMT)
        ppd["PreviousPolicyType"] = {CP: "1", TP: "2", OD: "3"}[s.prev_type]
        ppd["IsPreviousInsuranceClaimed"] = s.claim
        ppd["InsurerCode"] = prev_insurer.get("Id")
        if s.prev_type == TP:
            # The form disables NCB for a previous TP policy, so the key drops
            # out of the JSON altogether (pc-dont-know-number.ts:945-951).
            ppd.pop("PreviousNcbPercentage", None)
        else:
            ppd["PreviousNcbPercentage"] = str(s.ncb_value)
        b["PreviousPolicyDetails"] = ppd
        insurer = dict(b.get("PrevPolicyInsurer") or {})
        insurer.update({k: prev_insurer.get(k) for k in
                        ("Id", "CompanyCode", "CShortName", "Name") if k in prev_insurer})
        insurer.setdefault("Checked", False)
        b["PrevPolicyInsurer"] = insurer
        q["PreviousPolicyType"] = ppd["PreviousPolicyType"]
    else:
        b["PreviousPolicyDetails"] = None
        b.pop("PrevPolicyInsurer", None)
        q.pop("PrevPolicyInsurer", None)

    # --- add-ons, covers, discounts, IDV -------------------------------------------
    b["RequestedAddOnList"] = [] if s.policy == TP else [str(a) for a in s.addons]
    cd = dict(b.get("CoverageDetails") or {})
    dd = dict(b.get("DiscountDetails") or {})
    chosen = dict(s.covers)
    for name, (flag, amount, as_text, _, _) in {**COVERS, **DISCOUNTS}.items():
        target = dd if name in DISCOUNTS else cd
        if name in chosen:
            target[flag] = True
            if amount:
                value = chosen[name]
                target[amount] = str(value) if as_text else value
        else:
            target[flag] = False
            if amount:
                target.pop(amount, None)
    if "aai" in chosen:
        dd["AssociationName"] = "Automobile Association of India"
        dd["MembershipNumber"] = "AAI123456"
        dd["MembershipExpiryDate"] = (today + timedelta(days=365)).strftime(FMT)
    else:
        for key in ("AssociationName", "MembershipNumber", "MembershipExpiryDate"):
            dd.pop(key, None)
    b["CoverageDetails"], b["DiscountDetails"] = cd, dd

    if s.idv:
        b["CustomIDVAmount"] = int(s.idv)
    else:
        b.pop("CustomIDVAmount", None)

    # --- a new quote, or a Re-Calculate of an existing one ----------------------
    if recalc:
        b["QuotationNumber"] = recalc["QuotationNumber"]
        b["IsRecalculateQuote"] = True
        b["CompanyIdvDetails"] = recalc.get("CompanyIdvDetails") or []
        b["CompanyReferanceDetails"] = recalc.get("CompanyReferanceDetails") or []
    else:
        b.pop("QuotationNumber", None)
        b.pop("IsRecalculateQuote", None)
        b["CompanyIdvDetails"] = []
    if q:
        b[product.quote_key] = q
    return b


def in_city(body: dict, product: Product, city: dict) -> dict:
    """Move a request to another RTO: the five places the form writes it
    (pc/tw-dont-know-number: BimaPostRTOId, RegistrationNumber, BPRtoId,
    RTOCityName twice)."""
    b = json.loads(json.dumps(body))
    name, rto_id = city["Name"], str(city["Id"])
    vd = b.setdefault("VehicleDetails", {})
    vd["BimaPostRTOId"] = rto_id
    vd["RegistrationNumber"] = f"{name[:5]}-AB-1111"
    b["RTOCityName"] = name
    q = b.get(product.quote_key)
    if isinstance(q, dict):
        q["BPRtoId"] = int(rto_id) if isinstance(q.get("BPRtoId"), int) else rto_id
        q["RTOCityName"] = name
    return b


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


# Keys that name the vehicle, wherever they sit in the request.
_VEHICLE_KEYS = {"VariantCode": "variant_id", "MakeCode": "make_id",
                 "ModelCode": "model_id"}


def _set_vehicle(body: dict, row: dict) -> None:
    """Swap the vehicle everywhere the template names it. The server looks
    the rest (cc, fuel, seats, ex-showroom price) up from VariantCode."""
    def walk(node):
        if isinstance(node, dict):
            for key in list(node):
                if key in _VEHICLE_KEYS and row.get(_VEHICLE_KEYS[key]) is not None:
                    node[key] = row[_VEHICLE_KEYS[key]]
                elif key == "MakeName":
                    node[key] = row["make"]
                elif key == "ModelName":
                    node[key] = row["model"]
                elif key == "VariantName":
                    old = str(node[key] or "")
                    name = row.get("variant_name") or row["variant"]
                    # Keep the template's style: "<variant> - <fuel>" (car
                    # request, pc-dont-know-number.ts:402) or the plain name.
                    node[key] = f"{name} - {row['fuel']}" if " - " in old and row.get("fuel") \
                        else name
                else:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(body)


def recalc_details(items: list, quotation: str) -> dict:
    """What a Re-Calculate carries, built from the baseline's own answer the
    way the app builds it (pc-result.component.ts:1912-1933)."""
    idv, refs = [], []
    for item in items:
        if item.get("Status") != "Success":
            continue
        idv.append({"CompanyCode": item.get("CompanyCode"), "PlanId": item.get("PlanId"),
                    "MinIDV": item.get("MinInsuredDeclaredValue"),
                    "MaxIDV": item.get("MaxInsuredDeclaredValue"),
                    "IDV": item.get("InsuredDeclaredValue")})
        ref = {"CompanyOrderNumber": item.get("CompanyOrderNumber"),
               "CompanyQuotationNumber": item.get("CompanyQuotationNumber"),
               "CompanyCode": item.get("CompanyCode"),
               "CompanyPremiumYear": item.get("PremiumYear")}
        for n in range(1, 7):
            ref[f"ExtraParameter{n}"] = item.get(f"ExtraParameter{n}")
        refs.append(ref)
    return {"QuotationNumber": quotation, "CompanyIdvDetails": idv,
            "CompanyReferanceDetails": refs}


# =========================================================== choosing scenarios

RECALC_DIMENSIONS = {"addon", "idv", "cpa"} | set(COVERS) | set(DISCOUNTS)


def is_recalc(s: Scenario) -> bool:
    return any(d in RECALC_DIMENSIONS for d, _ in s.changes)


def offered(product: Product, s: State, dimension: str, fuel: str = "") -> bool:
    """Would the portal's own UI let a customer choose this? Not asking the
    insurer questions the screen never asks keeps every finding real."""
    if s.policy == OD and s.age > product.od_max_age:
        return False
    if s.policy == CP and s.age > product.cp_max_age:
        return False
    if s.policy == TP and dimension in HIDDEN_FOR_TP:
        return False
    if s.policy == OD and dimension in HIDDEN_FOR_OD:
        return False
    if dimension == "bifuel_kit" and fuel and fuel.upper() != "PETROL":
        return False
    if s.policy == OD and s.prev_type == TP:
        return False
    if s.policy != OD and s.prev_type == OD:
        return False
    if s.ncb is not None and s.ncb > ncb_for_age(s.age):
        return False              # more NCB than the vehicle can have earned
    if s.cpa and s.customer == "Organization":
        return False
    return True


def one_at_a_time(product: Product, addons: dict[str, str], fuel: str,
                  vehicles: list[dict], idv_range: tuple | None) -> list[Scenario]:
    """The baseline, then every value of every choice, one at a time."""
    base = State()
    out = [Scenario(base, (), "the quote everything else is compared with",
                    phase="baseline")]

    def add(dimension, value, state, why):
        if offered(product, state, dimension, fuel):
            out.append(Scenario(state, ((dimension, value),), why))

    for age in product.ages:
        add("year", age, replace(base, age=age), "does the price and IDV follow the vehicle's age?")
    add("policy", TP, replace(base, policy=TP), "Third Party only")
    add("policy", OD, replace(base, policy=OD), "OD Only (needs a live long-term TP)")
    add("previous", WITHIN_90, replace(base, previous=WITHIN_90), "break-in, within 90 days")
    add("previous", OVER_90, replace(base, previous=OVER_90), "lapsed 90+ days - NCB lost")
    add("previous", DONT_KNOW, replace(base, previous=DONT_KNOW), "previous policy unknown")
    add("prev_type", TP, replace(base, prev_type=TP), "previous policy was Third Party")
    for ncb in NCB_LADDER:
        if ncb != ncb_for_age(BASE_AGE):
            add("ncb", ncb, replace(base, ncb=ncb), "a different NCB")
    add("claim", True, replace(base, claim=True), "a claim was made - NCB must go")
    add("customer", "Organization", replace(base, customer="Organization"),
        "company-owned vehicle")
    add("owner_changed", True, replace(base, owner_changed=True), "owner changed")
    for addon_id in addons:
        add("addon", addon_id, replace(base, addons=(addon_id,)), "one add-on")
    if len(addons) > 1:
        add("addon", "all", replace(base, addons=tuple(addons)), "every add-on at once")
    for name, amounts in AMOUNTS.items():
        for amount in amounts:
            add(name, amount, replace(base, covers=((name, amount),)), "how much is allowed?")
    for amount in product.pa_options:
        add("pa_passenger", amount, replace(base, covers=(("pa_passenger", amount),)),
            "PA for unnamed passengers")
        add("paid_driver", amount, replace(base, covers=(("paid_driver", amount),)),
            "PA for the paid driver")
    add("ll_paid_driver", 1, replace(base, covers=(("ll_paid_driver", 1),)),
        "legal liability to a paid driver")
    add("fibre_glass", True, replace(base, covers=(("fibre_glass", True),)),
        "fibre-glass fuel tank")
    for amount in product.voluntary:
        add("voluntary_deductible", amount,
            replace(base, covers=(("voluntary_deductible", amount),)),
            "voluntary excess - must make it cheaper")
    for name in ("anti_theft", "aai", "tppd"):
        add(name, True, replace(base, covers=((name, True),)), "a discount - must not cost more")
    add("cpa", True, replace(base, cpa=True), "compulsory PA for the owner-driver")
    if idv_range:
        low, high = idv_range
        for tag, value in (("min", low), ("middle", (low + high) // 2), ("max", high),
                           ("below min", max(1000, low - 5000)), ("above max", high + 5000)):
            add("idv", int(value), replace(base, idv=int(value)), f"IDV at its {tag}")
    for row in vehicles:
        add("vehicle", row["key"], replace(base, vehicle=row["key"]),
            f"{row['band'] or 'another'} {row['fuel'].lower()} vehicle - is it mapped?")
    return out


def combinations(product: Product, addons: dict[str, str], fuel: str) -> list[Scenario]:
    """
    The pairs insurers are known to restrict. Not every pair - just the ones
    where limits live: add-ons by age, OD with add-ons and accessories, older
    vehicles with a high NCB, Third Party with the covers that still apply.
    """
    base = State()
    out: list[Scenario] = []

    def add(changes, state, why):
        dims = [d for d, _ in changes]
        if all(offered(product, state, d, fuel) for d in dims):
            out.append(Scenario(state, tuple(changes), why, phase="combination"))

    for addon_id in addons:
        for age in (1, 3, 5, 7, 10):
            if age != BASE_AGE:
                add((("addon", addon_id), ("year", age)),
                    replace(base, addons=(addon_id,), age=age),
                    "add-ons often stop at an age")
    od = replace(base, policy=OD)
    if addons:
        add((("policy", OD), ("addon", "all")), replace(od, addons=tuple(addons)),
            "OD Only with every add-on")
    add((("policy", OD), ("electrical", 10000)),
        replace(od, covers=(("electrical", 10000),)), "OD Only with accessories")
    add((("policy", OD), ("claim", True)), replace(od, claim=True), "OD Only after a claim")
    for age, ncb in ((5, 45), (7, 50), (10, 50)):
        add((("year", age), ("ncb", ncb)), replace(base, age=age, ncb=ncb),
            "an older vehicle with the NCB it can have earned")
    tp = replace(base, policy=TP)
    for name, value in (("pa_passenger", product.pa_options[-1]), ("paid_driver",
                         product.pa_options[0]), ("ll_paid_driver", 1), ("tppd", True)):
        add((("policy", TP), (name, value)), replace(tp, covers=((name, value),)),
            "Third Party with a cover it still allows")
    add((("previous", WITHIN_90), ("electrical", 10000)),
        replace(base, previous=WITHIN_90, covers=(("electrical", 10000),)),
        "break-in with accessories")
    return out
