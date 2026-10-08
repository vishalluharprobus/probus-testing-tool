"""
Prove the insurer lab learns the RIGHT rules - offline, in a second.

    venv\\Scripts\\python -m pytest tests/test_insurer_lab.py -q

The lab's promise is: "the insurer said no to 25,000 of electrical
accessories, so next time send at most 10,000". That is only worth having if
it reads messages correctly, blames the right field, and never mistakes our
own crash or an outage for an insurer's rule. Each of those is proven here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import labrules  # noqa: E402
from core.labclient import Template, learn_plan_shape, plan_body  # noqa: E402
from core.labrules import Notebook, Rule, read_limit, rule_from_refusal  # noqa: E402


# ============================================================ reading messages

@pytest.mark.parametrize("message, low, high", [
    ("Electrical accessories value should not be more than 10000", None, 10000),
    ("Electrical Accessories cannot exceed Rs. 10,000", None, 10000),
    ("Maximum electrical accessories allowed is 25000", None, 25000),
    ("Non electrical accessories must be less than 5000", None, 5000),
    ("IDV should be between 245000 and 330000", 245000, 330000),
    ("CNG kit value should not be less than 10000", 10000, None),
    ("Minimum value of bi-fuel kit is Rs 5,000", 5000, None),
])
def test_limits_are_read_from_the_message(message, low, high):
    got = read_limit(message)
    assert (got.low, got.high) == (low, high), message


def test_percent_of_idv():
    got = read_limit("Electrical accessories should not exceed 20% of IDV")
    assert got.pct_of_idv == 20 and got.high is None


def test_no_number_no_limit():
    assert not read_limit("Zero Depreciation not available for this vehicle")


@pytest.mark.parametrize("message, dim", [
    ("Non-Electrical accessories value exceeds limit", "non_electrical"),
    ("Electrical accessories should not be more than 10000", "electrical"),
    ("CNG kit amount invalid", "bifuel_kit"),
    ("Zero Dep cover is not allowed for vehicle age above 5 years", "addon"),
    ("Voluntary deductible 2500 not allowed", "voluntary_deductible"),
    ("PA cover for unnamed passenger not available", "pa_passenger"),
])
def test_the_field_a_message_is_about(message, dim):
    assert labrules.field_of(message) == dim


@pytest.mark.parametrize("message, source, kind", [
    ("Electrical accessories should not be more than 10000", "insurer",
     "company-validation"),
    ("Value cannot be null. (Parameter 'node')", "insurer", "our-defect"),
    ("Error reading JObject from JsonReader. Path '', line 0", "insurer", "our-defect"),
    ("Shriram's server is down.", "insurer", "insurer-down"),
    ("RTO not allowed", "probus-rule", "probus-rule"),
    ("Status=Error", "insurer", "unknown"),
])
def test_only_insurer_rules_count_as_company_validation(message, source, kind):
    assert labrules.kind_of(message, source) == kind


# ======================================================== turning into rules

def test_refusal_with_a_number_becomes_that_limit():
    rule = rule_from_refusal("electrical", 25000,
                             "Electrical accessories should not be more than 10000")
    assert (rule.dimension, rule.kind, rule.high) == ("electrical", "max", 10000)


def test_refusal_without_a_number_brackets_the_value():
    rule = rule_from_refusal("electrical", 25000, "Invalid electrical accessories")
    assert rule.kind == "max" and rule.high == 24999


def test_refused_choice_keeps_its_conditions():
    rule = rule_from_refusal("addon", "Zero Depreciation",
                             "Zero Dep not available", {"year": "2016"})
    assert rule.kind == "refused" and rule.when == {"year": "2016"}
    assert "refuses addon = Zero Depreciation when year = 2016" == rule.sentence()


def test_boundary_search_picks_round_numbers():
    assert labrules.nice(5000, 25000) == 15000
    assert labrules.nice(10000, 15000) == 12000 or labrules.nice(10000, 15000) == 12500
    assert 10000 < labrules.nice(10000, 10500) < 10500


# ================================================================ the notebook

@pytest.fixture
def book(tmp_path):
    return Notebook("NATIONAL", "car", folder=tmp_path)


def test_the_next_session_obeys_a_learned_limit(book, tmp_path):
    book.add_rule(Rule("electrical", "max", high=10000, message="not more than 10000"))
    book.save()
    again = Notebook("NATIONAL", "car", folder=tmp_path)
    assert again.blocks("electrical", 25000, {}) is not None
    assert again.blocks("electrical", 10000, {}) is None
    assert again.boundary_values("electrical") == [10000]


def test_a_refused_choice_only_blocks_under_its_conditions(book):
    book.add_rule(Rule("addon", "refused", value="Zero Depreciation",
                       when={"year": "2016"}))
    assert book.blocks("addon", "Zero Depreciation", {"year": "2016"})
    assert book.blocks("addon", "Zero Depreciation", {"year": "2022"}) is None


def test_percent_limits_use_the_scenarios_idv(book):
    book.add_rule(Rule("electrical", "max", pct_of_idv=20))
    assert book.blocks("electrical", 60000, {"idv": 250000})
    assert book.blocks("electrical", 40000, {"idv": 250000}) is None


def test_the_same_rule_twice_adds_evidence_not_a_duplicate(book):
    book.add_rule(Rule("electrical", "max", high=10000))
    rule, new = book.add_rule(Rule("electrical", "max", high=10000))
    assert not new and rule.evidence == 2 and len(book.rules()) == 1


def test_rules_are_rechecked_weekly(book):
    rule, _ = book.add_rule(Rule("addon", "refused", value="RSA"))
    assert not rule.due_for_recheck()
    stored = book.rules()[0]
    stored.last_checked = "2020-01-01"
    assert stored.due_for_recheck()


# ======================================================== the plan-call shape

def test_plan_body_is_built_the_way_the_app_builds_it():
    qualify = {"VehicleDetails": {"Make": "HONDA"}, "TWQuotation": {"x": 1},
               "PrevPolicyInsurer": {"Name": "BAJAJ"}, "IsODOnly": False}
    real_plan = {"VehicleDetails": {"Make": "HONDA"}, "IsODOnly": False,
                 "CompanyCode": "ICICI", "PlanId": 7, "ProductCode": "Motor",
                 "QuotationNumber": "OLD", "Uid": "old"}
    added, removed = learn_plan_shape(qualify, real_plan)
    assert set(removed) == {"TWQuotation", "PrevPolicyInsurer"}
    t = Template("bike", "http://x/api/Motor/TwoWheeler/", qualify,
                 {k: v for k, v in added.items() if k not in ("QuotationNumber", "Uid")},
                 removed)
    changed = dict(qualify, IsODOnly=True)
    body = plan_body(t, changed, {"Response": {"QuotationNumber": "NEW", "Uid": "u1"}},
                     {"CompanyCode": "NATIONAL", "PlanId": 367})
    assert body["IsODOnly"] is True                     # our change survives
    assert body["CompanyCode"] == "NATIONAL" and body["PlanId"] == 367
    assert body["QuotationNumber"] == "NEW" and body["ProductCode"] == "Motor"
    assert "TWQuotation" not in body
    json.dumps(body)                                    # still sendable
