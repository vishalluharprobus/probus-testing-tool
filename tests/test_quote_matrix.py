"""
Prove the quote matrix reaches the RIGHT verdicts - offline, in seconds.

    venv\\Scripts\\python -m pytest tests/test_quote_matrix.py -q

A checker that has never been shown a wrong quote cannot be trusted to find
one. So every check here is fed a quote with a KNOWN fault planted in it - GST
at 12%, own-damage premium on a Third Party policy, an NCB after a claim - and
must name that fault. It is also fed clean quotes and must stay silent, because
a checker that cries wolf is ignored within a week.

Nothing here touches the portal, an insurer, or reports/quote_matrix.json.
The two browser tests use a local page only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import matrixreport, quotechecks, quotenotes, vehiclecatalog  # noqa: E402
from core.quotecapture import Decline, PlanAnswer, QuoteCapture  # noqa: E402
from data import matrix  # noqa: E402
from data.matrix import CP, OD, OVER_90, TP  # noqa: E402
from pages.quote_list import Quote  # noqa: E402

YEAR = matrix.this_year()
BIKE = "HONDA ACTIVA 3G (110 CC) (PETROL)"
BIG = "ROYAL ENFIELD HIMALAYAN 411 (PETROL)"


def opts(bikes=(BIKE,), insurers=("BAJAJ", "ICICI")):
    return matrix.options(list(bikes), ["GJ-01 Ahmedabad", "MH-01 Mumbai"],
                          list(insurers))


def answer(code, premium=1180.0, od=500.0, tp=500.0, idv=40000.0, ncb=45.0,
           ncb_rs=-150.0, pa=0.0, **kw):
    net = round(premium / 1.18, 2)
    base = dict(insurer=code, plan_id="1", ok=True, premium=premium, net=net,
                gst=round(premium - net, 2), od=od, tp=tp, pa_cover=pa, idv=idv,
                idv_min=idv * 0.9, idv_max=idv * 1.1, ncb_percent=ncb,
                ncb_discount=ncb_rs, years="1")
    base.update(kw)
    return PlanAnswer(**base)


def card(code, premium):
    return Quote(insurer=code, premium=premium, idv=None)


# ================================================================ the planner

def test_every_planned_journey_is_one_the_form_allows():
    plan, _ = matrix.plan(opts(), budget=40)
    for s in plan:
        again = matrix.normalise(dict(zip(matrix.DIMENSIONS, s.values)))
        assert again is not None, s.key
        assert tuple(again[d] for d in matrix.DIMENSIONS) == s.values, s.key


def test_form_rules():
    def ok(**kw):
        base = {"vehicle": BIKE, "rto": "GJ-01 Ahmedabad", "year": str(YEAR - 4),
                "policy": CP, "previous": matrix.NOT_EXPIRED, "prev_type": CP,
                "prev_insurer": "BAJAJ", "ncb": "default", "claim": "No"}
        base.update(kw)
        return matrix.normalise(base)
    assert ok(policy=OD, year=str(YEAR - 5)) is None         # OD: up to 4 years
    assert ok(policy=OD, year=str(YEAR - 4)) is not None
    assert ok(year=str(YEAR - 26)) is None                   # CP: up to 25 years
    assert ok(policy=TP)["ncb"] is None and ok(policy=TP)["claim"] is None
    assert ok(ncb="50%") is None                             # 4-year bike: max 35%
    assert ok(claim="Yes")["ncb"] is None                    # a claim kills NCB
    assert ok(previous=OVER_90)["prev_insurer"] is None
    assert ok(prev_type=TP)["ncb"] is None                   # previous TP: no NCB
    assert ok(policy=OD, prev_type=TP) is None               # OD: prev CP or OD
    assert ok(policy=CP, prev_type=OD) is None
    assert ok(ncb="35%")["ncb"] == "default"                 # same thing, folded


def test_baseline_first_then_twins_that_change_exactly_one_thing():
    plan, _ = matrix.plan(opts(), budget=12, max_twins=7)
    assert plan[0].kind == "baseline"
    twins = [s for s in plan if s.kind == "twin"]
    assert {t.relation for t in twins} >= {"cp-vs-tp", "cp-vs-od", "ncb",
                                          "age-idv", "claim", "expired"}
    for t in twins:
        assert quotechecks._only_difference(plan[0], t) is not None, t.relation


def test_a_big_enough_budget_covers_every_pair():
    o = opts()
    plan, facts = matrix.plan(o, budget=200)
    assert facts["this_run"] == facts["wanted"]
    assert len(plan) < 60          # tens of journeys, not tens of thousands


def test_the_next_run_goes_after_pairs_never_tested():
    o = opts()
    first, _ = matrix.plan(o, budget=10, max_twins=0)
    history = {}
    for s in first:
        for unit in s.pairs():
            history[matrix.pair_key(unit)] = 1
    second, facts = matrix.plan(o, budget=10, max_twins=0, history_pairs=history)
    left = facts["wanted"] - len(history)
    new_in_second = {matrix.pair_key(u) for s in second for u in s.pairs()} \
        - set(history)
    # Without the notebook the second plan would be the first one again and
    # test nothing new. With it, most of what is left is picked up.
    assert [s.values for s in second] != [s.values for s in first]
    assert len(new_in_second) >= left / 2
    assert facts["with_this_run"] > facts["ever_before"]


def test_retests_come_first():
    o = opts()
    odd = matrix.plan(o, budget=30)[0][-1]
    plan, _ = matrix.plan(o, budget=5, retests=[odd])
    assert plan[0].kind == "retest" and plan[0].values == odd.values
    assert plan[1].kind == "baseline"


def test_choices_reach_the_page_objects():
    vehicles = {BIKE: vehiclecatalog.PROVEN}
    insurers = {"BAJAJ": ("BAJAJ ALLIANZ GENERAL INSURANCE CO. LTD.", "BAJAJ")}
    base = matrix.baseline(opts(insurers=("BAJAJ",)))
    od = base.with_(policy=OD)
    vehicle, policy, extra = matrix.choices(od, vehicles, insurers)
    assert vehicle.registration_year == str(YEAR - 4)
    assert policy.policy_type == OD and policy.previous_policy_type == CP
    assert extra.tp_insurer.startswith("BAJAJ")
    lapsed = base.with_(previous=OVER_90, prev_type=None, prev_insurer=None,
                        ncb=None, claim=None)
    _, policy, extra = matrix.choices(lapsed, vehicles, insurers)
    assert policy.remembers_previous and policy.previous_expiry_status == OVER_90
    assert extra.ncb_percent is None


# ============================================================ reading the wire

class FakeRequest:
    def __init__(self, url, body, method="POST"):
        self.url, self.post_data, self.method = url, json.dumps(body), method
        self.failure = "net::ERR_FAILED"


class FakeResponse:
    def __init__(self, request, body, status=200):
        self.request, self.url, self.status = request, request.url, status
        self._body = json.dumps(body)

    def text(self):
        return self._body


API = "http://localhost:53339/api/Motor/TwoWheeler/"


def feed(capture, code, items, status=200):
    req = FakeRequest(API + code, {"CompanyCode": code, "PlanId": "7"})
    capture._on_request(req)
    capture._on_response(FakeResponse(req, {"Response": items}, status))


def test_capture_reads_the_fan_out():
    c = QuoteCapture()
    qreq = FakeRequest(API + "QualifiedCompany", {"IsThirdPartyOnly": False})
    c._on_response(FakeResponse(qreq, {"Response": {
        "QuotationNumber": "PIBLMTRTW1",
        "QualifiedPlanList": [{"CompanyCode": "ICICI", "PlanId": "7"},
                              {"CompanyCode": "TATA", "PlanId": "7"},
                              {"CompanyCode": "ZUNO", "PlanId": "7"},
                              {"CompanyCode": "DIGIT", "PlanId": "7"}],
        "APIDeclineDetails": [{"CompanyCode": "SBI", "ErrorMessage": "RTO not allowed"}],
    }}))
    assert c.started and not c.complete
    feed(c, "ICICI", [
        {"CompanyCode": "ICICI", "Status": "Success", "FinalPremium": "1,416",
         "PremiumYear": 2, "PremiumBreakUpDetails": {"NetPremium": 1200}},
        {"CompanyCode": "ICICI", "Status": "Success", "FinalPremium": 1180,
         "PremiumYear": 1, "InsuredDeclaredValue": 40000,
         "PremiumBreakUpDetails": {"NetPremium": 1000, "ServiceTax": 180,
                                   "NetODPremium": 500, "NetTPPremium": 500,
                                   "CurrentNCB": 35}}])
    feed(c, "TATA", [{"CompanyCode": "TATA", "Status": "Failed",
                      "ErrorMessage": "COMPANY - TATA's server is down."}])
    feed(c, "ZUNO", [], status=500)
    assert not c.complete                     # DIGIT still out
    assert c.silent() == ["DIGIT"]
    feed(c, "DIGIT", [])
    assert c.complete

    offers = c.best_offers()
    assert list(offers) == ["ICICI"]
    assert offers["ICICI"].premium == 1180 and offers["ICICI"].years == "1"
    assert offers["ICICI"].tp == 500 and offers["ICICI"].ncb_percent == 35
    why = {d.insurer: d for d in c.declines()}
    assert why["SBI"].source == "probus-rule"
    assert why["TATA"].reason == "TATA's server is down."
    assert why["ZUNO"].source == "http"
    assert "empty answer" in why["DIGIT"].reason


def test_other_calls_under_the_same_prefix_are_not_answers():
    c = QuoteCapture()
    req = FakeRequest(API + "AddOn", {"Foo": 1})
    assert not c._is_plan_call(req)
    req = FakeRequest(API + "ICICI", {"Foo": 1})           # no PlanId in the body
    assert not c._is_plan_call(req)
    assert c._is_plan_call(FakeRequest(API + "ICICI",
                                       {"PlanId": 1, "CompanyCode": "ICICI"}))


# ================================================================ the checks

def base_scenario(**change):
    return matrix.baseline(opts()).with_(**change)


def run_checks(s, offers, cards=None, declines=(), sent=None, answers=None):
    answers = list(offers.values()) if answers is None else answers
    if cards is None:
        cards = [card(code, a.premium) for code, a in offers.items()]
    return quotechecks.check_journey(1, s, offers, answers, list(declines),
                                     sent or {}, cards, {"BAJAJ": "BAJAJ",
                                                         "ICICI": "ICICI"})


def checks_of(findings):
    return {(f.check, f.insurer) for f in findings}


def test_clean_quotes_raise_nothing():
    offers = {c: answer(c, premium=p) for c, p in
              (("ICICI", 1180.0), ("TATA", 1298.0), ("ZUNO", 1239.0))}
    assert run_checks(base_scenario(), offers) == []


def test_planted_faults_are_named():
    s = base_scenario()
    bad_gst = answer("ICICI")
    bad_gst.gst = bad_gst.net * 0.12
    bad_gst.premium = bad_gst.net + bad_gst.gst
    found = checks_of(run_checks(s, {"ICICI": bad_gst}))
    assert ("gst", "ICICI") in found

    tp_with_od = answer("TATA", od=300)
    assert ("tp-has-od", "TATA") in checks_of(run_checks(base_scenario(policy=TP),
                                                         {"TATA": tp_with_od}))
    od_with_tp = answer("ZUNO", tp=450, pa=0)
    assert ("od-has-tp", "ZUNO") in checks_of(run_checks(base_scenario(policy=OD),
                                                         {"ZUNO": od_with_tp}))
    after_claim = base_scenario(claim="Yes", ncb=None)
    assert ("ncb-unearned", "ICICI") in checks_of(run_checks(after_claim,
                                                             {"ICICI": answer("ICICI")}))
    outside = answer("ICICI", idv=60000)
    outside.idv_max = 50000
    assert ("idv-range", "ICICI") in checks_of(run_checks(s, {"ICICI": outside}))


def test_what_the_customer_sees_must_match_what_the_insurer_sent():
    offers = {"ICICI": answer("ICICI"), "TATA": answer("TATA", premium=1298.0)}
    found = checks_of(run_checks(base_scenario(), offers,
                                 cards=[card("ICICI", 999)]))
    assert ("card-price", "ICICI") in found
    assert ("no-card", "TATA") in found


def test_same_insurer_rule_for_the_wrong_insurer():
    decline = Decline("TATA", "Policy can not issue with same insurer.", "insurer")
    found = checks_of(run_checks(base_scenario(), {}, cards=[],
                                 declines=[decline]))
    assert ("same-insurer", "TATA") in found
    right = Decline("BAJAJ", "Policy can not issue with same insurer.", "insurer")
    assert run_checks(base_scenario(), {}, cards=[], declines=[right]) == []


def test_our_own_errors_are_defects_and_outages_are_not():
    ours = Decline("NATIONAL", "Value cannot be null. (Parameter 'node')", "insurer")
    down = Decline("SHRIRAM", "Shriram's server is down.", "insurer")
    found = run_checks(base_scenario(), {}, cards=[], declines=[ours, down])
    assert [(f.severity, f.insurer) for f in found] == [("DEFECT", "NATIONAL")]


def test_request_that_does_not_match_the_choice_is_flagged():
    sent = {"IsThirdPartyOnly": True, "IsODOnly": False}
    found = run_checks(base_scenario(), {"ICICI": answer("ICICI")}, sent=sent)
    assert ("request", "FORM") in checks_of(found)
    assert not quotechecks.ran_as_planned(found, 1)


# ======================================================= comparing two journeys

def compare(*journeys):
    return quotechecks.compare(list(journeys), [], {BIKE: "up to 150cc",
                                                   BIG: "over 350cc"})


def test_twin_rules_hold_on_sane_prices():
    base = base_scenario()
    found, state = compare(
        (1, base, {"ICICI": answer("ICICI", premium=1500)}),
        (2, base.with_(policy=TP, ncb=None, claim=None),
         {"ICICI": answer("ICICI", premium=800, od=0)}),
        (3, base.with_(ncb="0%"), {"ICICI": answer("ICICI", premium=1700)}),
        (4, base.with_(year=str(YEAR - 7)),
         {"ICICI": answer("ICICI", premium=1300, idv=30000)}),
        (5, base.with_(claim="Yes", ncb=None),
         {"ICICI": answer("ICICI", premium=1800)}),
    )
    assert found == []
    for relation in ("cp-vs-tp", "ncb", "age-idv", "claim"):
        assert state[relation] == "held", relation


def test_twin_rules_catch_planted_faults():
    base = base_scenario()
    found, state = compare(
        (1, base, {"ICICI": answer("ICICI", premium=700)}),
        (2, base.with_(policy=TP, ncb=None, claim=None),
         {"ICICI": answer("ICICI", premium=800, od=0)}),
        (3, base.with_(ncb="0%"), {"ICICI": answer("ICICI", premium=650)}),
        (4, base.with_(year=str(YEAR - 7)),
         {"ICICI": answer("ICICI", idv=45000)}),
    )
    assert {f.check for f in found} >= {"cp-vs-tp", "ncb", "age-idv"}
    assert state["cp-vs-tp"] == "broken"


def test_bigger_engine_must_not_get_cheaper_third_party():
    tp = base_scenario(policy=TP, ncb=None, claim=None)
    found, _ = compare(
        (1, tp, {"ICICI": answer("ICICI", premium=900, od=0, tp=714)}),
        (2, tp.with_(vehicle=BIG), {"ICICI": answer("ICICI", premium=800,
                                                     od=0, tp=600)}),
    )
    assert [f.check for f in found] == ["cc-tp"]


def test_a_journey_that_did_not_run_as_planned_is_not_compared():
    base = base_scenario()
    wrong = quotechecks.Finding("LOOK", "request", "FORM", "x", journeys=(2,))
    found, state = quotechecks.compare(
        [(1, base, {"ICICI": answer("ICICI", premium=700)}),
         (2, base.with_(policy=TP, ncb=None, claim=None),
          {"ICICI": answer("ICICI", premium=800, od=0)})], [wrong], {})
    assert found == [] and state["cp-vs-tp"] == "not checked"


# ================================================ replaying the first live run

# Real answers from the 2026-09-30 live run (journeys #1 baseline, #2 Third
# Party twin), trimmed to the fields the checks read.
LIVE_CP = {
    "LIBERTYVGI": dict(premium=1004.0, net=851.0, gst=153.0, od=452.0, tp=714.0,
                       ncb=45.0, ncb_rs=112.0, idv=26444.0),
    "RELIANCE": dict(premium=961.0, net=814.0, gst=146.52, od=172.92, tp=714.0,
                     ncb=45.0, ncb_rs=-77.81, idv=25310.0),
    "NATIONAL": dict(premium=903.0, net=765.3, gst=137.75, od=466.34, tp=714.0,
                     ncb=45.0, ncb_rs=209.85, idv=27303.0),
    "ZUNO": dict(premium=1060.0, net=898.67, gst=162.0, od=335.76, tp=714.0,
                 ncb=45.0, ncb_rs=151.09, idv=32763.0),
}
LIVE_DECLINES = [
    Decline("ICICI", "Error reading JObject from JsonReader. Path '', line 0, "
                     "position 0.", "insurer"),
    Decline("TATA", "Value cannot be null. (Parameter 'value')", "insurer"),
    Decline("BAJAJ", "Policy can not issue with same insurer.", "insurer"),
    Decline("SHRIRAM", "Shriram's server is down.", "insurer"),
    Decline("FUTURE", "Status=Error", "insurer"),
    Decline("IFFCOTOKIO", "empty answer - the page shows nothing for this "
                          "insurer", "insurer"),
]
UNITED_TP = Decline("UNITED", "Error Coming From GetCalculatedPremium Error in "
                    "Calculate: User is not authorized to underwrite Two Wheeler "
                    "as opted Registration /Chasis /Engine number (combination or "
                    "any one) belongs to PrivateCar for proposal number 1.",
                    "insurer")


def live_offers():
    out = {}
    for code, v in LIVE_CP.items():
        a = answer(code, premium=v["premium"], od=v["od"], tp=v["tp"],
                   idv=v["idv"], ncb=v["ncb"], ncb_rs=v["ncb_rs"])
        a.net, a.gst = v["net"], v["gst"]
        a.idv_min, a.idv_max = v["idv"] * 0.9, v["idv"] * 1.2
        out[code] = a
    return out


def test_live_baseline_replays_to_the_same_verdicts():
    found = run_checks(base_scenario(), live_offers(), declines=LIVE_DECLINES)
    got = {(f.severity, f.check, f.insurer) for f in found}
    assert got == {("DEFECT", "our-code", "ICICI"),      # raw exception leaked
                   ("DEFECT", "our-code", "TATA"),       # null in our request
                   ("LOOK", "no-reason", "FUTURE"),
                   ("LOOK", "vanished", "IFFCOTOKIO")}, got
    # Real sums, real NCB step (35% -> 45%), real screen: all clean.


def test_live_third_party_twin_names_the_placeholder_number():
    tp = base_scenario(policy=TP, ncb=None, claim=None)
    offers = {c: answer(c, premium=843.0, od=0.0, tp=714.0, idv=0.0, ncb=0.0,
                        ncb_rs=0.0) for c in ("BAJAJ", "NATIONAL", "ZUNO")}
    for a in offers.values():
        a.net, a.gst = 714.0, 128.52
    found = run_checks(tp, offers, declines=[UNITED_TP],
                       sent={"IsThirdPartyOnly": True,
                             "VehicleDetails": {"RegistrationNumber": "GJ-01-AB-1111"}})
    placeholder = [f for f in found if f.check == "placeholder-number"]
    assert placeholder and "GJ-01-AB-1111" in placeholder[0].title
    assert not [f for f in found if f.check == "tp-spread"]   # all Rs 714


def test_same_insurer_rule_applied_unevenly_is_noticed():
    base = base_scenario()
    tp = base.with_(policy=TP, ncb=None, claim=None)
    same = Decline("BAJAJ", "Policy can not issue with same insurer.", "insurer")
    found, _ = quotechecks.compare(
        [(1, base, {"ZUNO": answer("ZUNO", premium=1060)}, [same]),
         (2, tp, {"ZUNO": answer("ZUNO", premium=843, od=0),
                  "BAJAJ": answer("BAJAJ", premium=843, od=0)}, [])], [], {})
    assert [(f.check, f.insurer) for f in found] == [("same-insurer-uneven", "BAJAJ")]


def test_ncb_must_step_up_one_slab():
    offers = {"ZUNO": answer("ZUNO", ncb=35.0)}       # stayed at 35%, should be 45%
    found = run_checks(base_scenario(), offers)
    assert [(f.check, f.insurer) for f in found] == [("ncb-slab", "ZUNO")]
    assert quotechecks.next_ncb(0) == 20 and quotechecks.next_ncb(50) == 50


# ================================================================ the notebook

@pytest.fixture
def notebook(tmp_path, monkeypatch):
    monkeypatch.setattr(quotenotes, "NOTES_FILE", tmp_path / "quote_matrix.json")
    return tmp_path


def test_notebook_learns_a_rule_at_the_right_level(notebook):
    base = base_scenario()
    od_refusal = Decline("ICICI", "OD Only not available.", "insurer")
    for n, s in enumerate([base, base.with_(rto="MH-01 Mumbai"),
                           base.with_(policy=TP, ncb=None, claim=None)]):
        quotenotes.record_journey(s, {"ICICI": answer("ICICI")}, [], {}, f"Q{n}")
    for rto, year in (("GJ-01 Ahmedabad", YEAR - 4), ("MH-01 Mumbai", YEAR - 4),
                      ("GJ-01 Ahmedabad", YEAR - 1)):
        quotenotes.record_journey(base.with_(policy=OD, rto=rto, year=str(year)), {},
                                  [od_refusal], {"ICICI": "unknown"}, "Q")
    learned = [text for code, text in quotenotes.rules() if code == "ICICI"]
    assert any("policy = OD Only" in t for t in learned), learned
    # Two refusals are not enough - a 12-journey live run learned nonsense
    # from two. Three are.
    assert not any("RTO" in t for t in learned), learned   # not blamed on the city


def test_an_outage_never_becomes_a_rule(notebook):
    s = base_scenario(policy=OD)
    down = Decline("TATA", "TATA's server is down.", "insurer")
    quotenotes.record_journey(base_scenario(), {"TATA": answer("TATA")}, [], {}, "Q")
    for _ in range(3):
        quotenotes.record_journey(s, {}, [down], {"TATA": "insurer-down"}, "Q")
    assert quotenotes.rules("TATA") == []


def test_same_insurer_refusals_never_become_a_rule(notebook):
    same = Decline("BAJAJ", "Policy can not issue with same insurer.", "insurer")
    base = base_scenario()
    quotenotes.record_journey(base.with_(policy=TP, ncb=None, claim=None),
                              {"BAJAJ": answer("BAJAJ")}, [], {}, "Q")
    for rto in ("GJ-01 Ahmedabad", "MH-01 Mumbai", "GJ-01 Ahmedabad"):
        quotenotes.record_journey(base.with_(rto=rto), {}, [same],
                                  {"BAJAJ": "business-rule"}, "Q")
    assert quotenotes.rules("BAJAJ") == []


def test_notebook_spots_a_regression(notebook):
    s = base_scenario()
    quotenotes.record_journey(s, {"ICICI": answer("ICICI", premium=1200)}, [], {}, "Q1")
    refusal = Decline("ICICI", "Vehicle not covered.", "insurer")
    changes = quotenotes.record_journey(s, {}, [refusal], {"ICICI": "unknown"}, "Q2")
    assert any("REFUSES now" in c for c in changes)


def test_relations_rotate(notebook):
    quotenotes.record_run(3, [], {"cp-vs-tp": "held", "ncb": "not checked"}, [], {})
    ages = quotenotes.relation_age()
    assert ages == {"cp-vs-tp": 0}


# ================================================================== the bikes

def test_catalogue_is_built_from_the_apps_own_lists_and_spread_by_size():
    raw = {
        "makes": [{"Id": 1, "Name": "HONDA", "IsTop": True},
                  {"Id": 2, "Name": "ROYAL ENFIELD"}, {"Id": 3, "Name": "ATHER ENERGY"}],
        "models": [{"Id": 10, "Name": "ACTIVA", "MakeId": 1},
                   {"Id": 11, "Name": "CLASSIC 350", "MakeId": 2},
                   {"Id": 12, "Name": "HIMALAYAN", "MakeId": 2},
                   {"Id": 13, "Name": "450X", "MakeId": 3}],
        "variants": [{"VariantName": "3G (110 CC)", "FuelType": "PETROL", "ModelId": 10},
                     {"VariantName": "STD", "FuelType": "PETROL", "ModelId": 11,
                      "CubicCapacity": 349},
                     {"VariantName": "411 (411 CC)", "FuelType": "PETROL", "ModelId": 12},
                     {"VariantName": "GEN 3", "FuelType": "ELECTRIC", "ModelId": 13}],
        "insurers": [{"Name": "ICICI LOMBARD GENERAL INSURANCE CO. LTD.",
                      "CompanyCode": "ICICI"}],
    }
    cat = vehiclecatalog.build(raw, "test")
    assert {v["band"] for v in cat["vehicles"]} == set(vehiclecatalog.BANDS)
    picked = vehiclecatalog.pick(cat, 4)
    assert picked[0][0] == vehiclecatalog.PROVEN
    assert [band for _, band in picked] == list(vehiclecatalog.BANDS)
    assert picked[2][0].variant == "411 (411 CC) (PETROL)"
    prev = vehiclecatalog.previous_insurers(cat)
    assert [code for _, _, code in prev] == ["BAJAJ", "ICICI"]


def test_the_targets_own_bike_list_wins_over_the_login_sites():
    listener = vehiclecatalog.MasterListener(prefer_hosts={"localhost"})

    def arrive(host, names):
        for name, needle in vehiclecatalog.LISTS.items():
            req = FakeRequest(f"http://{host}{needle}", {}, method="GET")
            listener._on_response(FakeResponse(req, {"Response": [{"Name": names}]}))

    arrive("test.probusinsurance.com", "from the login site")
    assert not listener.complete               # not the target's own yet
    arrive("localhost", "from the target")
    assert listener.complete
    arrive("test.probusinsurance.com", "late, from the login site")
    assert listener.raw["makes"] == [{"Name": "from the target"}]


# ================================================================== the report

def test_report_files_are_written(tmp_path):
    s = base_scenario()
    j = SimpleNamespace(n=1, scenario=s, status="ok", note="", quotation="Q1",
                        offers={"ICICI": answer("ICICI")},
                        declines=[Decline("TATA", "server is down", "insurer")],
                        kinds={"TATA": "insurer-down"}, silent=[])
    f = quotechecks.Finding("DEFECT", "gst", "ICICI", "GST is wrong", journeys=(1,))
    paths = matrixreport.write(tmp_path, [j], ["ICICI", "TATA"], [f], {}, [], [],
                               {"this_run": 1, "wanted": 2, "with_this_run": 1},
                               {})
    assert "GST is wrong" in paths["html"].read_text(encoding="utf-8")
    assert "ICICI" in paths["csv"].read_text(encoding="utf-8-sig")
    assert matrixreport.cell(j, "TATA") == "D"


# ======================================================= in a real browser

def _chromium():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        return pw, pw.chromium.launch()
    except Exception as exc:                     # noqa: BLE001
        pytest.skip(f"no browser available: {exc}")


RADIOS = """
<div formcontrolname="prvPoliExpSts">
  <input type="radio" name="a" value="1" checked> <input type="radio" name="a" value="2">
  <input type="radio" name="a" value="3"></div>
