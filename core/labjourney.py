"""
Take the insurer lab's quotes on through the buy journey, in the browser.

    Quote             the lab's own API call (run_insurer_lab.py) - not here
    KYC               open the quote, Buy Now, KYC verified
    Company Specific  the proposal form, then "Continue to Preview": the
                      CompanySpecificQuotation call - the insurer's own quote
                      for this customer and this vehicle
    Proposal          the OTP, then Proceed: SendCompanyProposal - the
                      proposal goes to the insurer
    Payment           the payment link, and the browser lands on the payment
                      page. NOTHING on that page is touched.

Every stage ends Success or Error, and an Error carries the reason the portal
or the insurer gave - read from the wire when it says more than the screen. A
stage after an Error is "Not reached"; one the run was not allowed or asked
to do is "Not run".

HOW ONE QUOTE IS OPENED
-----------------------
The portal's share link, /two-wheeler/result/<quotation number>, re-opens a
saved quotation exactly as it was quoted (tw-result.component.ts
getDetailsByQuotation). The lab's quotes come from the API, so nothing saved
them: the journey first saves the request the way the result page does on its
own (labclient.save_quotation - NoMail, nothing is sent to anybody), then
opens the link. Every OTHER insurer's quote call is blocked on the way, so a
journey asks one insurer, not fifteen.

The KYC, proposal and Preview steps are the same page objects that
run_proposal_test.py drives; this file only decides what each outcome MEANS
for the spreadsheet.
"""
from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

from config import insurers as kyc_styles
from config import settings
from core import auth, browser, kycnotes, safety, ui
from core.labclient import ApiReplay
from data.customer import DEFAULT as CUSTOMER
from pages import routes
from pages.kyc import KycPage
from pages.proposal import ProposalPage
from pages.quote_list import QuoteListPage

# (key, column heading), in journey order.
STAGES = (("quote", "Quote"), ("kyc", "KYC"),
          ("company_specific", "Company Specific"),
          ("proposal", "Proposal"), ("payment", "Payment"))
JOURNEY = tuple(key for key, _ in STAGES[1:])
NAMES = dict(STAGES)

SUCCESS, ERROR, NOT_REACHED, NOT_RUN = "Success", "Error", "Not reached", "Not run"

# A journey is about this long when it goes all the way - for the estimate.
MINUTES_EACH = 3
# How long an insurer may take over one call (KYC check, company-specific
# quote) before the journey calls it unanswered.
ANSWER_WAIT_S = 180

# Insurer answers that carry no words of their own, and what is known about
# them - added to the row's notes so nobody starts from a bare code.
KNOWN_CODES = {
    "NIC-PA-Validation-B5149": (
        "NATIONAL's own underwriting check (InsureMO policy/calculateWithUW) refused "
        "the proposal; InsureBridge passes on only the code. Worth checking first: "
        "TWNationalInsuranceRules.PrepareProposalModelNew (InsureBridge-UAT-Publish, "
        "line ~3812) sends PrevInsuranceCompanyBranchId = PreviousPolicyDetails."
        "InsurerCode - OUR insurer id (1 for BAJAJ) - where the quote builder "
        "(line ~927) sends NATIONAL's own branch code, spModel.PrevPolicyInsurerName. "
        "Seen on every NATIONAL renewal proposal on 2026-10-01, with two different "
        "addresses."),
}


def explain(reason: str) -> str:
    """What is known about an insurer's bare error code, or ''."""
    return next((text for code, text in KNOWN_CODES.items() if code in (reason or "")), "")


# Proven live 2026-10-01 (NATIONAL two-wheeler, quotation opened by its link).
SHARE_LINK_BUG = (
    "A quote opened from its shared link (/{page}/result/<quotation number> - the "
    "link customers get by WhatsApp or e-mail) cannot pass KYC. The KYC screen "
    "takes the customer type only from the screen-1 data in session (Saarthi "
    "shared/kyc-insurance/kyc-insurance.component.ts, two-wheeler branch: "
    "RequestOBj.CustomerType || OneClickRenewalData.CustomerType - the private-car "
    "branch also falls back to the quotation), so the KYC request is sent without "
    "CustomerType, and InsureBridge KYCController.KycValidateWeb then crashes on "
    "model.CustomerType.Equals(...). Seen: {seen}. The tool filled the customer "
    "type in for the rest of the run, so KYC, proposal and payment could still be "
    "tested.")


