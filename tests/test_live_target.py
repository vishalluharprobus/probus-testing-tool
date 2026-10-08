"""
Prove the LIVE target can quote and can do nothing else - offline.

    venv\\Scripts\\python -m pytest tests/test_live_target.py -q

Written 2026-10-07, when the quote matrix was pointed at the real site
(buy.probusinsurance.com) for five insurers. Each test is one promise:
quotes only whatever the settings say, only on the live host, only the
insurers named, and its own notebook.

Nothing here opens the real site or reads the real settings.local.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from core import quotenotes, safety  # noqa: E402
from core.quotecapture import QuoteCapture  # noqa: E402

GREEDY = {"username": "u", "password": "p", "write_ceiling": "payment",
          "live_write_ceiling": "payment", "testsite_write_ceiling": "payment"}
TOP5 = ("BAJAJ", "TATA", "DIGIT", "SBI", "KOTAK")


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "_load_local_secrets", lambda: dict(GREEDY))
    return settings.load("live")


class FakePage:
    def __init__(self, url):
        self.url = url

    def content(self):
        return ""

    def evaluate(self, _script):
        return []


# ----------------------------------------------------------- quotes only

def test_live_points_at_the_real_journey(live):
    assert live.base_url == "https://buy.probusinsurance.com/motor-journey"
    assert live.live and live.manual_login and not live.is_local


def test_no_settings_key_raises_the_live_ceiling(live):
    assert live.write_ceiling == "quote"


def test_live_allows_quotes_and_nothing_more(live):
    safety.allow("read", live)
    safety.allow("quote", live)
    for step in ("proposal", "payment"):
        with pytest.raises(safety.SafetyRefusal, match="quotes only"):
            safety.allow(step, live)


def test_a_forged_live_target_is_still_refused(live):
    from dataclasses import replace
    forged = replace(live, write_ceiling="payment")
    with pytest.raises(safety.SafetyRefusal):
        safety.allow("proposal", forged)
    with pytest.raises(safety.SafetyRefusal, match="quotes only"):
        safety.verify_environment(
            FakePage("https://buy.probusinsurance.com/motor-journey/two-wheeler"), forged)


# ----------------------------------------------------------- right host

def test_live_passes_on_the_live_journey_host(live):
    safety.verify_environment(
        FakePage("https://buy.probusinsurance.com/motor-journey/two-wheeler"), live)


@pytest.mark.parametrize("url", [
    "https://www.probusinsurance.com/two-wheeler-insurance/",
    "https://test.probusinsurance.com/motor-journey/two-wheeler",
    "http://localhost:4200/two-wheeler",
])
def test_live_refused_anywhere_else(live, url):
    with pytest.raises(safety.SafetyRefusal):
        safety.verify_environment(FakePage(url), live)


# ----------------------------------------------------------- only the five

API = "https://api.probusinsurance.com/api/Motor/TwoWheeler/"


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


def test_capture_counts_only_the_named_insurers():
    c = QuoteCapture(only=frozenset({"BAJAJ", "TATA"}))
    q = FakeRequest(API + "QualifiedCompany", {})
    c._on_response(FakeResponse(q, {"Response": {
        "QuotationNumber": "PIBLMTRTW1",
        "QualifiedPlanList": [{"CompanyCode": "BAJAJ", "PlanId": "1"},
                              {"CompanyCode": "ZUNO", "PlanId": "2"},
                              {"CompanyCode": "TATA", "PlanId": "3"}],
        "APIDeclineDetails": [{"CompanyCode": "ICICI", "ErrorMessage": "no"},
                              {"CompanyCode": "TATA", "ErrorMessage": "RTO"}]}}))
    assert [code for code, _ in c.planned] == ["BAJAJ", "TATA"]
    assert [d.insurer for d in c.probus_declines] == ["TATA"]

    # A stopped call to an insurer we did not ask is not an answer or a decline.
    zuno = FakeRequest(API + "ZUNO", {"CompanyCode": "ZUNO", "PlanId": "2"})
    c._on_failed(zuno)
    assert c.answered_calls == 0

    for code, plan in (("BAJAJ", "1"), ("TATA", "3")):
        req = FakeRequest(API + code, {"CompanyCode": code, "PlanId": plan})
        c._on_response(FakeResponse(req, {"Response": [
            {"CompanyCode": code, "Status": "Success", "FinalPremium": 1500,
             "PremiumYear": 1}]}))
    assert c.complete
    assert sorted(c.best_offers()) == ["BAJAJ", "TATA"]
    assert {d.insurer for d in c.declines()} == set()


FAN_OUT_PAGE = """<!doctype html><title>x</title>
<script>
async function go() {
  const post = (u, b) => fetch(u, {method: 'POST', body: JSON.stringify(b),
                                   headers: {'Content-Type': 'application/json'}});
  const q = await (await post('/api/Motor/TwoWheeler/QualifiedCompany', {})).json();
  // Like the app: one call per plan, each on its own.
  await Promise.allSettled(q.Response.QualifiedPlanList.map(p =>
      post('/api/Motor/TwoWheeler/' + p.CompanyCode,
           {CompanyCode: p.CompanyCode, PlanId: p.PlanId})));
  document.title = 'done';
}
go();
</script>
"""


def test_other_insurers_calls_never_leave_the_browser():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        chromium = pw.chromium.launch()
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"no browser available: {exc}")
    reached = []
    try:
        context = chromium.new_context()

        def answer_route(route):
            url = route.request.url
            if url.endswith("/app"):
                return route.fulfill(content_type="text/html", body=FAN_OUT_PAGE)
            reached.append(url.rsplit("/", 1)[-1])
            if url.endswith("QualifiedCompany"):
                body = {"Response": {"QuotationNumber": "PIBLMTRTW9",
                        "QualifiedPlanList": [{"CompanyCode": c, "PlanId": n}
                                              for n, c in enumerate(
                                                  ["BAJAJ", "ZUNO", "KOTAK", "ICICI"])]}}
            else:
                code = url.rsplit("/", 1)[-1]
                body = {"Response": [{"CompanyCode": code, "Status": "Success",
                                      "FinalPremium": 1200, "PremiumYear": 1}]}
            route.fulfill(content_type="application/json", body=json.dumps(body))

        # Registered first, so the capture's blocker (registered after) sees
        # each request before this fake server does - as in a real run, where
        # nothing else routes these calls.
        context.route("http://fake.local/**", answer_route)
        capture = QuoteCapture.attach(context, "twowheeler", TOP5)
        page = context.new_page()
        page.goto("http://fake.local/app")
        page.wait_for_function("document.title === 'done'", timeout=15_000)
        assert capture.wait_until_complete(page, timeout_ms=5_000)
        assert sorted(capture.best_offers()) == ["BAJAJ", "KOTAK"]
        assert "ZUNO" not in reached and "ICICI" not in reached
        assert {"BAJAJ", "KOTAK"} <= set(reached)
    finally:
        chromium.close()
        pw.stop()


# ----------------------------------------------------------- own notebook

def test_live_learns_into_its_own_notebook():
    before = quotenotes.NOTES_FILE
    try:
        quotenotes.use("bike", "live")
        assert quotenotes.NOTES_FILE.name == "quote_matrix_live.json"
        quotenotes.use("car", "live")
        assert quotenotes.NOTES_FILE.name == "quote_matrix_car_live.json"
        quotenotes.use("bike", "testsite")
        assert quotenotes.NOTES_FILE.name == "quote_matrix.json"
    finally:
        quotenotes.NOTES_FILE = before


# ----------------------------------------------- lessons of the first live run

def test_previous_insurer_starts_neutral_then_each_asked_one():
    from core import vehiclecatalog
    catalog = {"insurers": [
        {"code": "BAJAJ", "name": "BAJAJ ALLIANZ GENERAL INSURANCE CO. LTD."},
        {"code": "ICICI", "name": "ICICI LOMBARD GENERAL INSURANCE COMPANY LIMITED"},
        {"code": "DIGIT", "name": "GO DIGIT GENERAL INSURANCE LIMITED"},
        {"code": "KOTAK", "name": "ZURICH KOTAK GENERAL INSURANCE COMPANY (INDIA) LIMITED"}]}
    prev = vehiclecatalog.previous_insurers(catalog, ("BAJAJ", "DIGIT", "KOTAK"))
    assert [code for _, _, code in prev] == ["ICICI", "BAJAJ", "DIGIT", "KOTAK"]
    assert dict((code, search) for _, search, code in prev)["KOTAK"] == "KOTAK"
    # Without --insurers nothing changes.
    assert [c for _, _, c in vehiclecatalog.previous_insurers(catalog)] == ["BAJAJ", "ICICI"]


def test_live_bajaj_wording_counts_as_same_insurer():
    from data import matrix
    assert matrix.says_same_insurer("Same company Renewal is not allowed.")
    assert matrix.says_same_insurer("Policy can not issue with same insurer")
    assert not matrix.says_same_insurer("Kindly enter Vehicle Registration number")


def test_bajaj_uneven_renewal_rule_is_caught():
    """The first live run: BAJAJ refused Comprehensive as a same-company
    renewal but priced Third Party, previous insurer BAJAJ both times."""
    from core import quotechecks
    from core.quotecapture import Decline
    from data import matrix
    def scenario(policy):
        v = dict(zip(matrix.DIMENSIONS, [None] * len(matrix.DIMENSIONS)))
        v.update(policy=policy, prev_insurer="BAJAJ")
        return matrix.Scenario(values=tuple(v[d] for d in matrix.DIMENSIONS))
    refused = [Decline("BAJAJ", "Same company Renewal is not allowed.", "insurer")]
    found = quotechecks._same_insurer_uneven([
        (1, scenario(matrix.CP), {}, refused),
        (2, scenario(matrix.TP), {"BAJAJ": object()}, [])])
    assert [f.insurer for f in found] == ["BAJAJ"]


def test_an_asked_insurer_left_off_the_list_is_reported():
    from core.quotecapture import NOT_LISTED
    c = QuoteCapture(only=frozenset({"DIGIT", "SBI"}))
    q = FakeRequest(API + "QualifiedCompany", {})
    c._on_response(FakeResponse(q, {"Response": {
        "QuotationNumber": "Q1",
        "QualifiedPlanList": [{"CompanyCode": "DIGIT", "PlanId": "1"},
                              {"CompanyCode": "ZUNO", "PlanId": "2"}]}}))
    assert c.qualified_all == ["DIGIT", "ZUNO"]
    why = {d.insurer: d for d in c.declines()}
    assert why["SBI"].reason == NOT_LISTED
    assert "DIGIT" in why                    # asked and not answered yet


def test_never_listed_names_the_codes_the_portal_used():
    import run_quote_matrix as runner
    j = runner.Journey(1, None, qualified=["DIGIT", "SBIGEN"])
    found = runner._never_listed([j], ("DIGIT", "SBI"))
    assert [f.insurer for f in found] == ["SBI"]
    assert "SBIGEN" in found[0].detail


@pytest.mark.parametrize("depth, word", [(1, "value"), (2, "pair"), (3, "triple")])
def test_every_depth_plans_to_full_coverage(depth, word):
    from data import matrix
    opts = matrix.options(["A", "B"], ["GJ-01 Ahmedabad", "MH-01 Mumbai"],
                          ["ICICI", "BAJAJ"])
    journeys, facts = matrix.plan(opts, budget=100_000, depth=depth)
    assert facts["this_run"] == facts["wanted"] > 0
    assert any(word in s.why for s in journeys if s.kind == "pairwise")


def test_pair_key_keeps_the_notebook_format():
    from data import matrix
    unit = (("rto", "GJ-01"), ("policy", "Third Party"))
    assert matrix.pair_key(unit) == "rto=GJ-01 & policy=Third Party"
