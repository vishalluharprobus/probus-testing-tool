"""
KYC by redirect - when the insurer does KYC on ITS OWN website.

HOW IT REALLY WORKS
-------------------
Read from the three code bases involved - Saarthi (the Angular app on :4200),
the MVC ProbusClientPortal (:50251) and InsureBridge (:53339):

  1. Proceed on our KYC screen calls /api/kyc/wb/validate. When the insurer's
     CKYC search cannot find the customer, it answers:
         CKYCStatus       "Fail"
         ReDirectionURL   OUR launch page  /KYCBridge?QuotationNo=..&KYCProposalNo=..
         ExtraParameter1  usually the insurer's own link (NATIONAL, SBI)
  2. The app saves the journey on the server, then asks "...we are redirect to
     you on insurance company portal" - Okay / Close. Okay sends the SAME tab
     to our /KYCBridge, which fetches the insurer's address from the server
     and forwards the browser there. (NIVABUPA and UNIVERSALSOMPO open a new
     tab instead, with no question.)
  3. The customer does KYC on the insurer's site - OTP, DigiLocker, selfie.
  4. The insurer sends them to OUR /KYCBridge/{Insurer}KYCResponse. That
     address is fixed on the SERVER - usually test.probusinsurance.com, even
     on a local run. Our server records the result, then /KYC/Success moves
     on to the proposal (Success or Pending) or shows "KYC Failed".
  5. The proposal reloads the journey from the server by quotation number.

So a redirect is a round trip, and each leg can break on its own.

WHAT EACH MODE TESTS
--------------------
  stop     steps 1-2, automatically: the answer, our launch page, the
           insurer's page actually loading.
  assist   steps 1-5, with a PERSON doing step 3. The tool types only what our
           own KYC call already sent (PAN, DOB, mobile, email, pincode, name),
           never an OTP, captcha or Aadhaar number, never presses a button on
           the insurer's site, and waits for the person to finish.
  abandon  the customer gives up at step 3 and goes back to our KYC screen.
           Nobody did KYC, so the portal must NOT move them on to the proposal.
           This is the negative test, and the cheapest one to run.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page

from core import ui
from data.customer import Customer
from pages import routes

KYC_SCREEN = "/two-wheeler/kyc-insurance"
SAARTHI_PROPOSAL = "/two-wheeler/proposal"
BRIDGE = "/kycbridge"             # launch page AND the insurer's way back in
SUCCESS_PAGE = "/kyc/success"     # our server's "KYC answer received" page
KYC_FAILED = re.compile(r"kyc\s*(verification\s*)?(has\s*)?failed", re.IGNORECASE)

# The key that carries our launch page. Matched case-insensitively, because the
# spelling is the developers' ("ReDirectionURL") and may be tidied one day.
LAUNCH_KEYS = ("redirectionurl", "redirecturl", "kycurl", "kycredirecturl")

# Keys that can carry the insurer's own link. NATIONAL and SBI use
# ExtraParameter1. A logo URL in the same answer is not a handover, which is
# why "any external link" is not good enough.
LINK_KEY = re.compile(r"url|link|redirect|extraparameter|kyc", re.IGNORECASE)
NOT_A_PAGE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|pdf)(\?|$)",
                        re.IGNORECASE)

# What an insurer page that did NOT load properly tends to say.
BROKEN_PAGE = re.compile(
    r"link (has )?expired|invalid (link|request|token|url)|session (has )?expired|"
    r"token expired|something went wrong|page not found|\b404\b|access denied|"
    r"unauthori[sz]ed|forbidden|bad request|internal server error|"
    r"service unavailable|this site can.t be reached",
    re.IGNORECASE)

# Boxes the tool must never type into on somebody else's site. An OTP and a
# captcha are the person's job by definition, and the Aadhaar number is a
# real government ID that stays with the person who owns it.
NEVER_FILL = re.compile(
    r"otp|one.?time|captcha|aadh|\buid\b|\bvid\b|password|passcode|mpin|"
    r"\bpin\b(?!.?code)",
    re.IGNORECASE)


@dataclass
class Handover:
    """What the server told the portal to do - read off the wire, not the screen."""
    ckyc_status: str = ""
    launch_link: str = ""        # our /KYCBridge page that forwards to the insurer
    insurer_link: str = ""       # the insurer's own page, when the answer names it
    link_field: str = ""         # which field carried it, e.g. ExtraParameter1
    kyc_proposal_no: str = ""
    quotation_no: str = ""
    endpoint: str = ""           # which API call said so

    @property
    def insurer_host(self) -> str:
        return _host_of(self.insurer_link)


@dataclass
class Check:
    """One line of the round-trip report. ok=None means 'for information'."""
    leg: str
    name: str
    ok: bool | None
    detail: str = ""

    def line(self) -> str:
        mark = {True: "PASS", False: "FAIL", None: "info"}[self.ok]
        return f"[{mark}] {self.name}" + (f" - {self.detail}" if self.detail else "")


@dataclass
class Landing:
    """Where the browser ended up after the insurer's leg."""
    # proposal | other-copy | kyc | kyc-failed | bounced | bridge | loading
    # | elsewhere | timeout
    kind: str
    url: str = ""
    message: str = ""
    tab: Page | None = None
    seconds: int = 0

    @property
    def resumable(self) -> bool:
        """Can the harness carry on into ITS proposal page from here?"""
        return (self.kind == "proposal" and self.tab is not None
                and routes.on(self.url, "proposal"))


