"""
The test customer used across the buy journey.

Taken from the team's own working example, so it is known to pass KYC on the
test environment. Everything here is test data on a test environment - but it is
still personal-shaped data, so treat it accordingly: this file is committed,
so never put a real customer's details in it.

One value deserves a note. The PAN is the one the team demonstrated, and PAN is
what the insurer actually verifies - if KYC starts failing for everyone, check
here first, because a PAN that stops working looks exactly like a broken test.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Customer:
    # --- identity (KYC screen) ---------------------------------------------
    pan: str = "ATTPB2942G"
    first_name: str = "Dilip"
    # "Bharat" - taken from the team's own proposal screen, where the customer
    # shows as "Mr. Dilip Bharat Bubane". It was left blank here at first, which
    # was simply wrong: some insurers require a middle name at KYC, and an empty
    # one silently blocked the form with no obvious cause. Use the real value.
    middle_name: str = "Bharat"
    last_name: str = "Bubane"
    gender: str = "Male"
    dob_kyc: str = "25 Jun 1983"        # as entered on the KYC screen
    mobile: str = "9512296502"
    email: str = "vishal@probus.com"
    pincode: str = "380009"

    # --- proposal screen ----------------------------------------------------
    # The portal asks for the date of birth again on the proposal form, and the
    # team's example used a different value there than on KYC. Kept separate
    # rather than unified, because the app treats them as separate fields and a
    # test should send what a human sends.
    # Both of these are the portal's OWN spellings, read back from the live
    # dropdowns rather than assumed. "Mr." fails because the option is "Mr"
    # with no full stop, and "Farmer" is not in the list at all - the insurer's
    # occupation master calls it "Agriculturist". Two small differences that
    # blocked the proposal form completely.
    salutation: str = "Mr"
    dob_proposal: str = "01 Jan 1984"
    marital_status: str = "Single"
    occupation: str = "Agriculturist"

    # ALTERNATIVES, because insurers do not share a vocabulary.
    #
    # The occupation dropdown offers "Agriculturist" at NATIONAL and only
    # "BUISNESSMAN / HOUSEWIFE / OTHER / STUDENT" at IFFCOTOKIO - their spelling
    # of businessman, not ours. Any single value is therefore wrong somewhere,
    # and hardcoding one blocked the proposal with no way forward.
    #
    # These are tried in order, so the closest real match wins and the generic
    # catch-all is only reached when nothing better is on offer. "OTHER" last is
    # the point: it always succeeds, so putting it earlier would quietly stop
    # the test from ever exercising a real occupation.
    occupation_choices: tuple = (
        "Agriculturist", "Farmer", "Agriculture",
        "BUISNESSMAN", "Businessman", "Business", "Self Employed",
        "Salaried", "Service", "OTHER", "Other",
    )
    salutation_choices: tuple = ("Mr", "Mr.", "MR", "Shri", "Sri")
    marital_choices: tuple = ("Single", "SINGLE", "Unmarried", "UNMARRIED")
    relation_choices: tuple = ("Father", "FATHER", "Other", "OTHER")
    address1: str = "A1"
    address2: str = "A1"
    address3: str = ""
    state: str = "GUJARAT"
    city: str = "Ahmedabad"
    gstin: str = ""
    aadhaar: str = ""

    # --- nominee (terms & conditions screen) --------------------------------
    nominee_name: str = "Test Nominee"
    nominee_relation: str = "Father"
    nominee_gender: str = "Male"
    nominee_dob: str = "11 Jun 2002"

    # --- previous policy (terms & conditions screen) ------------------------
    previous_policy_number: str = "213654978"
    previous_claim_made: str = "No"

    # --- KYC documents ------------------------------------------------------
    # The document TYPE chosen in the dropdown...
    identity_document: str = "Aadhaar Card"
    address_document: str = "Aadhaar Card"

    # ...and the file uploaded for each. Filenames only - the folder they live
    # in comes from 'test_documents_dir' in settings.local.json, because these
    # are real scans and must never be committed or referenced by a path in git.
    #
    # Aadhaar carries different information on each side, so they are not
    # interchangeable: the FRONT has the photo, name, DOB and number (identity),
    # the BACK has the address (address proof). Uploading the front as address
    # proof is the obvious mistake, and the screen's own rules say an address
    # proof "must contain user's address".
    identity_file: str = "Dilip_aadhar_front.png"
    address_file: str = "Dilip_aadhar_back.png"

    # WHICH DOCUMENT TYPES WE CAN ACTUALLY SUPPLY
    #
    # Insurers do not accept the same proofs. IFFCOTOKIO's identity dropdown
    # offers ONLY "PAN Card"; NATIONAL's lists CKYC, PAN, Voter ID, Driving
    # Licence, Passport and Aadhaar. Hardcoding "Aadhaar Card" therefore worked
    # for one insurer and silently left the type unselected for another - the
    # file uploaded, the type blank, and KYC unable to complete.
    #
    # So we declare what we HAVE, and the page picks whichever offered type we
    # can satisfy. Adding a document to the test folder means adding a line
    # here, not editing a page object.
    identity_documents: dict = field(default_factory=lambda: {
        "pan card": "Dilip_pan - ATTPB2942G.png",
        "pan": "Dilip_pan - ATTPB2942G.png",
        "aadhaar card": "Dilip_aadhar_front.png",
        "aadhar card": "Dilip_aadhar_front.png",      # the app spells it both ways
    })
    # Address proof must SHOW an address, which is why the Aadhaar BACK is used
    # here and the front is used above. A PAN card carries no address, so it is
    # deliberately absent - offering it would fail the screen's own rule that an
    # address proof "must contain user's address".
    address_documents: dict = field(default_factory=lambda: {
        "aadhaar card": "Dilip_aadhar_back.png",
        "aadhar card": "Dilip_aadhar_back.png",
    })

    def document_for(self, kind: str,
                     offered: list[str]) -> tuple[str, str]:
        """
        Pick a document type this insurer offers AND we hold a file for.

        Returns (option_text_to_click, filename), or ("", "") when the insurer
        wants nothing we have - which is a real, reportable finding rather than
        a crash: it names the gap in the test document set.
        """
        table = (self.identity_documents if kind == "identity"
                 else self.address_documents)

        # Exact first, then forgiving - the portal writes "PAN Card", "Pan card"
        # and "PAN CARD" in different places.
        for option in offered:
            if option.strip().lower() in table:
                return option, table[option.strip().lower()]
        for option in offered:
            key = option.strip().lower()
            for known, filename in table.items():
                if known in key or key in known:
                    return option, filename
        return "", ""


def form_values(who: "Customer") -> dict[str, str]:
    """
    Keyword -> value, for filling any form by MEANING rather than by field name.

    The keys are fragments that appear in a field's label, placeholder or
    formcontrolname. Longest match wins, so "nominee name" beats a bare "name"
    and "additional contact" never steals the main mobile number.

    Adding a new screen usually means adding a line here rather than writing a
    new page object - which is the point.
    """
    return {
        # --- most specific first; these must not be captured by shorter keys --
        # Nominee keys are matched with spaces removed, so these also find
        # `nomineeName`, `nomineeDob`, `nomineeGender`. They must stay LONGER
        # than the generic keys below - "nominee dob" beats a bare "dob", which
        # is what stops the nominee inheriting the proposer's date of birth.
        "nominee full name": who.nominee_name,
        "nominee date of birth": who.nominee_dob,
        "nominee dob": who.nominee_dob,
        "nominee name": who.nominee_name,
        "nominee gender": who.nominee_gender,
        "nominee relation": list(who.relation_choices),
        "nominee age": "",
        "previous policy number": who.previous_policy_number,
        "additional contact": "",          # optional - deliberately left blank
        "claim amount": "",                # only wanted when a claim was made
        "middle name": who.middle_name,
        "first name": who.first_name,
        "last name": who.last_name,
        "date of birth": who.dob_proposal,
        "mobile number": who.mobile,
        "contact number": who.mobile,
        "pin code": who.pincode,
        "pincode": who.pincode,
        "pan card": who.pan,
        "aadhar": who.aadhaar,
        "gstin": who.gstin,
        # Both spellings: the LABEL reads "Address 1" but the field is called
        # "address1". Matching looks at both, and which one wins depends on how
        # cleanly the label could be read - so list both rather than assume.
        "address 1": who.address1,
        "address 2": who.address2,
        "address 3": who.address3,
        "address1": who.address1,
        "address2": who.address2,
        "address3": who.address3,
        "dob": who.dob_proposal,          # the field is "dob", the label "Date of Birth"
        "search state": who.state,
        "search city": who.city,
        # Lists, not single values - see the *_choices fields above. The filler
        # tries each in order and reports which one the insurer accepted.
        "marital": list(who.marital_choices),
        "occupation": list(who.occupation_choices),
        "salutation": list(who.salutation_choices),
        "relation": list(who.relation_choices),
        "email": who.email,
        "gender": who.gender,
        "mobile": who.mobile,
        "state": who.state,
        "city": who.city,
    }


DEFAULT = Customer()
