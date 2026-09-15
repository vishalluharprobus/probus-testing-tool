"""
One-off probe: what exactly is inside an insurer quote card?

The card is where the test verdict comes from - did this insurer return a
premium, an error, or nothing at all - so it is worth understanding precisely
rather than guessing from a truncated text dump.

Assumes a result page is already reachable; run map_journey.py first if not.
"""
from __future__ import annotations

import json
import sys

from config import settings
from core import auth, browser, console, safety, ui
from map_journey import (HONDA_ACTIVA, _best_effort_additional,
                         _best_effort_previous_policy)
from pages.vehicle_details import VehicleDetailsPage

CARD_JS = r"""
() => {
  const cards = [...document.querySelectorAll('.plan-card')];
  return {
    count: cards.length,
    // Full structure of the first two cards, so we can see where the insurer
    // name and premium actually live.
    sample: cards.slice(0, 2).map(c => ({
      text: c.innerText.replace(/\s+/g, ' ').trim(),
      imgs: [...c.querySelectorAll('img')].map(i => ({
        alt: i.alt, src: (i.src || '').split('/').pop().slice(0, 45)
      })),
      // Every descendant that has its own class and short text - this is how we
      // find the premium element without guessing its class name.
      bits: [...c.querySelectorAll('[class]')]
        .map(e => ({ cls: e.className.toString().slice(0, 45),
                     t: (e.innerText || '').replace(/\s+/g, ' ').trim() }))
        .filter(b => b.t && b.t.length < 45)
        .slice(0, 30),
      buttons: [...c.querySelectorAll('button,a')]
        .map(b => b.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean),
    })),
    // Anything that looks like a failure message on the page.
    errorish: [...document.querySelectorAll(
        '[class*="error"],[class*="fail"],[class*="not-available"],[class*="noquote"]')]
      .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(t => t && t.length < 160).slice(0, 12),
  };
}
"""


def main() -> int:
    console.use_utf8()
    cfg = settings.load()

    with browser.browser_session(cfg, headed=False) as (context, run_dir):
        page = auth.log_in(context, cfg)
        safety.verify_environment(page, cfg)

        vp = VehicleDetailsPage(page).open(cfg.base_url)
        vp.fill(HONDA_ACTIVA)
        vp.proceed()
        page.wait_for_timeout(3000)

        ui.radio(page, "Comprehensive")
        page.wait_for_timeout(2000)
        _best_effort_previous_policy(page)
        ui.click_button(page, "Proceed")
        page.wait_for_timeout(3500)

        _best_effort_additional(page)
        ui.click_button(page, "Proceed")
        print("\nwaiting for insurer quotes ...")
        page.wait_for_timeout(45_000)

        data = page.evaluate(CARD_JS)
        print(f"\nPLAN CARDS FOUND: {data['count']}\n")

        for n, card in enumerate(data["sample"], 1):
            print(f"--- CARD {n} " + "-" * 58)
            print("TEXT   :", card["text"][:300])
            print("IMAGES :", card["imgs"])
            print("BUTTONS:", card["buttons"])
            print("PIECES :")
            for b in card["bits"]:
                print(f"   .{b['cls']:<45} {b['t']!r}")
            print()

        if data["errorish"]:
            print("ERROR-LIKE TEXT ON PAGE:")
            for e in data["errorish"]:
                print("  -", e)

        (run_dir / "cards.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nSaved: {run_dir / 'cards.json'}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
