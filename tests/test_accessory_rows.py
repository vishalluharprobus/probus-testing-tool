"""
Prove the proposal's accessory sections get filled - offline, in seconds.

    venv\\Scripts\\python -m pytest tests/test_accessory_rows.py -q

A quote with accessories brings "Electrical Accessory Detail (Max Amount : X,
Amount Remains : Y)" onto the proposal's vehicle step, and Continue stays put
until rows adding up to exactly X are listed (IFFCOTOKIO car journeys #9 and
#10, 2026-10-06). The fake page below has the same controls; its Add button
records what it was given. Nothing here touches the portal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pages.proposal import ProposalPage  # noqa: E402

SECTION = """
<h4>%(kind)s Accessory Detail (Max Amount : %(max)s, Amount Remains : %(left)s)</h4>
<form id="%(id)s">
  <mat-select formcontrolname="accessoryName" onclick="openList(this, ['Stereo','Fog Lamp'])">Select</mat-select>
  <input formcontrolname="manufactureName"><input formcontrolname="model">
  <mat-select formcontrolname="manufacturingYear" onclick="openList(this, ['2022','2021'])">Year</mat-select>
  <input formcontrolname="amount" type="number" value="0">
  <button type="button" onclick="add(this.form)">Add</button>
  <button type="button">Clear</button>
</form>"""

PAGE = """<html><body>%s
<div id="panel"></div>
<script>
window.added = [];
function openList(select, names) {
  const panel = document.getElementById('panel'); panel.innerHTML = '';
  names.forEach(n => { const o = document.createElement('mat-option'); o.textContent = n;
    o.onclick = () => { select.textContent = n; select.dataset.value = n; panel.innerHTML = ''; };
    panel.appendChild(o); });
}
function add(form) {
  const v = n => form.querySelector('[formcontrolname="' + n + '"]');
  window.added.push({form: form.id, name: v('accessoryName').dataset.value,
    year: v('manufacturingYear').dataset.value, make: v('manufactureName').value,
    model: v('model').value, amount: Number(v('amount').value)});
}
</script></body></html>"""


@pytest.fixture
def page():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:                     # noqa: BLE001
        pytest.skip(f"no browser available: {exc}")
    yield browser.new_page()
    browser.close()
    pw.stop()


def proposal(page) -> ProposalPage:
    p = ProposalPage(page)
    p.vehicle_log = []
    return p


def test_each_section_gets_one_row_for_what_remains(page):
    page.set_content(PAGE % (
        SECTION % {"kind": "Electrical", "max": 5000, "left": 5000, "id": "e"}
        + SECTION % {"kind": "Non Electrical", "max": 10000, "left": 10000, "id": "n"}))
    p = proposal(page)
    p.fill_accessories()
    added = page.evaluate("window.added")
    assert [(a["form"], a["amount"]) for a in added] == [("e", 5000), ("n", 10000)]
    assert all(a["name"] == "Stereo" and a["year"] == "2022" and a["make"] == "TEST MAKE"
               and a["model"] == "TEST MODEL" for a in added)
    assert p.vehicle_log == ["electrical accessory row added for Rs 5,000",
                             "non electrical accessory row added for Rs 10,000"]


def test_nothing_left_to_declare_adds_nothing(page):
    page.set_content(PAGE % SECTION % {"kind": "Electrical", "max": 5000, "left": 0,
                                        "id": "e"})
    proposal(page).fill_accessories()
    assert page.evaluate("window.added") == []


def test_no_accessory_section_is_a_no_op(page):
    page.set_content("<html><body><p>Vehicle Details</p></body></html>")
    p = proposal(page)
    p.fill_accessories()
    assert p.vehicle_log == []
