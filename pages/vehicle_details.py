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

    def __init__(self, page: Page):
        self.page = page

    def open(self, base_url: str) -> "VehicleDetailsPage":
        self.page.goto(f"{base_url}/two-wheeler/dontknownumber",
                       wait_until="domcontentloaded")
        ui.wait_for_screen(self.page, self.RTO)
        return self

    def fill(self, vehicle: Vehicle) -> "VehicleDetailsPage":
        # Order matters - see the cascade note above.
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
