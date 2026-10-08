"""
Screen 1 - Vehicle Details.

Route: /two-wheeler/dontknownumber

This is the "Don't have a bike number yet?" path, where vehicle details are
typed in rather than looked up from a registration number. It is the better
path to automate: a registration lookup depends on an external service that can
be slow, rate-limited, or return different data over time, whereas typed details
give the same input on every single run.

All five selectors below were confirmed against the running app.
The fields CASCADE - each one only appears once the previous is chosen, and each
choice triggers a server call, so they must be filled in this order.

THE RTO LIST COMES FROM THE PAGE BEFORE
---------------------------------------
This screen never downloads the RTO list. The journey's first page (/two-wheeler
or /private-car) downloads it (api/Motor/RTOcityJson) and saves it in the
browser's sessionStorage as 'RTOCity'; this screen reads that saved copy ONCE,
when it opens (Saarthi tw/pc-insurance-plan getRtoList, tw/pc-dont-know-number
ngOnInit). Jump here before the download has finished - the login check leaves
the first page the moment it renders - and the download is cancelled, nothing
is saved, and the RTO box offers nothing however often "GJ-01" is retyped.
Seen 2026-10-06 on the test site, where the list is slower to arrive than on
localhost: 4 of 8 journeys lost this way (RTOcityJson status -1).

So open() checks the saved list and, when it is missing, does what a person
would: one step back to the first page, wait for the list, come forward again.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from core import ui


@dataclass
class Vehicle:
    rto_type: str        # what to type,  e.g. "GJ-01"
    rto: str             # what to pick,  e.g. "GJ-01 Ahmedabad"
    make: str            # "HONDA"
    model: str           # "ACTIVA"
    variant: str         # "3G (110 CC) (PETROL)"
    registration_year: str  # "2022"


class VehicleDetailsPage:
    # formcontrolname values - all confirmed in the running app
    RTO = "rtoName"
    MAKE = "vehicleMake"
    MODEL = "vehicleModel"
    VARIANT = "vehicleVariant"
    YEAR = "registrationYear"   # a <mat-select>, not an autocomplete

    # Private car uses the very same form controls (Saarthi
    # pc-dont-know-number.component.ts:208-237), so one page object serves both.
    PATHS = {"bike": "/two-wheeler/dontknownumber",
             "car": "/private-car/dontknownumber"}
    # The page one step back - the one that downloads the RTO list.
    HOMES = {"bike": "/two-wheeler", "car": "/private-car"}

    # The real list is ~150 KB (plain or AES-encrypted). Anything this short is
    # an empty list - and an empty saved list is worse than none, because the
    # first page only downloads when nothing is saved.
    RTO_LIST_MIN_CHARS = 1000
    RTO_LIST_WAIT_MS = 30_000
    STEPS_BACK = 2

    def __init__(self, page: Page):
        self.page = page
        self._form_url = ""
        self._home_url = ""

    def open(self, base_url: str, product: str = "bike") -> "VehicleDetailsPage":
        self._form_url = f"{base_url}{self.PATHS[product]}"
        self._home_url = f"{base_url}{self.HOMES[product]}"
        self._go_to_form()
        if not self.rto_list_ready():
            self.step_back_for_rto_list()
        return self

    def _go_to_form(self) -> None:
        self.page.goto(self._form_url, wait_until="domcontentloaded")
        ui.wait_for_screen(self.page, self.RTO)

    def _saved_rto_chars(self) -> int:
        try:
            return self.page.evaluate(
                "() => (sessionStorage.getItem('RTOCity') || '').length")
        except Exception:
            return 0

    def rto_list_ready(self) -> bool:
        """Did the app hand this screen its RTO list?"""
        return self._saved_rto_chars() >= self.RTO_LIST_MIN_CHARS

    def step_back_for_rto_list(self) -> None:
        """
        Go one step back to the first page, let it download the RTO list, and
        come forward to the form again - up to STEPS_BACK times.
        """
        for attempt in range(1, self.STEPS_BACK + 1):
            print(f"        RTO list missing on screen 1 - one step back to fetch "
                  f"it ({attempt}/{self.STEPS_BACK})", flush=True)
            try:
                # Drop a saved EMPTY list, or the first page will not download.
                self.page.evaluate("() => sessionStorage.removeItem('RTOCity')")
            except Exception:
                pass
            self.page.goto(self._home_url, wait_until="domcontentloaded")
            try:
                self.page.wait_for_function(
                    "n => (sessionStorage.getItem('RTOCity') || '').length >= n",
                    arg=self.RTO_LIST_MIN_CHARS, timeout=self.RTO_LIST_WAIT_MS)
            except Exception:
                continue                  # never arrived - try the step again
            self._go_to_form()
            if self.rto_list_ready():
                print("        RTO list is back - carrying on", flush=True)
                return

        raise ui.LookupTimedOut(
            f"The 'rtoName' list never reached screen 1.\n"
            f"  Went one step back to the first page {self.STEPS_BACK} times and "
            f"waited {self.RTO_LIST_WAIT_MS // 1000}s each time for the app to "
            f"download the RTO list (api/Motor/RTOcityJson); it never arrived.\n"
            f"  This is the app's API being slow or down, not the test. Wait a few "
            f"minutes and run again.")

    def fill(self, vehicle: Vehicle) -> "VehicleDetailsPage":
        # Order matters - see the cascade note above.
        try:
            ui.autocomplete(self.page, self.RTO, vehicle.rto_type, vehicle.rto)
        except ui.LookupTimedOut:
            # The saved list was there but did not offer this RTO - it may have
            # been saved half-way. Same cure, once: step back, fetch it fresh.
            if not self._home_url:
                raise
            self.step_back_for_rto_list()
            ui.autocomplete(self.page, self.RTO, vehicle.rto_type, vehicle.rto)

        # Type only the first few characters: the portal matches on prefix, and
        # some full values (variants with brackets, for instance) return nothing.
        ui.autocomplete(self.page, self.MAKE, vehicle.make[:4], vehicle.make)
        ui.autocomplete(self.page, self.MODEL, vehicle.model[:4], vehicle.model)
        ui.autocomplete(self.page, self.VARIANT, vehicle.variant[:3], vehicle.variant)

        ui.dropdown(self.page, self.YEAR, vehicle.registration_year)
        return self

    def proceed(self) -> None:
        ui.click_button(self.page, "Proceed")

    def is_showing(self) -> bool:
        return ui.field_exists(self.page, self.RTO, timeout_ms=4000)
