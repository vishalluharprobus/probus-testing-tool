"""
Prove the two targets point at the right place and that the safety check still
refuses what it must - offline, in a second.

    venv\\Scripts\\python -m pytest tests/test_targets.py -q

Written 2026-10-06 after --target testsite stopped twice at login: the tool
opened test.probusinsurance.com/two-wheeler (the server's 404 page - the app
lives under /motor-journey/), and the test site never shows the PRE-LIVE
banner, so the guard rail refused it. Each test below is one of those facts.

Nothing here opens a browser or reads the real settings.local.json.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from core import safety  # noqa: E402

SECRETS = {"username": "u", "password": "p", "write_ceiling": "payment"}


@pytest.fixture
def load(monkeypatch):
    """settings.load with a made-up local file instead of the real one."""
    def _load(name, secrets=SECRETS):
        monkeypatch.setattr(settings, "_load_local_secrets", lambda: dict(secrets))
        return settings.load(name)
    return _load


class FakePage:
    """Just enough of a Playwright page for verify_environment."""

    def __init__(self, url, html="", fetched=()):
        self.url, self._html, self._fetched = url, html, list(fetched)

    def content(self):
        return self._html

    def evaluate(self, _script):
        return self._fetched


# ------------------------------------------------------------ where they point

def test_testsite_journey_lives_under_motor_journey(load):
    cfg = load("testsite")
    assert cfg.base_url == "https://test.probusinsurance.com/motor-journey"
    # The pages build their addresses as base_url + path.
    assert f"{cfg.base_url}/private-car/dontknownumber" == \
        "https://test.probusinsurance.com/motor-journey/private-car/dontknownumber"
    assert cfg.auth_base_url == "https://test.probusinsurance.com"
    assert cfg.api_url == "https://testapi.probusinsurance.com"
    assert not cfg.is_local


def test_local_target_unchanged(load):
    cfg = load("local")
    assert cfg.base_url == "http://localhost:4200"
    assert cfg.api_url == "http://localhost:53339"
    assert cfg.require_staging_banner


# ------------------------------------------------------------- write ceiling

def test_local_write_ceiling_comes_from_the_local_file(load):
    assert load("local").write_ceiling == "payment"


def test_shared_test_site_does_not_inherit_the_local_ceiling(load):
    assert load("testsite").write_ceiling == "quote"


def test_test_site_ceiling_can_be_raised_by_its_own_key(load):
    cfg = load("testsite", {**SECRETS, "testsite_write_ceiling": "proposal"})
    assert cfg.write_ceiling == "proposal"
    assert load("local", {**SECRETS, "testsite_write_ceiling": "proposal"}
                ).write_ceiling == "payment"


def test_local_api_override_does_not_reach_the_test_site(load):
    mine = {**SECRETS, "api_url": "http://localhost:9999"}
    assert load("local", mine).api_url == "http://localhost:9999"
    assert load("testsite", mine).api_url == "https://testapi.probusinsurance.com"


def test_refusal_names_the_key_to_raise(load):
    with pytest.raises(safety.SafetyRefusal, match="testsite_write_ceiling"):
        safety.allow("proposal", load("testsite"))


# ---------------------------------------------------------------- the proof

def test_local_still_needs_the_banner(load):
    cfg = load("local")
    with pytest.raises(safety.SafetyRefusal, match="staging banner"):
        safety.verify_environment(FakePage("http://localhost:4200/two-wheeler",
                                           "<div>no banner</div>"), cfg)
    safety.verify_environment(FakePage("http://localhost:4200/two-wheeler",
                                       f'<div title="{settings.STAGING_MARKER}">'), cfg)


def test_test_site_passes_on_its_own_host(load):
    safety.verify_environment(FakePage(
        "https://test.probusinsurance.com/motor-journey/two-wheeler",
        fetched=["https://testapi.probusinsurance.com/api/Motor/RTOcityJson"]),
        load("testsite"))


@pytest.mark.parametrize("url", [
    "https://www.probusinsurance.com/motor-journey/two-wheeler",
    "https://posp.probusinsurance.com/motor-journey/two-wheeler",
    "https://test.probusinsurance.com.evil.example/motor-journey/two-wheeler",
])
def test_test_site_refused_anywhere_else(load, url):
    with pytest.raises(safety.SafetyRefusal, match="known test host"):
        safety.verify_environment(FakePage(url), load("testsite"))


def test_test_site_refused_when_its_app_calls_the_live_api(load):
    page = FakePage("https://test.probusinsurance.com/motor-journey/two-wheeler",
                    fetched=["https://api.probusinsurance.com/api/Motor/RTOcityJson"])
    with pytest.raises(safety.SafetyRefusal, match="LIVE"):
        safety.verify_environment(page, load("testsite"))


def test_no_banner_never_means_no_check():
    """Turning the banner off must switch the host proof ON, not skip both."""
    cfg = SimpleNamespace(name="x", base_url="https://example.com",
                          require_staging_banner=False)
    with pytest.raises(safety.SafetyRefusal):
        safety.verify_environment(FakePage("https://example.com/two-wheeler"), cfg)
