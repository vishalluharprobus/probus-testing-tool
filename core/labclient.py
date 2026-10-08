"""
Ask ONE insurer for a quote directly through the API - in seconds, not minutes.

WHY
---
A browser journey takes about two minutes: three screens, then a fan-out to
every insurer. When you are building ONE integration, 95% of that is waiting
for things you do not care about. The quote itself is two HTTP calls:

    POST {api}/Motor/{Product}/QualifiedCompany      the whole request -> plans
    POST {api}/Motor/{Product}/{CompanyCode}         one plan -> the price

So the lab drives the browser ONCE to learn a real, complete request (the
"template"), then changes one field at a time and sends it straight to the
API, for the chosen insurer only. Each scenario is then a few seconds.

HOW IT STAYS HONEST
-------------------
Nothing here is invented. The template is exactly what the app sent; the
per-plan body is built the way the app builds it, learned by comparing the
app's own two requests (what it added, what it removed). The auth header is
the one the app itself sent ('mob-token', from localStorage 'buy-token' -
Saarthi jwt.interceptor.ts:12-31). It is kept in memory only: never printed,
never written to disk.

Only the chosen insurer is asked. Every other insurer is left alone - the
browser journey that captures the template blocks their calls too.
"""
from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Headers worth replaying. Everything else (host, length, browser noise) is
# rebuilt by urllib.
REPLAY_HEADERS = ("mob-token", "content-type", "accept")

# Fields the page takes from the QualifiedCompany ANSWER for each plan call
# (tw-result.component.ts:1405-1409 preparePremiumRequest).
FROM_ANSWER = ("QuotationNumber", "PolicyStartDate", "PolicyEndDate",
               "SubProductCode", "Uid")
FROM_PLAN = ("CompanyCode", "CompanyName", "PlanId")