<div formcontrolname="prvPolicyType">
  <input type="radio" name="b" value="1" checked> <input type="radio" name="b" value="2">
  <input type="radio" name="b" value="3"></div>
"""


def test_previous_policy_type_is_picked_inside_its_own_group():
    """The bug this fixes: 'the first radio with value 2' was the EXPIRY
    status 'Expired within 90 Days', not previous type 'Third Party'."""
    from pages.policy_details import PolicyDetailsPage
    pw, chromium = _chromium()
    try:
        page = chromium.new_page()
        page.set_content(RADIOS)
        PolicyDetailsPage(page)._radio_by_value("2", "prvPolicyType")
        assert page.locator('[formcontrolname="prvPolicyType"] [value="2"]').is_checked()
        assert page.locator('[formcontrolname="prvPoliExpSts"] [value="1"]').is_checked()
    finally:
        chromium.close()
        pw.stop()


FAN_OUT_PAGE = """
<script>
async function go() {
  const post = (u, b) => fetch(u, {method: 'POST', body: JSON.stringify(b),
                                   headers: {'Content-Type': 'application/json'}});
  const q = await (await post('/api/Motor/TwoWheeler/QualifiedCompany', {IsODOnly: false})).json();
  for (const p of q.Response.QualifiedPlanList) {
    await post('/api/Motor/TwoWheeler/' + p.CompanyCode,
               {CompanyCode: p.CompanyCode, PlanId: p.PlanId});
  }
  await post('/api/v2/Client/MailQuotation', {NoMail: true});
  document.title = 'done';
}
go();
</script>
"""


def test_capture_listens_to_a_real_browser():
    pw, chromium = _chromium()
    try:
        context = chromium.new_context()
        capture = QuoteCapture.attach(context)

        def answer_route(route):
            url = route.request.url
            if url.endswith("/app"):
                return route.fulfill(content_type="text/html", body=FAN_OUT_PAGE)
            if url.endswith("QualifiedCompany"):
                body = {"Response": {"QuotationNumber": "PIBLMTRTW9",
                        "QualifiedPlanList": [{"CompanyCode": "ICICI", "PlanId": 1},
                                              {"CompanyCode": "TATA", "PlanId": 2}]}}
            elif url.endswith("/ICICI"):
                body = {"Response": [{"CompanyCode": "ICICI", "Status": "Success",
                                      "FinalPremium": 1180, "PremiumYear": 1}]}
            elif url.endswith("/TATA"):
                body = {"Response": [{"CompanyCode": "TATA", "Status": "Fail",
                                      "ErrorMessage": "COMPANY - Vehicle not covered"}]}
            else:
                body = {}
            route.fulfill(content_type="application/json", body=json.dumps(body))

        context.route("http://fake.local/**", answer_route)
        page = context.new_page()
        page.goto("http://fake.local/app")
        assert capture.wait_until_complete(page, timeout_ms=15_000)
        assert capture.quotation_no == "PIBLMTRTW9"
        assert list(capture.best_offers()) == ["ICICI"]
        assert [d.reason for d in capture.declines()] == ["Vehicle not covered"]
    finally:
        chromium.close()
        pw.stop()
