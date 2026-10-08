"""
Watch one browser journey and keep what the lab needs to go on without it.

During the journey the app's own calls are intercepted:

    any /api/ call            -> its 'mob-token' header (kept in memory only)
    .../QualifiedCompany      -> the full request body: the TEMPLATE
    .../{CompanyCode}         -> the chosen insurer's plan call: let through,
                                 body kept, so the lab learns the plan shape
                                 every OTHER insurer's call: blocked

Blocking the others matters twice over. The capture journey costs one quote
at one insurer instead of a fan-out to fifteen, and nobody else's UAT is
disturbed by an integration that is being built.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from core.labclient import FROM_ANSWER, Template, learn_plan_shape

NOT_A_PLAN = ("qualifiedcompany", "addon", "occupation", "getquotation",
              "previousinsurer", "rtocity", "make", "model", "variant",
              "manufacturer", "allmodel", "allvariant", "idv", "cover")


class JourneyTap:
    def __init__(self, insurer: str, segment: str, hosts=()):
        self.insurer = insurer.upper()
        # The auth header is borrowed only from calls to OUR target's API -
        # on the local target the login visits the test site first, whose
        # calls carry the test site's token.
        self.hosts = {h for h in hosts if h}
        self.segment = segment.lower()          # "twowheeler" | "privatecar"
        self.headers: dict = {}
        self.qualify: dict | None = None
        self.qualify_answer: dict | None = None
        self.api_base = ""
        self.plan_body: dict | None = None
        self.plan_answer: dict | None = None
        # Any insurer's plan call: same shape for all, so it teaches the plan
        # shape even when the chosen insurer is not asked this time.
        self.shape_body: dict | None = None
        self.blocked: list[str] = []

    def attach(self, context) -> "JourneyTap":
        context.route("**/api/**", self._route)
        context.on("response", self._on_response)
        return self

    # ---------------------------------------------------------------- routing
    def _route(self, route) -> None:
        request = route.request
        try:
            host = urlparse(request.url).hostname or ""
            if not self.headers and (not self.hosts or host in self.hosts):
                token = request.headers.get("mob-token")
                if token:
                    self.headers = {"mob-token": token,
                                    "content-type": "application/json"}
            path = urlparse(request.url).path.lower()
            prefix = f"/motor/{self.segment}/"
            if request.method == "POST" and prefix in path:
                tail = path.rsplit("/", 1)[-1]
                if tail == "qualifiedcompany":
                    self.qualify = json.loads(request.post_data or "{}")
                    self.api_base = request.url.split("?", 1)[0][:-len(tail)]
                elif tail and not any(w in tail for w in NOT_A_PLAN):
                    body = json.loads(request.post_data or "{}")
                    code = str(body.get("CompanyCode") or tail).upper()
                    if "PlanId" in body and self.shape_body is None:
                        self.shape_body = body
                    if "PlanId" in body and not _same(code, self.insurer):
                        self.blocked.append(code)
                        route.abort()
                        return
                    if "PlanId" in body:
                        self.plan_body = body
        except Exception:
            pass
        route.continue_()

    def _on_response(self, response) -> None:
        try:
            path = urlparse(response.url).path.lower()
            if response.request.method != "POST" or f"/motor/{self.segment}/" not in path:
                return
            tail = path.rsplit("/", 1)[-1]
            if tail == "qualifiedcompany":
                self.qualify_answer = json.loads(response.text() or "{}")
            elif self.plan_body is not None and _same(tail, self.insurer):
                self.plan_answer = json.loads(response.text() or "{}")
        except Exception:
            pass

    # ---------------------------------------------------------------- results
    @property
    def done(self) -> bool:
        """The chosen insurer answered, or was never going to be asked."""
        if self.plan_answer is not None:
            return True
        if self.qualify_answer is None:
            return False
        plans = (self.qualify_answer.get("Response") or {}).get("QualifiedPlanList") or []
        return not any(_same(p.get("CompanyCode"), self.insurer) for p in plans)

    def template(self, product: str, label: str, when: str) -> Template | None:
        if not self.qualify or not self.api_base:
            return None
        added, removed = ({}, [])
        shape = self.plan_body or self.shape_body
        if shape:
            added, removed = learn_plan_shape(self.qualify, shape)
        clean = {k: v for k, v in self.qualify.items() if k != "QuotationNumber"}
        return Template(product=product, api_base=self.api_base, qualify=clean,
                        plan_added={k: v for k, v in added.items()
                                    if k not in FROM_ANSWER},
                        plan_removed=removed, captured=when, label=label)


def _same(a, b) -> bool:
    x, y = str(a or "").upper(), str(b or "").upper()
    return bool(x and y) and (x == y or x in y or y in x)
