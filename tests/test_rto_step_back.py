"""
Prove screen 1 gets its RTO list back by stepping back - offline, in seconds.

    venv\\Scripts\\python -m pytest tests/test_rto_step_back.py -q

The fake site below copies the one rule that broke 4 of 8 test-site journeys on
2026-10-06 (see pages/vehicle_details.py): the FIRST page downloads the RTO list
and saves it in sessionStorage 'RTOCity' - but only when nothing is saved yet -
and the FORM only reads that saved copy. Nothing here touches the portal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import ui  # noqa: E402
from pages.vehicle_details import VehicleDetailsPage  # noqa: E402

BASE = "http://fake.local/motor-journey"
RTO_LIST = "[" + ",".join(['{"Id":1,"Name":"GJ-01 Ahmedabad"}'] * 60) + "]"

FORM = """<html><body><input formcontrolname="rtoName">
<script>window.rtos = (sessionStorage.getItem('RTOCity') || '').length;</script>
</body></html>"""

# The first page: downloads (after a moment) only when nothing is saved.
HOME = """<html><body><p>Have a vehicle number?</p><script>
if (!sessionStorage.getItem('RTOCity') && %(delivers)s) {
  setTimeout(() => sessionStorage.setItem('RTOCity', %(list)r), 300);
}
</script></body></html>"""


@pytest.fixture
def site():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:                     # noqa: BLE001
        pytest.skip(f"no browser available: {exc}")

    def make(delivers: bool = True, saved: str | None = None):
        context = browser.new_context()
        visits = {"home": 0, "form": 0}

        def answer(route):
            path = route.request.url.split("fake.local", 1)[1].split("?")[0]
            if path.endswith("/dontknownumber"):
                visits["form"] += 1
                body = FORM
            else:
                visits["home"] += 1
                body = HOME % {"delivers": "true" if delivers else "false",
                               "list": RTO_LIST}
            route.fulfill(status=200, content_type="text/html", body=body)

        context.route("http://fake.local/**", answer)
        page = context.new_page()
        if saved is not None:                    # something saved earlier
            page.goto(f"{BASE}/blank")
            page.evaluate("v => sessionStorage.setItem('RTOCity', v)", saved)
            visits["home"] = 0
        return page, visits

    yield make
    browser.close()
    pw.stop()


def test_list_already_there_means_no_step_back(site):
    page, visits = site(saved=RTO_LIST)
    screen = VehicleDetailsPage(page).open(BASE, "car")
    assert screen.rto_list_ready()
    assert visits == {"home": 0, "form": 1}


def test_missing_list_steps_back_once_and_comes_forward(site):
    """The 2026-10-06 case: the download was cancelled, nothing was saved."""
    page, visits = site()
    screen = VehicleDetailsPage(page).open(BASE, "car")
    assert screen.rto_list_ready()
    assert visits == {"home": 1, "form": 2}
    assert page.url == f"{BASE}/private-car/dontknownumber"
    assert page.evaluate("window.rtos") >= VehicleDetailsPage.RTO_LIST_MIN_CHARS


def test_saved_empty_list_is_dropped_so_the_first_page_downloads(site):
    page, visits = site(saved="[]")
    screen = VehicleDetailsPage(page).open(BASE, "bike")
    assert screen.rto_list_ready()
    assert visits["home"] == 1
    assert page.url == f"{BASE}/two-wheeler/dontknownumber"


def test_list_that_never_arrives_is_named_not_retyped_forever(site, monkeypatch):
    monkeypatch.setattr(VehicleDetailsPage, "RTO_LIST_WAIT_MS", 1500)
    page, visits = site(delivers=False)
    with pytest.raises(ui.LookupTimedOut, match="RTOcityJson"):
        VehicleDetailsPage(page).open(BASE, "car")
    assert visits["home"] == VehicleDetailsPage.STEPS_BACK
