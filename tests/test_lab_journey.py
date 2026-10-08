"""
The buy-journey stages and the Excel - without a browser.

    venv\\Scripts\\python -m pytest tests/test_lab_journey.py -q

The quotes come from the fake NATIONAL in rehearse_insurer_lab.py; the
browser journeys are scripted by FakeJourneys, so what the lab does WITH a
journey's answer - which rows go on, which quotation each opens, what lands
in each column - is checked in seconds.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import rehearse_insurer_lab as rehearsal  # noqa: E402
import run_insurer_lab as lab_runner  # noqa: E402
from core import labjourney as lj  # noqa: E402
from core import labrules  # noqa: E402
from core.labjourney import ERROR, NOT_REACHED, NOT_RUN, SUCCESS, Stage  # noqa: E402
from data import labscenarios as ls  # noqa: E402
from pages import routes  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")


# ===================================================== reading the answers

@pytest.mark.parametrize("url, stage", [
    ("http://localhost:53339/api/Motor/TwoWheeler/CompanySpecificQuotation", "company_specific"),
    ("http://localhost:53339/api/Motor/privatecar/CompanySpecificQuotation", "company_specific"),
    ("http://localhost:53339/api/Motor/TwoWheeler/Proposal", "proposal"),
    ("http://localhost:53339/api/Motor/privatecar/Proposal", "proposal"),
    ("http://localhost:50251/api/two-wheeler/Proposal", "payment"),
    ("http://localhost:50251/api/privatecar/Proposal", "payment"),
    ("http://localhost:53339/api/Motor/TwoWheeler/QualifiedCompany", ""),
    ("http://localhost:53339/api/Motor/TwoWheeler/NATIONAL", ""),
])
def test_each_answer_belongs_to_its_stage(url, stage):
    assert lj.stage_of(url) == stage


def test_company_specific_success_is_the_apps_own_test():
    ok, message, resp = lj.answer_of('{"Error":null,"Message":null,'
                                      '"Response":{"Status":"Success","FinalPremium":903}}')
    assert ok and message == "" and resp["FinalPremium"] == 903


def test_a_refusal_carries_the_insurers_own_words():
    ok, message, _ = lj.answer_of(
        '{"Error":null,"Message":null,"Response":{"Status":"Error","ErrorMessage":'
        '"No district found matching your state, city & pincode combination."}}')
    assert not ok and message.startswith("No district found")


def test_underwriting_is_accepted_like_the_app_accepts_it():
    ok, _, resp = lj.answer_of('{"Error":null,"Message":null,'
                               '"Response":{"Status":"UnderWriting","ProposalNumber":"P1"}}')
    assert ok and resp["ProposalNumber"] == "P1"


def test_a_top_level_error_is_a_refusal():
    ok, message, _ = lj.answer_of('{"Error":"Object reference not set","Response":null}')
    assert not ok and message == "Object reference not set"


def test_the_payment_link_needs_a_link():
    assert lj.payment_answer_of('{"Error":"","Message":"","Response":"https://pg/x"}')[0]
    ok, message, _ = lj.payment_answer_of('{"Error":"","Message":"","Response":""}')
    assert not ok and message == "no payment link in the answer"


# ================================================================ verdicts

def test_status_names_the_first_stage_that_failed():
    stages = {"quote": Stage(SUCCESS), "kyc": Stage(SUCCESS),
              "company_specific": Stage(ERROR, "No district found"),
              "proposal": Stage(NOT_REACHED), "payment": Stage(NOT_REACHED)}
    assert lj.verdict(stages) == ("Failure", "Company Specific: No district found")
    assert lj.verdict({"quote": Stage(SUCCESS), "kyc": Stage(NOT_RUN, "x")}) == ("Success", "")


def test_a_failure_leaves_every_later_stage_not_reached():
    j = lj.Journey()
    j.done("kyc")
    j.fail("company_specific", "why")
    assert j.stages == {"kyc": Stage(SUCCESS), "company_specific": Stage(ERROR, "why"),
                        "proposal": Stage(NOT_REACHED), "payment": Stage(NOT_REACHED)}


def test_a_quote_without_a_price_is_an_error_with_its_reason():
    assert lj.quote_stage("priced", "") == Stage(SUCCESS)
    assert lj.quote_stage("refused", "MMV is not mapped.") == Stage(ERROR, "MMV is not mapped.")


def test_the_spreadsheet_reason_keeps_the_portals_words():
    text = ("The OTP was accepted, but the insurer did not accept the proposal.\n"
            "  The portal said: Chassis number already exists")
    assert lj.short_reason(text) == ("The OTP was accepted, but the insurer did not "
                                     "accept the proposal: Chassis number already exists")


def test_the_payment_amount_is_read_off_the_page():
    billdesk = ("Internet Banking\nMerchant Name\nEdelweiss General Insurance Company "
                "Limited - UAT\nPayment Amount:  ₹ 1060.00\nBillDesk")
    assert lj.amount_in(billdesk) == 1060.0
    assert lj.amount_in("Total payable Rs. 1,23,456.50") == 123456.5
    assert lj.amount_in("nothing to see") is None


def test_a_bare_insurer_code_gets_what_is_known_about_it():
    assert "PrevInsuranceCompanyBranchId" in lj.explain("NIC-PA-Validation-B5149")
    assert lj.explain("Some new message") == ""


def test_a_limited_run_covers_each_kind_of_change_first():
    def o(n, *changes):
        return SimpleNamespace(n=n, scenario=SimpleNamespace(changes=changes))
    rows = [o(1), o(2, ("year", 1)), o(3, ("year", 2)), o(4, ("year", 3)),
            o(5, ("policy", "OD Only")), o(6, ("ncb", 0)), o(7, ("addon", "1"))]
    assert [r.n for r in lab_runner.Lab._spread(rows, 4)] == [1, 2, 5, 6]
    assert len(lab_runner.Lab._spread(rows, 99)) == len(rows)


def test_routes_work_for_both_products():
    assert routes.on("http://localhost:4200/two-wheeler/proposal-payment", "proposal-payment")
    assert routes.on("http://localhost:4200/private-car/kyc-insurance", "kyc-insurance")
    assert routes.on("https://test.probusinsurance.com/motor-journey/two-wheeler/proposal",
                     "proposal")
    assert not routes.on("https://insurer.example/proposal/kyc", "proposal")
    assert not routes.on("http://localhost:4200/two-wheeler/renewal-result", "result")


def test_the_standard_quote_is_not_called_baseline():
    label = ls.Scenario(ls.State(), ()).label()
    assert "baseline" not in label and label.startswith("standard quote (Comprehensive")


# ======================================================= the lab, end to end

class FakeJourneys:
    """Stands in for the browser. Plays `script` in order, then succeeds."""
    script: list = []
    driven: list = []

    def __init__(self, cfg, product, insurer, codes, rto, api, headed=True):
        self.api = api

    @contextmanager
    def open(self):
        yield self

    def drive(self, n, quotation, request, sub_product):
        FakeJourneys.driven.append({"n": n, "quotation": quotation,
                                    "sent": request.get("QuotationNumber"),
                                    "sub_product": sub_product})
        if FakeJourneys.script:
            return FakeJourneys.script.pop(0)
        j = lj.Journey(proposal_no=f"PROP-{n}", seconds=1.0)
        for key in lj.JOURNEY:
            j.done(key)
        return j


def _refused_at_company_specific():
    j = lj.Journey(seconds=1.0)
    j.done("kyc")
    j.fail("company_specific", "No district found matching your state, city & pincode")
    return j


def _no_payment_link():
    j = lj.Journey(proposal_no="PROP-X", seconds=1.0)
    for key in ("kyc", "company_specific", "proposal"):
        j.done(key)
    j.fail("payment", "could not get a payment link")
    return j


def run_lab(folder: Path, journeys: int, monkeypatch, ceiling: str = "payment",
            script=(), api_up=lambda url: True):
    FakeJourneys.script = list(script)
    FakeJourneys.driven = []
    # The lab asks whether OUR APIs answer before each journey; here they are
    # whatever the test says, never the real ports.
    monkeypatch.setattr(lab_runner.backend, "check",
                        lambda url: lab_runner.backend.Health(api_up(url), "test"))
    monkeypatch.setattr(lab_runner.time, "sleep", lambda s: None)
    server, api_base = rehearsal.start_fake()
    labrules.LAB_DIR = folder
    args = SimpleNamespace(max=30, workers=3, vehicles=2, vehicle="", rto="GJ-01 Ahmedabad",
                           no_combos=True, recheck=False, refresh_template=False,
                           headless=True, journeys=journeys)
    cfg = SimpleNamespace(name="rehearsal", base_url="http://localhost:4200", api_url="",
                          buy_url="http://localhost:50251", write_ceiling=ceiling)
    book = labrules.Notebook("NATIONAL", "car", folder=folder)
    try:
        lab = lab_runner.Lab(cfg, ls.PRODUCTS["car"], "NATIONAL", args, book,
                             rehearsal.session(api_base))
        lab.run()
    finally:
        server.shutdown()
    return lab


def excel_rows(lab) -> list[dict]:
    ws = openpyxl.load_workbook(lab.folder / "results.xlsx")["Results"]
    heads = [c.value for c in ws[5]]
    return [dict(zip(heads, row)) for row in ws.iter_rows(min_row=6, values_only=True)]


def test_every_priced_quote_goes_on_and_lands_in_its_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(lj, "Journeys", FakeJourneys)
    lab = run_lab(tmp_path, journeys=-1, monkeypatch=monkeypatch,
                  script=[lj.Journey(seconds=1.0, stages={k: Stage(SUCCESS) for k in lj.JOURNEY}),
                          _refused_at_company_specific(), _no_payment_link()])
    priced = sorted(o.n for o in lab.outcomes if o.status == "priced")
    assert [d["n"] for d in FakeJourneys.driven] == priced
    # Two journeys must never buy the same quotation.
    quotations = [d["quotation"] for d in FakeJourneys.driven]
    assert all(quotations) and len(set(quotations)) == len(quotations)
    # ...and the saved request is that quotation's own.
    assert all(d["sent"] in (None, d["quotation"]) for d in FakeJourneys.driven)
    assert {d["sub_product"] for d in FakeJourneys.driven} == {"MTRPC"}

    rows = {r["#"]: r for r in excel_rows(lab)}
    second, third = rows[priced[1]], rows[priced[2]]
    assert [second[k] for k in ("Quote", "KYC", "Company Specific", "Proposal", "Payment")] \
        == [SUCCESS, SUCCESS, ERROR, NOT_REACHED, NOT_REACHED]
    assert second["Status"] == "Failure"
    assert second["Reason"].startswith("Company Specific: No district found")
    assert third["Proposal"] == SUCCESS and third["Payment"] == ERROR
    assert third["Proposal No."] == "PROP-X"
    assert rows[priced[3]]["Status"] == SUCCESS
    # A quote that did not price stops there.
    refused = next(r for r in rows.values() if r["Quote"] == ERROR)
    assert refused["KYC"] == NOT_REACHED and refused["Reason"].startswith("Quote: ")


def test_a_hung_server_stops_the_journeys_instead_of_blaming_the_insurer(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(lj, "Journeys", FakeJourneys)
    # The buy API answers for the quotes, then hangs before the 3rd journey.
    calls = {"n": 0}

    def api_up(url):
        if "50251" not in url:
            return True
        calls["n"] += 1
        return calls["n"] <= 2
    lab = run_lab(tmp_path, journeys=-1, monkeypatch=monkeypatch, api_up=api_up)
    assert len(FakeJourneys.driven) == 2
    assert "buy API" in lab.journey_stopped
    rows = excel_rows(lab)
    stopped = [r for r in rows if r["Quote"] == SUCCESS and r["KYC"] == NOT_RUN]
    assert stopped and all("restart it" in (r["Notes"] or "") for r in stopped)


def test_journeys_limit_and_write_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(lj, "Journeys", FakeJourneys)
    lab = run_lab(tmp_path / "two", journeys=2, monkeypatch=monkeypatch)
    assert len(FakeJourneys.driven) == 2
    rows = excel_rows(lab)
    later = [r for r in rows if r["Quote"] == SUCCESS and r["#"] not in
             {d["n"] for d in FakeJourneys.driven}]
    assert later and all(r["KYC"] == NOT_RUN for r in later)
    assert "--journeys 2" in lab.journey_why

    lab = run_lab(tmp_path / "quote", journeys=-1, monkeypatch=monkeypatch, ceiling="quote")
    assert FakeJourneys.driven == []
    assert "write_ceiling is 'quote'" in lab.journey_why