@dataclass
class Verdict:
    code: int            # 0 pass, 1 our side broke, 5 stopped for an outside reason
    headline: str
    advice: list[str] = field(default_factory=list)


class KycRedirect:
    """
    Watches one buy journey for a KYC redirect and tests the round trip.

    Create it BEFORE Buy Now. The redirect is announced by an API answer during
    the KYC submit, and a listener attached afterwards never hears it.
    """

    # How long each waypoint may take before it counts as stuck. Class-level so
    # the offline rehearsal (tests/rehearse_kyc_redirect.py) can shorten them.
    BRIDGE_PATIENCE_MS = 45_000       # /KYCBridge or /KYC/Success moving on
    ELSEWHERE_PATIENCE_MS = 15_000    # an unexpected page of ours settling
    LOADING_PATIENCE_MS = 20_000      # a bare "Loading" screen
    KYC_SETTLE_S = 20                 # on the KYC screen: time to move on or not

    def __init__(self, context: BrowserContext, journey: Page, home_hosts):
        self.context = context
        self.journey = journey                   # the tab the journey runs in
        # Hosts that are THIS copy of the portal. Hostnames only, not ports:
        # the app (:4200) and the MVC portal with /KYCBridge (:50251) are both
        # "localhost" on a local run.
        self.home_hosts = {h.lower() for h in home_hosts if h}
        self.handover = Handover()
        self.checks: list[Check] = []
        self.trail: list[str] = []               # every main-frame URL, in order
        self.insurer_tab: Page | None = None
        self.opened_in = ""                      # "same tab" | "new tab"
        self.launch_seen = ""                    # our /KYCBridge, on the way out
        self.kyc_url = ""                        # our KYC screen, for "abandon"
        self.filled: list[str] = []
        self.landing: Landing | None = None      # set once leg 3 is judged
        self._insurer_documents: list[tuple[str, int]] = []
        self._watched: set[int] = set()

        context.on("response", self._on_response)
        context.on("page", self._watch_tab)
        for tab in context.pages:
            self._watch_tab(tab)

    # ------------------------------------------------------------ listening

    def _watch_tab(self, tab: Page) -> None:
        if id(tab) in self._watched:
            return
        self._watched.add(id(tab))
        tab.on("framenavigated",
               lambda frame, tab=tab: self._on_navigated(tab, frame))

    def _on_navigated(self, tab: Page, frame) -> None:
        try:
            if frame != tab.main_frame:
                return
            url = frame.url
            if not url.lower().startswith(("http://", "https://")):
                return
            if not self.trail or short_url(self.trail[-1]) != short_url(url):
                self.trail.append(url)
            path = urlparse(url).path.lower()
            if self.is_home(url) and routes.on(path, "kyc-insurance"):
                self.kyc_url = url
            if self.insurer_tab is None:
                # Our launch page, on the way OUT. The same /KYCBridge path is
                # also the way back in, so only what comes before the insurer
                # counts as the launch.
                if self.is_ours(url) and BRIDGE in path:
                    self.launch_seen = url
                elif self.is_insurer(url):
                    self._note_departure(tab)
        except Exception:
            pass

    def _on_response(self, response) -> None:
        """Read the handover off the wire. Never raises - evidence is not worth a run."""
        try:
            url = response.url
            # How the insurer's own pages answered. A 404 or 500 here is the
            # whole story of a broken handover, and the screen may show only a
            # blank page.
            if response.request.resource_type == "document" and self.is_insurer(url):
                self._insurer_documents.append((_host_of(url), response.status))
                return
            low = url.lower()
            if "/api/" not in low or "kyc" not in low or not self.is_ours(url):
                return
            self._read_handover(json.loads(response.text() or "null"), url)
        except Exception:
            pass

    def _read_handover(self, body, url: str) -> None:
        data = body.get("Response") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return

        status = str(data.get("CKYCStatus") or "")
        launch, direct, named = "", "", []
        for path, link in _links_in(data):
            key = path.rsplit(".", 1)[-1].lower()
            if key in LAUNCH_KEYS:
                # Usually our /KYCBridge. ORIENTAL puts the insurer's page here
                # directly, and then there is no launch page at all.
                if self.is_ours(link):
                    launch = launch or link
                else:
                    direct = direct or link
            elif (self.is_insurer(link) and LINK_KEY.search(key)
                  and not NOT_A_PAGE.search(link)):
                named.append((path, link))

        # Only an answer that says something may overwrite an earlier one.
        # SaveKYCDeatils echoes the handover, and must never blank what the
        # validate answer already told us.
        if not (status or launch or direct or named):
            return
        h = self.handover
        h.ckyc_status = status or h.ckyc_status
        h.launch_link = launch or h.launch_link
        if named or direct:
            h.link_field, h.insurer_link = named[0] if named else ("ReDirectionURL", direct)
            h.endpoint = url.split("?")[0].rsplit("/api/", 1)[-1]
        h.kyc_proposal_no = str(data.get("KycProposalNumber") or h.kyc_proposal_no or "")
        h.quotation_no = str(data.get("QuotationNumber") or h.quotation_no or "")

    # ---------------------------------------------------------------- hosts

    def is_home(self, url: str) -> bool:
        """THIS copy of the portal."""
        return _host_of(url) in self.home_hosts

    def is_ours(self, url: str) -> bool:
        """Any copy of the portal - this one, or another probusinsurance.com."""
        host = _host_of(url)
        return bool(host) and (host in self.home_hosts or _is_probus(host))

    def is_insurer(self, url: str) -> bool:
        """A real web page that is not ours - about:blank and data: are not."""
        return (url.lower().startswith(("http://", "https://"))
                and bool(_host_of(url)) and not self.is_ours(url))

    @property
    def expected(self) -> bool:
        """Did the server announce a redirect, whether or not the browser went?"""
        h = self.handover
        return bool(h.launch_link or h.insurer_link) and h.ckyc_status.lower() != "success"

    def _open_tabs(self) -> list[Page]:
        return [t for t in self.context.pages if not t.is_closed()]

    def _note_departure(self, tab: Page) -> None:
        self.insurer_tab = tab
        self.opened_in = "same tab" if tab is self.journey else "new tab"

    def _pause(self, ms: int) -> None:
        tabs = self._open_tabs()
        if tabs:
            tabs[0].wait_for_timeout(ms)

    # --------------------------------------------------------------- leg 1

    def wait_for_departure(self, timeout_ms: int = 30_000) -> Page | None:
        """The tab that is on the insurer's site - same tab or a new one."""
        waited = 0
        while True:
            if self.insurer_tab is not None and not self.insurer_tab.is_closed():
                return self.insurer_tab
            for tab in self._open_tabs():
                if self.is_insurer(tab.url):
                    self._note_departure(tab)
                    return tab
            if waited >= timeout_ms:
                return None
            self._pause(500)
            waited += 500

    def check_handover(self, tab: Page | None, expected_hosts=()) -> None:
        """Leg 1: did the server hand over properly, and did the browser follow?"""
        h = self.handover
        leg = "1 handover"

        if h.ckyc_status:
            self._add(leg, "insurer's CKYC search", None,
                      f"{h.ckyc_status} - the central KYC registry could not "
                      f"confirm the customer, so the insurer's own KYC takes over"
                      if h.ckyc_status.lower() == "fail" else h.ckyc_status)

        # A launch link seen only in the browser still counts - the answer's
        # body is not always readable once the page has moved on.
        launch = h.launch_link or self.launch_seen
        direct = h.link_field == "ReDirectionURL"
        self._add(leg, "server gave the way out", bool(launch or direct),
                  short_url(launch) if launch else
                  f"straight to the insurer ({h.insurer_host})" if direct else
                  "no ReDirectionURL in the KYC answer and no launch page seen")
        if launch:
            home = self.is_home(launch)
            self._add(leg, "launch page is on THIS portal", home,
                      _host_of(launch) if home else
                      f"{_host_of(launch)} - but this run is on "
                      f"{', '.join(sorted(self.home_hosts))}, so the customer "
                      f"would leave this environment for a different one")
            if h.quotation_no:
                carried = h.quotation_no in launch
                self._add(leg, "launch page carries this quotation", carried,
                          h.quotation_no if carried else
                          f"{h.quotation_no} is not in it - the server cannot "
                          f"tell which journey is going to the insurer")

        if tab is None:
            self._add(leg, "browser reached the insurer", False,
                      "the server chose a redirect, but no tab ever left our "
                      "portal - the customer is stuck on the KYC screen"
                      if self.expected else "no tab left our portal")
            return
        if self.insurer_tab is None:
            self._note_departure(tab)

        first = next((u for u in self.trail if self.is_insurer(u)), tab.url)
        went = _host_of(first)
        via = " via our launch page" if self.launch_seen else ""
        self._add(leg, "browser reached the insurer", True,
                  f"{went}, in the {self.opened_in}{via}")
        if h.insurer_host and h.insurer_host != went:
            # Not a failure on its own: SBI's link lives in the database, not
            # in the answer, and some insurers bounce through a login host.
            self._add(leg, "insurer host vs the one in the answer", None,
                      f"answer named {h.insurer_host}, browser went to {went}")
        # config/insurers.py records where this insurer's KYC lives. Being
        # sent elsewhere is what that list exists to catch - a changed vendor,
        # or a UAT build pointing at the wrong insurer.
        if expected_hosts:
            known = went in expected_hosts
            self._add(leg, "insurer page is the one on record", known,
                      went if known else f"{went} - config/insurers.py expects "
                                         f"{', '.join(expected_hosts)}")

    # --------------------------------------------------------------- leg 2

    def inspect_insurer_page(self, tab: Page, settle_ms: int = 6000) -> dict:
        """Did the insurer's page actually load, and what does it ask for?"""
        leg = "2 insurer"
        try:
            tab.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        # These are single-page apps: the document arrives first and the form
        # renders a few seconds later. Judging the empty shell would call every
        # one of them broken.
        tab.wait_for_timeout(settle_ms)

        seen = {"title": "", "text": "", "inputs": 0, "otp": False, "captcha": False}
        try:
            seen = tab.evaluate(INSPECT_JS)
        except Exception:
            pass

        bad = [f"{host} -> HTTP {status}"
               for host, status in self._insurer_documents if status >= 400]
        broken = BROKEN_PAGE.search(seen.get("text", "") or "")
        empty = len((seen.get("text") or "").strip()) < 15 and not seen.get("inputs")
        why = ("; ".join(bad) if bad
               else f"it says '{broken.group(0)}'" if broken
               else "the page is blank" if empty
               else f"'{seen.get('title') or _host_of(tab.url)}', "
                    f"{seen.get('inputs', 0)} field(s) on screen")
        self._add(leg, "insurer's KYC page loaded",
                  not bad and not broken and not empty, why)

        asks = [label for label, flag in (("an OTP", seen.get("otp")),
                                          ("a captcha", seen.get("captcha"))) if flag]
        if asks:
            self._add(leg, "needs a person for", None, " and ".join(asks))
        return seen

    def prefill(self, tab: Page, who: Customer) -> list[str]:
        """
        Type what our own KYC call already sent into the insurer's form.

        Never clicks, never overwrites a box that has a value, and refuses
        OTP, captcha and Aadhaar boxes outright (NEVER_FILL). Frames are
        searched too, because hosted KYC forms often sit inside one.
        """
        filled: list[str] = []
        for frame in tab.frames:
            try:
                boxes = frame.evaluate(MARK_FIELDS_JS)
            except Exception:
                continue
            for box in boxes:
                value = _value_for(box["hint"], box["type"], who)
                if not value:
                    continue
                target = frame.locator(f'[data-harness-ext="{box["index"]}"]').first
                try:
                    target.fill(value, timeout=4000)
                    if target.input_value(timeout=1500).strip():
                        filled.append(f"{box['label'] or 'a box'} = {value}")
                except Exception:
                    continue
        self.filled += filled
        return filled

    def banner(self, tab: Page, text: str) -> None:
        """A strip across the bottom of the insurer's page. Never blocks a click."""
        try:
            if self.is_insurer(tab.url):
                tab.evaluate(BANNER_JS, text)
        except Exception:
            pass

    def assist(self, tab: Page, who: Customer, minutes: int) -> Landing:
        """
        Leg 2 with a person: show a banner and wait for them to be sent back.

        Call prefill() first, so the caller can tell the person what was typed
        for them. Later insurer pages are pre-filled as they appear.
        """
        return self.wait_for_return(
            minutes * 60_000, who=who,
            banner_text="PROBUS TEST TOOL - please finish KYC on this page "
                        "(OTP, consent, selfie). Do not close the browser: the "
                        "tool notices by itself when you are sent back.")

    def abandon(self, fallback_kyc_url: str) -> Landing:
        """
        The customer gives up on the insurer's page and returns to our KYC screen.

        Done by opening our KYC screen again rather than pressing Back: Back
        from the insurer lands on our launch page, which simply forwards to
        the insurer again. Nothing is typed or clicked on the insurer's site.
        """
        screen = self.journey
        if self.opened_in == "new tab" and self.insurer_tab is not None:
            try:
                self.insurer_tab.close()
            except Exception:
                pass
        elif self.insurer_tab is not None:
            screen = self.insurer_tab
        try:
            screen.goto(self.kyc_url or fallback_kyc_url,
                        wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass                    # where it ends up is what gets judged
        return self._watch_screen(screen, 0)

    # --------------------------------------------------------------- leg 3

    def wait_for_return(self, timeout_ms: int, banner_text: str = "",
                        who: Customer | None = None) -> Landing:
        """
        Watch every tab until one is back on our portal AND has settled.

        Settled matters: /KYCBridge/{Insurer}KYCResponse and /KYC/Success are
        waypoints, not destinations. Arriving there proves only that the
        insurer sent the customer back; the verdict comes from where they
        forward to - or from the fact that they never do.
        """
        waited, step = 0, 1000
        since: dict[str, int] = {}
        prefilled = {self.insurer_tab.url} if (who and self.insurer_tab) else set()

        while waited < timeout_ms:
            for tab in self._open_tabs():
                url = tab.url
                if not self.is_ours(url):
                    continue
                path = urlparse(url).path.lower()
                # The tab left behind when the insurer opened in a NEW tab. It
                # sits on the KYC screen throughout, so it only counts once it
                # moves on by itself.
                if (tab is self.journey and self.opened_in == "new tab"
                        and "proposal" not in path):
                    continue

                if "proposal" in path:
                    return self._landed("proposal" if self.is_home(url)
                                        else "other-copy", tab, waited)
                if routes.on(path, "kyc-insurance"):
                    return self._watch_screen(tab, waited)
                if SUCCESS_PAGE in path and KYC_FAILED.search(_body_excerpt(tab, 600)):
                    return self._landed("kyc-failed", tab, waited)

                kind = "bridge" if (BRIDGE in path or SUCCESS_PAGE in path) else "elsewhere"
                since.setdefault(kind, waited)
                if (ui.stuck_loading(tab)
                        and waited - since[kind] >= self.LOADING_PATIENCE_MS):
                    return self._landed("loading", tab, waited)
                patience = (self.BRIDGE_PATIENCE_MS if kind == "bridge"
                            else self.ELSEWHERE_PATIENCE_MS)
                if waited - since[kind] >= patience:
                    return self._landed(kind, tab, waited)

            tab = self.insurer_tab
            if (waited % 5000 == 0 and tab is not None and not tab.is_closed()
                    and self.is_insurer(tab.url)):
                # Multi-step insurer flows load a new page per step. Each one
                # gets the known boxes filled ONCE, so a box the person clears
                # on purpose stays cleared.
                if who is not None and tab.url not in prefilled:
                    prefilled.add(tab.url)
                    self.prefill(tab, who)
                if banner_text:
                    left = max(0, (timeout_ms - waited) // 60_000)
                    self.banner(tab, f"{banner_text}  ({left} min left)")
            self._pause(step)
            waited += step

        return Landing("timeout", seconds=waited // 1000)

    def _watch_screen(self, tab: Page, waited: int) -> Landing:
        """
        On (or back on) our KYC screen: does it move on, and what does it say?

        Read-only on purpose. The redirect question's "Okay" button is exactly
        what a dismiss helper would press, and pressing it here would launch
        the customer to the insurer again.
        """
        from pages.kyc import KycPage       # kyc.py does not import this module
        screen = KycPage(tab)
        message = ""
        for second in range(self.KYC_SETTLE_S):
            try:
                url = tab.url
            except Exception:
                break
            if "proposal" in urlparse(url).path.lower():
                return self._landed("proposal" if self.is_home(url) else "other-copy",
                                    tab, waited + second * 1000, message)
            if self.is_insurer(url):
                return self._landed("bounced", tab, waited + second * 1000, message)
            message = screen.dialog_text() or message
            tab.wait_for_timeout(1000)
        waited += self.KYC_SETTLE_S * 1000
        if ui.stuck_loading(tab):
            return self._landed("loading", tab, waited)
        if not routes.on(tab.url, "kyc-insurance"):
            return self._landed("elsewhere", tab, waited)
        return self._landed("kyc", tab, waited, message or _toast(tab))

    def _landed(self, kind: str, tab: Page, waited: int, message: str = "") -> Landing:
        if not message and kind != "proposal":
            message = _toast(tab) or _body_excerpt(tab)
        return Landing(kind, tab.url, message, tab, waited // 1000)

    def check_landing(self, landing: Landing, mode: str) -> Verdict:
        """Leg 3: judge where the browser ended up, for the mode that ran."""
        self.landing = landing
        leg = "3 way back" if mode == "assist" else "3 give up"
        where = short_url(landing.url) if landing.url else "(nowhere)"
        said = f" - the page says: \"{landing.message[:160]}\"" if landing.message else ""

        if landing.kind == "timeout":
            self._add(leg, "customer came back to our portal", False,
                      f"no tab returned in {landing.seconds}s")
            return Verdict(5, "Nobody finished the insurer's KYC page in time.",
                           ["Run again with more time: --kyc-wait 20",
                            "Or test the give-up path alone: --kyc-redirect abandon"])

        if mode == "assist":
            # The first page of ours after the insurer: the insurer's way back
            # in, which the browser never sees in any API answer.
            out = next((i for i, u in enumerate(self.trail) if self.is_insurer(u)), 0)
            back = next((u for u in self.trail[out:] if self.is_ours(u)), landing.url)
            self._add(leg, "insurer sent the customer back to us", True,
                      f"{short_url(back)}, after {landing.seconds}s")

        if landing.tab is not None and landing.kind in ("proposal", "kyc"):
            q = self.handover.quotation_no
            if q:
                kept = _quotation_on(landing.tab, q)
                self._add(leg, "same journey (quotation kept)", kept,
                          q if kept else f"{q} is nowhere on the page, in the "
                                         f"address or in storage - a fresh "
                                         f"journey may have started")

        if landing.kind == "loading":
            self._add(leg, "app usable afterwards", False, f"stuck on 'Loading' at {where}")
            return Verdict(1, "The app hangs on 'Loading' after the round trip.",
                           ["Most likely the login token expired while the "
                            "customer was on the insurer's site. Real customers "
                            "take minutes there too - so a token that dies in a "
                            "few minutes is shorter than a KYC round trip."])
        if landing.kind == "bridge":
            self._add(leg, "our KYCBridge / KYC Success page moved on", False,
                      f"still on {where} after "
                      f"{self.BRIDGE_PATIENCE_MS // 1000}s{said}")
            return Verdict(1, "Our return page received the customer and never "
                              "forwarded them anywhere.",
                           ["For SBI this is the known shape of a not-completed "
                            "KYC: the server sets no ResponseURL, so the page "
                            "spins for ever with no message (SBIKYCResponse.cs).",
                            f"Server logs: look for KYCProposalNo "
                            f"{self.handover.kyc_proposal_no or '(unknown)'}"])
        if landing.kind == "elsewhere":
            self._add(leg, "landed on a journey screen", False, f"{where}{said}")
            return Verdict(1, f"The customer ended up on {where}, which is "
                              f"neither the proposal nor the KYC screen.",
                           ["If that is a login page, the session did not "
                            "survive the round trip."])

        if mode == "abandon":
            if landing.kind in ("kyc", "bounced"):
                self._add(leg, "unfinished KYC NOT treated as done", True,
                          "stayed on the KYC screen" if landing.kind == "kyc"
                          else f"sent back out to {_host_of(landing.url)}")
                return Verdict(0, "Correct: a customer who gave up on the "
                                  "insurer's KYC is not let into the proposal.")
            self._add(leg, "unfinished KYC NOT treated as done", False,
                      f"the portal moved on to {where} although nobody did KYC")
            return Verdict(1, "FINDING: the portal opened the proposal for a "
                              "customer who never finished KYC.",
                           ["On return the KYC screen asks the server "
                            "(api/kyc/CheckInsuranceKYCStatus) whether KYC is "
                            "done; it answered yes. Check that flag for this "
                            f"quotation: {self.handover.quotation_no or '(unknown)'}",
                            "Raise it with the team - it lets a customer skip KYC."])

        # assist: a person did the insurer's KYC.
        if landing.kind == "proposal":
            self._add(leg, "verified customer reached the proposal", True, where)
            return Verdict(0, "Round trip complete: KYC done on the insurer's "
                              "site, and the customer is back on our proposal.")
        if landing.kind == "other-copy":
            self._add(leg, "came back to THIS copy of the portal", None,
                      f"{_host_of(landing.url)}, not "
                      f"{', '.join(sorted(self.home_hosts))}")
            return Verdict(5, f"The insurer sent the customer back to "
                              f"{_host_of(landing.url)}, not to this copy of "
                              f"the portal.",
                           ["Expected on a LOCAL run: the insurer's way back is "
                            "fixed on the server (test.probusinsurance.com), "
                            "and the journey's session lives only on this copy.",
                            "For a full round trip, run against the test site: "
                            "--target testsite"])
        if landing.kind == "kyc-failed":
            self._add(leg, "verified customer reached the proposal", False,
                      f"our server says KYC failed{said}")
            return Verdict(5, "The insurer did not approve the KYC, and our "
                              "portal said so.",
                           ["The way back worked; the KYC itself was refused. "
                            "Often the name or DOB on the Aadhaar does not match "
                            "what was typed on our form."])
        self._add(leg, "verified customer reached the proposal", False,
                  f"back on the KYC screen{said}" if landing.kind == "kyc"
                  else f"sent back out to {_host_of(landing.url)}{said}")
        return Verdict(5, "The customer came back, but the portal does not "
                          "consider the KYC finished.",
                       ["Finish every step on the insurer's page, including any "
                        "final Submit / Done button, then wait for it to send "
                        "you back by itself."])

    # ------------------------------------------------------------- report

    def _add(self, leg: str, name: str, ok: bool | None, detail: str = "") -> None:
        self.checks.append(Check(leg, name, ok, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]

    def summary(self) -> dict:
        """What is worth remembering about this insurer's redirect."""
        h = self.handover
        insurer = next((u for u in self.trail if self.is_insurer(u)), "")
        launch = h.launch_link or self.launch_seen
        return {
            "link_host": _host_of(insurer) or h.insurer_host,
            "launch_path": urlparse(launch).path if launch else "",
            "opens_in": self.opened_in,
            "trail": [short_url(u) for u in self.trail[-6:]],
        }


# --------------------------------------------------------------------- JS

INSPECT_JS = r"""
() => {
  const text = ((document.body && document.body.innerText) || '')
                 .replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect();
                          return r.width > 0 && r.height > 0; };
  const inputs = [...document.querySelectorAll('input, textarea, select')]
      .filter(el => visible(el) && el.type !== 'hidden').length;
  const captcha = !!document.querySelector(
      '.g-recaptcha, iframe[src*="recaptcha"], iframe[src*="hcaptcha"], ' +
      '[id*="captcha" i], [class*="captcha" i], img[src*="captcha" i]');
  return {
    title: document.title || '',
    text: text.slice(0, 800),
    inputs,
    otp: /\botp\b|one.?time password/i.test(text),
    captcha: captcha || /captcha/i.test(text),
  };
}
"""

# Mark every EMPTY, typeable box and describe it, so Python decides what (if
# anything) belongs in it. The typing itself is a real Playwright event:
# script-set values are ignored by most frameworks - this project's oldest
# lesson.
MARK_FIELDS_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden';
  };
  const SKIP = ['hidden', 'submit', 'button', 'checkbox', 'radio', 'file',
                'password', 'image', 'reset', 'range', 'color'];
  document.querySelectorAll('[data-harness-ext]')
          .forEach(el => el.removeAttribute('data-harness-ext'));
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll('input, textarea')) {
    const type = (el.type || 'text').toLowerCase();
    if (SKIP.includes(type) || !visible(el) || el.disabled || el.readOnly) continue;
    if ((el.value || '').trim()) continue;          // never overwrite anything
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = (l.innerText || '').trim();
    }
    const wrap = el.closest('label, .form-group, .field, [class*="form-field"], ' +
                            '[class*="FormControl"], [class*="input"]');
    const around = wrap && (wrap.innerText || '').length < 120 ? wrap.innerText : '';
    const hint = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                  el.getAttribute('autocomplete'), label, around]
        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
    el.setAttribute('data-harness-ext', String(i));
    out.push({index: i, type, hint: hint.slice(0, 160),
              label: (label || around || el.placeholder
                      || el.getAttribute('aria-label') || el.name || '')
                     .replace(/\s+/g, ' ').trim().slice(0, 40)});
    i++;
  }
  return out;
}
"""

BANNER_JS = r"""
(text) => {
  let b = document.getElementById('probus-harness-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'probus-harness-banner';
    b.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;'
      + 'background:#1b5e20;color:#fff;padding:10px 16px;pointer-events:none;'
      + 'font:600 14px/1.4 "Segoe UI",Arial,sans-serif;'
      + 'box-shadow:0 -2px 8px rgba(0,0,0,.35)';
    document.documentElement.appendChild(b);
  }
  b.textContent = text;
}
"""

QUOTATION_JS = r"""
(q) => {
  const inStore = s => { try { return Object.values(s).some(v => String(v).includes(q)); }
                         catch (e) { return false; } };
  return location.href.includes(q)
      || ((document.body && document.body.innerText) || '').includes(q)
      || inStore(sessionStorage) || inStore(localStorage);
}
"""


# ---------------------------------------------------------------- helpers

def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_probus(host: str) -> bool:
    return host == "probusinsurance.com" or host.endswith(".probusinsurance.com")


def short_url(url: str, limit: int = 90) -> str:
    """
    A URL fit for a report and for the notebook.

    Query values and long path pieces are dropped: the insurer links carry
    login tokens (NATIONAL's path is a signed JWT), and those have no
    business in a log file.
    """
    parsed = urlparse(url)
    path = "/".join(p if len(p) <= 24 else ".." for p in parsed.path.split("/"))
    text = f"{parsed.netloc}{path}"
    if parsed.query:
        text += "?" + "&".join(p.split("=")[0] + "=.." for p in parsed.query.split("&"))
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _links_in(node, path: str = "") -> list[tuple[str, str]]:
    """Every http(s) string in a JSON answer, with the key path it sat under."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _links_in(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, item in enumerate(node[:20]):
            found += _links_in(item, f"{path}[{i}]")
    elif isinstance(node, str) and node.lower().startswith(("http://", "https://")):
        found.append((path, node))
    return found


def _dob(who: Customer, fmt_hint: str, input_type: str) -> str:
    try:
        born = datetime.strptime(who.dob_kyc, "%d %b %Y")
    except ValueError:
        return ""
    if input_type == "date" or "yyyy-mm-dd" in fmt_hint:
        return born.strftime("%Y-%m-%d")
    if "mm/dd" in fmt_hint:
        return born.strftime("%m/%d/%Y")
    if "dd-mm" in fmt_hint:
        return born.strftime("%d-%m-%Y")
    return born.strftime("%d/%m/%Y")


def _value_for(hint: str, input_type: str, who: Customer) -> str:
    """What, if anything, belongs in a box described by `hint`."""
    h = hint.lower()
    if NEVER_FILL.search(h):
        return ""
    squeezed = h.replace(" ", "").replace("_", "").replace("-", "")
    pan = re.search(r"\bpan(?![a-z])|\bpan(card|no|num)", h) or "pancard" in squeezed
    # "Name as per PAN" is a NAME box that mentions the PAN. Only a box that
    # also asks for a number, or does not mention a name at all, gets the PAN.
    if pan and ("name" not in h or re.search(r"\bno\b|number|num\b", h)):
        return who.pan
    if "birth" in h or re.search(r"\bdob\b", h) or input_type == "date":
        return _dob(who, h, input_type)
    if "email" in h or input_type == "email":
        return who.email
    if any(w in h for w in ("mobile", "phone", "contact")) or input_type == "tel":
        return who.mobile
    if any(w in squeezed for w in ("pincode", "postal", "zipcode")):
        return who.pincode
    if any(w in h for w in ("father", "mother", "spouse", "nominee", "user name",
                            "username", "company", "organi")):
        return ""
    full = " ".join(p for p in (who.first_name, who.middle_name, who.last_name) if p)
    # Full name BEFORE the parts: "fullname" contains "lname", and checking
    # the parts first typed the surname into a full-name box.
    if any(w in squeezed for w in ("fullname", "nameasper", "customername",
                                   "applicantname")):
        return full
    if "firstname" in squeezed or "givenname" in squeezed or re.search(r"\bfname\b", h):
        return who.first_name
    if "middlename" in squeezed:
        return who.middle_name
    if "lastname" in squeezed or "surname" in squeezed or re.search(r"\blname\b", h):
        return who.last_name
    if "name" in h:
        return full
    return ""


def _toast(tab: Page) -> str:
    try:
        return " ".join(tab.locator(
            ".toast-message, .mat-mdc-snack-bar-label, .swal2-html-container, "
            "simple-snack-bar").first.inner_text(timeout=800).split())
    except Exception:
        return ""


def _body_excerpt(tab: Page, limit: int = 160) -> str:
    try:
        return " ".join(tab.inner_text("body", timeout=2000).split())[:limit]
    except Exception:
        return ""


def _quotation_on(tab: Page, quotation: str) -> bool:
    try:
        return bool(tab.evaluate(QUOTATION_JS, quotation))
    except Exception:
        return False
