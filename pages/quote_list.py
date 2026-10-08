"""
Screen 4 - the quote list. Route: /two-wheeler/result

This is where the test verdict actually comes from. Everything before it is
just getting here; this page is the one that says whether each insurer
integration works.

Card anatomy, confirmed against the running app:

    .plan-card
      img[alt]              -> insurer code, e.g. "NATIONAL"   (the logo's alt!)
      .card-plan-tenure     -> "1 Year Comprehensive"
      .card-plan-name       -> "Two Wheeler Package Policy"
      .card-metric-val      -> "Rs 27,303"                     (IDV)
      .buy-now-btn          -> "Buy Now Rs 903"                (premium)

Two things worth knowing:
  * The insurer's name is only in an image alt attribute. There is no text
    element carrying it, so that alt is the identity of the card.
  * The page renders some empty .plan-card elements (templates or placeholders).
    They are skipped rather than reported as failures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Page

from core import auth
from pages import routes

# Money as the portal writes it: an optional rupee sign, then digits with commas.
MONEY = re.compile(r"₹\s*([\d,]+)")

ADD_ONS = (
    "Zero Depreciation Cover", "Return to Invoice", "Consumables",
    "Engine Protector", "Emergency Cover", "Road Side Assistance",
    "Emergency Medical Assistance",
)


@dataclass
class Quote:
    insurer: str
    premium: int | None      # rupees; None when the card shows no price
    idv: int | None
    plan_name: str = ""
    tenure: str = ""
    raw: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        """A usable quote: we know who it is from and what it costs."""
        return bool(self.insurer) and self.premium is not None and self.premium > 0


READ_CARDS_JS = r"""
() => {
  const money = t => {
    const m = (t || '').match(/₹\s*([\d,]+)/);
    return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
  };
  const pick = (c, sel) => {
    const e = c.querySelector(sel);
    return e ? (e.innerText || '').replace(/\s+/g, ' ').trim() : '';
  };

  return [...document.querySelectorAll('.plan-card')].map(c => {
    const text = (c.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text) return null;                       // empty placeholder card

    // Skip cards we cannot actually buy from. The portal renders the card list
    // TWICE - a desktop layout and a mobile one - and hides one with CSS. Both
    // copies carry a premium and a Buy Now button in the DOM, so reading both
    // double-counts every insurer, and clicking the hidden one hangs.
    const buy = c.querySelector('.buy-now-btn');
    if (!buy) return null;

    const cardBox = c.getBoundingClientRect();
    const buyBox = buy.getBoundingClientRect();
    if (cardBox.width === 0 || cardBox.height === 0) return null;
    if (buyBox.width === 0 || buyBox.height === 0) return null;

    // The logo's alt is the only place the insurer is named. Skip the decorative
    // icons (scooter, shield, chevrons) - they have blank or known alts.
    const skip = new Set(['', 'scooter', 'IDV', 'compare']);
    const logo = [...c.querySelectorAll('img')]
      .map(i => i.alt).find(a => a && !skip.has(a));

    return {
      insurer: logo || '',
      premium: money(pick(c, '.buy-now-btn')),
      idv: money(pick(c, '.card-metric-val')),
      plan_name: pick(c, '.card-plan-name'),
      tenure: pick(c, '.card-plan-tenure'),
      raw: text.slice(0, 220),
    };
  }).filter(Boolean);
}
"""


# How to read the reason an insurer gave. The categories matter more than the
# wording: they decide whether anyone should act, and who.
FAILURE_KINDS = (
    # (needle, kind, meaning)
    ("server is down", "insurer-down",
     "The insurer's own service is unavailable. Nothing to fix at our end."),
    ("cannot be null", "our-defect",
     "A null value reached the insurer request - this is a defect in OUR "
     "integration code, not the insurer's. Worth a ticket."),
    ("same insurer", "business-rule",
     "Correct behaviour: this insurer is the PREVIOUS insurer on the policy, "
     "so it cannot also be the new one. Not a bug."),
    ("core service", "our-defect",
     "InsureBridge's own core service errored. Ours to investigate."),
    # Before "getcalculatedpremium": UNITED wraps this one in that prefix, and
    # the more specific meaning should win (first match decides).
    ("belongs to privatecar", "our-defect",
     "The insurer's records say the registration number we sent is a CAR. On "
     "the don't-know-number journey the app sends a made-up number (RTO + "
     "'-AB-1111', Saarthi tw-dont-know-number.component.ts:438-440), and that "
     "number is real somewhere. Ours to fix: the placeholder must not be a "
     "real registration."),
    ("getcalculatedpremium", "our-defect",
     "Premium calculation failed inside our integration."),
    # Newtonsoft's wording when our code parses an empty or non-JSON answer.
    # Seen from ICICI on every journey, 2026-09-30.
    ("error reading jobject", "our-defect",
     "Our code crashed reading the insurer's answer (empty or not JSON) and "
     "showed the raw exception. Even if the insurer sent nothing, handling "
     "that is ours."),
    ("no error message provided", "unknown",
     "The insurer declined without saying why - nothing to act on, but worth "
     "counting if it persists."),
    ("timeout", "insurer-down",
     "The insurer did not answer in time."),
    # BAJAJ, live 2026-09-30: "Error in Service ~~ I/O error on POST request
    # for https://htauth.preprod.bajajgeneral.com/... peer not authenticated".
    # Their service could not reach their own login server.
    ("peer not authenticated", "insurer-down",
     "The insurer's service could not reach its own auth server (SSL). Theirs."),
    ("i/o error on", "insurer-down",
     "The insurer's service failed calling one of its own systems. Theirs."),
)


@dataclass
class Failure:
    """An insurer that did not quote, and the reason the portal gave."""
    insurer: str
    reason: str

    @property
    def kind(self) -> str:
        low = self.reason.lower()
        for needle, kind, _ in FAILURE_KINDS:
            if needle in low:
                return kind
        return "unknown"

    @property
    def meaning(self) -> str:
        low = self.reason.lower()
        for needle, _, meaning in FAILURE_KINDS:
            if needle in low:
                return meaning
        return "Not a recognised message - worth reading in full."

    @property
    def is_our_problem(self) -> bool:
        """Should someone on this team act on it?"""
        return self.kind == "our-defect"


READ_FAILURES_JS = r"""
() => {
  // SCOPE TO THE PANEL. An earlier version scanned every logo on the page and
  // happily reported the header, the calendar icons and the vehicle summary as
  // "failed insurers" - because plenty of things on a page are an image next to
  // some text. The panel announces itself ("8 insurers unavailable"), so we
  // find that heading and read only what sits under it.
  const heading = [...document.querySelectorAll('*')].find(el =>
    /\d+\s+insurers?\s+unavailable/i.test((el.textContent || '')) &&
    el.children.length < 6);
  if (!heading) return [];

  // Climb to the block that holds both the heading and the cards beneath it.
  let panel = heading;
  for (let i = 0; i < 6 && panel.parentElement; i++) {
    panel = panel.parentElement;
    if (panel.querySelectorAll('img[alt]').length >= 2) break;
  }

  const out = [];
  const seen = new Set();

  for (const img of panel.querySelectorAll('img[alt]')) {
    const alt = (img.alt || '').trim();
    // Insurer logos carry a real name. Decorative icons carry "not available",
    // "calendar", "user" and the like, or nothing at all.
    if (!alt || alt.length > 24) continue;
    if (/^(not available|calendar|user|scooter|insurer|policy|expiry|icon)/i.test(alt)) {
      continue;
    }

    // The message sits in the same card as the logo.
    let card = img.parentElement;
    for (let i = 0; i < 4 && card && card !== panel; i++) {
      const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
      if (text.replace(alt, '').trim().length > 6) break;
      card = card.parentElement;
    }
    if (!card) continue;

    const reason = (card.innerText || '').replace(/\s+/g, ' ')
                     .replace(alt, '').trim();
    if (reason.length < 6 || reason.length > 200) continue;

    if (seen.has(alt)) continue;      // one entry per insurer
    seen.add(alt);
    out.push({ insurer: alt, reason });
  }
  return out;
}
"""


class QuoteListPage:
    URL_MARKER = "/two-wheeler/result"
    CARD = ".plan-card"

    def __init__(self, page: Page):
        self.page = page

    def wait_until_loaded(self, timeout_ms: int = 90_000) -> "QuoteListPage":
        """
        Wait for the insurer fan-out to finish.

        The portal asks every insurer in turn, so cards arrive over many seconds.
        We wait for the first card, then wait for the count to stop changing -
        otherwise we would read a half-filled page and wrongly report the slower
        insurers as missing.
        """
        self.page.wait_for_url(lambda u: routes.on(u, "result"), timeout=timeout_ms)
        self.page.locator(self.CARD).first.wait_for(state="attached", timeout=timeout_ms)

        # Poll often, settle quickly. Insurers answer at different speeds, so we
        # wait for the count to stop changing rather than for a fixed duration -
        # a fast run finishes in ~3s instead of always paying the slow case.
        stable_for = 0
        last = -1
        waited = 0
        while stable_for < 3 and waited < timeout_ms:
            self.page.wait_for_timeout(1000)
            waited += 1000
            now = self.page.locator(self.CARD).count()
            stable_for = stable_for + 1 if now == last else 0
            last = now
        return self

    def wait_for_insurer(self, insurer: str, timeout_ms: int = 90_000) -> Quote | None:
        """
        Return as soon as ONE named insurer has priced - don't wait for the rest.

        The portal asks every insurer in turn and cards trickle in over ~45
        seconds. When a run only cares about ZUNO, waiting for the full fan-out
        to settle is 20-30 seconds spent watching insurers we are going to
        ignore. This returns the moment the one we want appears.

        Still bounded: if the insurer never shows we wait the full timeout and
        return None, so "not offered" stays a real answer rather than a hang.
        """
        self.page.wait_for_url(lambda u: routes.on(u, "result"), timeout=timeout_ms)
        waited = 0
        while waited < timeout_ms:
            for quote in self.quotes():
                if insurer.upper() in quote.insurer.upper() and quote.ok:
                    return quote
            self.page.wait_for_timeout(1500)
            waited += 1500
        return None

    def quotes(self) -> list[Quote]:
        return [Quote(**row) for row in self.page.evaluate(READ_CARDS_JS)]

    def unavailable(self) -> list["Failure"]:
        """
        Read the "N insurers unavailable" panel, with each insurer's REASON.

        This panel is the most valuable thing on the page and the tool ignored
        it for far too long. Reporting "NO QUOTE" when the portal is plainly
        displaying "Shriram's server is down" throws away the answer and makes
        every absence look identical - when in fact they are completely
        different problems:

            "Shriram's server is down."              -> their infrastructure
            "Value cannot be null. (Parameter 'node')" -> a defect in OUR code
            "Policy can not issue with same insurer."  -> correct business rule
            "Error occured in Core service"            -> InsureBridge itself

        Only one of those is worth waking a developer for, and you cannot tell
        which without reading the text.
        """
        rows = self.page.evaluate(READ_FAILURES_JS)

        # Keep only rows that are plausibly an insurer failing. The panel sits
        # inside the page, so a scrape of it can pick up the header, the filter
        # buttons and the IDV badge - all of which are an image beside some text.
        # Two cheap tests remove all of that: the logo must name something we
        # recognise as an insurer, and the message must not be a UI label.
        noise = ("compare", "raise query", "customize", "set your idv",
                 "insurers unavailable", "dashboard", "premium breakup")
        out = []
        for row in rows:
            reason = row["reason"].strip()
            if any(word in reason.lower() for word in noise):
                continue
            if len(reason) < 10:
                continue
            out.append(Failure(**row))
        return out

    def buy(self, insurer: str) -> None:
        """
        Click "Buy Now" on one insurer's card, then confirm the dialog.

        This is the doorway out of the safe part of the journey. Clicking Buy Now
        alone does nothing permanent - it opens a confirmation dialog - but
        confirming it moves into the KYC/proposal flow, which does reach the
        insurer. Callers must clear it with core.safety.allow("proposal", cfg).
        """
        # Look again for a few seconds before giving up.
        #
        # The quote list is still settling when we get here: insurers answer at
        # their own pace and every arrival re-sorts the list, which replaces card
        # elements wholesale. So a card found a moment ago can be a stale node by
        # the time we click it, and the run then reports "IFFCOTOKIO did not
        # return a quote" while listing IFFCOTOKIO among the insurers present -
        # a message that contradicts itself and sends someone hunting an insurer
        # outage that never happened.
        card = None
        waited = 0
        while waited < 15_000:
            card = self._card_for(insurer)
            if card is not None:
                break
            self.page.wait_for_timeout(1500)
            waited += 1500

        if card is None:
            present = ", ".join(q.insurer for q in self.quotes()) or "none"
            raise LookupError(
                f"No buyable quote card for {insurer!r} after {waited // 1000}s.\n"
                f"  Insurers present: {present}\n"
                f"  If {insurer} is in that list, it priced but its card has no "
                f"clickable Buy Now - which is a finding about the page, not "
                f"about the insurer."
            )

        self._raise_if_logged_out(insurer)
        card.locator(".buy-now-btn").first.click(timeout=20_000)
        self.page.wait_for_timeout(2500)

        # Buy Now usually opens the NCB confirmation box first
        # (tw-result.component.ts:602-612, 'ncb-confirmation-popup'). There is
        # no NCB to confirm on a Third Party policy, so there the app goes
        # straight on to KYC - waiting for a Confirm that never comes failed
        # every Third Party journey (2026-10-01). Either ending is fine.
        confirm = self.page.get_by_role("button", name="Confirm").first
        waited = 0
        while waited < 20_000:
            if not routes.on(self.page.url, "result"):
                return                      # already on its way to KYC
            try:
                if confirm.is_visible():
                    confirm.click()
                    self.page.wait_for_timeout(3000)
                    return
            except Exception:
                pass
            self.page.wait_for_timeout(500)
            waited += 500
        raise LookupError(
            f"Pressed Buy Now on {insurer}, but neither the confirmation box nor "
            f"the KYC page appeared in 20s. Still on: {self.page.url}")

    def _raise_if_logged_out(self, insurer: str) -> None:
        """
        The app's "Login Required" box over the quote list, said plainly.

        Saarthi opens it whenever one of its OWN calls answers 401 (HTTP, or
        StatusCode 401 inside a 200 - core/interceptors/error.interceptor.ts),
        and it wipes the login as it does. On 2026-10-05 that call was the quote
        save the result page makes after the last insurer answers
        (api/v2/Client/MailQuotation on the buy API, :50251), answering
        "Invalid user id" - for bikes too. A bike usually escapes because Buy
        Now is pressed before its long list of insurers is done; a car's three
        insurers are done in seconds, so the box is already up.
        """
        try:
            showing = self.page.get_by_text("Login Required", exact=True).first.is_visible()
        except Exception:
            showing = False
        if showing:
            raise auth.SessionRejected(
                f"Cannot press Buy Now on {insurer}: the app has put up its "
                f"'Login Required' box and logged us out.\n"
                f"  It does that when one of its own API calls answers 401. Look in "
                f"this run's api-responses.log for StatusCode 401 - on 2026-10-05 it "
                f"was the quote save (api/v2/Client/MailQuotation, buy API :50251) "
                f"answering 'Invalid user id' to a saved token the quote API still "
                f"accepted.\n"
                f"  Not this insurer and not the test: a stale login. A fresh one "
                f"fixed it.")

    def _card_for(self, insurer: str):
        """
        Find the BUYABLE .plan-card for this insurer.

        Two things make the naive version wrong, and both bit us:

        1. The page renders empty placeholder .plan-card elements alongside the
           real ones. We saw four cards where one had no content at all.
        2. A card can carry the right logo and still have no Buy Now button -
           hidden, collapsed, or a template clone.

        Matching on the logo alone therefore picks a card you cannot click, and
        the failure looks like a mysterious 20-second timeout on a button that
        "should be there". So we require the card to be visible AND to actually
        have a Buy Now button before accepting it.
        """
        cards = self.page.locator(self.CARD)
        for i in range(cards.count()):
            card = cards.nth(i)
            try:
                button = card.locator(".buy-now-btn").first
                # The BUTTON must be visible, not merely present. The portal
                # renders the whole card list twice - once for desktop, once for
                # mobile - and hides one set with CSS. A hidden card still has a
                # Buy Now button in the DOM, so "does the button exist" happily
                # returns a card you can never click, and the run then waits out
                # a 20-second timeout on a button nobody can see.
                if not button.is_visible(timeout=800):
                    continue
                alts = card.locator("img").evaluate_all(
                    "els => els.map(e => e.alt).filter(Boolean)")
            except Exception:
                continue
            if any(insurer.upper() in (a or "").upper() for a in alts):
                return card
        return None

    def select_add_ons(self, names: list[str]) -> "QuoteListPage":
        """
        Tick add-ons, then wait for the prices to be recalculated.

        Add-ons live on this page rather than a separate screen, and changing one
        re-prices every card.
        """
        for name in names:
            if name not in ADD_ONS:
                raise ValueError(
                    f"Unknown add-on {name!r}. Available: {', '.join(ADD_ONS)}")
            self.page.get_by_role("checkbox", name=name, exact=True).first.check(
                timeout=15_000)
            self.page.wait_for_timeout(800)

        self.page.wait_for_timeout(6000)            # let the re-quote settle
        return self
