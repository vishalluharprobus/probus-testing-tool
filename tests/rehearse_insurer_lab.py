"""
Rehearse the insurer lab against a FAKE insurer - no portal, no network.

    python tests/rehearse_insurer_lab.py            # watch two sessions, ~1 minute
    venv\\Scripts\\python -m pytest tests/rehearse_insurer_lab.py -q

WHY
---
The lab's promise is that it finds an insurer's rules by itself and obeys them
next time. That can only be proven against an insurer whose rules we KNOW. So
this runs a small fake of our quote API, on this machine, that behaves like
NATIONAL private car as InsureBridge's code describes it - plus planted faults:

    electrical accessories   refused above 10,000, and the message SAYS 10000
    non-electrical           refused above 15,000, message gives NO number
    Zero Depreciation        accepted, but silently left out from 6 years old
    Return to Invoice        accepted, but silently left out from 4 years old
    owner changed            "If Owner has changed then policy creation is not
                             allowed by National...!!"
    one vehicle              "MMV is not mapped." (our own decline rules)
    voluntary excess         accepted and ignored (NATIONAL never sends it)
    custom IDV               quietly clamped to the insurer's range
    CNG/LPG kit              PLANTED BUG: makes the quote CHEAPER

Session 1 must find every one of those. Session 2 must obey what it learned:
no electrical above 10,000, no owner change, no Zero Dep on old vehicles, not
the unmapped vehicle - and test exactly 10,000 instead.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_insurer_lab as lab_runner  # noqa: E402
from core import console, labrules  # noqa: E402
from core.labclient import Template  # noqa: E402
from data import labscenarios as ls  # noqa: E402

ADDONS = [{"Id": 2, "Name": "Zero Depreciation", "Rank": 1},
          {"Id": 8, "Name": "Return to Invoice", "Rank": 1},
          {"Id": 12, "Name": "Road Side Assistance", "Rank": 0}]
UNMAPPED_VARIANT = 9003
TOKEN = "rehearsal-token"


def price(body: dict) -> dict:
    """The fake NATIONAL: one per-plan answer for one request body."""
    cd, dd = body.get("CoverageDetails") or {}, body.get("DiscountDetails") or {}
    age = 2026 - int(str(body["VehicleDetails"]["RegistrationDate"])[-4:])
    fail = None
    if body.get("IsOwnerChanged"):
        fail = "If Owner has changed then policy creation is not allowed by National...!!"
    elif cd.get("IsElectricalAccessories") and int(cd.get("ElectricalAccessoryAmount", 0)) > 10000:
        fail = "Electrical accessories value should not be more than 10000"
    elif cd.get("IsNonElectricalAccessories") and \
            int(cd.get("NonElectricalAccessoryAmount", 0)) > 15000:
        fail = "Invalid non electrical accessories amount"
    if fail:
        return {"CompanyCode": "NATIONAL", "Status": "Error", "ErrorMessage": fail}

    wear = max(0.2, 1 - 0.07 * age)
    low, high = 520000 * wear, 620000 * wear
    idv = int(body.get("CustomIDVAmount") or (low + high) / 2)
    idv = int(min(max(idv, low), high))                       # clamped, silently
    tp_only, od_only = body.get("IsThirdPartyOnly"), body.get("IsODOnly")
    od = 0.0 if tp_only else round(idv * 0.022, 2)
    ppd = body.get("PreviousPolicyDetails") or {}
    ncb = int(ppd.get("PreviousNcbPercentage") or 0)
    new_ncb = 0 if (ppd.get("IsPreviousInsuranceClaimed") or not ppd or tp_only
                    or ppd.get("PreviousPolicyType") == "2") \
        else next((n for n in ls.NCB_LADDER if n > ncb), 50)
    ncb_rs = round(od * new_ncb / 100, 2)
    tp = 0.0 if od_only else 3416.0
    addon, applicable = 0.0, {}
    for aid in body.get("RequestedAddOnList") or []:
        if aid == "2":
            applicable["IsZeroDepreciation"] = "Applicable" if age <= 5 else "NotApplicable"
            addon += 900 if age <= 5 else 0
        elif aid == "8":
            applicable["IsReturnToInvoice"] = "Applicable" if age <= 3 else "NotApplicable"
            addon += 700 if age <= 3 else 0
        elif aid == "12":
            applicable["IsRoadSideAssistance"] = "Applicable"
            addon += 199
    elec = 0.04 * int(cd.get("ElectricalAccessoryAmount", 0)) if cd.get("IsElectricalAccessories") else 0
    nonel = 0.03 * int(cd.get("NonElectricalAccessoryAmount", 0)) \
        if cd.get("IsNonElectricalAccessories") else 0
    cng = -100.0 if cd.get("IsBiFuelKit") else 0.0            # PLANTED BUG
    passenger = 0.0005 * int(cd.get("UnNamedSumInsured", 0)) * 5 \
        if cd.get("IsPACoverUnnamedPerson") else 0
    driver = 0.0005 * int(cd.get("PaidDriverSumInsured", 0)) if cd.get("IsPACoverPaidDriver") else 0
    ll = 50.0 if cd.get("IsLegalLiablityPaidDriver") else 0
    theft = round(od * 0.025, 2) if dd.get("IsAntiTheftDevice") else 0
    aai = 50.0 if dd.get("IsMemberOfAutomobileAssociation") else 0
    tppd = 100.0 if dd.get("IsTPPDRestrictedto6000") else 0
    # Owner-driver PA: individuals only (PCNationalInsuranceRules.cs:844-858).
    cpa = 0.0 if body.get("IsExistingPACover", True) or \
        body.get("CustomerType") == "Organization" else 375.0
    fibre = 20.0 if cd.get("IsFiberGlassFuelTank") else 0
    # Voluntary excess: accepted and ignored, like NATIONAL.
    net = round(od - ncb_rs - theft - aai + elec + nonel + cng + fibre + addon
                + tp + passenger + driver + ll + cpa - tppd, 2)
    gst = round(net * 0.18, 2)
    return {"CompanyCode": "NATIONAL", "Status": "Success", "PlanId": 367,
            "PremiumYear": 1, "FinalPremium": round(net + gst),
            "InsuredDeclaredValue": idv, "MinInsuredDeclaredValue": int(low),
            "MaxInsuredDeclaredValue": int(high), "ApplicableAddonDetails": applicable,
            "PremiumBreakUpDetails": {
                "NetPremium": net, "ServiceTax": gst, "NetODPremium": od,
                "NetTPPremium": tp + passenger + driver + ll + cpa, "NetAddonPremium": addon,
                "CurrentNCB": new_ncb, "NCBDiscount": ncb_rs,
                "ElecAccessoriesPremium": elec, "NonElecAccessoriesPremium": nonel,
                "CNGLPGKitPremium": 60.0 if cd.get("IsBiFuelKit") else 0,
                "PACoverToUnNamedPerson": passenger, "PAToPaidDriver": driver,
                "LLToPaidDriver": ll, "AntiTheftDiscount": theft, "AAIDiscount": aai,
                "RestrictLiability": tppd, "PACoverToOwnDriver": cpa,
                "FiberGlassTankPremium": fibre, "VoluntaryDiscount": 0,
                "BasicThirdPartyLiability": tp}}


class FakeApi(BaseHTTPRequestHandler):
    quotes = 0

    def log_message(self, *args):
        pass

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("mob-token") != TOKEN:
            return self._send({"StatusCode": 401})
        if self.path.lower().endswith("/addon"):
            return self._send({"Response": ADDONS})
        self._send({})

    def do_POST(self):
        if self.headers.get("mob-token") != TOKEN:
            return self._send({"StatusCode": 401})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.lower().endswith("/qualifiedcompany"):
            FakeApi.quotes += 1
            variant = body["VehicleDetails"].get("VariantCode")
            if variant == UNMAPPED_VARIANT:
                return self._send({"Response": {
                    "QuotationNumber": f"PIBLMTRPC{FakeApi.quotes:06d}",
                    "QualifiedPlanList": [],
                    "APIDeclineDetails": [{"CompanyCode": "NATIONAL",
                                           "ErrorMessage": "MMV is not mapped."}]}})
            return self._send({"Response": {
                "QuotationNumber": body.get("QuotationNumber") or
                f"PIBLMTRPC{FakeApi.quotes:06d}", "Uid": "u", "SubProductCode": "MTRPC",
                "QualifiedPlanList": [{"CompanyCode": "NATIONAL", "PlanId": 367,
                                       "CompanyName": "National Insurance"}]}})
        assert body.get("CompanyCode") == "NATIONAL" and body.get("ProductCode") == "Motor"
        self._send({"Response": [price(body)]})


def start_fake() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/api/Motor/privatecar/"


CARS = [
    {"make": "MARUTI", "model": "SWIFT", "variant": "VXI (PETROL)", "variant_name": "VXI",
     "fuel": "PETROL", "band": "1000-1500cc", "top": True, "make_id": 1, "model_id": 10,
     "variant_id": 9001, "cc": 1197},
    {"make": "HYUNDAI", "model": "CRETA", "variant": "SX (DIESEL)", "variant_name": "SX",
     "fuel": "DIESEL", "band": "over 1500cc", "top": True, "make_id": 2, "model_id": 20,
     "variant_id": 9002, "cc": 1582},
    {"make": "TATA", "model": "NEXON EV", "variant": "XZ (ELECTRIC)", "variant_name": "XZ",
     "fuel": "ELECTRIC", "band": "electric", "top": True, "make_id": 3, "model_id": 30,
     "variant_id": UNMAPPED_VARIANT, "cc": None},
    {"make": "RENAULT", "model": "KWID", "variant": "RXT (PETROL)", "variant_name": "RXT",
     "fuel": "PETROL", "band": "up to 1000cc", "top": False, "make_id": 4, "model_id": 40,
     "variant_id": 9004, "cc": 999},
]
INSURERS = [{"name": "BAJAJ ALLIANZ GENERAL INSURANCE CO. LTD.", "code": "BAJAJ",
             "id": 5, "short": "BAJAJ"},
            {"name": "NATIONAL INSURANCE CO. LTD.", "code": "NATIONAL", "id": 11,
             "short": "NIC"}]


def session(api_base: str) -> dict:
    """What open_session() hands the lab after a real capture journey."""
    qualify = {
        "CustomerType": "Individual", "OrganizationName": "", "IsOwnerChanged": False,
        "NewBusinessPolicyType": "0", "IsValidLicence": True, "IsExistingPACover": True,
        "PolicyType": "Renewal", "PrevPolicyExpiryStatus": "1", "BrokerId": "PIBL",
        "VehicleDetails": {"VariantCode": 9001, "BimaPostRTOId": 22,
                           "RegistrationNumber": "GJ-01-AB-1111"},
        "PreviousPolicyDetails": {"IsPreviousInsuranceClaimed": False,
                                  "PreviousNcbPercentage": "35", "InsurerCode": 5,
                                  "PolicyNumber": "123456789", "PreviousPolicyType": "1"},
        "PrevPolicyInsurer": {"CompanyCode": "BAJAJ", "Id": 5, "Name": INSURERS[0]["name"]},
        "CoverageDetails": {}, "DiscountDetails": {}, "RequestedAddOnList": [],
        "DontKnowPreviousInsurer": False, "IsBreakingCase": False,
        "MakeName": "MARUTI", "ModelName": "SWIFT", "VariantName": "VXI - PETROL",
        "PCQuotation": {"MakeCode": 1, "ModelCode": 10, "VariantCode": 9001,
                        "RegistrationYear": 2022},
        "CompanyIdvDetails": [], "IsThirdPartyOnly": False, "IsODOnly": False,
        "DeviceId": "Web", "LoginUserId": 1, "IsNewUIJourney": True,
        "PreviousPolicyDetailsRequired": True}
    t = Template("car", api_base, qualify, plan_added={"ProductCode": "Motor",
                                                       "MotorQuoteReqId": 0},
                 plan_removed=["PCQuotation", "PrevPolicyInsurer", "MakeName",
                               "ModelName", "VariantName"],
                 captured="rehearsal", label="MARUTI SWIFT VXI / GJ-01")
    return {"template": t, "headers": {"mob-token": TOKEN},
            "catalog": {"vehicles": CARS, "insurers": INSURERS},
            "vehicle": CARS[0], "insurers": INSURERS}


def run_session(api_base: str, folder: Path, **overrides):
    labrules.LAB_DIR = folder
    args = SimpleNamespace(max=200, workers=3, vehicles=3, vehicle="", rto="GJ-01 Ahmedabad",
                           no_combos=False, recheck=False, refresh_template=False,
                           headless=True, **overrides)
    book = labrules.Notebook("NATIONAL", "car", folder=folder)
    lab = lab_runner.Lab(SimpleNamespace(name="rehearsal", base_url="", api_url=""),
                         ls.PRODUCTS["car"], "NATIONAL", args, book, session(api_base))
    code = lab.run()
    return lab, code


def main() -> int:
    console.use_utf8()
    server, api_base = start_fake()
    folder = Path(tempfile.mkdtemp(prefix="insurer-lab-rehearsal-"))
    print("SESSION 1 - the lab meets the fake NATIONAL for the first time\n")
    run_session(api_base, folder)
    print("\nSESSION 2 - same insurer, next day: it should obey what it learned\n")
    lab, _ = run_session(api_base, folder)
    server.shutdown()
    return 0


# ================================================================== the proof

def test_first_session_finds_every_planted_rule_and_fault(tmp_path):
    server, api_base = start_fake()
    try:
        lab, code = run_session(api_base, tmp_path)
    finally:
        server.shutdown()
    rules = {r.sentence(): r for r in lab.book.rules()}
    text = " || ".join(rules)
    assert "electrical at most 10,000" in text, text
    elec = next(r for r in rules.values() if r.dimension == "electrical")
    assert elec.confirmed                                  # 10,000 ok, 10,001 not
    nonel = next(r for r in rules.values() if r.dimension == "non_electrical")
    assert 10000 <= nonel.high <= 15000, nonel             # found without a number
    assert "refuses owner changed = True" in text
    zd = next(r for r in rules.values() if r.dimension == "addon" and r.value == "2")
    assert zd.kind == "silent" and zd.when == {"age_min": 7}
    rti = next(r for r in rules.values() if r.dimension == "addon" and r.value == "8")
    assert rti.kind == "silent" and rti.when == {"age_min": 4}
    unmapped = next(r for r in rules.values() if r.dimension == "vehicle")
    assert unmapped.source == "our portal"

    checks = {f.check for o in lab.outcomes for f in o.findings}
    assert "cover-cheaper" in checks                       # the planted CNG bug
    assert "discount-ignored" in checks                    # voluntary excess
    assert "idv-clamped" in checks
    assert code == 1                                       # a DEFECT was found


def test_second_session_obeys_what_it_learned(tmp_path):
    server, api_base = start_fake()
    try:
        run_session(api_base, tmp_path)
        before = api_calls = None
        lab, _ = run_session(api_base, tmp_path)
    finally:
        server.shutdown()
    asked = {o.scenario.key for o in lab.outcomes}
    assert "electrical=25000" not in asked and "electrical=100000" not in asked
    assert "electrical=10000" in asked                     # exactly on the line
    assert "owner_changed=True" not in asked
    assert "addon=2 & year=10" not in asked                # Zero Dep on an old car
    assert "addon=2 & year=3" in asked                     # still tested where it works
    assert not any(o.scenario.dimension == "vehicle" and o.scenario.value.startswith(
        "TATA|NEXON EV") for o in lab.outcomes)
    skipped = {s.key for s, _ in lab.skipped}
    assert "owner_changed=True" in skipped and "electrical=50000" in skipped
    refused = [o for o in lab.outcomes if o.status in ("refused", "not-asked")]
    assert len(refused) <= 3, [o.scenario.key for o in refused]


if __name__ == "__main__":
    sys.exit(main())
