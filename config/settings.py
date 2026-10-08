"""
Where the tests point, and the guard rails that stop them pointing somewhere bad.

Credentials never live in this file. They are read from settings.local.json,
which is git-ignored, so a password cannot reach TFS by accident.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent
LOCAL_FILE = CONFIG_DIR / "settings.local.json"

# Every target must prove it is a test environment before a test runs.
# The portal renders this banner on non-production builds; if it is missing,
# we refuse rather than guess. See SafetyRefusal in core/safety.py.
STAGING_MARKER = "This is the PRE-LIVE (staging) environment"

# The banner is only built in when Saarthi's environment says prelive or
# isLocal (app.component.html). The deployed test site has neither, so it never
# shows one - checked 2026-10-06. A target like that proves itself differently
# (core/safety.py): the page must really be on one of these hosts, and the app
# on it must not be talking to the live back end. Fixed here in code on
# purpose - settings.local.json cannot add to it.
KNOWN_TEST_HOSTS = frozenset({"test.probusinsurance.com"})

# The live back end (Saarthi environment LIVEAPIURL, LIVEBUYAPIURL). An app that
# calls either of these is the live site, whatever its address says.
LIVE_API_HOSTS = frozenset({"api.probusinsurance.com", "buy.probusinsurance.com"})

# The live site's own journey host. Only the "live" target may be on it, and
# that target can never go past quotes (see Target.live below and
# core/safety.py). Found 2026-10-07: the "2 wheeler" card on
# www.probusinsurance.com redirects to buy.probusinsurance.com/motor-journey/.
KNOWN_LIVE_HOSTS = frozenset({"buy.probusinsurance.com"})

# The consent OTP on the Preview page. The developer environment accepts this
# fixed OTP for ANY mobile number, so no real SMS has to be read. If the
# developers change it, change it here - this is the only place it lives.
DEV_OTP = "87490"


@dataclass(frozen=True)
class Target:
    name: str
    base_url: str            # where the journey runs
    auth_base_url: str       # where we log in (always the deployed test site)
    needs_token_carry: bool  # copy .mob-token-w across origins?
    username: str = ""
    password: str = ""

    # --- guard rails -------------------------------------------------------
    # write_ceiling limits how far a run may go, regardless of what a scenario
    # asks for:
    #   "quote"    - quotes only; never submits a proposal
    #   "proposal" - may create a proposal at the insurer
    #   "payment"  - may reach the payment page (a human still pays)
    write_ceiling: str = "quote"
    # False = this build shows no banner; prove it by host instead (above).
    require_staging_banner: bool = True

    # Where the team's real KYC test documents live. Deliberately NOT in this
    # repository: they are genuine scans with a real Aadhaar number and PAN, so
    # the path comes from the git-ignored local settings file and the documents
    # stay wherever the team already keeps them.
    test_documents_dir: str = ""

    # The .NET API the Angular app fetches its master data from. The portal is
    # only the front half; when this is down the portal shows "Loading" forever
    # rather than an error, so a run checks it before opening a browser.
    # Overridable in settings.local.json, because the port is whatever the
    # developer's Visual Studio happens to use.
    api_url: str = ""

    # The portal's second API ("BUYURL" in Saarthi motor-routes.ts): it stores
    # a quotation so the share link /two-wheeler/result/<quotation number> can
    # open it again (api/v2/Client/MailQuotation, then .../GetQuotation). The
    # insurer lab uses it to open each of its API quotes in the browser and
    # carry on to KYC, proposal and payment.
    buy_url: str = ""

    # The real, customer-facing site. Quotes only, fixed in code: load()
    # ignores any write ceiling for it, and core/safety.py refuses anything
    # past a quote whatever this object says.
    live: bool = False

    # Log in by hand in the browser window (the live login may ask for an
    # OTP), once; the session is then saved and reused (core/auth.py).
    manual_login: bool = False

    @property
    def is_local(self) -> bool:
        return "localhost" in self.base_url or "127.0.0.1" in self.base_url


TARGETS = {
    # While developing. The local Angular build, authenticated by carrying a
    # token from the deployed site (see core/auth.py).
    "local": Target(
        name="local",
        base_url="http://localhost:4200",
        auth_base_url="https://test.probusinsurance.com",
        needs_token_carry=True,
        write_ceiling="quote",
        # Observed from the app's own network traffic, not guessed: the portal
        # on :4200 calls this for RTOcityJson and the previous-insurer list.
        api_url="http://localhost:53339",
        # Saarthi environment LOCALBUYAPIURL.
        buy_url="http://localhost:50251",
    ),
    # The deployed test site. Always up, so this is the one for nightly runs.
    # No token carrying needed - logging in is the whole story here.
    # The Angular app is served under /motor-journey/ (<base href> on the
    # page); /two-wheeler on its own is the server's 404 page.
    "testsite": Target(
        name="testsite",
        base_url="https://test.probusinsurance.com/motor-journey",
        auth_base_url="https://test.probusinsurance.com",
        needs_token_carry=False,
        # Shared with other people: quotes only, whatever the local file says
        # for your own machine. Raise it with "testsite_write_ceiling".
        write_ceiling="quote",
        require_staging_banner=False,
        # Seen in the test site's own network traffic (Saarthi TESTAPIURL).
        api_url="https://testapi.probusinsurance.com",
        # Its buy API (MailQuotation, KYC, proposal) is the site itself -
        # seen 2026-10-06: test.probusinsurance.com/api/v2/Client/MailQuotation.
        buy_url="https://test.probusinsurance.com",
    ),
    # THE LIVE SITE - real customers, real insurers. Quotes only, always:
    # nothing here can press Buy Now, fill KYC or reach a proposal. Each
    # journey costs one real quote request per insurer asked, so a run asks
    # only the insurers you name (run_quote_matrix.py --insurers).
    # Login is by hand, once (the live login can ask for an OTP); no live
    # password is ever stored.
    "live": Target(
        name="live",
        base_url="https://buy.probusinsurance.com/motor-journey",
        auth_base_url="https://buy.probusinsurance.com",
        needs_token_carry=False,
        write_ceiling="quote",
        require_staging_banner=False,
        api_url="https://api.probusinsurance.com",
        buy_url="https://buy.probusinsurance.com",
        live=True,
        manual_login=True,
    ),
}

DEFAULT_TARGET = "local"


def load(target_name: str | None = None) -> Target:
    """Build a Target with credentials merged in from the git-ignored local file."""
    name = target_name or DEFAULT_TARGET
    if name not in TARGETS:
        raise KeyError(f"Unknown target '{name}'. Choose from: {', '.join(TARGETS)}")

    base = TARGETS[name]
    secrets = _load_local_secrets()
    # "write_ceiling", "api_url" and "buy_url" in the local file describe the
    # developer's own machine, so they only reach a local target. A shared
    # site keeps its own values unless a "<target>_write_ceiling" key names it.
    mine = secrets if base.is_local else {}
    # The live site is quotes only, and no settings file can change that.
    ceiling = ("quote" if base.live else
               secrets.get(f"{base.name}_write_ceiling",
                           mine.get("write_ceiling", base.write_ceiling)))

    return Target(
        name=base.name,
        base_url=base.base_url,
        auth_base_url=base.auth_base_url,
        needs_token_carry=base.needs_token_carry,
        username=secrets.get("username", ""),
        password=secrets.get("password", ""),
        write_ceiling=ceiling,
        require_staging_banner=base.require_staging_banner,
        test_documents_dir=secrets.get("test_documents_dir", ""),
        api_url=mine.get("api_url", base.api_url),
        buy_url=mine.get("buy_url", base.buy_url),
        live=base.live,
        manual_login=base.manual_login,
    )


def _load_local_secrets() -> dict:
    if not LOCAL_FILE.exists():
        raise FileNotFoundError(
            f"Missing {LOCAL_FILE.name}.\n"
            f"Copy config/settings.local.example.json to config/settings.local.json "
            f"and put the portal username and password in it.\n"
            f"That file is git-ignored, so the password stays off TFS."
        )
    with LOCAL_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)
