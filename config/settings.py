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
    ),
    # The deployed test site. Always up, so this is the one for nightly runs.
    # No token carrying needed - logging in is the whole story here.
    "testsite": Target(
        name="testsite",
        base_url="https://test.probusinsurance.com",
        auth_base_url="https://test.probusinsurance.com",
        needs_token_carry=False,
        write_ceiling="quote",
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

    return Target(
        name=base.name,
        base_url=base.base_url,
        auth_base_url=base.auth_base_url,
        needs_token_carry=base.needs_token_carry,
        username=secrets.get("username", ""),
        password=secrets.get("password", ""),
        write_ceiling=secrets.get("write_ceiling", base.write_ceiling),
        require_staging_banner=base.require_staging_banner,
        test_documents_dir=secrets.get("test_documents_dir", ""),
        api_url=secrets.get("api_url", base.api_url),
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
