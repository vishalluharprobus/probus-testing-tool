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
without notice, and that often has its own OTP or captcha. So we detect it,
record it, and hand back to a human rather than pretending to automate it.

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
              "steps, then stalls at Preview: CompanySpecificQuotation returns "
              "'No district found matching your state, city & pincode "
              "combination' inside an HTTP 200, because the portal sends "
              "VehicleAddressDetails.StateName/CityName empty while "
              "CommunicationAddressDetails has both. Reproduced 6/6. That is a "
              "product defect, not a harness limitation.",
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
    "BAJAJ": InsurerProfile(code="BAJAJ", kyc_style=UNKNOWN),
    "ICICI": InsurerProfile(code="ICICI", kyc_style=UNKNOWN),
    "TATA": InsurerProfile(code="TATA", kyc_style=UNKNOWN),
    "DIGIT": InsurerProfile(code="DIGIT", kyc_style=UNKNOWN),
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