@dataclass
class Stage:
    result: str = NOT_RUN
    reason: str = ""


# ============================================================ verdicts (pure)

def quote_stage(status: str, reason: str) -> Stage:
    """The lab's quote answer as a stage: a price is Success, anything else Error."""
    if status == "priced":
        return Stage(SUCCESS)
    return Stage(ERROR, reason or f"no price ({status})")


def mark_not_run(stages: dict, why: str) -> None:
    """Fill every journey stage that has no result yet with Not run."""
    for key in JOURNEY:
        stages.setdefault(key, Stage(NOT_RUN, why))


def verdict(stages: dict) -> tuple[str, str]:
    """("Success" | "Failure", "<Stage>: <reason>" of the first stage that failed)."""
    for key, name in STAGES:
        stage = stages.get(key)
        if stage is not None and stage.result == ERROR:
            return "Failure", f"{name}: {stage.reason}" if stage.reason else name
    return "Success", ""


def short_reason(text: str, limit: int = 300) -> str:
    """
    One spreadsheet line from a page object's explanation.

    The page objects explain a stop over several lines - what happened, then
    "The portal said: ..." with the portal's own words. The first line and
    those words are what a reader needs; the rest is in the console.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return "no reason given"
    head = lines[0]
    for marker in ("The portal said:", "The app said:", "It asked:"):
        said = next((line.split(marker, 1)[1].strip() for line in lines
                     if marker in line), "")
        if said and said not in head:
            head = f"{head.rstrip('.:')}: {said}"
            break
    return head if len(head) <= limit else head[: limit - 3] + "..."


# ============================================================ the wire (pure)

def stage_of(url: str) -> str:
    """Which journey stage an API call answers for, or ''.

    Saarthi motor-routes.ts: CompanySpecificQuotation and SendCompanyProposal
    go to the quote API (Motor/TwoWheeler/...), CompanyPaymentLink to the buy
    API (api/two-wheeler/Proposal, api/privatecar/Proposal)."""
    path = urlparse(url).path.lower().rstrip("/")
    # The KYC submit (motor-routes.ts ValidateInsKyc): kyc/wb/validate, or
    # kyc/KycValidate on the test build.
    if path.endswith("/kyc/wb/validate") or path.endswith("/kyc/kycvalidate"):
        return "kyc"
    if path.endswith("/companyspecificquotation"):
        return "company_specific"
    if re.search(r"/motor/(twowheeler|privatecar)/proposal$", path):
        return "proposal"
    if re.search(r"/api/(two-wheeler|privatecar)/proposal$", path):
        return "payment"
    return ""


def answer_of(body: str) -> tuple[bool, str, dict]:
    """
    (ok, message, Response) for a company-specific or proposal answer.

    ok is the app's own test: no Error, no Message, and Response.Status
    "Success" (proposal-personal-details.component.ts:7781-7786) - or
    "UnderWriting", which the app carries on with
    (proposal-otp.component.ts:929-938). The message is the one it shows.
    """
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return False, f"the answer was not JSON: {body[:120]}", {}
    if not isinstance(data, dict):
        return False, f"unexpected answer: {str(data)[:120]}", {}
    response = data.get("Response")
    resp = response if isinstance(response, dict) else {}
    status = str(resp.get("Status") or "")
    ok = (not data.get("Error") and not data.get("Message") and response is not None
          and status in ("Success", "UnderWriting") and not resp.get("ErrorMessage"))
    message = str(resp.get("ErrorMessage") or data.get("Error")
                  or data.get("Message") or "").strip()
    if not ok and not message:
        message = f"Status={status or 'none'}, no message"
    return ok, ("" if ok else message), resp


def kyc_answer_of(body: str) -> tuple[bool, str, dict]:
    """
    (ok, message, Response) for the KYC submit, judged the way the KYC
    screen judges it (insurance-kyc.component.ts handleValidateResponse):
    ok means the insurer's check RAN - CKYCStatus then decides whether the
    customer goes on, uploads documents, or is sent to the insurer's site.
    """
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return False, f"the answer was not JSON: {body[:120]}", {}
    if not isinstance(data, dict):
        return False, f"unexpected answer: {str(data)[:120]}", {}
    response = data.get("Response")
    resp = response if isinstance(response, dict) else {}
    if str(data.get("StatusCode")) == "401" or data.get("Error") == "You are not authorized.":
        return False, "the portal says the login has expired", resp
    if str(data.get("StatusCode")) != "200" or not resp:
        return False, str(data.get("Error") or "KYC verification failed."), resp
    if resp.get("ErrorMessage"):
        return False, str(resp["ErrorMessage"]).strip(), resp
    if resp.get("Status") != "Success":
        return False, "KYC verification could not be completed.", resp
    return True, "", resp


def payment_answer_of(body: str) -> tuple[bool, str, dict]:
    """(ok, message, {}) for the payment-link answer: a link and no
    Error or Message (proposal-otp.component.ts:1393-1397)."""
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return False, f"the answer was not JSON: {body[:120]}", {}
    if not isinstance(data, dict):
        return False, f"unexpected answer: {str(data)[:120]}", {}
    ok = not data.get("Error") and not data.get("Message") and bool(data.get("Response"))
    message = str(data.get("Error") or data.get("Message") or "").strip()
    if not ok and not message:
        message = "no payment link in the answer"
    return ok, ("" if ok else message), {}


class Wire:
    """The journey's answers, as the app received them - and which are still
    on their way, because a slow insurer and a hung app both look like the
    portal's "Loading" screen, and only the wire can tell them apart."""

    def __init__(self):
        self.answers: dict[str, tuple[bool, str, dict]] = {}
        # Refusals in the shape ProposalPage.hidden_errors expects, so its
        # waits stop the moment the insurer says no.
        self.hidden: list[str] = []
        self.sent: dict[str, float] = {}       # stage -> when its call left
        self.took: dict[str, float] = {}       # stage -> seconds it took
        # Did the KYC submit carry a CustomerType? None until it is sent.
        self.kyc_customer_type: bool | None = None
        # Every POST to OUR APIs still waiting for its answer (id -> url):
        # core.ui asks this before calling a "Loading" screen stuck.
        self.hosts: set[str] = set()
        self.pending: dict[int, float] = {}

    def reset(self) -> None:
        self.answers.clear()
        self.hidden.clear()
        self.sent.clear()
        self.took.clear()
        self.kyc_customer_type = None
        self.pending.clear()

    def busy(self) -> bool:
        # A call that never comes back (its tab closed) stops counting
        # after the longest wait anyone would give it.
        now = time.monotonic()
        return any(now - sent < ui.BUSY_WAIT_S for sent in self.pending.values())

    def _ours(self, request) -> bool:
        try:
            return (request.method == "POST" and "/api/" in request.url.lower()
                    and (urlparse(request.url).hostname or "") in self.hosts)
        except Exception:
            return False

    def failure(self, stage: str) -> str:
        """The insurer's reason, if this stage's answer was a refusal."""
        ok, message, _ = self.answers.get(stage, (True, "", {}))
        return "" if ok else message

    def waiting_for(self, stage: str) -> bool:
        """Has this stage's call gone out and not come back yet?"""
        return stage in self.sent and stage not in self.answers

    def on_request(self, request) -> None:
        if self._ours(request):
            self.pending[id(request)] = time.monotonic()
        try:
            stage = stage_of(request.url)
            if stage and request.method == "POST":
                self.sent[stage] = time.monotonic()
                self.answers.pop(stage, None)
                if stage == "kyc":
                    sent = request.post_data_json or {}
                    self.kyc_customer_type = bool(isinstance(sent, dict)
                                                  and sent.get("CustomerType"))
        except Exception:
            pass

    def on_request_failed(self, request) -> None:
        self.pending.pop(id(request), None)
        try:
            stage = stage_of(request.url)
            if stage and request.method == "POST":
                self._answer(stage, (False, f"the call failed: {request.failure}", {}),
                             request.url)
        except Exception:
            pass

    def on_response(self, response) -> None:
        try:
            self.pending.pop(id(response.request), None)
        except Exception:
            pass
        try:
            if response.request.method != "POST":
                return
            stage = stage_of(response.url)
            if not stage:
                return
            body = response.text() or ""
            if response.status >= 400:
                answer = (False, f"HTTP {response.status}: {body[:160]}", {})
            elif stage == "payment":
                answer = payment_answer_of(body)
            elif stage == "kyc":
                answer = kyc_answer_of(body)
            else:
                answer = answer_of(body)
            self._answer(stage, answer, response.url)
        except Exception:
            pass                  # an unreadable answer is not worth a journey

    def _answer(self, stage: str, answer: tuple, url: str) -> None:
        self.answers[stage] = answer
        if stage in self.sent:
            self.took[stage] = round(time.monotonic() - self.sent[stage], 1)
        if not answer[0]:
            tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
            self.hidden.append(f"{tail}: {answer[1]}")


