"""
Rehearse the KYC redirect test against a FAKE insurer - no portal, no network.

    python tests/rehearse_kyc_redirect.py                 # every scenario, ~2 min
    python tests/rehearse_kyc_redirect.py --headed        # watch it happen
    python tests/rehearse_kyc_redirect.py --only abandon  # just some of them

WHY THIS EXISTS
---------------
A real redirect is rare and slow to reach: NATIONAL's CKYC search finds our
test customer 17 times in 20, so most runs never redirect, and each one that
does costs minutes of quote fan-out first. That is no way to find out whether
the redirect TEST itself reaches the right verdict.

So this builds a small fake of every party, inside the browser, copying the
real flow as read from Saarthi, the MVC portal and InsureBridge:

    our KYC screen    localhost:4200/two-wheeler/kyc-insurance
                      asks "...redirect to you on insurance company portal"
                      Okay / Close, like the real SweetAlert
    our KYC API       localhost:53339/api/kyc/wb/validate   (NATIONAL's answer shape)
    our launch page   localhost:50251/KYCBridge?QuotationNo=..  -> the insurer
    the insurer       go.onboardings.co                      (a fake KYC page)
    its way back      <host>/KYCBridge/NATIONALKYCResponse   -> /KYC/Success
                      -> the proposal, "KYC Failed", or a spinner for ever

and runs the real code - pages/kyc.py, pages/kyc_redirect.py and the runner's
follow_redirect() - against each way the round trip can go. Every scenario
states the RIGHT verdict; the rehearsal passes only if the tool reaches it.

Nothing here touches the real portal, a real insurer, or reports/kyc_styles.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

import run_proposal_test as runner  # noqa: E402
from core import console, kycnotes  # noqa: E402
from data.customer import DEFAULT as CUSTOMER  # noqa: E402
from pages import kyc_redirect  # noqa: E402
from pages.kyc import KycPage  # noqa: E402
from pages.kyc_redirect import KycRedirect  # noqa: E402

QUOTE = "PIBLMTRTW2026092900000001"
KYC_ID = "2f2884c7-0000-0000-0000-rehearsal000"
APP = "http://localhost:4200"
INSURER_LINK = "https://go.onboardings.co/NIC/al/REHEARSAL-TOKEN"
LAUNCH = f"http://localhost:50251/KYCBridge?QuotationNo={QUOTE}&KYCProposalNo={KYC_ID}"
WAY_BACK = f"http://localhost:50251/KYCBridge/NATIONALKYCResponse?txn_id={QUOTE}"
TEST_SITE_WAY_BACK = (f"https://test.probusinsurance.com/KYCBridge/"
                      f"NATIONALKYCResponse?txn_id={QUOTE}")


@dataclass
class Scenario:
    name: str
    mode: str                  # stop | assist | abandon  (what --kyc-redirect would be)
    expect_code: int           # the exit code a correct tool reaches
    expect_landing: str = ""   # where it should judge the customer ended up
    opens: str = "same"        # same | new | none  - how our KYC screen leaves
    launch: str = LAUNCH       # ReDirectionURL in the validate answer
    way_back: str = WAY_BACK   # where the insurer sends the customer afterwards
    approves: bool = True      # does the insurer approve the KYC?
    bridge: str = "honest"     # honest | spins  - our way-back page
    leaks: bool = False        # KYC status says "done" once the launch happened
    insurer_status: int = 200
    human: bool = False        # a fake person finishes the insurer's page
    story: str = ""


SCENARIOS = [
    Scenario("handover only", "stop", 0,
             story="the default: prove the handover and stop at the insurer"),
    Scenario("finishes, same tab", "assist", 0, "proposal", human=True,
             story="a person completes the insurer's KYC; the portal must open "
                   "the proposal"),
    Scenario("local run, back to test site", "assist", 5, "other-copy", human=True,
             way_back=TEST_SITE_WAY_BACK,
             story="the insurer's way back is fixed to test.probusinsurance.com, "
                   "so a local journey cannot resume"),
    Scenario("insurer refuses", "assist", 5, "kyc-failed", human=True, approves=False,
             story="the insurer says no; our portal must say 'KYC Failed'"),
    Scenario("way-back page spins", "assist", 1, "bridge", human=True,
             approves=False, bridge="spins",
             story="SBI's shape: not approved, so our page spins for ever"),
    Scenario("gives up, same tab", "abandon", 0, "kyc",
             story="the customer gives up and goes back to our KYC screen"),
    Scenario("gives up, NEW tab", "abandon", 0, "kyc", opens="new",
             story="insurer opened in a second tab, as NIVABUPA does"),
    Scenario("status leak", "abandon", 1, "proposal", leaks=True,
             story="our server calls KYC done as soon as the launch happens - "
                   "must be reported as a FINDING"),
    Scenario("wrong environment", "stop", 1,
             launch=f"https://www.probusinsurance.com/KYCBridge?QuotationNo={QUOTE}",
             story="the launch page is on a different copy of the portal"),
    Scenario("insurer link expired", "stop", 1, insurer_status=404,
             story="the insurer's page answers 404 'link has expired'"),
    Scenario("never leaves", "stop", 1, opens="none",
             story="the server chose a redirect, but the screen never goes "
                   "(SaveKYCQuotationParameter failing does exactly this)"),
]


# ------------------------------------------------------------------ the fakes

KYC_HTML = """<!doctype html><title>KYC (fake)</title>
<h3>EKYC Details</h3>
<input formcontrolname="pan" value="ATTPB2942G">
<button id="proceed">keyboard_backspace Proceed</button>
<div id="ask" class="swal2-popup" style="display:none;position:fixed;top:30%;
     left:25%;background:#fff;border:1px solid #333;padding:20px">
  <div class="swal2-html-container">Your details are not fulfilled with kyc
    process, so we are redirect to you on insurance company portal.</div>
  <button id="okay">Okay</button> <button id="close">Close</button>
