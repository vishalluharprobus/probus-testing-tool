"""
Read every insurer's quote from the wire, not just from the screen.

WHY THIS EXISTS
---------------
The quote cards show one number per insurer - the "Buy Now" price. The API
behind them says far more, and most of it is checkable:

    POST /api/Motor/TwoWheeler/QualifiedCompany       ONE call
         -> QuotationNumber
         -> QualifiedPlanList[]     which insurer plans will be asked
         -> APIDeclineDetails[]     insurers OUR rules turned away, with why

    POST /api/Motor/TwoWheeler/{CompanyCode}          ONE call PER PLAN
         -> Response[]  FinalPremium, InsuredDeclaredValue (IDV),
                        PremiumBreakUpDetails.NetODPremium / NetTPPremium /
                        NetPremium / ServiceTax (GST) / CurrentNCB / NCBDiscount,
                        Status + ErrorMessage when it failed

(Read from Saarthi's tw-result.component.ts, 2026-09-30.) Private cars do the
same under /api/Motor/privatecar/ (pc-result.component.ts, 2026-10-05), so a
capture is told which product to listen to: QuoteCapture.attach(context,
"privatecar").

With that we can check the SUMS, not only that a number appeared: that GST is
18% of the net premium, that a Third Party policy carries no own-damage premium,
that an OD-only policy carries no third-party premium, that the NCB given is the
one the customer earned. None of that is visible on a card.

WHEN IS THE FAN-OUT FINISHED?
-----------------------------
The page asks each plan in turn and cards trickle in. The honest signal is the
count: every plan named in QualifiedPlanList has answered (success, failure or
HTTP error). Waiting for "the card count stopped changing" - what
QuoteListPage.wait_until_loaded does - is a guess that is wrong on a slow day.

This file never raises into a run: a body it cannot read is skipped.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

# The API path segment per product, as labscenarios.PRODUCTS spells it.
SEGMENTS = ("twowheeler", "privatecar")

# Other endpoints under the same prefix that are NOT a per-insurer quote.
# The body test in _is_plan_call is the real guard; this list only saves
# parsing bodies. Car-only ones (motor-routes.ts PRIVATECAR) included.
NOT_A_PLAN = ("qualifiedcompany", "addon", "occupation", "getquotation",
              "previousinsurer", "rtocity", "make", "model", "variant",
              "proposal", "companyspecific", "salutation", "maritalstatus",
              "nominee", "organizationtype", "breakin", "bodytype",
              "getdetailsfor")

# The reason given for an asked insurer the portal never put on its list.
NOT_LISTED = ("not asked - the portal left this insurer out of its own list for "
              "this journey (no plan, no reason given)")


def _num(value) -> float | None:
    """The API writes money as numbers, strings, or strings with commas."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _path(url: str) -> str:
    return url.split("?", 1)[0].lower()


@dataclass
class PlanAnswer:
    """One plan's answer from one insurer."""
    insurer: str
    plan_id: str = ""
    plan_name: str = ""
    ok: bool = False
    error: str = ""
    http: int = 200
    premium: float | None = None       # FinalPremium - the Buy Now figure
    net: float | None = None           # NetPremium, before GST
    gst: float | None = None           # ServiceTax
    od: float | None = None            # NetODPremium
    tp: float | None = None            # NetTPPremium
    addon: float | None = None
    discount: float | None = None
    ncb_percent: float | None = None   # CurrentNCB
    ncb_discount: float | None = None  # NCBDiscount (rupees)
    pa_cover: float | None = None      # PACoverToOwnDriver
    idv: float | None = None
    idv_min: float | None = None
    idv_max: float | None = None
    years: str = ""                    # PremiumYear (tenure)
    seconds: float | None = None       # how long the insurer took

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Decline:
    """An insurer that did not quote, and who said no."""
    insurer: str
    reason: str
    source: str          # "probus-rule" | "insurer" | "http" | "silent"