# ================================================================ a journey

@dataclass
class Journey:
    stages: dict = field(default_factory=dict)
    proposal_no: str = ""
    premium_on_screen: int | None = None
    payment_amount: float | None = None    # what the payment page asks for
    notes: list = field(default_factory=list)
    seconds: float = 0.0
    evidence: str = ""             # screenshot / trace of a failure
    redirected: str = ""           # the insurer's KYC site, if it sent us there

    def done(self, stage: str, note: str = "") -> None:
        self.stages[stage] = Stage(SUCCESS)
        if note:
            self.notes.append(note)

    def fail(self, stage: str, reason: str) -> None:
        self.stages[stage] = Stage(ERROR, reason)
        after = JOURNEY[JOURNEY.index(stage) + 1:]
        for key in after:
            self.stages.setdefault(key, Stage(NOT_REACHED))


class KycRedirected(LookupError):
    """The insurer sent the customer to its own KYC site - a person's job."""

    def __init__(self, insurer: str, host: str):
        super().__init__(_redirect_reason(insurer, host))
        self.host = host


class NeedsTestData(LookupError):
    """The journey needs details the test customer does not have - so the
    stage is Not run, never an invented value and never an Error."""


class LoggedOut(RuntimeError):
    """The browser's login ran out - log in again and repeat the journey."""