</div>
<script>
  const S = SETTINGS;
  // On arrival the real screen asks our server whether KYC is already done
  // (api/kyc/CheckInsuranceKYCStatus) and moves on to the proposal if so.
  fetch('http://localhost:50251/api/kyc/CheckInsuranceKYCStatus?quoteNo=' + S.quote)
    .then(r => r.json())
    .then(j => { if (j.KYCStatus) location.href = '/two-wheeler/proposal?from=kyc'; });

  document.getElementById('proceed').onclick = async () => {
    // text/plain keeps it a "simple" request: no CORS preflight to fake.
    const r = await fetch('http://localhost:53339/api/kyc/wb/validate',
                          {method: 'POST', body: '{}',
                           headers: {'Content-Type': 'text/plain'}});
    const link = (await r.json()).Response.ReDirectionURL;
    if (S.opens === 'none') return;
    if (S.opens === 'new') { window.open(link, '_blank'); return; }
    document.getElementById('ask').style.display = 'block';
    document.getElementById('okay').onclick = () => { location.href = link; };
    document.getElementById('close').onclick =
        () => { document.getElementById('ask').style.display = 'none'; };
  };
</script>"""

INSURER_HTML = """<!doctype html><title>NIC KYC (fake insurer)</title>
<h2>Complete your KYC</h2>
<p><label for="pan">PAN Number</label> <input id="pan" name="panNumber"></p>
<p><label for="nm">Name as per PAN</label> <input id="nm" name="fullName"></p>
<p><label for="dob">Date of Birth</label> <input id="dob" placeholder="DD/MM/YYYY"></p>
<p><label for="mob">Mobile Number</label> <input id="mob" type="tel"></p>
<p><label for="aad">Aadhaar Number</label> <input id="aad" name="aadhaar"></p>
<p><label for="otp">Enter OTP</label> <input id="otp" name="otp"></p>
<button id="verify">Verify</button>
<script>
  const S = SETTINGS;
  document.getElementById('verify').onclick = async () => {
    await fetch('/api/verify', {method: 'POST', body: 'x'});
    location.href = S.wayBack;
  };
  // The fake PERSON: once the tool has typed the PAN, they read the page,
  // type the OTP that "arrived on their phone", and press Verify.
  if (S.human) {
    const t = setInterval(() => {
      if (!document.getElementById('pan').value) return;
      clearInterval(t);
      setTimeout(() => { document.getElementById('otp').value = '123456';
                         document.getElementById('verify').click(); }, 3000);
    }, 300);
  }
</script>"""

EXPIRED_HTML = "<!doctype html><title>Error</title><h1>This link has expired</h1>"
PROPOSAL_HTML = "<!doctype html><title>Proposal (fake)</title><h3>Personal Details</h3>"
SPINNER_HTML = "<!doctype html><title>KYCBridge</title><p>Processing, please wait...</p>"
FAILED_HTML = "<!doctype html><title>KYC</title><h2>KYC Failed</h2><p>Please try again.</p>"


def validate_answer(launch: str) -> dict:
    """NATIONAL's real answer shape (run 20260928-125412), with fake values."""
    return {"Error": None, "IsAuthenticated": True, "Message": None, "StatusCode": 200,
            "Response": {
                "Status": "Success", "CKYCStatus": "Fail", "CompanyCode": "NATIONAL",
                "CompanyReferanceDetails": {"ExtraParameter1": INSURER_LINK,
                                            "ExtraParameter2": KYC_ID,
                                            "ExtraParameter3": "100003300569"},
                "ReDirectionURL": launch, "KycProposalNumber": KYC_ID,
                "QuotationNumber": QUOTE, "ErrorMessage": "",
                "LogoUrl": "https://cdn.example.com/national-logo.png"}}


