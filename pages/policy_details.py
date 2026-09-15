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
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from core import ui

POLICY_TYPES = {"Comprehensive": "CP", "Third Party": "TP", "OD Only": "OD"}
EXPIRY_STATUSES = {"Not Expired": "1",
                   "Expired within 90 Days": "2",
                   "Expired more than 90 Days": "3"}
PREVIOUS_POLICY_TYPES = {"Comprehensive": "1", "Third Party": "2"}


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

    def is_showing(self) -> bool:
        return self.page.get_by_role("radio", name="Comprehensive").count() > 0

    def wait_until_loaded(self, timeout_ms: int = 30_000) -> "PolicyDetailsPage":
        """
        Wait for this screen to actually appear.

        Used instead of a fixed sleep after the previous screen's Proceed. A
        guessed delay is wrong in both directions: too short and the run fails
        on a slow day, too long and every run pays for the worst case. Waiting
        for the real marker is faster AND more reliable.
        """
        self.page.get_by_role("radio", name="Comprehensive").first.wait_for(
            state="visible", timeout=timeout_ms)
        return self

    def fill(self, choice: PolicyChoice) -> "PolicyDetailsPage":
        if choice.policy_type not in POLICY_TYPES:
            raise ValueError(f"Unknown policy type {choice.policy_type!r}. "
                             f"Expected: {', '.join(POLICY_TYPES)}")

        self._radio_by_value(POLICY_TYPES[choice.policy_type])
        self.page.wait_for_timeout(1500)

        # Third-party-only journeys never ask about the previous policy.
        if not self._asks_about_previous_policy():
            return self

        self._radio_by_value("true" if choice.remembers_previous else "false")
        self.page.wait_for_timeout(1500)

        if not choice.remembers_previous:
            return self

        self._radio_by_value(EXPIRY_STATUSES[choice.previous_expiry_status])
        self.page.wait_for_timeout(800)

        ui.autocomplete(self.page, self.PREV_INSURER,
                        choice.previous_insurer_search, choice.previous_insurer)
        self.page.wait_for_timeout(1200)

        self._radio_by_value(PREVIOUS_POLICY_TYPES[choice.previous_policy_type])
        self.page.wait_for_timeout(800)
        return self

    def proceed(self) -> None:
        ui.click_button(self.page, "Proceed")

    # ----------------------------------------------------------------- helpers

    def _radio_by_value(self, value: str) -> None:
        """
        Select by the radio's value attribute rather than its label.

        Values are unique across the screen; labels are not ("Comprehensive"
        and "Yes"/"No" each appear more than once). Angular's group names are
        auto-generated and would shift if the form were reordered, so value is
        the only stable, unambiguous handle.
        """
        self.page.locator(f'input[type="radio"][value="{value}"]').first.check(
            timeout=ui.FIELD_TIMEOUT_MS)

    def _asks_about_previous_policy(self) -> bool:
        return self.page.get_by_text(
            "Do you remember previous policy details", exact=False).count() > 0
