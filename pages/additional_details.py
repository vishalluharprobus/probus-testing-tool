"""
Screen 3 - Additional Details. Still /two-wheeler/dontknownumber.

Confirmed fields: manufactYear, purchaseDate, policyExpDate (text inputs),
ncb (mat-select), plus three yes/no-style radio groups.

THE DATE QUESTION, ANSWERED BY THE APP
--------------------------------------
The app pre-fills all three dates itself:
  * manufactYear / purchaseDate  - derived from the registration year on screen 1
  * policyExpDate                - today

So the "should the expiry date be today or a fixed date?" question is already
settled: the app uses today, which keeps "Not Expired" true on every future run.
Leaving the dates alone is therefore both simpler AND more correct than writing
our own date logic - a scenario only overrides them when it deliberately wants
an older bike or a lapsed policy.

The same goes for the other expiry statuses (Saarthi component.ts:1014-1126):
"Expired within 90 Days" pre-fills an expiry date 50 days ago, and "more than
90 days" hides the date, the claim question and NCB altogether.

OD ONLY asks one thing more: who holds the vehicle's third-party cover
(tpPolicyInsurer, required). Its expiry date is pre-filled - for a car, to
today's day and month in the registration year + 3 (bikes: + 5). A 4-year-old
car therefore gets a date in the past, below the picker's own minimum, and
Proceed stays grey with no message (seen live 2026-10-05; run_quote_matrix.py
reports it through ui.proceed_blocked).

NCB is pre-filled from the registration year (component.ts:1177-1196):
1 year old 0%, 2 years 20%, 3 years 25%, 4 years 35%, 5 years 45%, older 50%.
"Claim made: Yes" resets it to 0% and hides it.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from core import ui

CUSTOMER_TYPES = ("Individual", "Organization")


@dataclass
class AdditionalChoice:
    customer_type: str = "Individual"
    owner_changed: bool = False
    claim_made: bool = False

    # None = keep whatever the app pre-filled (the normal case).
    manufacturing_date: str | None = None
    registration_date: str | None = None
    policy_expiry_date: str | None = None
    ncb_percent: str | None = None          # e.g. "35%"

    # OD Only: who holds the third-party cover. None = leave it alone.
    tp_insurer: str | None = None
    tp_insurer_search: str = ""


class AdditionalDetailsPage:
    MANUFACTURING = "manufactYear"
    REGISTRATION = "purchaseDate"
    POLICY_EXPIRY = "policyExpDate"
    NCB = "ncb"
    TP_INSURER = "tpPolicyInsurer"
    OWNER_CHANGED = "ownerChange"
    CLAIM = "claim"

    def __init__(self, page: Page):
        self.page = page

    def is_showing(self) -> bool:
        return ui.field_exists(self.page, self.MANUFACTURING, timeout_ms=8000)

    def wait_until_loaded(self, timeout_ms: int = 30_000) -> "AdditionalDetailsPage":
        """Wait for this screen rather than sleeping a guessed amount."""
        self.page.locator(
            f'input[formcontrolname="{self.MANUFACTURING}"]').wait_for(
            state="visible", timeout=timeout_ms)
        return self

    def prefilled(self) -> dict[str, str]:
        """What the app filled in for us - recorded in the report as evidence."""
        out: dict[str, str] = {}
        for name, fc in (("manufacturing", self.MANUFACTURING),
                         ("registration", self.REGISTRATION),
                         ("policy_expiry", self.POLICY_EXPIRY)):
            try:
                out[name] = self.page.locator(
                    f'input[formcontrolname="{fc}"]').input_value(timeout=3000)
            except Exception:
                out[name] = ""
        try:
            out["ncb"] = self.page.locator(
                f'mat-select[formcontrolname="{self.NCB}"]').inner_text(
                timeout=3000).strip()
        except Exception:
            out["ncb"] = ""
        return out

    def fill(self, choice: AdditionalChoice) -> "AdditionalDetailsPage":
        if choice.customer_type not in CUSTOMER_TYPES:
            raise ValueError(f"customer_type must be one of {CUSTOMER_TYPES}")

        # Only touch dates a scenario explicitly asked to change.
        for fc, value in ((self.MANUFACTURING, choice.manufacturing_date),
                          (self.REGISTRATION, choice.registration_date),
                          (self.POLICY_EXPIRY, choice.policy_expiry_date)):
            if value:
                ui.text_field(self.page, fc, value)
                self.page.wait_for_timeout(400)

        if choice.tp_insurer:
            ui.autocomplete(self.page, self.TP_INSURER,
                            choice.tp_insurer_search or choice.tp_insurer[:5],
                            choice.tp_insurer)
            self.page.wait_for_timeout(600)

        if choice.ncb_percent:
            ui.dropdown(self.page, self.NCB, choice.ncb_percent)

        self._radio_by_value(choice.customer_type)      # "Individual"/"Organization"
        self.page.wait_for_timeout(500)

        # Owner-changed and claim-made are two separate Yes/No groups that share
        # the same labels, so each is found by its formcontrolname. Answering
        # claim LAST matters: "Yes" resets NCB to 0% and hides it.
        self._yes_no(0, choice.owner_changed, self.OWNER_CHANGED)
        self._yes_no(1, choice.claim_made, self.CLAIM)
        return self

    def ncb_offered(self) -> bool:
        """Is the NCB box on screen? Hidden for lapsed policies, claims and a
        previous Third Party policy."""
        try:
            return self.page.locator(
                f'mat-select[formcontrolname="{self.NCB}"]').is_visible(timeout=1500)
        except Exception:
            return False

    def proceed(self) -> None:
        ui.click_button(self.page, "Proceed")

    # ----------------------------------------------------------------- helpers

    def _radio_by_value(self, value: str) -> None:
        self.page.locator(f'input[type="radio"][value="{value}"]').first.check(
            timeout=ui.FIELD_TIMEOUT_MS)

    def _yes_no(self, group_index: int, answer: bool, group: str = "") -> None:
        """
        Answer one Yes/No question - by its group when the page has it.

        Position alone was fragile: the driving-licence and PA-cover questions
        use the same Yes/No values and sit above these two (commented out in
        the HTML today, html:690-730), so bringing them back would silently
        have moved every answer one question up. The claim question is also
        hidden in several cases, and a hidden question must not be answered.
        """
        wanted = "Yes" if answer else "No"
        radio = f'input[type="radio"][value="{wanted}"]'
        if group:
            scoped = self.page.locator(f'[formcontrolname="{group}"] {radio}')
            if scoped.count():
                scoped.first.check(timeout=ui.FIELD_TIMEOUT_MS)
                self.page.wait_for_timeout(400)
                return
            if self.page.locator(f'[formcontrolname="{group}"]').count() == 0:
                return          # the question is not on screen - nothing to answer
        radios = self.page.locator(radio)
        if radios.count() > group_index:
            radios.nth(group_index).check(timeout=ui.FIELD_TIMEOUT_MS)
            self.page.wait_for_timeout(400)