@dataclass
class QuoteCapture:
    """
    Attach to a browser context BEFORE the last Proceed; read after.

        capture = QuoteCapture.attach(context)            # bikes
        capture = QuoteCapture.attach(context, "privatecar")  # cars
        ... drive screens 1-3 ...
        capture.wait_until_complete(page)
        capture.best_offers()      # one PlanAnswer per insurer
        capture.declines           # who said no, and why
    """
    quotation_no: str = ""
    planned: list[tuple[str, str]] = field(default_factory=list)  # (code, plan)
    probus_declines: list[Decline] = field(default_factory=list)
    answers: list[PlanAnswer] = field(default_factory=list)
    sent: dict = field(default_factory=dict)      # the QualifiedCompany body
    answered_calls: int = 0
    saved_signal: bool = False                    # MailQuotation NoMail seen
    qualified_all: list = field(default_factory=list)  # every code the portal named
    segment: str = "twowheeler"                   # which product's API
    # Only these insurers (company codes) are asked; empty = everybody.
    # Everyone else's quote call is stopped in the browser before it leaves
    # (block_others), and left out of every count, answer and decline.
    only: frozenset = frozenset()
    _started: dict = field(default_factory=dict, repr=False)

    @classmethod
    def attach(cls, context, segment: str = "twowheeler",
               only=()) -> "QuoteCapture":
        if segment not in SEGMENTS:
            raise ValueError(f"segment must be one of {SEGMENTS}, not {segment!r}")
        capture = cls(segment=segment,
                      only=frozenset(str(c).upper() for c in only if c))
        context.on("request", capture._on_request)
        context.on("response", capture._on_response)
        context.on("requestfailed", capture._on_failed)
        if capture.only:
            context.route(re.compile(rf"/api/motor/{segment}/[^/?]+$", re.IGNORECASE),
                          capture._block_others)
        return capture

    def _wanted(self, code: str) -> bool:
        return not self.only or code.upper() in self.only

    def _block_others(self, route) -> None:
        """Stop a quote call to an insurer this run does not ask."""
        try:
            request = route.request
            if (self._is_plan_call(request, any_insurer=True)
                    and not self._wanted(self._code_from(request))):
                route.abort()
                return
        except Exception:
            pass
        # fallback, not continue_: let any other handler see the request
        # (with none, it goes to the network just the same).
        route.fallback()

    # ------------------------------------------------------------ listeners

    def _on_request(self, request) -> None:
        try:
            if self._is_plan_call(request):
                self._started[id(request)] = time.monotonic()
        except Exception:
            pass

    def _on_failed(self, request) -> None:
        try:
            if not self._is_plan_call(request):
                return
            code = self._code_from(request)
            self._record_http_error(code, 0, "request never answered "
                                    f"({request.failure or 'failed'})")
        except Exception:
            pass

    def _on_response(self, response) -> None:
        try:
            url, method = response.url, response.request.method
            path = _path(url)
            if method == "POST" and "mailquotation" in path:
                sent = response.request.post_data or ""
                if '"NoMail":true' in sent.replace(" ", ""):
                    self.saved_signal = True
                return
            if method == "POST" and path.endswith(self.qualify_path):
                self._read_qualify(response)
            elif self._is_plan_call(response.request):
                self._read_plan(response)
        except Exception:
            pass

    # -------------------------------------------------------------- readers

    @property
    def qualify_path(self) -> str:
        return f"/api/motor/{self.segment}/qualifiedcompany"

    def _is_plan_call(self, request, any_insurer: bool = False) -> bool:
        """
        A per-insurer quote call: POST /api/Motor/TwoWheeler/{code} (cars:
        /privatecar/{code}) whose body names a plan. The body test matters -
        other calls live under the same prefix, and counting one of them as
        an answer would end the wait before the last insurer had replied.
        """
        path = _path(request.url)
        if request.method != "POST" or f"/api/motor/{self.segment}/" not in path:
            return False
        tail = path.rsplit("/", 1)[-1]
        if not tail or any(word in tail for word in NOT_A_PLAN):
            return False
        body = request.post_data or ""
        if not ('"PlanId"' in body and '"CompanyCode"' in body):
            return False
        return any_insurer or self._wanted(self._code_from(request))

    @staticmethod
    def _code_from(request) -> str:
        try:
            body = json.loads(request.post_data or "{}")
            if body.get("CompanyCode"):
                return str(body["CompanyCode"]).upper()
        except (ValueError, TypeError):
            pass
        return request.url.split("?", 1)[0].rsplit("/", 1)[-1].upper()

    def _read_qualify(self, response) -> None:
        try:
            self.sent = json.loads(response.request.post_data or "{}")
        except (ValueError, TypeError):
            self.sent = {}
        body = json.loads(response.text() or "{}")
        data = body.get("Response") or {}
        # A second QualifiedCompany (a re-quote) starts a fresh fan-out.
        self.planned = []
        self.answers = []
        self.probus_declines = []
        self.answered_calls = 0
        self.saved_signal = False
        self.quotation_no = str(data.get("QuotationNumber") or "")
        # Every company the portal named, asked by this run or not - so an
        # asked insurer that never appears can be told apart from one whose
        # code is spelled differently.
        self.qualified_all = sorted({
            str(item.get("CompanyCode") or "").upper()
            for key in ("QualifiedPlanList", "APIDeclineDetails")
            for item in data.get(key) or []} - {""})
        for plan in data.get("QualifiedPlanList") or []:
            code = str(plan.get("CompanyCode") or "").upper()
            if self._wanted(code):
                self.planned.append((code, str(plan.get("PlanId") or "")))
        for item in data.get("APIDeclineDetails") or []:
            if not self._wanted(str(item.get("CompanyCode") or "")):
                continue
            self.probus_declines.append(Decline(
                insurer=str(item.get("CompanyCode") or "").upper(),
                reason=str(item.get("ErrorMessage") or "").strip()
                or "declined by our own rules (no reason given)",
                source="probus-rule"))

    def _read_plan(self, response) -> None:
        request = response.request
        started = self._started.pop(id(request), None)
        seconds = round(time.monotonic() - started, 1) if started else None
        code = self._code_from(request)
        self.answered_calls += 1

        if response.status >= 400:
            self._record_http_error(code, response.status,
                                    f"our API answered HTTP {response.status}",
                                    seconds, counted=True)
            return
        try:
            body = json.loads(response.text() or "{}")
        except ValueError:
            self._record_http_error(code, response.status,
                                    "answer was not JSON", seconds, counted=True)
            return

        items = body.get("Response") or []
        if isinstance(items, dict):
            items = [items]
        if not items:
            # The page shows nothing for an empty answer - no card, no entry in
            # the unavailable panel. The insurer silently vanishes, which is
            # worth saying out loud.
            self.answers.append(PlanAnswer(
                insurer=code, http=response.status, seconds=seconds,
                error=str(body.get("Error") or body.get("Message") or "")
                or "empty answer - the page shows nothing for this insurer"))
            return

        self.answers += answer_items(items, code, response.status, seconds)

    def _record_http_error(self, code: str, status: int, why: str,
                           seconds: float | None = None,
                           counted: bool = False) -> None:
        if not counted:
            self.answered_calls += 1
        self.answers.append(PlanAnswer(insurer=code, http=status, error=why,
                                       seconds=seconds))

    # -------------------------------------------------------------- queries

    @property
    def started(self) -> bool:
        return bool(self.quotation_no or self.planned or self.probus_declines)

    @property
    def complete(self) -> bool:
        """Every planned call has answered - or the page saved the quote,
        which it only does after the last one."""
        if self.saved_signal:
            return True
        return self.started and self.answered_calls >= len(self.planned)

    def wait_until_complete(self, page, timeout_ms: int = 150_000) -> bool:
        """
        Block until the fan-out is done. True when it finished, False on
        timeout - the caller reports the insurers that never answered rather
        than pretending they declined.
        """
        waited = 0
        while waited < timeout_ms:
            if self.complete:
                # A short grace period: the page renders cards after the last
                # answer arrives, and the DOM cross-check reads those.
                page.wait_for_timeout(2500)
                return True
            page.wait_for_timeout(1000)
            waited += 1000
        return False

    def silent(self) -> list[str]:
        """Insurers that were asked and never answered at all."""
        answered = {a.insurer for a in self.answers}
        return sorted({code for code, _ in self.planned
                       if code and code not in answered})

    def best_offers(self) -> dict[str, PlanAnswer]:
        """
        One successful answer per insurer: the one-year plan, cheapest first.

        Insurers can return several tenures (1 year, 2 years...) and several
        plans. Comparing a two-year price with somebody's one-year price is how
        a checker invents defects, so the shortest tenure wins.
        """
        out: dict[str, PlanAnswer] = {}
        for answer in self.answers:
            if not answer.ok or not answer.premium:
                continue
            best = out.get(answer.insurer)
            key = (_tenure(answer.years), answer.premium)
            if best is None or key < (_tenure(best.years), best.premium):
                out[answer.insurer] = answer
        return out

    def declines(self) -> list[Decline]:
        """Everybody who said no, once each, with the most useful reason."""
        offered = set(self.best_offers())
        out: dict[str, Decline] = {}
        for d in self.probus_declines:
            if d.insurer and d.insurer not in offered:
                out.setdefault(d.insurer, d)
        for a in self.answers:
            if a.ok or a.insurer in offered or a.insurer in out:
                continue
            # Only OUR call failing counts as "http". An insurer message that
            # merely contains a URL ("I/O error on POST request for https://
            # ...bajajgeneral.com") is the insurer's answer - seen live
            # 2026-09-30, and wrongly reported as our API failing.
            ours = a.http != 200 or a.error.startswith(
                ("our API answered HTTP", "request never answered", "answer was not JSON"))
            source = "http" if ours else "insurer"
            out[a.insurer] = Decline(a.insurer, _clean(a.error), source)
        for code in self.silent():
            out.setdefault(code, Decline(code, "asked, never answered", "silent"))
        # An insurer the run asked for that the portal left out entirely - no
        # plan, no reason (SBI on the live site, 2026-10-07). Said out loud,
        # or it would simply be missing from the report.
        planned = {code for code, _ in self.planned}
        for code in sorted(self.only) if self.started else ():
            if code not in planned and code not in offered:
                out.setdefault(code, Decline(code, NOT_LISTED, "probus-rule"))
        return sorted(out.values(), key=lambda d: d.insurer)


