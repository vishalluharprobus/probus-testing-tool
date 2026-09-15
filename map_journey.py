"""
Mapping tool - not a test.

Walks the journey and dumps the real structure of each screen, so page objects
are written from what the app actually is rather than from a screenshot. Run it
whenever the portal changes and you need to re-learn a screen.

    python map_journey.py              # walk as far as it can, dump each screen
    python map_journey.py --stop 2     # stop after screen 2
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

from config import settings
from core import auth, browser, console, safety, ui
from pages.vehicle_details import Vehicle, VehicleDetailsPage

HONDA_ACTIVA = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
                       "3G (110 CC) (PETROL)", "2022")

# Pulls every form control on the screen with the attributes we select by.
DUMP_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const labelFor = el => {
    const wrap = el.closest('mat-radio-button, mat-checkbox, mat-slide-toggle');
    if (wrap) return wrap.textContent.trim().slice(0, 40);
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) return l.textContent.trim().slice(0, 40);
    }
    const f = el.closest('mat-form-field');
    return f ? f.textContent.trim().slice(0, 40) : '';
  };

  const inputs = [...document.querySelectorAll('input')].filter(vis).map(i => ({
    kind: i.type, fc: i.getAttribute('formcontrolname'), id: i.id,
    ph: i.placeholder || '', value: i.value, name: i.name,
    checked: i.type === 'radio' || i.type === 'checkbox' ? i.checked : undefined,
    label: labelFor(i),
  }));

  const selects = [...document.querySelectorAll('mat-select')].filter(vis).map(s => ({
    kind: 'mat-select', fc: s.getAttribute('formcontrolname'),
    text: s.textContent.trim().slice(0, 30),
  }));

  const checks = [...document.querySelectorAll('mat-checkbox')].filter(vis).map(c => ({
    kind: 'mat-checkbox', fc: c.getAttribute('formcontrolname'),
    label: c.textContent.trim().slice(0, 40),
  }));

  const buttons = [...document.querySelectorAll('button')].filter(vis)
    .map(b => b.textContent.trim().replace(/\s+/g, ' ').slice(0, 30))
    .filter(Boolean);

  // Card-ish blocks - on the quote screen these are the insurer results.
  const cards = [...document.querySelectorAll(
      '[class*="card"],[class*="quote"],[class*="plan"],[class*="insurer"]')]
    .filter(vis)
    .map(c => ({ cls: c.className.toString().slice(0, 60),
                 txt: c.innerText.replace(/\s+/g, ' ').trim().slice(0, 110) }))
    .filter(c => c.txt.length > 12)
    .slice(0, 14);

  const heads = [...document.querySelectorAll('h1,h2,h3,h4,[class*="title"],[class*="header"]')]
    .filter(vis).map(h => h.innerText.trim().slice(0, 50)).filter(Boolean).slice(0, 8);

  return { url: location.href, heads, inputs, selects, checks, buttons, cards };
}
"""


def dump(page, screen: str) -> dict:
    data = page.evaluate(DUMP_JS)
    print(f"\n{'=' * 72}\nSCREEN: {screen}\nURL: {data['url']}\n{'=' * 72}")
    if data["heads"]:
        print("headings :", " | ".join(data["heads"]))

    radios = [i for i in data["inputs"] if i["kind"] == "radio"]
    others = [i for i in data["inputs"] if i["kind"] not in ("radio", "hidden")]

    if others:
        print("\nFIELDS")
        for i in others:
            print(f"  formcontrolname={i['fc']!r:<26} type={i['kind']:<10} "
                  f"value={i['value']!r:<24} label={i['label']!r}")
    for s in data["selects"]:
        print(f"  formcontrolname={s['fc']!r:<26} type=mat-select  shows={s['text']!r}")
    for c in data["checks"]:
        print(f"  formcontrolname={c['fc']!r:<26} type=mat-checkbox label={c['label']!r}")

    if radios:
        print("\nRADIOS")
        for r in radios:
            mark = "x" if r["checked"] else " "
            print(f"  [{mark}] label={r['label']!r:<34} value={r['value']!r:<8} "
                  f"group={r['name']!r}")

    if data["cards"]:
        print("\nCARD-LIKE BLOCKS")
        for c in data["cards"]:
            print(f"  .{c['cls']}\n      {c['txt']}")

    print("\nBUTTONS:", " | ".join(dict.fromkeys(data["buttons"])))
    return data


def _save(captured: dict, run_dir) -> int:
    (run_dir / "screen-map.json").write_text(
        json.dumps(captured, indent=2), encoding="utf-8")
    print(f"\n\nSaved: {run_dir / 'screen-map.json'}")
    return 0


def _report_proceed_state(page) -> None:
    """
    Is Proceed clickable yet? A disabled Proceed is the app telling us the form
    is incomplete - far more useful to print that than to sit in a click timeout.
    """
    try:
        btn = page.get_by_role("button", name="Proceed").first
        disabled = btn.is_disabled(timeout=2000)
        print(f"\n>>> Proceed button: {'DISABLED - form incomplete' if disabled else 'enabled'}")
    except Exception as exc:
        print(f"\n>>> Could not read Proceed state: {exc}")