class Journeys:
    """
    One logged-in browser for the whole run, one journey per quote.

        with Journeys(cfg, product, insurer, insurer_codes, rto, api).open() as j:
            journey = j.drive(n, quotation, request, sub_product)
    """

    def __init__(self, cfg, product, insurer: str, insurer_codes, rto: str,
                 api: ApiReplay, headed: bool = True):
        self.cfg, self.product, self.insurer = cfg, product, insurer.upper()
        self.rto = rto
        self.api = api
        self.headed = headed
        # Every OTHER insurer's quote call is blocked on the result page.
        self.others = {str(c).lower() for c in insurer_codes
                       if c and not _same(c, insurer)}
        self.our_host = urlparse(cfg.base_url).hostname or ""
        self.api_hosts = {urlparse(u).hostname for u in (cfg.api_url, cfg.buy_url) if u}
        self.token = ""
        self.wire = Wire()
        self.context = None
        self.run_dir = None
        self.can_pay = safety.permits("payment", cfg)
        # Portal bugs this run proved, in words for the report.
        self.bugs: list[str] = []
        # Set once the shared-link KYC bug is proven (see SHARE_LINK_BUG).
        self.fill_customer_type = False

    # ---------------------------------------------------------------- browser
    @contextmanager
    def open(self):
        with browser.browser_session(self.cfg, headed=self.headed,
                                     trace=False) as (context, run_dir):
            self.context, self.run_dir = context, run_dir
            self.wire.hosts = self.api_hosts
            ui.in_flight = self.wire.busy
            context.route(re.compile(r"/api/motor/(twowheeler|privatecar)/[^/?]+$",
                                     re.IGNORECASE), self._block_others)
            context.on("response", self.wire.on_response)
            context.on("request", self.wire.on_request)
            context.on("requestfailed", self.wire.on_request_failed)
            context.on("request", self._borrow_token)
            try:
                self._log_in()
                yield self
            finally:
                ui.in_flight = None

    def _borrow_token(self, request) -> None:
        """
        The 'mob-token' header exactly as the app sends it to OUR APIs.

        Not localStorage: the app keeps 'buy-token' JSON-encoded, in quotes
        (Saarthi localstorage.service.ts setItem), and the buy API decrypts the
        header to find the user (BaseAPIController.GetUserId), so a token with
        its quotes still on is "not valid" - which is how the first live run
        lost every journey.
        """
        try:
            token = request.headers.get("mob-token")
            host = urlparse(request.url).hostname or ""
            if token and host in self.api_hosts:
                self.token = token
        except Exception:
            pass

    def _log_in(self) -> None:
        self.token = ""
        page = auth.log_in(self.context, self.cfg)
        safety.verify_environment(page, self.cfg)
        safety.allow("proposal", self.cfg)
        waited = 0
        while not self.token and waited < 15_000:
            page.wait_for_timeout(500)
            waited += 500
        if not self.token:
            try:
                self.token = page.evaluate(
                    "() => { const v = localStorage.getItem('buy-token');"
                    " try { return JSON.parse(v); } catch (e) { return v; } }") or ""
            except Exception:
                pass
        if self.token:
            self.api = ApiReplay({"mob-token": self.token})
        page.close()

    def _block_others(self, route) -> None:
        try:
            request = route.request
            tail = urlparse(request.url).path.rstrip("/").rsplit("/", 1)[-1].lower()
            if request.method == "POST" and tail in self.others:
                route.abort()
                return
        except Exception:
            pass
        route.continue_()

    # ---------------------------------------------------------------- journey
    def drive(self, n: int, quotation: str, request: dict, sub_product: str) -> Journey:
        """
        Run one journey.

        If KYC fails because the portal sent no customer type - the shared-link
        bug, SHARE_LINK_BUG - that is proven once, written down, and the same
        quote is driven again with the customer type filled in, as is every
        journey after it. Should the portal be fixed, the first journey passes
        KYC on its own and nothing is filled in.
        """
        j = self._attempt(n, quotation, request, sub_product)
        kyc = j.stages.get("kyc")
        # Proven only by an ANSWER: the KYC call came back refused and had
        # gone without a customer type. A KYC call that never came back
        # (a hung server) proves nothing.
        answered = self.wire.answers.get("kyc")
        if (not self.fill_customer_type and kyc is not None and kyc.result == ERROR
                and self.wire.kyc_customer_type is False
                and answered is not None and not answered[0]):
            self.bugs.append(SHARE_LINK_BUG.format(page=self.product.page,
                                                   seen=kyc.reason[:120]))
            self.fill_customer_type = True
            print(f"      PORTAL BUG: the KYC request went without a customer type "
                  f"({kyc.reason[:70]})")
            print("      - written down for the report; driving the same quote again "
                  "with the customer type filled in ...")
            first = j
            j = self._attempt(n, quotation, request, sub_product)
            j.notes.insert(0, "first try failed KYC - the portal sent no customer "
                              "type (portal bug, see Summary); passed this time with "
                              "it filled in")
            j.seconds = round(j.seconds + first.seconds, 1)
        if j.redirected:
            # Redirect KYC is a fallback the insurer takes when its registry
            # search misses, and the same customer is found on the next try
            # more often than not (NATIONAL: 17 inline, 3 redirected in 20).
            # One more try; if it redirects again, that is the answer.
            print(f"      {self.insurer} sent KYC to its own site ({j.redirected}) - "
                  f"its registry search missed; trying KYC once more ...")
            first = j
            j = self._attempt(n, quotation, request, sub_product)
            passed = j.stages.get("kyc", Stage()).result == SUCCESS
            j.notes.insert(0, f"first KYC try was sent to {first.redirected} (the "
                              f"insurer's registry search missed); "
                              f"{'passed' if passed else 'tried again'} on the second try")
            j.seconds = round(j.seconds + first.seconds, 1)
        return j

    def _attempt(self, n, quotation, request, sub_product) -> Journey:
        """One journey; a login that ran out is renewed and tried once more."""
        for attempt in (1, 2):
            try:
                return self._drive_once(n, quotation, request, sub_product)
            except LoggedOut:
                if attempt == 2:
                    break
                print("      the login ran out - logging in again ...")
                self._log_in()
        j = Journey()
        j.fail("kyc", "the portal kept asking for a login")
        return j

    def _drive_once(self, n, quotation, request, sub_product) -> Journey:
        j = Journey()
        started = time.monotonic()
        self.wire.reset()
        page = self.context.new_page()
        try:
            self.context.tracing.start(screenshots=True, snapshots=True)
        except Exception:
            pass                  # no trace for this one is not worth stopping for
        stage = "kyc"
        try:
            self._buy(page, j, quotation, request, sub_product)
            j.done("kyc", self._kyc(page))

            stage = "company_specific"
            proposal = self._company_specific(page)
            j.done("company_specific")

            if not self.can_pay:
                why = (f"stopped before Proceed - write_ceiling is "
                       f"'{self.cfg.write_ceiling}', and Proceed sends the proposal "
                       f"(set it to \"payment\" in config/settings.local.json)")
                j.stages["proposal"] = Stage(NOT_RUN, why)
                j.stages["payment"] = Stage(NOT_RUN, why)
                return j

            stage = "proposal"
            proposal.enter_otp(settings.DEV_OTP)
            safety.allow("payment", self.cfg)
            try:
                proposal.proceed_to_payment(settings.DEV_OTP)
            finally:
                # The proposal call answers before the payment link is asked
                # for: an accepted proposal moves any failure on to Payment.
                ok, _, resp = self.wire.answers.get("proposal", (False, "", {}))
                if ok:
                    # ZUNO answers with no ProposalNumber; its own reference
                    # is the next best thing to quote to the insurer.
                    j.proposal_no = str(resp.get("ProposalNumber")
                                        or resp.get("CompanyQuotationNumber")
                                        or resp.get("CompanyProposalNumber") or "")
                    j.done("proposal")
                    stage = "payment"
            if "proposal" not in j.stages:
                j.done("proposal")       # landed, though the answer went unread
                stage = "payment"

            safety.assert_never_pays(page)
            j.done("payment", self._payment_page(page.url))
            j.payment_amount = _amount_on(page)
            j.evidence = str(browser.capture(page, self.run_dir, f"payment-{n}"))
            return j

        except (safety.SafetyRefusal, LoggedOut):
            raise
        except NeedsTestData as exc:
            for key in JOURNEY[JOURNEY.index(stage):]:
                j.stages.setdefault(key, Stage(NOT_RUN, str(exc)))
            return j
        except Exception as exc:
            reason = self.wire.failure(stage) or short_reason(_explain(exc))
            j.fail(stage, reason)
            if explain(reason):
                j.notes.append(explain(reason))
            if isinstance(exc, KycRedirected):
                j.redirected = exc.host
            j.evidence = str(browser.capture_failure(page, self.run_dir,
                                                     f"{n}-{stage}"))
            return j
        finally:
            j.seconds = round(time.monotonic() - started, 1)
            failed = any(s.result == ERROR for s in j.stages.values())
            try:
                if failed:
                    trace = self.run_dir / f"trace-{n}.zip"
                    self.context.tracing.stop(path=str(trace))
                    j.evidence = f"{j.evidence}  trace: {trace}".strip()
                else:
                    self.context.tracing.stop()
            except Exception:
                pass
            for tab in list(self.context.pages):
                try:
                    tab.close()
                except Exception:
                    pass

    # ----------------------------------------------------------------- stages
    def _buy(self, page, j: Journey, quotation, request, sub_product) -> None:
        """Open this quotation's share link and press Buy Now on our insurer."""
        why = self.api.save_quotation(self.cfg.buy_url, quotation, request, sub_product)
        if why:
            raise LookupError(f"could not open quotation {quotation} in the "
                              f"browser - {why}")
        page.goto(f"{self.cfg.base_url}/{self.product.page}/result/{quotation}",
                  wait_until="domcontentloaded")
        quotes = QuoteListPage(page)
        card = quotes.wait_for_insurer(self.insurer, timeout_ms=90_000)
        if card is None:
            if auth._login_dialog_showing(page):
                raise LoggedOut()
            said = next((f.reason for f in quotes.unavailable()
                         if _same(f.insurer, self.insurer)), "")
            raise LookupError(
                f"the quote page did not show a price from {self.insurer}"
                + (f": {said}" if said else
                   f" (the API had quoted it on {quotation})"))
        j.premium_on_screen = card.premium
        if self.fill_customer_type:
            self._supply_customer_type(page, request, j)
        safety.allow("proposal", self.cfg)
        quotes.buy(self.insurer)

    def _supply_customer_type(self, page, request: dict, j: Journey) -> None:
        """
        Give the KYC screen the customer type that screens 1-3 would have left
        in session ({TW|PC}RequestObj) and a shared link does not. Only those
        two fields: after Buy Now nothing else reads that object
        (kyc-insurance, tw-kycat-proposal, proposal-otp - checked 2026-10-01).
        """
        key = "TWRequestObj" if self.product.page == "two-wheeler" else "PCRequestObj"
        value = {"CustomerType": request.get("CustomerType") or "Individual",
                 "OrganizationName": request.get("OrganizationName") or ""}
        try:
            page.evaluate("([key, value]) => { if (!sessionStorage.getItem(key)) "
                          "sessionStorage.setItem(key, JSON.stringify(value)); }",
                          [key, value])
        except Exception:
            pass
        j.notes.append(f"the tool supplied customer type '{value['CustomerType']}' for "
                       f"KYC (shared-link portal bug - see Summary)")

    def _kyc(self, page) -> str:
        """Buy Now leads here. Returns a note; raises LookupError on failure."""
        kyc = KycPage(page)
        style, detail = kyc.detect_style(self.our_host)
        if style == kyc_styles.REDIRECT:
            kycnotes.record(self.insurer, style, detail)
            raise KycRedirected(self.insurer, detail)
        if style == kyc_styles.SKIPPED:
            return "no KYC asked - went straight to the proposal"
        if style == kyc_styles.UNKNOWN:
            raise LookupError(f"the KYC screen never appeared - now on {_short_url(detail)}")

        if not ui.field_exists(page, KycPage.FIRST, timeout_ms=3000) and \
                ui.field_exists(page, "organisationname", timeout_ms=3000):
            raise NeedsTestData(
                "the KYC screen is the COMPANY form (company PAN, date of "
                "incorporation) and the test customer in data/customer.py is a "
                "person - a company PAN is never invented; add a test company's "
                "KYC details there to take Organization quotes further")
        kyc.wait_until_loaded()
        kyc.fill_details(CUSTOMER)
        if not kyc.can_proceed():
            wanted = kyc.missing_required() or kyc.why_blocked()
            raise LookupError("the KYC form would not take the test customer: "
                              + ("; ".join(wanted) or "Proceed stayed disabled"))
        kyc.proceed()
        # The insurer's KYC check can take minutes, and the app shows nothing
        # but "Loading" meanwhile - which settle() would call stuck. Wait for
        # the answer itself first.
        self._wait_for_answer(page, "kyc", lambda u: routes.on(u, "proposal"))
        refused = self.wire.failure("kyc")
        if refused:
            raise LookupError(refused)
        kyc.wait_for_kyc_response()
        message = kyc.settle()
        if (not kyc.redirected_to and not routes.on(page.url, "proposal")
                and kyc.upload_step_showing()):
            kyc.upload_documents(CUSTOMER, self.cfg.test_documents_dir)
            message = kyc.finish() or message
        if kyc.redirected_to:
            kycnotes.record(self.insurer, kyc_styles.REDIRECT, kyc.redirected_to)
            raise KycRedirected(self.insurer, kyc.redirected_to)
        try:
            page.wait_for_url(lambda u: routes.on(u, "proposal"), timeout=30_000)
        except Exception:
            raise LookupError(f"KYC was not accepted: "
                              f"{message or 'the portal never left the KYC screen'}")
        kycnotes.record(self.insurer, kyc_styles.DOCUMENTS if kyc.uploaded
                        else kyc_styles.INLINE)
        took = self.wire.took.get("kyc") or 0
        return (f"{self.insurer} took {took:.0f}s to answer the KYC check"
                if took > 30 else "")

    def _company_specific(self, page) -> ProposalPage:
        """The proposal form, then Continue to Preview (CompanySpecificQuotation)."""
        proposal = ProposalPage(page).wait_until_loaded()
        proposal.hidden_errors = self.wire.hidden
        proposal.wait_for_address_lookup()
        proposal.fill_owner_details(CUSTOMER, self.insurer)
        _complete(proposal, "personal details")
        proposal.continue_to_vehicle()
        proposal.fill_vehicle_details(self.rto, self.insurer)
        _complete(proposal, "vehicle details")
        proposal.continue_to_terms(self.insurer, self.rto)
        proposal.fill_terms(CUSTOMER)
        _complete(proposal, "nominee and previous policy")
        try:
            proposal.continue_to_preview(CUSTOMER, self.insurer, self.rto)
        except ui.PageStuckLoading:
            # "Loading" while the insurer prices this customer is not a hang.
            if not self.wire.waiting_for("company_specific"):
                raise
            self._wait_for_answer(page, "company_specific",
                                  lambda u: routes.on(u, "proposal-payment"))
            refused = self.wire.failure("company_specific")
            if refused:
                raise LookupError(refused)
            try:
                page.wait_for_url(lambda u: routes.on(u, "proposal-payment"),
                                  timeout=30_000)
            except Exception:
                raise LookupError("the company-specific quote came back, but the "
                                  "Preview page never opened")
        refused = self.wire.failure("company_specific")
        if refused:
            raise LookupError(refused)
        return proposal

    def _wait_for_answer(self, page, stage: str, arrived, limit_s: int = ANSWER_WAIT_S) -> None:
        """Wait while the insurer is still answering this stage's call - up to
        limit_s - or until the page moves on by itself."""
        waited = 0.0
        while waited < limit_s:
            if stage in self.wire.answers or arrived(page.url):
                return
            if waited >= 10 and stage not in self.wire.sent:
                return               # the call never went out: the page decides
            page.wait_for_timeout(1000)
            waited += 1
        call = {"kyc": "the KYC check", "company_specific": "CompanySpecificQuotation",
                "proposal": "the proposal", "payment": "the payment link"}[stage]
        raise LookupError(f"{self.insurer} did not answer {call} in {limit_s}s - the "
                          f"portal showed only its Loading screen")

    def _payment_page(self, url: str) -> str:
        """Is where Proceed landed a payment page? Returns a note, or raises."""
        if routes.on(url, "inspectionsuccess"):
            raise LookupError("sent to the inspection page (break-in or underwriting), "
                              "not to payment")
        host = urlparse(url).hostname or ""
        if host == self.our_host and any(routes.on(url, p) for p in
                                         ("proposal", "result", "kyc-insurance")):
            raise LookupError(f"came back to {_short_url(url)} instead of a payment page")
        return f"payment page: {_short_url(url)}"