class FakeWorld:
    def __init__(self, s: Scenario):
        self.s = s
        self.launched = False
        self.kyc_done = False

    def html(self, route, body: str, status: int = 200) -> None:
        route.fulfill(status=status, content_type="text/html", body=body)

    def json(self, route, data) -> None:
        route.fulfill(status=200, content_type="application/json",
                      headers={"Access-Control-Allow-Origin": "*"},
                      body=json.dumps(data))

    def forward(self, route, where: str) -> None:
        # A server page that moves the browser on with a script, as ours do.
        self.html(route, f"<!doctype html><title>KYCBridge</title>"
                         f"<script>location.replace({json.dumps(where)})</script>")

    def handle(self, route, request) -> None:
        u = urlparse(request.url)
        host, port, path = u.hostname or "", u.port, u.path.lower()
        s = self.s
        origin = f"{u.scheme}://{u.netloc}"

        if host == "localhost" and port == 4200:
            if path.startswith("/two-wheeler/proposal"):
                return self.html(route, PROPOSAL_HTML)
            if path.startswith("/two-wheeler/kyc-insurance"):
                settings = {"opens": s.opens, "quote": QUOTE}
                return self.html(route, KYC_HTML.replace("SETTINGS", json.dumps(settings)))
        if host == "localhost" and port == 53339 and "kyc/wb/validate" in path:
            return self.json(route, validate_answer(s.launch))
        if "checkinsurancekycstatus" in path:
            return self.json(route, {"KYCStatus": self.kyc_done
                                     or (s.leaks and self.launched)})
        if host == "go.onboardings.co":
            if path == "/api/verify":
                self.kyc_done = s.approves
                return route.fulfill(status=200, body="{}")
            if s.insurer_status >= 400:
                return self.html(route, EXPIRED_HTML, s.insurer_status)
            settings = {"wayBack": s.way_back, "human": s.human}
            return self.html(route, INSURER_HTML.replace("SETTINGS", json.dumps(settings)))
        if path == "/kycbridge":                       # the launch page
            self.launched = True
            return self.forward(route, INSURER_LINK)
        if path.startswith("/kycbridge/") and "kycresponse" in path:  # the way back
            if s.bridge == "spins" and not self.kyc_done:
                return self.html(route, SPINNER_HTML)
            return self.forward(route, f"{origin}/KYC/Success?QuotationNo={QUOTE}")
        if path == "/kyc/success":
            if not self.kyc_done:
                return self.html(route, FAILED_HTML)
            if host == "localhost":
                return self.forward(route, f"{APP}/two-wheeler/proposal?QuotationNo={QUOTE}")
            # The MVC server forwards to its own, older proposal page.
            return self.forward(route, f"{origin}/insurance/two-wheeler-insurance/"
                                       f"Proposal?QuotationNumber={QUOTE}")
        if "/insurance/two-wheeler-insurance/proposal" in path:
            return self.html(route, PROPOSAL_HTML)
        route.abort()          # nothing else exists in this world


# ------------------------------------------------------------------- running

def run(s: Scenario, browser, work: Path) -> tuple[bool, str]:
    context = browser.new_context(viewport={"width": 1200, "height": 800})
    context.route("**/*", FakeWorld(s).handle)
    try:
        page = context.new_page()
        redirect = KycRedirect(context, page, {"localhost"})
        page.goto(f"{APP}/two-wheeler/kyc-insurance")

        kyc = KycPage(page)
        style, _ = kyc.detect_style("localhost", timeout_ms=10_000)
        if style != "inline":
            return False, f"our fake KYC screen was read as '{style}'"
        kyc.proceed()
        if s.opens == "none":
            # The real runner first sits out the KYC waits (about 100s) and
            # then reaches this same verdict; the rehearsal skips to it.
            page.wait_for_timeout(1500)
        else:
            kyc.wait_for_kyc_response(timeout_ms=15_000)
            kyc.settle(timeout_ms=15_000)

        args = SimpleNamespace(kyc_redirect=s.mode, kyc_wait=1, proposal=False)
        target = SimpleNamespace(insurer="REHEARSAL")
        code, tab = runner.follow_redirect(redirect, kyc, target, args, work, APP)

        # (None, tab) means "carry on into the proposal" - exit code 0.
        got = 0 if code is None else code
        landed = redirect.landing.kind if redirect.landing else ""

        problems = []
        if got != s.expect_code:
            problems.append(f"exit code {got}, expected {s.expect_code}")
        if s.expect_landing and landed != s.expect_landing:
            problems.append(f"judged the landing '{landed or 'nothing'}', "
                            f"expected '{s.expect_landing}'")
        if s.opens == "new" and redirect.opened_in != "new tab":
            problems.append(f"insurer opened in '{redirect.opened_in}', "
                            f"expected the new tab")
        if s.human:
            filled = " | ".join(redirect.filled).lower()
            for must in ("pan number", "name as per pan", "date of birth", "mobile"):
                if must not in filled:
                    problems.append(f"did not type into the '{must}' box")
            for never in ("otp", "aadhaar"):
                if never in filled:
                    problems.append(f"typed into the {never} box - it must never")
        return not problems, "; ".join(problems) or "right verdict"
    finally:
        context.close()


