"""
Probe: what does the upload screen do after we attach the real documents?

The run reaches the upload step, attaches both files, clicks Proceed - and then
nothing visible happens. This dumps the screen's state at each point so we can
see whether the files registered, whether Proceed is enabled, and whether the
app is showing an error we are walking straight past.
"""
from __future__ import annotations

import sys

from config import insurers, settings
from core import auth, browser, console, safety
from data.customer import DEFAULT as CUSTOMER
from pages.additional_details import AdditionalChoice, AdditionalDetailsPage
from pages.kyc import KycPage
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.quote_list import QuoteListPage
from pages.vehicle_details import Vehicle, VehicleDetailsPage
from urllib.parse import urlparse

HONDA_ACTIVA = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
                       "3G (110 CC) (PETROL)", "2022")

STATE_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0; };
  return {
    url: location.href,
    selects: [...document.querySelectorAll('mat-select')].filter(vis)
      .map(s => s.textContent.replace(/\s+/g,' ').trim().slice(0, 30)),
    fileInputs: [...document.querySelectorAll('input[type=file]')]
      .map(i => ({ files: i.files ? i.files.length : 0,
                   name: i.files && i.files[0] ? i.files[0].name : '' })),
    buttons: [...document.querySelectorAll('button')].filter(vis)
      .map(b => ({ text: b.textContent.replace(/\s+/g,' ').trim().slice(0,24),
                   disabled: b.disabled })),
    // Anything that looks like a validation message or an error
    messages: [...document.querySelectorAll(
        'mat-error,.error,.text-danger,.invalid-feedback,[class*="error"],.modal-body')]
      .filter(vis)
      .map(e => e.innerText.replace(/\s+/g,' ').trim())
      .filter(t => t && t.length < 200),
    bodyTail: document.body.innerText.replace(/\s+/g,' ').trim().slice(-400),
  };
}
"""


def show(page, label):
    d = page.evaluate(STATE_JS)
    print(f"\n----- {label} -----")
    print("url        :", d["url"])
    print("selects    :", d["selects"])
    print("file inputs:", d["fileInputs"])
    print("buttons    :", [f"{b['text']}{'(DISABLED)' if b['disabled'] else ''}"
                           for b in d["buttons"]])
    if d["messages"]:
        print("MESSAGES   :")
        for m in dict.fromkeys(d["messages"]):
            print("   -", m[:180])
    return d


def main() -> int:
    console.use_utf8()
    cfg = settings.load()

    with browser.browser_session(cfg, headed=False) as (context, run_dir):
        page = auth.log_in(context, cfg)
        safety.verify_environment(page, cfg)

        VehicleDetailsPage(page).open(cfg.base_url).fill(HONDA_ACTIVA).proceed()
        PolicyDetailsPage(page).wait_until_loaded().fill(PolicyChoice()).proceed()
        AdditionalDetailsPage(page).wait_until_loaded().fill(
            AdditionalChoice()).proceed()

        quote_page = QuoteListPage(page).wait_until_loaded()
        quotes = quote_page.quotes()
        print("quotes:", ", ".join(f"{q.insurer}={q.premium}" for q in quotes))

        target = next((q for q in quotes if "ZUNO" in q.insurer.upper()), None)
        if target is None:
            print("ZUNO not available this run - nothing to probe")
            return 5

        quote_page.buy(target.insurer)
        kyc = KycPage(page)
        kyc.detect_style(urlparse(cfg.base_url).hostname or "")
        kyc.wait_until_loaded()
        kyc.fill_details(CUSTOMER)
        kyc.proceed()
        kyc.wait_for_kyc_response()

        show(page, "AFTER KYC DETAILS SUBMITTED")

        if not kyc.upload_step_showing():
            print("\nno upload step - stopping")
            return 0

        kyc.upload_documents(CUSTOMER, cfg.test_documents_dir)
        page.wait_for_timeout(3000)
        show(page, "AFTER FILES ATTACHED")

        # Now click Proceed ourselves and watch what changes.
        try:
            btn = page.get_by_role("button", name="Proceed").first
            print(f"\nProceed enabled? {btn.is_enabled(timeout=3000)}")
            btn.click(timeout=15_000)
            print("clicked Proceed")
        except Exception as exc:
            print("could not click Proceed:", type(exc).__name__, str(exc)[:120])

        page.wait_for_timeout(12_000)
        show(page, "AFTER PROCEED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
