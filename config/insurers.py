"""
What each insurer does differently.

The single most important idea in this file: insurer variation is DATA, not
code. Adding an insurer should be a few lines here, never a new Python module.
That is deliberate - the InsureBridge codebase has 91 near-duplicate per-insurer
service files precisely because variation was expressed as code, and nobody
wants to repeat that shape in the test tool.

KYC STYLES
----------
KYC is not one flow. The insurer decides which shape you get:

  inline     fill a form on our portal, insurer verifies from the PAN
  documents  inline, then upload identity + address proof files
  redirect   the browser LEAVES our site for the insurer's own portal, the
             customer does KYC there, and is sent back to us afterwards
  skipped    already-known CKYC, so the journey goes straight to the proposal

`redirect` is the awkward one. Once the browser is on the insurer's own website
we are driving somebody else's UI - one we have not mapped, that can change
without notice, and that often has its own OTP or captcha. So the harness tests
OUR two ends of it automatically - the handover out, and the way back in - and
leaves the insurer's own page to a person (run_proposal_test.py --kyc-redirect
stop | assist | abandon; see pages/kyc_redirect.py for how the round trip works).

A redirect is usually a FALLBACK, not a fixed style: the insurer searches the
central CKYC registry first and only redirects when that search fails. So the
same insurer can be inline one run and redirect the next (NATIONAL: 17 inline,
3 redirect with the same customer).

The values below are EXPECTATIONS, not rules. The runtime detector looks at
where the browser actually goes and trusts that instead - config is there so a
surprise ("Liberty went inline today?") shows up as a flagged difference rather
than passing unnoticed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

INLINE = "inline"
DOCUMENTS = "documents"
REDIRECT = "redirect"
SKIPPED = "skipped"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class InsurerProfile:
    code: str                         # logo alt text on the quote card
    kyc_style: str = UNKNOWN
    notes: str = ""
    # Hosts the KYC redirect is expected to land on. Used to tell "we were sent
    # to the insurer, as designed" apart from "we were sent somewhere unexpected",
    # which is a finding rather than a normal step.
    kyc_hosts: tuple[str, ...] = field(default_factory=tuple)

    # A known wall this insurer hits that we cannot fix from the test side -
    # a product defect, not a gap in the harness. Recorded so the runner can
    # warn BEFORE spending three minutes driving into it, and so nobody
    # rediscovers it next week.
    known_blocker: str = ""


PROFILES: dict[str, InsurerProfile] = {
    "ZUNO": InsurerProfile(
        code="ZUNO",
        kyc_style=DOCUMENTS,
        notes="CONFIRMED 2026-09-15. Inline EKYC form, then an identity + "
              "address document upload - both accept 'Aadhaar Card', so the "
              "front goes in as identity and the back as address proof. Ends "
              "with a 'KYC verification success.' dialog. Its occupation list "
              "does include 'Farmer'. Did NOT hit the district error that "
              "stops NATIONAL at Preview.",
    ),
    "LIBERTY": InsurerProfile(
        code="LIBERTY",
        kyc_style=REDIRECT,
        notes="Sends the browser to Liberty's own portal for KYC, then returns. "
              "Not automatable end to end - see REDIRECT above.",
        # REQUIRED INFORMATION: the actual host Liberty redirects to. Left empty
        # on purpose - the detector will report whatever host it sees, and that
        # observed value is what should be filled in here.
    ),
    "NATIONAL": InsurerProfile(
        code="NATIONAL",
        kyc_style=INLINE,
        notes="CONFIRMED 2026-09-15 by repeated runs. Verifies from the PAN "
              "alone - no document upload - and answers with a 'KYC "
              "verification success.' dialog that MUST be dismissed before the "
              "app will move on. Reaches the proposal and completes all three "
              "steps, then Preview answers 'No district found matching your "
              "state, city & pincode combination' (16/16 runs with 380009). "
              "CORRECTED 2026-09-25 from the server code: the empty "
              "VehicleAddressDetails.StateName/CityName is a red herring. "
              "InsureBridge TWNationalInsuranceRules."
              "PrepareCompanySpecificQuoteModelNew looks up the COMMUNICATION "
              "address ids via sp_get_nic_city_master(22, 922001, 380009), and "
              "NATIONAL's city table has no row for that pincode. The harness "
              "now switches pincode itself and remembers which ones NATIONAL "
              "accepts - see core/addressnotes.py. "
              "REDIRECTS when its CKYC search misses (3 runs in 20): the answer "
              "carries a Signzy login link in ExtraParameter1, and the browser "
              "goes go.onboardings.co -> link-kyc.idv.hyperverge.co. Its way "
              "back (/KYCBridge/NATIONALKYCResponse) is fixed on the server to "
              "test.probusinsurance.com, and our server asks Signzy for the "
              "result rather than trusting the browser.",
        kyc_hosts=("go.onboardings.co", "link-kyc.idv.hyperverge.co"),
    ),
    "SBI": InsurerProfile(
        code="SBI",
        kyc_style=REDIRECT,
        notes="From the server code, 2026-09-29 - not yet seen on a two-wheeler "
              "run (SBI has not appeared in any quote list here). CKYC search "
              "by PAN first; 'No record found' or a DOB mismatch answers "
              "CKYCStatus Fail with SBI's CKYCSubmissionURL in ExtraParameter1, "
              "and the browser is auto-POSTed to SBI from our /KYCBridge. The "
              "way back is /KYCBridge/SBIKYCResponse on test.probusinsurance.com, "
              "and our server checks the result with SBI itself. When SBI says "
              "not complete, that page SPINS FOR EVER with no message - the "
              "harness reports this as 'our return page never moved on'. Also "
              "worth a test: the way-back lookup has no two-wheeler product "
              "branch (it maps to private car / PV).",
    ),
    "UNITED": InsurerProfile(
        code="UNITED",
        kyc_style=UNKNOWN,
        notes="Seen once handing out a hyperverge link (ExtraParameter1 -> "
              "link-kyc.idv.hyperverge.co) with CKYCStatus Fail. A database "
              "'KycBypass' switch can make it inline instead, so either shape "
              "is normal.",
    ),
    "RELIANCE": InsurerProfile(
        code="RELIANCE",
        kyc_style=REDIRECT,
        kyc_hosts=("ukyc.brobotinsurance.com",),
        notes="CONFIRMED 2026-09-15 by a real run. Looks inline - the KYC form "
              "is on our portal - but SUBMITTING it hands the browser to "
              "ukyc.brobotinsurance.com. So the redirect happens after the "
              "form, not instead of it. Everything up to the handover is "
              "automatable; the external portal is not.",
    ),
    "IFFCOTOKIO": InsurerProfile(
        code="IFFCOTOKIO",
        kyc_style=DOCUMENTS,
        notes="CONFIRMED 2026-09-15. Identity proof dropdown offers ONLY 'PAN "
              "Card' - not Aadhaar - and each proof needs its NUMBER typed as "
              "well as a file. A user photograph is required too, in a third "
              "slot with no document-type dropdown. KYC is ASYNCHRONOUS: it "
              "answers 'Your KYC request is successfully logged. KYC "
              "verification process will execute automatically soon.' rather "
              "than a pass/fail. Occupation list is BUISNESSMAN / HOUSEWIFE / "
              "OTHER / STUDENT (their spelling).",
    ),
    # The rest: read from the server code 2026-10-06 (InsureBridge
    # InsureBridge.Service.KYC/*ResponseExtension.cs, Agent/KYCAgentController.cs).
    # "Never redirects" = no code path hands the browser to the insurer.
    "BAJAJ": InsurerProfile(
        code="BAJAJ", kyc_style=INLINE,
        notes="Inline; never redirects. Asks for a document upload when the ID "
              "proof is found but the address proof is not; has a KYC OCR path "
              "(Aadhaar front/back). BajajResponseExtension.cs:192-231."),
    "ICICI": InsurerProfile(
        code="ICICI", kyc_style=INLINE,
        notes="Inline; never redirects. A failed non-CKYC document means a "
              "document upload. ICICIResponseExtension.cs:32-34, 147-161."),
    "TATA": InsurerProfile(
        code="TATA", kyc_style=SKIPPED,
        notes="No check at the KYC screen ('TATA will do KYC at proposal "
              "stage'): the real check runs inside the proposal call. Form60 "
              "means an upload. Never redirects. PCTATAServiceV2.cs:300-347."),
    "DIGIT": InsurerProfile(
        code="DIGIT", kyc_style=SKIPPED,
        notes="Never redirects: the KYC screen saves the request as Pending and "
              "Digit verifies AFTER payment, by e-mailing the customer a link "
              "(DigitService.cs:18-37). The portal skips KYC altogether when the "
              "premium is Rs 5,000 or less (kyc-waiver.util.ts). Chosen "
              "2026-10-06 as the first non-redirect car insurer to take to "
              "payment on the test site (Hyundai Creta, MH-01)."),
    "SHRIRAM": InsurerProfile(
        code="SHRIRAM", kyc_style=DOCUMENTS,
        notes="No live check: saved as Pending. Offers CKYC number or 'Upload "
              "Document' (father's name, photo, ID + address proof) - no PAN. "
              "Never redirects. SHRIRAMService.cs:19-33."),
    "ROYALSUNDRAM": InsurerProfile(
        code="ROYALSUNDRAM", kyc_style=INLINE,
        notes="Inline CKYC search; REDIRECTS as a fallback when the insurer "
              "answers with a url. RSAResponseExtension.cs:160-170."),
    "CHOLAMANDLAM": InsurerProfile(
        code="CHOLAMANDLAM", kyc_style=INLINE,
        notes="Inline; redirects as a fallback only when the server flag "
              "IsCholaRedirectAllow=1. CHOLAMANDLAMResponseExtension.cs:182-208."),
    "HDFCERGO": InsurerProfile(
        code="HDFCERGO", kyc_style=INLINE,
        notes="Inline (PAN, Aadhaar or CKYC); redirects as a fallback when "
              "iskycVerified=0. HDFCResponseExtension.cs:188-196."),
    "KOTAK": InsurerProfile(
        code="KOTAK", kyc_style=INLINE,
        notes="Inline CKYC; redirects whenever the answer carries TokenId + "
              "RequestURL (probably fallback only - unconfirmed). "
              "KotakResponseExtension.cs:95-108."),
    "FUTURE": InsurerProfile(
        code="FUTURE", kyc_style=INLINE,
        notes="Inline; redirects as a fallback only when IsFutureRedirectAllow=1. "
              "FutureResponseExtension.cs:165-196."),
}


def profile_for(insurer: str) -> InsurerProfile:
    """
    Look up an insurer, tolerating the portal's naming.

    The quote card's logo alt is not always the tidy code - "LIBERTYVGI" and
    "ZURICHKOTAK" both appear in InsureBridge - so we match loosely rather than
    failing on an exact-key miss.
    """
    key = insurer.strip().upper()
    if key in PROFILES:
        return PROFILES[key]
    for code, profile in PROFILES.items():
        if code in key or key in code:
            return profile
    return InsurerProfile(code=key, kyc_style=UNKNOWN,
                          notes="Not yet profiled - the run will report what it finds.")