# ================================================================== helpers

def _amount_on(page) -> float | None:
    try:
        return amount_in(page.inner_text("body", timeout=5000))
    except Exception:
        return None


def amount_in(text: str) -> float | None:
    """The amount a payment page asks for - BillDesk UAT writes "Payment
    Amount: ₹ 1060.00" - or None."""
    money = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)"
    found = (re.search(r"(?:amount|total|pay)[^0-9₹]{0,30}" + money, text or "",
                       re.IGNORECASE)
             or re.search(r"₹\s*([\d,]+(?:\.\d{1,2})?)", text or ""))
    try:
        return float(found.group(1).replace(",", "")) if found else None
    except ValueError:
        return None


def _complete(proposal: ProposalPage, step: str) -> None:
    missing = proposal.missing_required()
    if missing:
        raise LookupError(f"proposal form, {step}: still needs {', '.join(missing)}")


def _redirect_reason(insurer: str, host: str) -> str:
    return (f"{insurer} sends the customer to its own KYC site ({host}) - a person "
            f"has to finish it there (python run_proposal_test.py --insurer "
            f"{insurer} --kyc-redirect assist)")


def _explain(exc: Exception) -> str:
    if isinstance(exc, ui.PageStuckLoading):
        return f"the app got stuck loading - {exc}"
    if isinstance(exc, ui.LookupTimedOut):
        return f"a master-data lookup timed out - {exc}"
    if isinstance(exc, LookupError):
        return str(exc).strip("'\"")
    return f"{type(exc).__name__}: {exc}"


def _short_url(url: str) -> str:
    try:
        parts = urlparse(url)
        return f"{parts.hostname or ''}{parts.path}" or url
    except Exception:
        return url


def _same(a, b) -> bool:
    x, y = str(a or "").upper(), str(b or "").upper()
    return bool(x and y) and (x == y or x in y or y in x)
