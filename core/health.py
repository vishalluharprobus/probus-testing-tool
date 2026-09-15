"""
Telling "the environment is broken" apart from "the app has a bug".

A test suite that reports both as FAILED is a suite nobody trusts: the team
spends a morning hunting a regression that was really a quota limit or a dev
server that had stopped. So the harness watches the browser console and the
network while it drives, and classifies what it sees.

The first rule here came from a real run: the portal hung on "Loading" forever
and the only clue was a Firebase warning saying the database had reached its
peak connection count. Without this, that surfaces as a 20-second timeout on
whatever field happened to be first, which points at entirely the wrong thing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Health(Enum):
    OK = "ok"
    ENVIRONMENT = "environment"   # infrastructure - not the app's fault, not ours
    APP_ERROR = "app-error"       # the app itself threw
    UNKNOWN = "unknown"


# Console text -> (classification, plain-English explanation). Matched as a
# case-insensitive substring, so these stay readable rather than regexy.
SIGNATURES: tuple[tuple[str, Health, str], ...] = (
    # Exact wording seen in the wild: "has reached its peak connections limit".
    # Matching on "peak connection" catches both that and the singular variant.
    ("peak connection", Health.ENVIRONMENT,
     "Firebase Realtime Database has hit its CONNECTION LIMIT, so the portal "
     "hangs on 'Loading' and no form ever renders.\n"
     "  This is a quota/infrastructure problem - nothing to do with the test or "
     "the app's code.\n"
     "  What to do: wait for connections to free up, check who else is "
     "connected to that Firebase project, and avoid running several browsers "
     "at once against this environment."),
    ("firebase database", Health.ENVIRONMENT,
     "Firebase reported a problem; the app may not load."),
    ("err_connection_refused", Health.ENVIRONMENT,
     "Nothing is listening. Is 'ng serve' still running?"),
    ("err_name_not_resolved", Health.ENVIRONMENT,
     "DNS lookup failed - check VPN or network."),
    ("500 (internal server error)", Health.APP_ERROR,
     "The backend returned a 500."),
)

# Noise that is always present and means nothing. Filtered so the report shows
# only what a human should look at.
IGNORE = (
    "google-analytics.com",
    "injection context",
    "may destabilize your application",
    "favicon",
)


@dataclass
class Diagnosis:
    verdict: Health = Health.OK
    explanation: str = ""
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)

    @property
    def is_environment_problem(self) -> bool:
        return self.verdict is Health.ENVIRONMENT


class Watcher:
    """
    Attach before driving; ask for a diagnosis afterwards.

        watcher = Watcher.for_context(context)   # covers every page, incl. login
        ... drive the journey ...
        diag = watcher.diagnose()

    Prefer for_context(): the most useful errors often appear during the very
    first page load, which happens inside the login helper - before any page
    object exists to attach to.
    """

    def __init__(self, page=None):
        self._messages: list[str] = []
        self._failures: list[str] = []
        if page is not None:
            self.attach(page)

    @classmethod
    def for_context(cls, context) -> "Watcher":
        watcher = cls()
        for existing in context.pages:
            watcher.attach(existing)
        context.on("page", watcher.attach)
        return watcher

    def attach(self, page) -> None:
        page.on("console", self._on_console)
        page.on("requestfailed", self._on_request_failed)
        page.on("response", self._on_response)

    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            self._keep(self._messages, f"{msg.type}: {msg.text}")

    def _on_request_failed(self, req) -> None:
        self._keep(self._failures, f"{req.method} {req.url} -> {req.failure}")

    def _on_response(self, resp) -> None:
        if resp.status >= 400:
            self._keep(self._failures, f"HTTP {resp.status} {resp.url}")

    @staticmethod
    def _keep(bucket: list[str], line: str) -> None:
        low = line.lower()
        if any(noise in low for noise in IGNORE):
            return
        if len(bucket) < 60:
            bucket.append(line)

    def diagnose(self, page=None) -> Diagnosis:
        """
        Classify what went wrong.

        `page` is optional but strongly recommended. Without it we can only say
        "a Firebase warning appeared at some point in this run" - and since these
        warnings appear EARLY and the console buffer covers the whole run, that
        would blame infrastructure for any failure that happened later, including
        a plain selector bug three screens on.

        So an ENVIRONMENT verdict now needs corroboration: the page must still be
        sitting on "Loading". A warning alone is recorded but not blamed.
        """
        haystack = " ".join(self._messages + self._failures).lower()
        for needle, verdict, explanation in SIGNATURES:
            if needle not in haystack:
                continue

            if verdict is Health.ENVIRONMENT and page is not None:
                if not _page_is_stuck(page):
                    # The warning happened, but the app rendered fine afterwards -
                    # so whatever failed later is a different problem. Keep looking.
                    continue

            return Diagnosis(verdict, explanation,
                             self._messages[:10], self._failures[:10])
        return Diagnosis(Health.OK, "", self._messages[:10], self._failures[:10])


def _page_is_stuck(page) -> bool:
    """Is the app still showing its loading placeholder rather than content?"""
    try:
        body = page.inner_text("body")
    except Exception:
        return False
    return "Loading" in body and len(body) < 400