def _best_effort_previous_policy(page) -> None:
    """
    Answer the previous-policy questions well enough to move on.
    Every step is optional - we are exploring, not asserting.
    """
    for label in ("Yes", "Not Expired", "Comprehensive"):
        try:
            page.get_by_role("radio", name=label, exact=True).last.check(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:
            pass

    # Previous insurer - confirmed formcontrolname is 'prevInsurer'. We pick
    # whatever the dropdown offers first rather than naming an insurer, because
    # the list is server-driven and any valid entry gets us to the next screen.
    try:
        box = page.locator('input[formcontrolname="prevInsurer"]')
        box.wait_for(state="visible", timeout=5000)
        box.click()
        box.fill("BAJAJ")
        page.wait_for_timeout(2000)
        opts = page.get_by_role("option")
        print(f"    prevInsurer options: {opts.count()} -> "
              f"{opts.first.inner_text().strip()[:40] if opts.count() else 'none'}")
        opts.first.click(timeout=5000)
        page.wait_for_timeout(1500)
    except Exception as exc:
        print(f"    prevInsurer failed: {type(exc).__name__}: {str(exc)[:90]}")

    # Previous policy type appears only after the insurer is chosen.
    try:
        page.get_by_role("radio", name="Comprehensive", exact=True).last.check(timeout=3000)
        page.wait_for_timeout(800)
    except Exception:
        pass


def _best_effort_additional(page) -> None:
    """
    Screen 3 asks for dates plus a few yes/no questions.

    Confirmed field names: manufactYear, purchaseDate, policyExpDate, ncb.

    Worth knowing: the app PRE-FILLS all three dates itself - the two vehicle
    dates from the registration year chosen on screen 1, and the policy expiry
    as today. So the happy path needs no date handling at all, and the "should
    the expiry date be today or fixed?" question is already answered by the app.
    We only override when a scenario deliberately wants different dates.
    """
    for fc in ("manufactYear", "purchaseDate", "policyExpDate"):
        try:
            val = page.locator(f'input[formcontrolname="{fc}"]').input_value(timeout=2000)
            print(f"    {fc}: app pre-filled with {val!r}")
        except Exception:
            print(f"    {fc}: not found")

    try:
        ncb = page.locator('mat-select[formcontrolname="ncb"]')
        print(f"    ncb: app pre-filled with {ncb.inner_text(timeout=2000).strip()!r}")
    except Exception:
        pass

    # Customer type and the two yes/no questions. These default sensibly
    # (Individual / No / No) but we set them explicitly so a scenario controls them.
    for label in ("Individual",):
        try:
            page.get_by_role("radio", name=label, exact=True).first.check(timeout=2000)
            page.wait_for_timeout(300)
        except Exception:
            pass


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--stop", type=int, default=6)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    cfg = settings.load(args.target)
    captured = {}

    with browser.browser_session(cfg, headed=not args.headless) as (context, run_dir):
        page = None
        try:
            page = auth.log_in(context, cfg)
            safety.verify_environment(page, cfg)

            vp = VehicleDetailsPage(page).open(cfg.base_url)
            captured["1-vehicle"] = dump(page, "1 - Vehicle Details")
            if args.stop < 2:
                return 0

            vp.fill(HONDA_ACTIVA)
            vp.proceed()
            page.wait_for_timeout(3500)
            captured["2-policy-initial"] = dump(page, "2 - Policy Details (initial)")
            if args.stop < 3:
                return _save(captured, run_dir)

            # Screen 2 cascades like screen 1: the previous-policy questions only
            # appear once a policy type is chosen.
            ui.radio(page, "Comprehensive")
            page.wait_for_timeout(2500)
            captured["2-policy-expanded"] = dump(page, "2 - Policy Details (after Comprehensive)")
            if args.stop < 4:
                return _save(captured, run_dir)

            # Answer whatever the expanded form is asking, then move on. This is
            # best-effort on purpose: the point is to reach the next screen and
            # see it, not to be correct yet.
            _best_effort_previous_policy(page)
            captured["2-policy-filled"] = dump(page, "2 - Policy Details (after answering)")
            _report_proceed_state(page)
            ui.click_button(page, "Proceed")
            page.wait_for_timeout(4000)
            captured["3-additional"] = dump(page, "3 - Additional Details")
            if args.stop < 5:
                return _save(captured, run_dir)

            _best_effort_additional(page)
            ui.click_button(page, "Proceed")
            print("\n\n   ... waiting for insurer quotes (this can take a minute) ...")
            page.wait_for_timeout(45_000)
            captured["4-quotes"] = dump(page, "4 - Quote List")
            return _save(captured, run_dir)

        except Exception as exc:
            print(f"\nERROR: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            if page:
                browser.capture_failure(page, run_dir, "map-error")
            return 1


if __name__ == "__main__":
    sys.exit(main())