def answer_items(items: list, code: str, http: int = 200,
                 seconds: float | None = None) -> list[PlanAnswer]:
    """Per-plan Response items -> PlanAnswer, shared with the insurer lab."""
    out = []
    for item in items:
        breakup = item.get("PremiumBreakUpDetails") or {}
        error = str(item.get("ErrorMessage") or "").strip()
        out.append(PlanAnswer(
            insurer=str(item.get("CompanyCode") or code).upper(),
            plan_id=str(item.get("PlanId") or ""),
            plan_name=str(item.get("PlanName") or ""),
            ok=item.get("Status") == "Success" and not error,
            error=error or ("" if item.get("Status") == "Success"
                            else f"Status={item.get('Status')}"),
            http=http,
            premium=_num(item.get("FinalPremium")),
            net=_num(breakup.get("NetPremium")),
            gst=_num(breakup.get("ServiceTax")),
            od=_num(breakup.get("NetODPremium")),
            tp=_num(breakup.get("NetTPPremium")),
            addon=_num(breakup.get("NetAddonPremium")),
            discount=_num(breakup.get("NetDiscount")),
            ncb_percent=_num(breakup.get("CurrentNCB")),
            ncb_discount=_num(breakup.get("NCBDiscount")),
            pa_cover=_num(breakup.get("PACoverToOwnDriver")),
            idv=_num(item.get("InsuredDeclaredValue")),
            idv_min=_num(item.get("MinInsuredDeclaredValue")),
            idv_max=_num(item.get("MaxInsuredDeclaredValue")),
            years=str(item.get("PremiumYear") or ""),
            seconds=seconds,
        ))
    return out


def _tenure(years: str) -> int:
    found = re.search(r"\d+", years or "")
    return int(found.group()) if found else 1


def _clean(reason: str) -> str:
    """'COMPANY - Shriram's server is down.' -> 'Shriram's server is down.'"""
    reason = re.sub(r"\s+", " ", reason or "").strip()
    return re.sub(r"^(PROBUS|COMPANY)\s*-\s*", "", reason, flags=re.I) or \
        "no reason given"
