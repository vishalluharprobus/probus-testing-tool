"""
Prove the quote sweep's fast mode (core/fastsweep.py) - offline, no server.

    venv\\Scripts\\python -m pytest tests/test_fast_sweep.py -q

Fast mode replaces ~3 minutes of clicking per journey with two kinds of API
call. That is only worth having if it is the SAME test: the request carries
the journey's choices, only the chosen companies are asked, and the answers
are read exactly as the browser mode reads them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import fastsweep, quotechecks  # noqa: E402
from core.labclient import ApiRefused, Template  # noqa: E402
from data import matrix  # noqa: E402
from pages.vehicle_details import Vehicle  # noqa: E402

API = "https://api.example/api/Motor/TwoWheeler/"
ACTIVA = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA", "3G (110 CC) (PETROL)", "2022")
LABEL = "HONDA ACTIVA 3G (110 CC) (PETROL)"
CATALOG = {
    "vehicles": [{"make": "HONDA", "model": "ACTIVA", "variant": "3G (110 CC) (PETROL)",
                  "variant_id": 77, "make_id": "9", "model_id": "41", "fuel": "PETROL",
                  "band": "up to 150cc", "cc": 110.0}],
    "insurers": [{"code": "ICICI", "id": "5", "name": "ICICI LOMBARD GENERAL INSURANCE"},
                 {"code": "BAJAJ", "id": "1", "name": "BAJAJ ALLIANZ GENERAL INSURANCE"}],
}
TEMPLATE = {
    "VehicleDetails": {"RegistrationNumber": "GJ-01-AB-1111", "BimaPostRTOId": "259",
                       "VariantCode": 1, "MakeCode": "1", "ModelCode": "1"},
    "TWQuotation": {"BPRtoId": 259, "RTOCityName": "GJ-01 Ahmedabad", "VariantCode": 1},
    "RTOCityName": "GJ-01 Ahmedabad", "IsThirdPartyOnly": False, "IsODOnly": False,
    "PreviousPolicyDetails": {"PolicyNumber": "123", "InsurerName": ""},
}


class FakeApi:
    """The two calls, answered like the portal's API; remembers who was asked."""

    def __init__(self, plans=("BAJAJ", "TATA", "ZUNO"), fail_401=False):
        self.plans, self.fail_401 = plans, fail_401
        self.asked: list[str] = []
        self.sent: dict | None = None

    def post(self, url, body):
        code = url.rsplit("/", 1)[-1]
        if code == "QualifiedCompany":
            self.sent = body
            return {"Response": {
                "QuotationNumber": "PIBLMTRTW9", "SubProductCode": "MTRTW",
                "QualifiedPlanList": [{"CompanyCode": c, "PlanId": i}
                                      for i, c in enumerate(self.plans, 1)],
                "APIDeclineDetails": [{"CompanyCode": "SBI", "ErrorMessage": "RTO not allowed"}]}}
        self.asked.append(code)
        if self.fail_401:
            raise ApiRefused(401, "the API says the token is not valid")
        if code == "TATA":
            return {"Response": [{"CompanyCode": "TATA", "Status": "Failed",
                                  "ErrorMessage": "COMPANY - Value cannot be null."}]}
        return {"Response": [{"CompanyCode": code, "Status": "Success",
                              "FinalPremium": 1084, "PremiumYear": 1,
                              "PremiumBreakUpDetails": {"NetPremium": 918, "ServiceTax": 166,
                                                        "NetODPremium": 204, "NetTPPremium": 714}}]}


def sweep(only=("BAJAJ", "TATA", "SBI"), api=None):
    setup = {"vehicles": {LABEL: ACTIVA}}
    s = fastsweep.FastSweep(cfg=None, product_name="bike", setup=setup, only=only)
    s.template = Template(product="bike", api_base=API, qualify=TEMPLATE)
    s.catalog = CATALOG
    s.api = api or FakeApi()
    s.rtos = {"MH-12 Pune": {"Name": "MH-12 Pune", "Id": "738"},
              "GJ-01 Ahmedabad": {"Name": "GJ-01 Ahmedabad", "Id": "259"}}
    return s


def scenario(**change):
    values = {"vehicle": LABEL, "rto": "MH-12 Pune", "year": str(matrix.this_year() - 4),
              "policy": matrix.TP, "previous": matrix.WITHIN_90, "prev_type": matrix.CP,
              "prev_insurer": "BAJAJ", "ncb": None, "claim": None}
    values.update(change)
    return matrix.Scenario(values=tuple(values[d] for d in matrix.DIMENSIONS))


def test_the_request_carries_the_journeys_choices():
    body = sweep().request_for(scenario())
    assert body["IsThirdPartyOnly"] is True and body["IsODOnly"] is False
    assert body["RTOCityName"] == "MH-12 Pune"
    assert body["VehicleDetails"]["BimaPostRTOId"] == "738"
    assert body["VehicleDetails"]["RegistrationNumber"].startswith("MH-12")
    assert body["TWQuotation"]["BPRtoId"] == 738
    assert body["VehicleDetails"]["VariantCode"] == 77          # the Activa, by id
    assert body["PrevPolicyExpiryStatus"] == "2"                 # within 90 days
    assert body["PreviousPolicyDetails"]["InsurerCode"] == "1"   # BAJAJ
    assert body["TWQuotation"]["RegistrationYear"] == matrix.this_year() - 4


def test_the_request_passes_the_same_did_it_send_what_we_chose_check():
    s = scenario(policy=matrix.CP, previous=matrix.NOT_EXPIRED, ncb="25%", claim="No")
    body = sweep().request_for(s)
    assert quotechecks._request_checks((1,), s, body) == []


def test_only_the_chosen_companies_are_asked_and_read_like_the_browser():
    api = FakeApi()
    r = sweep(api=api).run(scenario())
    assert sorted(api.asked) == ["BAJAJ", "TATA"]               # ZUNO never called
    assert r.status == "ok" and list(r.offers) == ["BAJAJ"]
    assert r.offers["BAJAJ"].premium == 1084 and r.offers["BAJAJ"].tp == 714
    why = {d.insurer: d for d in r.declines}
    assert why["TATA"].reason == "Value cannot be null."
    assert why["SBI"].source == "probus-rule"
    assert r.quotation == "PIBLMTRTW9" and "ZUNO" in r.qualified
    assert r.sent["IsThirdPartyOnly"] is True


def test_an_expired_login_stops_the_run():
    with pytest.raises(fastsweep.TokenExpired):
        sweep(api=FakeApi(fail_401=True)).run(scenario())


def test_an_unknown_vehicle_or_rto_is_a_sentence_not_a_crash():
    assert "not in the portal's RTO list" in sweep().run(scenario(rto="ZZ-99 Nowhere")).note
    assert "vehicle list" in sweep().run(scenario(vehicle="NOPE")).note


def test_no_screen_means_no_screen_checks():
    s = scenario()
    r = sweep().run(s)

    def checks(cards):
        return {f.check for f in quotechecks.check_journey(
            1, s, r.offers, r.answers, r.declines, r.sent, cards, {}, None)}
    assert "no-card" in checks([])        # a screen with no card: a real bug
    assert "no-card" not in checks(None)  # no screen at all: nothing to say