class ApiRefused(RuntimeError):
    """The API itself said no to the call (401, 500...) - not an insurer answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail[:160]}")
        self.status = status


@dataclass
class Template:
    """One real request the app sent, and how it turns it into a plan call."""
    product: str                      # "bike" | "car"
    api_base: str                     # e.g. http://localhost:53339/api/Motor/TwoWheeler/
    qualify: dict                     # the QualifiedCompany body
    plan_added: dict = field(default_factory=dict)    # keys the plan call adds
    plan_removed: list = field(default_factory=list)  # keys it drops
    captured: str = ""
    label: str = ""                   # the vehicle + RTO it was captured on

    def to_json(self) -> dict:
        return {"product": self.product, "api_base": self.api_base,
                "qualify": self.qualify, "plan_added": self.plan_added,
                "plan_removed": self.plan_removed, "captured": self.captured,
                "label": self.label}

    @classmethod
    def from_json(cls, data: dict) -> "Template":
        return cls(**{k: data[k] for k in ("product", "api_base", "qualify",
                                            "plan_added", "plan_removed",
                                            "captured", "label") if k in data})


def learn_plan_shape(qualify: dict, plan_body: dict) -> tuple[dict, list]:
    """
    What the page adds to and removes from the QualifiedCompany body to make a
    per-plan body - learned from one real pair rather than re-implemented.
    """
    added = {k: v for k, v in plan_body.items()
             if k not in qualify or k in FROM_ANSWER + FROM_PLAN}
    removed = [k for k in qualify if k not in plan_body]
    return added, removed


def plan_body(t: Template, qualify_sent: dict, answer: dict, plan: dict) -> dict:
    body = {k: copy.deepcopy(v) for k, v in qualify_sent.items()
            if k not in t.plan_removed}
    body.update(copy.deepcopy(t.plan_added))
    data = answer.get("Response") or {}
    for key in FROM_ANSWER:
        if key in data:
            body[key] = data[key]
    for key in FROM_PLAN:
        if key in plan:
            body[key] = plan[key]
    return body


@dataclass
class Reply:
    """What one scenario got back from the chosen insurer."""
    qualified: bool = False           # did OUR rules let the insurer be asked?
    decline: str = ""                 # Probus decline reason, when not
    quotation: str = ""
    sub_product: str = ""             # SubProductCode from the answer, e.g. MTRTW
    items: list = field(default_factory=list)   # per-plan Response items
    seconds: float = 0.0
    http_error: str = ""
    # Who our server WOULD ask, and who it declined - so "not in the list"
    # can be explained instead of just reported.
    others: list = field(default_factory=list)
    declined: dict = field(default_factory=dict)
    not_listed: bool = False          # absent from plans AND from declines


class ApiReplay:
    """Send requests with the app's own auth header."""

    def __init__(self, headers: dict, timeout: int = 90):
        self.headers = {k: v for k, v in headers.items()
                        if k.lower() in REPLAY_HEADERS}
        self.headers.setdefault("content-type", "application/json")
        self.timeout = timeout

    def get(self, url: str) -> dict:
        """A read-only call, e.g. the add-on list (Motor/privatecar/AddOn)."""
        return self._send(urllib.request.Request(url, method="GET",
                                                 headers=self.headers))

    def post(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        return self._send(urllib.request.Request(url, data=data, method="POST",
                                                 headers=self.headers))

    def _send(self, request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise ApiRefused(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ApiRefused(0, str(getattr(exc, "reason", exc))) from exc
        try:
            answer = json.loads(text or "{}")
        except ValueError:
            raise ApiRefused(200, f"not JSON: {text[:120]}")
        # The app treats HTTP 200 with StatusCode 401 as logged out
        # (Saarthi http-response.util.ts:12-18) - so do we.
        if isinstance(answer, dict) and str(answer.get("StatusCode")) == "401":
            raise ApiRefused(401, "the API says the token is not valid")
        return answer

    def quote(self, t: Template, qualify_body: dict, insurer: str) -> Reply:
        """QualifiedCompany, then the chosen insurer's plans only."""
        reply = Reply()
        started = time.monotonic()
        try:
            answer = self.post(t.api_base + "QualifiedCompany", qualify_body)
            data = answer.get("Response") or {}
            if not data and answer.get("Error"):
                # Model validation: HTTP 200, reasons joined with " | " in the
                # top-level Error (InsureBridge BaseController.cs:51-60).
                reply.decline = f"request refused: {answer['Error']}"
                return reply
            reply.quotation = str(data.get("QuotationNumber") or "")
            reply.sub_product = str(data.get("SubProductCode") or "")
            plans = [p for p in data.get("QualifiedPlanList") or []
                     if _same(p.get("CompanyCode"), insurer)]
            declines = [d for d in data.get("APIDeclineDetails") or []
                        if _same(d.get("CompanyCode"), insurer)]
            reply.others = sorted({str(p.get("CompanyCode") or "")
                                   for p in data.get("QualifiedPlanList") or []} - {""})
            reply.declined = {str(d.get("CompanyCode") or ""): str(d.get("ErrorMessage") or "")
                              for d in data.get("APIDeclineDetails") or []}
            if not plans:
                reply.not_listed = not declines
                reply.decline = (str(declines[0].get("ErrorMessage") or "").strip()
                                 if declines else
                                 f"{insurer} was not in the list of plans to ask")
                return reply
            reply.qualified = True
            for plan in plans:
                body = plan_body(t, qualify_body, answer, plan)
                code = str(plan.get("CompanyCode") or insurer)
                result = self.post(t.api_base + code, body)
                items = result.get("Response") or []
                items = items if isinstance(items, list) else [items]
                if not items and result.get("Error"):
                    items = [{"CompanyCode": code, "Status": "Error",
                              "ErrorMessage": str(result["Error"])}]
                reply.items += items
        except ApiRefused as exc:
            reply.http_error = str(exc)
            if exc.status == 401:
                raise
        finally:
            reply.seconds = round(time.monotonic() - started, 1)
        return reply

    def save_quotation(self, buy_url: str, quotation: str, request: dict,
                       sub_product: str) -> str:
        """
        Store a quotation so its share link opens it: what the result page does
        by itself, silently, every time it shows quotes (SaveQuatationDetails,
        tw-result.component.ts:1986-2006 - NoMail, so nothing is sent).

        Without this, /two-wheeler/result/<quotation> answers "No data found."
        for a quote the lab made through the API. Returns "" when saved, else
        the reason it was not.
        """
        body = {"IsWhatsappQuotation": True, "NoMail": True,
                "RegistrationNumber": (request.get("VehicleDetails") or {}).get(
                    "RegistrationNumber", ""),
                "QuotationNumber": quotation,
                "QuotationParameter": json.dumps({**request, "QuotationNumber": quotation}),
                "ExpiryTime": time.strftime("%d %b %Y"),
                "Base64string": "",
                "SubProductCode": sub_product}
        try:
            answer = self.post(buy_url.rstrip("/") + "/api/v2/Client/MailQuotation", body)
        except ApiRefused as exc:
            return f"the portal would not save it ({exc})"
        error = answer.get("Error") if isinstance(answer, dict) else None
        return f"the portal would not save it: {error}" if error else ""


def _same(a, b) -> bool:
    x, y = str(a or "").upper(), str(b or "").upper()
    return bool(x and y) and (x == y or x in y or y in x)
