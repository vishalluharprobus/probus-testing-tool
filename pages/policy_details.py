"""
Screen 2 - Policy Details.

Same route as screen 1 (/two-wheeler/dontknownumber) - the first three screens
are one page, so a test cannot jump straight here.

All selectors below are confirmed against the running app. This screen cascades
twice: choosing a policy type reveals the previous-policy question, and
answering Yes reveals the expiry status, the insurer box, and the previous
policy type.

One gotcha worth naming: "Comprehensive" appears TWICE on this screen once it is
fully expanded - as the required policy type (value CP) and as the previous
policy type (value 1). We disambiguate by radio group rather than by label,
because label alone silently picks the wrong one.

Values are NOT unique either, which the first version assumed. Expiry status
and previous policy type both use "1", "2" and "3" (Saarthi
tw-dont-know-number.component.html:300 and :378), so "the first radio with
value 2" meant "Expired within 90 Days" even when we wanted previous policy
"Third Party". Every radio is therefore picked inside its own
formcontrolname group.

What the form offers depends on the vehicle's age (component.ts:1198-1212):
OD Only for bikes up to 4 years old, Comprehensive up to 25 years, Third Party
always. "Expired more than 90 Days" hides the previous insurer and type.
Private cars (/private-car/dontknownumber, same controls): Comprehensive only up
to 14 years, OD Only up to 4 (pc-dont-know-number.component.ts:929-943).
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from core import ui

POLICY_TYPES = {"Comprehensive": "CP", "Third Party": "TP", "OD Only": "OD"}
EXPIRY_STATUSES = {"Not Expired": "1",
                   "Expired within 90 Days": "2",
                   "Expired more than 90 Days": "3"}
# "OD Only" is offered as a previous type only when the NEW policy is OD Only.
PREVIOUS_POLICY_TYPES = {"Comprehensive": "1", "Third Party": "2", "OD Only": "3"}

# The radio groups, by formcontrolname (the app's own spelling).
POLICY_GROUP = "reqPolicyType"
REMEMBER_GROUP = "remeberPrePolicy"
EXPIRY_GROUP = "prvPoliExpSts"
PREVIOUS_TYPE_GROUP = "prvPolicyType"


@dataclass
class PolicyChoice:
    policy_type: str = "Comprehensive"
    remembers_previous: bool = True
    previous_expiry_status: str = "Not Expired"
    previous_insurer: str = "BAJAJ ALLIANZ GENERAL INSURANCE CO. LTD."
    previous_insurer_search: str = "BAJAJ"
    previous_policy_type: str = "Comprehensive"


class PolicyDetailsPage:
    PREV_INSURER = "prevInsurer"

    def __init__(self, page: Page):
        self.page = page

    # The marker that this screen is up. "Third Party" because it is the one
    # policy type ALWAYS offered: a car over 14 years old gets no Comprehensive
    # button at all, and waiting for one timed out (live, 2026-10-05).
    MARKER = "Third Party"

    def is_showing(self) -> bool:
        return self.page.get_by_role("radio", name=self.MARKER).count() > 0

    def wait_until_loaded(self, timeout_ms: int = 30_000) -> "PolicyDetailsPage":
        """
        Wait for this screen to actually appear.

        Used instead of a fixed sleep after the previous screen's Proceed. A
        guessed delay is wrong in both directions: too short and the run fails
        on a slow day, too long and every run pays for the worst case. Waiting
        for the real marker is faster AND more reliable.
        """
        self.page.get_by_role("radio", name=self.MARKER).first.wait_for(
            state="visible", timeout=timeout_ms)
        return self

    def fill(self, choice: PolicyChoice) -> "PolicyDetailsPage":
        if choice.policy_type not in POLICY_TYPES:
            raise ValueError(f"Unknown policy type {choice.policy_type!r}. "
                             f"Expected: {', '.join(POLICY_TYPES)}")

        self._radio_by_value(POLICY_TYPES[choice.policy_type], POLICY_GROUP)
        self.page.wait_for_timeout(1500)

        # Third-party-only journeys never ask about the previous policy.
        if not self._asks_about_previous_policy():
            return self

        self._radio_by_value("true" if choice.remembers_previous else "false",
                             REMEMBER_GROUP)
        self.page.wait_for_timeout(1500)

        if not choice.remembers_previous:
            return self

        self._radio_by_value(EXPIRY_STATUSES[choice.previous_expiry_status],
                             EXPIRY_GROUP)
        self.page.wait_for_timeout(800)

        # Lapsed for more than 90 days: the app hides the previous insurer and
        # type (html:317-319) - there is nothing more to answer.
        if choice.previous_expiry_status == "Expired more than 90 Days":
            return self

        ui.autocomplete(self.page, self.PREV_INSURER,
                        choice.previous_insurer_search, choice.previous_insurer)
        self.page.wait_for_timeout(1200)

        self._radio_by_value(PREVIOUS_POLICY_TYPES[choice.previous_policy_type],
                             PREVIOUS_TYPE_GROUP)
        self.page.wait_for_timeout(800)
        return self

    def proceed(self) -> None:
        ui.click_button(self.page, "Proceed")

    # ----------------------------------------------------------------- helpers

    def _radio_by_value(self, value: str, group: str = "") -> None:
        """
        Select by the radio's value attribute, inside its own group.

        Labels repeat ("Comprehensive" twice, "Yes"/"No" often) and so do
        values ("1", "2", "3" in two groups), so neither alone is safe. The
        formcontrolname on the mat-radio-group is the app's own name for the
        question, which makes group + value unambiguous. Without a group, or if
        the group is not found, fall back to the first radio with that value.
        """
        radio = f'input[type="radio"][value="{value}"]'
        if group:
            scoped = self.page.locator(f'[formcontrolname="{group}"] {radio}')
            if scoped.count():
                scoped.first.check(timeout=ui.FIELD_TIMEOUT_MS)
                return
        self.page.locator(radio).first.check(timeout=ui.FIELD_TIMEOUT_MS)

    def offers_policy_type(self, policy_type: str) -> bool:
        """Is this policy type on offer? It depends on the vehicle's age."""
        value = POLICY_TYPES.get(policy_type, "")
        return self.page.locator(
            f'[formcontrolname="{POLICY_GROUP}"] input[type="radio"]'
            f'[value="{value}"]').count() > 0

    def _asks_about_previous_policy(self) -> bool:
        return self.page.get_by_text(
            "Do you remember previous policy details", exact=False).count() > 0