def unit_checks() -> list[str]:
    """The small decisions, checked without a browser."""
    problems = []
    cases = [
        ("pannumber pan pan number", "text", CUSTOMER.pan),
        ("fullname nm name as per pan", "text", "Dilip Bharat Bubane"),
        ("enter otp", "text", ""),
        ("aadhaar number", "text", ""),
        ("mpin", "text", ""),
        ("pin code", "text", CUSTOMER.pincode),
        ("dob date of birth dd/mm/yyyy", "text", "25/06/1983"),
        ("birthdate", "date", "1983-06-25"),
        ("company name", "text", ""),
        ("father's name", "text", ""),
        ("first name", "text", CUSTOMER.first_name),
        ("lname surname", "text", CUSTOMER.last_name),
        ("mobile number", "tel", CUSTOMER.mobile),
        ("email address", "email", CUSTOMER.email),
    ]
    for hint, kind, want in cases:
        got = kyc_redirect._value_for(hint, kind, CUSTOMER)
        if got != want:
            problems.append(f"box '{hint}' got {got!r}, expected {want!r}")

    # Reading the answer: the logo URL in the same answer must be ignored.
    probe = KycRedirect.__new__(KycRedirect)
    probe.home_hosts = {"localhost"}
    probe.handover = kyc_redirect.Handover()
    probe._read_handover(validate_answer(LAUNCH),
                         "http://localhost:53339/api/kyc/wb/validate")
    h = probe.handover
    if h.insurer_link != INSURER_LINK:
        problems.append(f"read the insurer link as {h.insurer_link!r}")
    if h.launch_link != LAUNCH:
        problems.append(f"read the launch link as {h.launch_link!r}")
    if (h.ckyc_status, h.quotation_no, h.kyc_proposal_no) != ("Fail", QUOTE, KYC_ID):
        problems.append(f"read status/quote/id as {h.ckyc_status}/{h.quotation_no}/"
                        f"{h.kyc_proposal_no}")

    # Tokens must never reach a report: NATIONAL's link path is a signed JWT.
    shown = kyc_redirect.short_url("https://go.onboardings.co/NIC/al/"
                                   "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.abc?t=secret")
    if "eyJ" in shown or "secret" in shown:
        problems.append(f"a token survived into the report: {shown}")
    return problems


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--only", default="", help="run scenarios whose name contains this")
    args = ap.parse_args()

    # Fast waypoints: the fakes answer instantly, so a stuck page is obvious
    # in seconds rather than the real run's 45.
    KycRedirect.BRIDGE_PATIENCE_MS = 6000
    KycRedirect.ELSEWHERE_PATIENCE_MS = 5000
    KycRedirect.KYC_SETTLE_S = 5

    results: list[tuple[str, bool, str]] = []
    problems = unit_checks()
    results.append(("small decisions (boxes, answer, tokens)",
                    not problems, "; ".join(problems) or "all right"))

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        kycnotes.NOTES_FILE = work / "kyc_styles.json"     # never the real notebook
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            for s in SCENARIOS:
                if args.only and args.only.lower() not in f"{s.name} {s.mode}".lower():
                    continue
                print(f"\n\n{'#' * 70}\n# REHEARSAL: {s.name}  (--kyc-redirect {s.mode})"
                      f"\n#   {s.story}\n{'#' * 70}")
                try:
                    ok, why = run(s, browser, work)
                except Exception as exc:
                    ok, why = False, f"{type(exc).__name__}: {exc}"
                results.append((f"{s.name} ({s.mode})", ok, why))
            browser.close()

    print(f"\n\n{'=' * 70}\nREHEARSAL SUMMARY - did the tool reach the right verdict?"
          f"\n{'=' * 70}")
    for name, ok, why in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<42} {why}")
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n  {len(results) - failed} of {len(results)} right.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
