"""
The quote sweep through the API - minutes, not hours.

A browser journey costs ~3 minutes: a fresh browser, a login, three screens
of clicking, the quote fan-out, then a 20-second pause. 87 journeys was over
four hours (2026-10-07), which is no help to a developer waiting on a fix.

The quote itself is only two kinds of call (core/labclient.py):

    POST .../QualifiedCompany   the whole request -> which plans to ask
    POST .../{CompanyCode}      one plan          -> that company's price

So fast mode drives the browser ONCE (the insurer lab's template capture -
its saved request is reused for a week), then turns every planned journey
into that same request with the journey's choices written in
(data/labscenarios.build_body + in_city), sends it straight to the API, and
reads the answers with the SAME QuoteCapture the browser mode uses - so the
offers, declines, checks, Excel and notebook are exactly as before.

What it does not do: click the form. Checks that need the screen (a card
missing, the form keeping Proceed grey) only run in browser mode, which is
still there (run_quote_matrix.py without --fast).

The live site: only the companies named in --insurers are asked, as in
browser mode; QualifiedCompany itself calls no insurer.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import time
from argparse import Namespace
from dataclasses import dataclass, field

from core import vehiclecatalog
from core.labclient import ApiRefused, ApiReplay, Template, plan_body
from core.quotecapture import QuoteCapture
from data import labscenarios as ls
from data import matrix

# The template is captured on this RTO (the insurer lab's default) and moved
# to each journey's RTO by in_city.
TEMPLATE_RTO = "GJ-01 Ahmedabad"
PLAN_CALLS_AT_ONCE = 6
NO_INSURER = "-NO-INSURER-"


class TokenExpired(RuntimeError):
    """The API says the borrowed login is no longer valid."""


@dataclass
class FastResult:
    status: str = "ok"            # ok | environment | error
    note: str = ""
    offers: dict = field(default_factory=dict)
    answers: list = field(default_factory=list)
    declines: list = field(default_factory=list)
    silent: list = field(default_factory=list)
    sent: dict = field(default_factory=dict)
    quotation: str = ""
    qualified: list = field(default_factory=list)
    seconds: float = 0.0
    evidence: dict = field(default_factory=dict)


# Just enough of Playwright's request/response for QuoteCapture to read.
class _Request:
    def __init__(self, url: str, body: dict):
        self.url, self.method = url, "POST"
        self.post_data = json.dumps(body)
        self.failure = ""


class _Response:
    def __init__(self, request: _Request, body: dict, status: int = 200):
        self.request, self.url, self.status = request, request.url, status
        self._text = json.dumps(body)

    def text(self) -> str:
        return self._text


class FastSweep:
    def __init__(self, cfg, product_name: str, setup: dict, only=(), headless=True):
        self.cfg = cfg
        self.product = ls.PRODUCTS[product_name]
        self.setup = setup
        self.only = tuple(only)
        self.headless = headless
        self.template: Template | None = None
        self.api: ApiReplay | None = None
        self.catalog: dict = {}
        self.rtos: dict[str, dict] = {}

    # ------------------------------------------------------------ open
    def open(self) -> None:
        """Log in and get the request to replay. Raises auth.LoginFailed,
        safety.SafetyRefusal; RuntimeError with a sentence otherwise."""
        import run_insurer_lab as lab       # heavy; only when fast mode runs

        saved = lab.load_template(self.product, TEMPLATE_RTO, self.cfg.name)
        fresh = lab.template_fresh(saved, self.cfg)
        print(f"  fast mode: {'reusing the request captured ' + saved.get('captured', '') if fresh else 'one browser journey to capture a real request (once a week)'}")
        # NO_INSURER matches no company, so the capture journey blocks EVERY
        # company's quote call: the request's shape is all we need, and no
        # insurer (on the live site, no real insurer) is asked during it.
        session = lab.open_session(
            self.cfg, self.product, NO_INSURER,
            Namespace(refresh_template=False, headless=self.headless,
                      rto=TEMPLATE_RTO, vehicle=""), saved, trace=False)
        if session is None:
            raise RuntimeError("could not capture a request from the portal - "
                               "see the lines above")
        self.template = session["template"]
        self.catalog = session["catalog"] or vehiclecatalog.load(self.product.name)
        self.api = ApiReplay(session["headers"])
        self.rtos = self._rto_list()

    def _rto_list(self) -> dict[str, dict]:
        try:
            rows = self.api.get(self.cfg.api_url.rstrip("/") + "/api/Motor/RTOcityJson")
            rows = rows.get("Response") or []
        except ApiRefused:
            rows = []
        if not rows:                           # the Studio's cached copy
            try:
                from portal.catalog import RTO_FILE
                cached = json.loads(RTO_FILE.read_text(encoding="utf-8"))["rtos"]
                rows = [{"Name": r["name"], "Id": r["id"]} for r in cached]
            except Exception:
                rows = []
        return {str(r.get("Name")): r for r in rows if r.get("Name")}

    # ------------------------------------------------------------- run
    def run(self, s: matrix.Scenario) -> FastResult:
        started = time.monotonic()
        result = FastResult()
        try:
            body = self.request_for(s)
        except ValueError as exc:
            result.status, result.note = "error", str(exc)
            return result
        capture = QuoteCapture(segment=self.product.segment,
                               only=frozenset(c.upper() for c in self.only))
        base = self.template.api_base
        qualify = _Request(base + "QualifiedCompany", body)
        try:
            answer = self.api.post(qualify.url, body)
        except ApiRefused as exc:
            if exc.status == 401:
                raise TokenExpired(str(exc)) from exc
            result.status = "environment"
            result.note = f"QualifiedCompany: {exc}"
            return result
        if not (answer.get("Response") or {}) and answer.get("Error"):
            result.status = "error"
            result.note = f"the API refused the request: {str(answer['Error'])[:200]}"
            result.evidence = {"sent": body, "answer": answer}
            return result
        capture._on_response(_Response(qualify, answer))

        plans = [p for p in (answer.get("Response") or {}).get("QualifiedPlanList") or []
                 if capture._wanted(str(p.get("CompanyCode") or ""))]
        calls = {}
        with futures.ThreadPoolExecutor(PLAN_CALLS_AT_ONCE) as pool:
            for plan in plans:
                code = str(plan.get("CompanyCode") or "")
                req = _Request(base + code, plan_body(self.template, body, answer, plan))
                capture._on_request(req)
                calls[pool.submit(self._post, req)] = req
            for done in futures.as_completed(calls):
                req = calls[done]
                status, reply = done.result()
                if status == 401:
                    raise TokenExpired("the API says the token is not valid")
                capture._on_response(_Response(req, reply, status))

        result.offers = capture.best_offers()
        result.answers = capture.answers
        result.declines = capture.declines()
        result.silent = capture.silent()
        result.sent = capture.sent
        result.quotation = capture.quotation_no
        result.qualified = capture.qualified_all
        result.seconds = round(time.monotonic() - started, 1)
        result.evidence = {"sent": body, "qualified": answer.get("Response"),
                           "answers": [a.as_dict() for a in capture.answers]}
        return result

    def _post(self, req: _Request) -> tuple[int, dict]:
        try:
            return 200, self.api.post(req.url, json.loads(req.post_data))
        except ApiRefused as exc:
            return (exc.status or 599), {"Error": str(exc)}

    # --------------------------------------------------------- request
    def request_for(self, s: matrix.Scenario) -> dict:
        """The template with this journey's choices written in."""
        label = s.get("vehicle")
        vehicle = self.setup["vehicles"].get(label)
        row = vehiclecatalog.row_for(vehicle, self.catalog) if vehicle else {}
        if not row or row.get("variant_id") is None:
            raise ValueError(f"{label} is not in the portal's vehicle list with an id")
        rto = self.rtos.get(s.get("rto"))
        if not rto:
            raise ValueError(f"RTO {s.get('rto')!r} is not in the portal's RTO list")
        ncb = s.get("ncb")
        state = ls.State(
            age=matrix.age_of(s.get("year")), policy=s.get("policy"),
            previous=s.get("previous"), prev_type=s.get("prev_type") or ls.CP,
            ncb=None if ncb in (None, "default") else int(str(ncb).rstrip("%")),
            claim=s.get("claim") == "Yes")
        body = ls.build_body(self.template.qualify, self.product, state,
                             prev_insurer=self._insurer(s.get("prev_insurer")),
                             vehicle=row)
        return ls.in_city(body, self.product, rto)

    def _insurer(self, code: str | None) -> dict:
        rows = self.catalog.get("insurers") or []
        row = next((r for r in rows if str(r.get("code") or "").upper() == str(code or "").upper()),
                   None) or next((r for r in rows if "ICICI" in str(r.get("name", "")).upper()),
                                 rows[0] if rows else {})
        return {"Id": row.get("id"), "CompanyCode": row.get("code"),
                "CShortName": row.get("short") or row.get("code"), "Name": row.get("name")}
