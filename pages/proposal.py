"""
The proposal form - one wizard, several steps, all on /two-wheeler/proposal.

    1/3  Owner Details       salutation, name, gender, marital status, DOB,
                             occupation, email, mobile, address, pincode,
                             state, city, PAN, GSTIN, Aadhaar
    2/3  Vehicle Details     engine / chassis numbers, hypothecation
    3/3  Terms & Conditions  nominee, previous policy, consent tickbox
    4/4  Preview             /two-wheeler/proposal-payment - premium summary,
                             payment method, OTP

WHY THIS CLASS TOLERATES NOT KNOWING FIELD NAMES
------------------------------------------------
The screens were mapped from the team's screenshots, so the visible LABELS are
right but the formcontrolname values are inferred. Rather than block until a
mapping run succeeds - on an environment that keeps refusing to give us one -
every field is tried by formcontrolname FIRST and by visible label SECOND.

That makes the journey runnable today and precise later: run the probe, replace
the guesses with facts, and the fallbacks simply stop being used. Each guess is
marked TODO(map).

WHAT THIS CLASS DELIBERATELY CANNOT DO
--------------------------------------
There is no method here that does anything ON the payment page. The harness
types the developer OTP and presses Proceed (enter_otp, proceed_to_payment),
which sends the proposal to the insurer - and it stops the moment the browser
arrives at payment. Paying moves real money, so that capability does not exist
in the code at all, which is a stronger guarantee than remembering not to call
it.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from core import addressnotes, smartfill, ui, vehiclenotes
from data.customer import Customer, form_values
from pages import routes

URL_MARKER = "/two-wheeler/proposal"
PREVIEW_MARKER = "/two-wheeler/proposal-payment"

# Visible step headings, in order. Used to report progress in words a human
# recognises from the screen rather than as a step number only we understand.
STEP_HEADINGS = ("Personal Details", "Vehicle Details",
                 "Terms & Conditions", "Preview Information")

# What a "move the wizard on" button reads like, and what it must never be.
# Word boundaries matter in both: the portal's buttons carry a Material icon
# ligature in their text, so a Proceed button literally contains the string
# "backspace" and a loose match for "back" would send the journey backwards.
FORWARD = re.compile(r"\b(continue|next|proceed|submit|save)\b", re.IGNORECASE)
BACKWARD = re.compile(r"\b(back|previous|cancel|edit)\b", re.IGNORECASE)

# Which step headings are really on screen.
#
# Exact text, and never inside a button. Both conditions earned their place: the
# forward control on the vehicle step reads "Continue to Terms & Conditions", so
# a substring search finds "Terms & Conditions" on the page we are trying to
# LEAVE and reports that we already arrived. Requiring the element's whole text
# to be the heading also stops a wrapper div - which contains every heading -
# from matching all of them at once.
HEADINGS_JS = r"""
(headings) => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const found = [];
  const candidates = document.querySelectorAll(
      'h1,h2,h3,h4,h5,h6,span,div,p,label,strong,b,a,li');

  for (const heading of headings) {
    for (const el of candidates) {
      if (el.closest('button')) continue;
      const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (text !== heading) continue;
      if (!visible(el)) continue;
      found.push(heading);
      break;
    }
  }
  return found;
}
"""


class ProposalPage:
    def __init__(self, page: Page):
        self.page = page
        self._missing: list[str] = []
        # What the smart filler actually set, for the run report.
        self.filled: list[str] = []
        # The caption of the button that last moved the wizard on. Recorded
        # because the labels here were guessed from screenshots, and knowing
        # what the button really says is how the guesses get replaced by facts.
        self.advanced_by: str = ""
        # The registration number on the vehicle step, and what happened to
        # each one tried - printed by the runner, so a swap is never silent.
        self.registration: str = ""
        self.vehicle_log: list[str] = []
        # Errors the app received inside an HTTP 200 and never showed. The
        # runner hands over its own list (see hidden_error() there), so a wait
        # can stop the moment the answer lands instead of sitting out its
        # timeout. How many were already there when we last clicked:
        self.hidden_errors: list[str] = []
        self._hidden_mark = 0
        # The communication-address pincode, and every change made to it -
        # see core.addressnotes for why it sometimes has to change.
        self.pincode: str = ""
        self.city: str = ""
        self.state_name: str = ""
        self.cities_offered: list[str] = []
        # (state id, city id, pincode) the portal last SENT at Preview - the
        # exact key the insurer's district lookup was given.
        self.sent_address: tuple[str, str, str] = ("", "", "")
        self.address_log: list[str] = []
        # What happened on the Preview's OTP step, for the run report.
        self.otp_log: list[str] = []

    # ------------------------------------------------------------------ state

    def wait_until_loaded(self, timeout_ms: int = 60_000) -> "ProposalPage":
        try:
            self.page.wait_for_url(lambda u: routes.on(u, "proposal"),
                                   timeout=timeout_ms)
            self.page.get_by_text("Personal Details", exact=False).first.wait_for(
                state="visible", timeout=timeout_ms)
        except Exception:
            # Distinguish "the app is hung" from "the proposal never came".
            # Both look like a timeout from here, but only one of them is worth
            # anybody's time to investigate.
            ui.raise_if_stuck(self.page, "waiting for the proposal page")
            raise
        return self

    def current_step(self) -> str:
        """
        Which step is on screen, by its own heading.

        Takes the LAST visible heading, not the first. The wizard leaves the
        headings of completed steps on the page, so matching the first one made
        a run print "-> Vehicle Details" twice in a row: once on arriving there,
        and once after moving on to Terms & Conditions. A progress report that
        says you are somewhere you have already left is worse than none.
        """
        present = self.headings_present()
        seen = [h for h in STEP_HEADINGS if h in present]
        return seen[-1] if seen else f"unknown step ({self.page.url})"

    def headings_present(self) -> list[str]:
        """
        Which step headings are on screen, ignoring any inside a button.

        The button-exclusion is the whole point. The control that leaves the
        vehicle step is captioned "Continue to Terms & Conditions", so a plain
        text search for "Terms & Conditions" matches the BUTTON and concludes we
        have already arrived - on the step we are trying to leave. Matching the
        heading exactly, and only outside buttons, asks the real question.
        """
        try:
            return self.page.evaluate(HEADINGS_JS, list(STEP_HEADINGS))
        except Exception:
            return []

    def wait_for_step(self, heading: str, timeout_ms: int = 60_000) -> "ProposalPage":
        """
        Wait until a named step is actually on screen.

        Replaces a fixed three-second pause, which was simply too short and in a
        way that lied: moving between steps saves the proposal server-side, and
        while that is in flight the page has no heading and no buttons at all.
        Looking then produced "unknown step" and "buttons on screen: (none)",
        which reads like the wizard broke when it was merely busy.
        """
        waited = 0
        step = 500
        while waited < timeout_ms:
            if heading in self.headings_present():
                self.page.wait_for_timeout(600)   # let the fields render
                return self
            # The app said no. Stop now - waiting out the timeout cannot change
            # the answer, and it is what made one refusal cost a minute.
            refusal = self.app_said_no()
            if refusal:
                raise LookupError(
                    f"The app refused to move on to '{heading}'.\n"
                    f"  The app said: {refusal}")
            # Only after a grace period: a blank moment mid-transition is
            # normal, staying blank is not.
            if waited >= 15_000:
                ui.raise_if_stuck(self.page, f"waiting for '{heading}'")
            self.page.wait_for_timeout(step)
            waited += step

        raise LookupError(
            f"'{heading}' never appeared after {timeout_ms // 1000}s.\n"
            f"  Now showing : {self.current_step()}\n"
            f"  Buttons here: "
            f"{', '.join(self.buttons_on_screen()) or '(none)'}"
        )

    def prefilled(self) -> dict[str, str]:
        """
        What the app carried over from KYC.

        Printed by the runner because it is the EVIDENCE that KYC actually
        worked: a proposal form pre-filled with the verified name and PAN proves
        the KYC data landed, which "KYC returned success" alone does not.
        """
        out: dict[str, str] = {}
        for name, candidates in (
            ("first_name", ("firstName", "firstname")),
            ("last_name", ("lastName", "lastname")),
            ("dob", ("dob",)),
            ("email", ("email",)),
            ("mobile", ("mobileNo", "mobileno")),
            ("pan", ("panCard", "pan")),
            ("pincode", ("pinCode", "pincode")),
        ):
            for fc in candidates:
                try:
                    field = self.page.locator(f'input[formcontrolname="{fc}"]')
                    if field.count():
                        value = field.first.input_value(timeout=1500)
                        if value:
                            out[name] = value
                        break
                except Exception:
                    continue
        return out

    def missing_required(self) -> list[str]:
        """
        Required fields the app itself says are still empty.

        We ask the APP rather than judging for ourselves: it marks invalid
        controls with Angular's own classes, so this reports the form's opinion,
        not our guess at which fields matter.
        """
        # Ask for the app's own error TEXT too, not just the field name.
        # "chassisNumber" says where to look; "Chassis number must be 17
        # characters" says what to do, and the app is already displaying it.
        reported = self.page.evaluate(r"""
        () => {
          const bad = [...document.querySelectorAll(
              'input.ng-invalid, mat-select.ng-invalid, textarea.ng-invalid')];
          return bad.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          }).map(el => {
            const name = el.getAttribute('formcontrolname')
                       || el.getAttribute('placeholder') || el.id || 'unnamed';
            // The message sits beside the field, inside the same form-field box.
            let why = '';
            const box = el.closest('mat-form-field, .form-group, .col, .field');
            if (box) {
              const note = box.querySelector(
                  'mat-error, .mat-mdc-form-field-error, .error, .invalid-feedback');
              if (note) why = (note.innerText || '').replace(/\s+/g, ' ').trim();
            }
            const value = el.tagName === 'MAT-SELECT'
                        ? (el.innerText || '').trim() : (el.value || '');
            // No message on screen? Then report the RULE the control declares.
            // A silent ng-invalid with a 19-character value and maxlength=17
            // explains itself; "chassisNumber" alone does not.
            const rule = [];
            for (const attr of ['maxlength', 'minlength', 'pattern']) {
              const set = el.getAttribute(attr);
              if (set) rule.push(`${attr}=${set}`);
            }
            if (value) rule.push(`length=${value.length}`);
            return why ? `${name}: ${why}`
                 : (rule.length
                     ? `${name} (has "${value.slice(0, 24)}", ${rule.join(', ')})`
                     : name);
          });
        }
        """)
        # The app names a field ("occupation"); we name it with the reason
        # ("occupation (tried 'Farmer' - offers: ...)"). Both are the same
        # field, so keep the one that explains itself.
        detailed = list(dict.fromkeys(self._missing))
        explained = {entry.split(" (")[0] for entry in detailed if " (" in entry}
        bare = [name for name in dict.fromkeys(reported)
                if name not in explained and name not in detailed]
        return sorted(bare + detailed)

    # ------------------------------------------------------------ filling bits

    def _fill(self, form_control: str, label: str, value: str) -> None:
        """formcontrolname first, visible label as a fallback."""
        if not value:
            return
        try:
            field = self.page.locator(f'input[formcontrolname="{form_control}"]')
            if field.count():
                field.first.fill(value, timeout=8000)
                return
        except Exception:
            pass
        for locator in (self.page.get_by_label(label, exact=False),
                        self.page.locator(f'input[placeholder*="{label}" i]')):
            try:
                if locator.count():
                    locator.first.fill(value, timeout=6000)
                    return
            except Exception:
                continue
        self._missing.append(label)

    def _choose(self, form_control: str, value: str) -> None:
        """Pick from a mat-select, tolerating an unconfirmed formcontrolname."""
        if not value:
            return
        try:
            ui.dropdown(self.page, form_control, value)
            return
        except Exception:
            pass
        # Fall back to whichever select currently offers that option.
        try:
            selects = self.page.locator("mat-select")
            for i in range(min(selects.count(), 12)):
                selects.nth(i).click(timeout=3000)
                self.page.wait_for_timeout(500)
                option = self.page.get_by_role("option", name=value, exact=True)
                if option.count():
                    option.first.click(timeout=5000)
                    self.page.wait_for_timeout(400)
                    return
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(200)
        except Exception:
            pass
        self._missing.append(form_control)

    # ------------------------------------------------------------- step 1 / 3

    def fill_owner_details(self, who: Customer,
                           insurer: str = "") -> "ProposalPage":
        """
        Complete the personal-details step.

        Most of this arrives pre-filled from KYC, so we only write a field when
        it is empty. Overwriting a KYC-verified name or PAN would be actively
        wrong - the whole point of KYC is that those values came from the
        insurer, not from us.

        The one exception is the ADDRESS, and only when core.addressnotes knows
        this insurer refuses it (NATIONAL refused every Ahmedabad pincode
        tried). Changing it here costs a few seconds; finding out at Preview
        costs a trip back through the whole wizard.
        """
        self._missing.clear()

        # Let the app's own pincode lookup finish before touching anything.
        #
        # Entering a pincode makes the app fetch the matching state and city and
        # write them into the form. Filling them ourselves first - or during that
        # fetch - leaves a state and city that look right on screen but are not
        # the ones the app selected, and the insurer then rejects the proposal
        # with "No district found matching your state, city & pincode
        # combination". That error never reaches the screen, so the wizard simply
        # stops moving, which is how it cost an afternoon to find.
        #
        # Waiting is the whole fix: the app knows the right answer, and our job
        # is to not overwrite it.
        self.wait_for_address_lookup()

        self.pincode = self._pincode_on_screen()
        self.city = self._city_on_screen()
        # Only changes anything if the notes say this address is refused - and
        # never once the notes have concluded the insurer's data is missing:
        # then the customer's own address stays, and doubles as the check that
        # tells us when the backend has fixed it.
        if insurer and not addressnotes.looks_hopeless(insurer):
            self._switch_address(who, insurer, refused=[], skip_cities=[])

        # Read the form and fill what is missing, matching on MEANING rather
        # than on field names we guessed from a screenshot. Anything the insurer
        # already pre-filled from KYC is left exactly as it is.
        filled, unmatched = smartfill.autofill(self.page, form_values(who))
        self.filled = filled
        self._missing.extend(unmatched)

        # A second pass catches fields that only appear once something else is
        # chosen - picking a state can reveal a city box, for instance.
        if unmatched or self.missing_required():
            self.page.wait_for_timeout(1200)
            more, still = smartfill.autofill(self.page, form_values(who))
            self.filled += more
            self._missing = still

        return self

    def advance(self, to_heading: str, *labels: str,
                attempts: int = 3) -> "ProposalPage":
        """
        Press the forward button and make sure the wizard actually moved.

        Clicking once and assuming is not good enough here. These forms re-render
        underneath you - choosing a state reloads the city list, and a pincode
        lookup can rewrite several fields a second after they were filled - and a
        click that lands during a re-render is simply discarded. The run then sat
        on 'Personal Details' with the Continue button plainly in front of it,
        reporting that the next step never appeared.

        So: click, confirm we arrived, and if we did not, try again. That is what
        a person does, and it fails honestly after a few goes rather than
        pretending a swallowed click was a broken form.
        """
        problem = ""
        for attempt in range(1, attempts + 1):
            # Arrived after all? Then clicking again would press the NEXT
            # step's button and skip a whole page.
            if attempt > 1 and to_heading in self.headings_present():
                return self.wait_for_step(to_heading)
            try:
                self._click_forward(*labels)
            except LookupError as exc:
                problem = str(exc)

            try:
                return self.wait_for_step(to_heading, timeout_ms=20_000)
            except LookupError as exc:
                problem = str(exc)
                if attempt < attempts:
                    # A refusal is an answer, so try again straight away;
                    # a click that got swallowed needs the page to settle.
                    self.page.wait_for_timeout(
                        1000 if "The app said:" in problem else 2500)

        still = self.missing_required()
        verdict = (", ".join(still) if still else
                   "nothing - so the click is being swallowed, not rejected")
        raise LookupError(
            f"{problem}\n"
            f"  Tried {attempts} times.\n"
            f"  The form still wants: {verdict}"
        )

    def continue_to_vehicle(self) -> "ProposalPage":
        return self.advance("Vehicle Details", "Continue To Vehicle Details")

    # ------------------------------------------------------------- step 2 / 3

    def fill_vehicle_details(self, rto: str = "GJ-01",
                             insurer: str = "") -> "ProposalPage":
        """
        Engine number, chassis number, registration number and body colour.

        Engine and chassis are generated per run and made obviously synthetic -
        a tester finding ENG... in a record should be able to tell instantly
        that a test put it there. They must also be unique, because insurers
        reject a duplicate chassis number on a second proposal.

        The registration number is NOT invented any more. The app checks it
        against the RTO's records, and a random one was rejected about half the
        time ("Vehicle sub class is not matching with vehicle registration
        number data" - the plate belonged to a car). So it comes from
        core.vehiclenotes: a number that passed that check before, for this
        RTO, and a new random one only when no proven number is left.
        """
        self._missing.clear()
        from datetime import datetime
        now = datetime.now()
        stamp = now.strftime("%Y%m%d%H%M%S")

        # Built from the RTO the journey actually selected, so the plate cannot
        # contradict it - vehiclenotes.choose() reads the "GJ-01" code out of
        # "GJ-01" or "GJ-01 Ahmedabad" alike.
        registration, why = vehiclenotes.choose(rto, insurer, product=self.product)
        self.vehicle_log = [f"{registration} - {why} "
                            f"(notes: {vehiclenotes.known(self.product)})"]

        # Lengths matter. A chassis number is a VIN, which is exactly 17
        # characters, and "CHSTW" + a 14-digit timestamp came to 19 - rejected
        # by Angular with no message on screen at all. Three-letter prefixes
        # keep both inside the real-world limits while staying obviously
        # synthetic and unique per run.
        chassis = f"CHS{stamp}"            # 3 + 14 = 17, the VIN length
        engine = f"ENG{now.strftime('%y%m%d%H%M%S')}"     # 3 + 12 = 15

        filled, unmatched = smartfill.autofill(self.page, {
            "engine": engine,
            "chasis": chassis,             # the app spells it "Chasis"
            "chassis": chassis,
            "registration number": registration,
            "registrationnumber": registration,
            "registration no": registration,
            # Colour is spelled both ways in different places, and may be a
            # free-text box or a dropdown. If it is a dropdown and "Black" is
            # not on it, the run now reports the list it DOES offer.
            "colour": "Black",
            "color": "Black",
        })
        self.filled = filled
        self._missing.extend(unmatched)
        # What is really in the box - the app may have pre-filled it, and the
        # filler never overwrites a value it finds there.
        self.registration = self._registration_on_screen() or registration
        self.fill_accessories()
        return self

    # "Electrical Accessory Detail (Max Amount : 5000, Amount Remains : 5000)"
    ACCESSORY_HEADING = re.compile(
        r"((?:Non[ -]?)?Electrical) Accessory Detail\s*\(Max Amount\s*:\s*([\d.]+),"
        r"\s*Amount Remains\s*:\s*([\d.]+)\)", re.IGNORECASE)

    def fill_accessories(self) -> None:
        """
        One accessory row per section, for the whole amount still to declare.

        A quote with electrical / non-electrical accessories brings a section
        per kind onto this step, and Continue stays put until rows adding up
        to EXACTLY the quoted amount are listed: the Continue check wants at
        least that much, the Add button refuses more (Saarthi pc proposal-
        personal-details.component.ts:5434-5467 and addElectricalAccessory
        7241-7276). Found 2026-10-06, IFFCOTOKIO car journeys #9 and #10.
        """
        try:
            text = self.page.inner_text("body")
        except Exception:
            return
        sections = self.ACCESSORY_HEADING.findall(text)
        forms = self.page.locator("form").filter(
            has=self.page.locator('[formcontrolname="accessoryName"]'))
        for (kind, _, remains), form in zip(sections, forms.all()):
            amount = int(float(remains))
            if amount <= 0:
                continue
            try:
                self._accessory_row(form, amount)
                self.vehicle_log.append(f"{kind.lower()} accessory row added for "
                                        f"Rs {amount:,}")
            except Exception as exc:                 # noqa: BLE001
                self._missing.append(f"{kind.lower()} accessory details "
                                     f"({type(exc).__name__})")

    def _accessory_row(self, form, amount: int) -> None:
        for control in ("accessoryName", "manufacturingYear"):
            form.locator(f'mat-select[formcontrolname="{control}"]').click(timeout=8000)
            self.page.locator("mat-option").first.click(timeout=8000)
            self.page.wait_for_timeout(300)
        form.locator('input[formcontrolname="manufactureName"]').fill("TEST MAKE")
        form.locator('input[formcontrolname="model"]').fill("TEST MODEL")
        form.locator('input[formcontrolname="amount"]').fill(str(amount))
        form.locator("button").filter(
            has_text=re.compile(r"^\s*Add\s*$")).first.click(timeout=8000)
        self.page.wait_for_timeout(500)

    @property
    def product(self) -> str:
        """'car' on /private-car/..., else 'bike' - so a car journey never
        reuses a plate that was proven for a bike (core/vehiclenotes)."""
        return routes.product_of(self.page.url)

    # The one control that holds the plate: formcontrolname vehicleRegistrationNumber.
    REGISTRATION = 'input[formcontrolname*="registrationnumber" i]'

    def _registration_on_screen(self) -> str:
        try:
            return vehiclenotes.normalise(
                self.page.locator(self.REGISTRATION).first.input_value(timeout=2000))
        except Exception:
            return ""

    def _set_registration(self, number: str) -> None:
        box = self.page.locator(self.REGISTRATION).first
        box.scroll_into_view_if_needed(timeout=4000)
        box.fill("", timeout=6000)
        box.fill(number, timeout=6000)
        box.press("Tab", timeout=3000)       # let the app format it (GJ-01-..)
        self.page.wait_for_timeout(500)
        self.registration = self._registration_on_screen() or number

    def _press_and_read_vehicle_check(self, labels) -> tuple[bool, str, str] | None:
        """
        Click Continue and read the app's registration-number check.

        Returns (accepted, reason, the plate that was checked), or None if no
        check came back - an insurer that does not check, or a click the page
        swallowed.

        Reading the ANSWER is the point. The app shows the verdict only as a
        toast that fades, and the old code just waited for the next step,
        clicked twice more with the same rejected number and gave up about a
        minute later.

        Two waits, because two different questions: did the click SEND a check
        (it leaves within a tenth of a second), and what did it answer (a plate
        the RTO lookup has not seen before takes 12-16 s - half of all first
        checks in the traces). One 10-second wait for both gave up on those and
        fell back to blind re-clicking.
        """
        check = lambda r: vehiclenotes.CHECK_URL in r.url.lower()
        try:
            # The request timer starts BEFORE the click, and the click itself
            # may take up to 12 s to become possible - so 25 s, not 8.
            with self.page.expect_response(check, timeout=60_000) as answer:
                with self.page.expect_request(check, timeout=25_000):
                    self._click_forward(*labels)
            response = answer.value
            ok, reason = vehiclenotes.read_answer(response.json())
        except Exception:
            return None
        # File the verdict under the plate the app SENT, not under whatever
        # the box shows now.
        try:
            plate = (response.request.post_data_json or {}).get("RegistrationNumber", "")
        except Exception:
            plate = ""
        return ok, reason, vehiclenotes.normalise(plate)

    def continue_to_terms(self, insurer: str = "", rto: str = "GJ-01",
                          attempts: int = 6) -> "ProposalPage":
        """
        Move to Terms & Conditions, swapping the plate if the RTO check says no.

        Every answer goes into core.vehiclenotes, so a number rejected today is
        never offered again, and one that passed is reused next run.
        A rejection that a different number cannot fix stops the run with the
        app's own reason rather than burning attempts on it.
        """
        # "Continue to Terms & Conditions" is what the button actually says -
        # read off the screen by a failing run, not guessed from a screenshot.
        labels = ("Continue to Terms & Conditions",
                  "Continue To Terms", "Continue To Proposal")
        tried: list[str] = []

        for attempt in range(1, attempts + 1):
            number = self._registration_on_screen() or self.registration
            tried.append(number)

            answer = self._press_and_read_vehicle_check(labels)
            if answer is None:
                # No check to learn from. The click may still have worked - an
                # insurer that does not check - so look before clicking again:
                # a second click would land on the NEXT step's button.
                if "Terms & Conditions" in self.headings_present():
                    return self.wait_for_step("Terms & Conditions")
                return self.advance("Terms & Conditions", *labels)

            ok, reason, sent = answer
            number = sent or number
            tried[-1] = number
            vehiclenotes.record(insurer, number, ok, reason, product=self.product)
            if ok:
                self.vehicle_log.append(
                    f"{number} accepted" + (f" ({reason})" if reason else ""))
                return self.wait_for_step("Terms & Conditions", timeout_ms=30_000)

            self.vehicle_log.append(f"{number} REJECTED - {reason}")
            if not vehiclenotes.number_problem(reason):
                raise LookupError(
                    f"The app rejected the vehicle details, and a different "
                    f"registration number would not fix it:\n  {reason}")
            if attempt == attempts:
                break                 # no point typing a number nobody checks

            ui.close_overlays(self.page)
            replacement, why = vehiclenotes.choose(rto, insurer, avoid=tried,
                                                   product=self.product)
            self.vehicle_log.append(f"trying {replacement} - {why}")
            self._set_registration(replacement)

        raise LookupError(
            f"Tried {len(tried)} registration numbers and the app rejected "
            f"every one:\n    " + "\n    ".join(self.vehicle_log))

    # ------------------------------------------------------------- step 3 / 3

    def fill_terms(self, who: Customer) -> "ProposalPage":
        self._missing.clear()

        filled, unmatched = smartfill.autofill(self.page, form_values(who))
        self.filled = filled
        self._missing.extend(unmatched)

        # The consent tickbox is mandatory and the form will not submit without
        # it. It is a real declaration a human makes, so it is ticked explicitly
        # here rather than hidden inside a helper.
        if not self.tick_consent():
            self._missing.append("consent checkbox")

        return self

    def close_overlays(self, attempts: int = 4) -> None:
        """Clear any dropdown panel covering the page - see core.ui."""
        ui.close_overlays(self.page, attempts)

    def buttons_on_screen(self) -> list[str]:
        """
        Every button caption currently visible, for when a step will not move.

        Guessing button labels from screenshots has now failed three times on
        this wizard, so when a transition fails the run should print what is
        actually there rather than leave the next person to open the browser.
        """
        out: list[str] = []
        try:
            buttons = self.page.locator("button")
            for i in range(min(buttons.count(), 40)):
                one = buttons.nth(i)
                try:
                    if not one.is_visible(timeout=600):
                        continue
                    caption = " ".join((one.inner_text(timeout=600) or "").split())
                    if caption:
                        out.append(
                            f"{caption}{'' if one.is_enabled(timeout=600) else ' [disabled]'}")
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def tick_consent(self) -> bool:
        """
        Tick every consent box on the step, and confirm each one took.

        `.check()` on the <input> does not work here. Angular Material hides the
        real checkbox behind a styled box - the native input has no size - so
        Playwright correctly refuses to click something invisible, and the run
        reported "consent checkbox" missing while the box sat there untouched.

        Clicking the mat-checkbox HOST is what a human does, and Material
        forwards it to the input. Anything genuinely hidden is skipped rather
        than forced: a checkbox nobody can see is not one a customer agreed to.
        """
        ticked = False
        boxes = self.page.locator("mat-checkbox")
        try:
            count = boxes.count()
        except Exception:
            return False

        for i in range(min(count, 6)):
            box = boxes.nth(i)
            try:
                if not box.is_visible(timeout=1200):
                    continue
                # Already agreed? Material records it on the host element.
                state = box.get_attribute("class") or ""
                if "checked" in state.lower():
                    ticked = True
                    continue
                box.scroll_into_view_if_needed(timeout=4000)
                box.click(timeout=6000)
                self.page.wait_for_timeout(400)
                if "checked" in (box.get_attribute("class") or "").lower():
                    ticked = True
            except Exception:
                continue

        if ticked:
            return True

        # No mat-checkbox took it. Fall back to a plain input, clicking its
        # label - which is what makes an invisible native checkbox reachable.
        try:
            native = self.page.locator('input[type="checkbox"]')
            for i in range(min(native.count(), 6)):
                one = native.nth(i)
                try:
                    if one.is_checked(timeout=1000):
                        return True
                    one.click(timeout=4000)
                except Exception:
                    # Invisible native input - click whatever labels it.
                    box_id = one.get_attribute("id")
                    if box_id:
                        self.page.locator(f'label[for="{box_id}"]').first.click(
                            timeout=4000)
                if one.is_checked(timeout=1000):
                    return True
        except Exception:
            pass
        return False

    def continue_to_preview(self, who: Customer | None = None,
                            insurer: str = "", rto: str = "GJ-01",
                            attempts: int = 3,
                            address_fixes: int = 3) -> "ProposalPage":
        """
        Move to the Preview, which is a different PAGE, not another panel.

        The first three steps are panels on /two-wheeler/proposal, but Preview
        lives on /two-wheeler/proposal-payment. Waiting for a heading therefore
        misses it whenever the new page words its title differently, so arrival
        is judged by the URL as well - and the URL is the stronger signal, since
        it is the app's own routing rather than its copywriting.

        WHEN THE ADDRESS IS REFUSED
        ---------------------------
        "No district found matching your state, city & pincode combination"
        means the insurer's city table has no row for this address (see
        core.addressnotes). Clicking again sends the same address and gets the
        same answer, so instead - given `who` and `insurer` - this goes BACK to
        Personal Details, changes the address by core.addressnotes' ladder
        (another pincode, then another CITY once a city is refused whole),
        comes forward again and retries - at most `address_fixes` times, or
        once if every city tried so far has been refused, because then more
        round trips cannot help and only the evidence is worth having.

        After this come enter_otp() and proceed_to_payment(), which stops as
        soon as the browser reaches the payment page.
        """
        refused: list[str] = []
        skip_cities: list[str] = []
        fixes = 0
        while True:
            refusal, problem = self._try_preview(attempts)
            if not problem:
                if insurer and self.pincode:
                    addressnotes.record(insurer, self.pincode, self.city, True)
                return self

            if not (who and insurer and addressnotes.address_problem(refusal)):
                raise LookupError(self._preview_failure(attempts, problem))

            addressnotes.record(insurer, self.pincode, self.city, False, refusal)
            refused.append(self.pincode)
            # Is there anything left worth a round trip? Ask BEFORE going back.
            # Once two whole cities are refused the answer is no - the data is
            # missing on the server, and every further trip only costs time.
            nxt, _, why = addressnotes.choose(insurer, who.city, self.pincode,
                                              avoid=refused, skip_cities=skip_cities)
            if (fixes >= address_fixes or not nxt
                    or addressnotes.looks_hopeless(insurer)):
                problem += self._address_verdict(insurer, refused, why)
                raise LookupError(self._preview_failure(attempts, problem))
            fixes += 1

            self.address_log.append(
                f"{insurer} refused {self.pincode} / {self.city or '?'}: going back")
            self._go_back_to_personal_details()
            if not self._switch_address(who, insurer, refused, skip_cities):
                problem += self._address_verdict(insurer, refused, "")
                raise LookupError(self._preview_failure(attempts, problem))
            self._walk_forward_to_terms(who, insurer, rto)

    def _address_verdict(self, insurer: str, refused: list[str], why: str) -> str:
        """The evidence, and what it means, for the failure report."""
        lines = [f"\n  Addresses {insurer} refused in this run: "
                 f"{', '.join(refused)}",
                 f"  Everything {insurer} has answered so far: "
                 f"{addressnotes.summary(insurer)}"]
        if addressnotes.looks_hopeless(insurer):
            state, city, pincode = self.sent_address
            lookup = (f"sp_get_nic_city_master({state}, {city}, {pincode})"
                      if state and city else "its city/district lookup")
            lines.append(
                f"  VERDICT: {insurer} refused every CITY tried and never "
                f"accepted one, so test data cannot fix it - the rows are "
                f"missing on the server.\n"
                f"  For the backend team: {lookup} returned no row (state id, "
                f"city id, pincode, from the CommunicationAddressDetails the "
                f"portal sent). InsureBridge's MotorData.GetCityMasterNIC also "
                f"returns nothing on a SQL error, but its error log (LogFile/"
                f"Data/Error) had no GetCityMasterNIC entry on 2026-09-25 - so "
                f"it is missing data, not a crash.\n"
                f"  Until that is fixed, NATIONAL runs keep the customer's own "
                f"address and stop here in seconds instead of going back.")
        elif why:
            lines.append(f"  {why}")
        return "\n".join(lines)

    def _try_preview(self, attempts: int) -> tuple[str, str]:
        """
        Press Continue to Preview. Returns ("", "") on arrival, otherwise
        (the app's refusal, a description of where we are stuck).

        An address refusal comes back after ONE click: repeating the same
        pincode cannot change the answer, so the caller fixes it instead.
        """
        # Keep the address ids the portal actually sends - the verdict names
        # them, because they are what the insurer's lookup was keyed on.
        def grab(request) -> None:
            if addressnotes.CHECK_URL in request.url.lower():
                try:
                    comm = (request.post_data_json or {}).get(
                        "CommunicationAddressDetails") or {}
                    self.sent_address = (str(comm.get("State", "")),
                                         str(comm.get("City", "")),
                                         str(comm.get("Pincode", "")))
                except Exception:
                    pass
        try:
            self.page.on("request", grab)
        except Exception:
            pass
        try:
            return self._try_preview_clicks(attempts)
        finally:
            try:
                self.page.remove_listener("request", grab)
            except Exception:
                pass

    def _try_preview_clicks(self, attempts: int) -> tuple[str, str]:
        problem, refusal = "", ""
        for attempt in range(1, attempts + 1):
            if attempt > 1 and routes.on(self.page.url, "proposal-payment"):
                return "", ""        # it got there late - do not click again
            try:
                self._click_forward("Continue to Preview", "Continue To Preview")
            except LookupError as exc:
                problem = str(exc)

            waited = 0
            refusal = ""
            while waited < 30_000:
                if (routes.on(self.page.url, "proposal-payment")
                        or "Preview Information" in self.headings_present()):
                    self.page.wait_for_timeout(1500)
                    return "", ""
                # Stop the moment the app says no - on screen or buried in a
                # 200 - rather than waiting out 30 seconds for nothing.
                refusal = self.app_said_no()
                if refusal:
                    break
                if waited >= 15_000:
                    ui.raise_if_stuck(self.page, "waiting for the preview")
                self.page.wait_for_timeout(500)
                waited += 500

            problem = (f"Still on {self.page.url} showing "
                       f"'{self.current_step()}'")
            # Apps of this kind usually DO say what went wrong - in a toast
            # that fades after a few seconds, long before anyone looks at a
            # screenshot. Catching it while it is still on screen turns a
            # silent stall into the app's own explanation.
            note = refusal or self.notice_on_screen()
            if note:
                problem += f"\n  The app said: {note}"
            if addressnotes.address_problem(refusal):
                return refusal, problem
            if attempt < attempts:
                self.page.wait_for_timeout(1000 if refusal else 2500)
        return refusal, problem or "the preview never opened"

    def _preview_failure(self, attempts: int, problem: str) -> str:
        still = self.missing_required()
        hidden = self.hidden_blockers()
        return (
            f"The preview never opened.\n"
            f"  {problem}\n"
            f"  Buttons here : "
            f"{', '.join(self.buttons_on_screen()) or '(none)'}\n"
            f"  Form wants   : {', '.join(still) if still else 'nothing visible'}\n"
            f"  Out of sight : "
            f"{'; '.join(hidden) if hidden else 'nothing'}"
        )

    # ------------------------------------------------- changing the pincode

    # The communication-address pincode - formcontrolname "pinCode", and NOT
    # "pinCodeanother" (the registration address, which stays hidden).
    PINCODE = 'input[formcontrolname="pincode" i]'
    CITY = 'input[formcontrolname="city"]'

    def _pincode_on_screen(self) -> str:
        try:
            box = self.page.locator(self.PINCODE)
            if box.count():
                return re.sub(r"\D", "", box.first.input_value(timeout=2000))
        except Exception:
            pass
        return self.pincode

    STATE = 'input[formcontrolname="state"]'

    def _box_value(self, selector: str) -> tuple[str, bool]:
        """(value, valid) of one input - '' and False if it is not there."""
        try:
            box = self.page.locator(selector).first
            value = box.input_value(timeout=2000).strip()
            classes = box.get_attribute("class", timeout=2000) or ""
            return value, bool(value) and "ng-invalid" not in classes
        except Exception:
            return "", False

    def _city_on_screen(self) -> str:
        return self._box_value(self.CITY)[0]

    def _pick_from_dropdown(self, selector: str, names) -> tuple[str, list[str]]:
        """
        Choose one of `names` from an autocomplete by CLICKING the option.

        Returns (what was picked or '', every option the list offered). The
        offered list is evidence too: NATIONAL's Gujarat list is just
        "AHMEDABAD", and knowing that rules out Surat and Vadodara without
        trying either. Typed text selects nothing in these boxes.
        """
        box = self.page.locator(selector).first
        offered: list[str] = []
        try:
            box.fill("", timeout=4000)
            box.click(timeout=4000)
            self.page.get_by_role("option").first.wait_for(state="visible",
                                                          timeout=6000)
            offered = ui.options_on_screen(self.page)
            for name in names:
                if not any(name.lower() in o.lower() for o in offered):
                    continue
                picked, _ = ui.pick_option(self.page, name)
                if picked:
                    self.page.wait_for_timeout(600)
                    return box.input_value(timeout=2000).strip(), offered
                box.click(timeout=4000)          # reopen for the next name
        except Exception:
            pass
        finally:
            try:
                box.press("Escape", timeout=2000)
            except Exception:
                pass
        return "", offered

    def _change_pincode(self, pincode: str, city_key: str) -> bool:
        """
        Move the communication address to `pincode` in `city_key`.

        The app does the work: typing a pincode makes it look the pincode up
        (CityStateName) and select the matching state and city from the
        insurer's own lists - the values the insurer will accept. Only what it
        leaves empty or wrong is picked from those lists - clicked, never typed.

        Returns False when the insurer's city list simply has no such city (so
        the caller moves on WITHOUT a wasted trip to Preview), True once the
        screen really shows the new pincode and a city by that name.
        """
        info = addressnotes.CITIES.get(city_key, {})
        names = info.get("names", (city_key,))
        box = self.page.locator(self.PINCODE).first
        box.scroll_into_view_if_needed(timeout=4000)
        try:
            with self.page.expect_response(
                    lambda r: "citystatename" in r.url.lower(), timeout=6000):
                box.fill(pincode, timeout=6000)
        except Exception:
            pass                     # no lookup seen - judge by the screen below
        # The app (onPincodeChange -> getCityStateName -> getCitiesByState)
        # clears state and city, sets the state, loads that state's cities and
        # selects the one whose name matches exactly. Wait for the state to
        # land rather than a fixed pause, then give the city a moment.
        for _ in range(20):
            if self._box_value(self.STATE)[0]:
                break
            self.page.wait_for_timeout(250)
        self.page.wait_for_timeout(600)

        # State first - the city list depends on it.
        state, state_ok = self._box_value(self.STATE)
        if not state_ok or info.get("state", "").lower() not in state.lower():
            if info.get("state"):
                self._pick_from_dropdown(self.STATE, (info["state"],))
                self.page.wait_for_timeout(1000)

        city, city_ok = self._box_value(self.CITY)
        self.cities_offered = []
        if not (city_ok and any(n.lower() in city.lower() or city.lower() in n.lower()
                                for n in names)):
            _, self.cities_offered = self._pick_from_dropdown(self.CITY, names)

        self.pincode = self._pincode_on_screen() or pincode
        self.city = self._city_on_screen()
        self.state_name = self._box_value(self.STATE)[0]
        city, city_ok = self._box_value(self.CITY)
        return city_ok and any(n.lower() in city.lower() or city.lower() in n.lower()
                               for n in names) and self.pincode == pincode

    def _switch_address(self, who: Customer, insurer: str, refused: list[str],
                        skip_cities: list[str]) -> bool:
        """
        Put the next address from core.addressnotes' ladder on the form.

        Returns True once the form really shows a new address worth trying,
        False when nothing is left (the reason is in address_log). Cities the
        insurer's dropdown does not even offer are skipped here, on this page,
        instead of costing a trip to Preview to find out.
        """
        before = f"{self.pincode or '(empty)'} / {self.city or '?'}"
        for _ in range(4):
            pincode, city_key, why = addressnotes.choose(
                insurer, who.city, self.pincode,
                avoid=refused, skip_cities=skip_cities)
            if not pincode:
                if why:
                    self.address_log.append(f"keeping {self.pincode}: {why}")
                return False
            if self._change_pincode(pincode, city_key):
                self.address_log.append(
                    f"address {before} -> {self.pincode} / "
                    f"{self.state_name or '?'} / {self.city} ({why})")
                return True
            # The insurer does not even list this city. Every other city of the
            # same state that is not on its list is ruled out too - and all of
            # it is remembered, so no later run spends a second on them.
            state = addressnotes.CITIES.get(city_key, {}).get("state", "")
            ruled_out = [city_key] + [
                c for c, info in addressnotes.CITIES.items()
                if info["state"] == state and self.cities_offered
                and not any(n.lower() in o.lower() for n in info["names"]
                            for o in self.cities_offered)]
            ruled_out = list(dict.fromkeys(ruled_out))
            for city in ruled_out:
                addressnotes.record_not_offered(insurer, city)
            skip_cities.extend(ruled_out)
            refused.append(pincode)
            offered = ", ".join(self.cities_offered[:6]) or "nothing we could read"
            self.address_log.append(
                f"{insurer}'s {state.title() or 'city'} list offers only "
                f"{offered} - ruling out {', '.join(c.title() for c in ruled_out)}")
        return False

    def _go_back_to_personal_details(self) -> None:
        """Terms -> Vehicle -> Personal Details, by the wizard's own Back buttons."""
        for label in ("Back to Vehicle Details", "Back to Personal Details"):
            self.close_overlays()
            # The portal draws duplicate layouts, so ask for the VISIBLE one,
            # and give the step a moment to render after the previous click.
            button = None
            for _ in range(12):
                button = ui.live_button(self.page, label)
                if button is not None:
                    break
                self.page.wait_for_timeout(500)
            if button is None:
                continue             # already past this step
            button.click(timeout=8000)
        self.page.locator(self.PINCODE).first.wait_for(state="visible",
                                                      timeout=15_000)

    def _walk_forward_to_terms(self, who: Customer, insurer: str,
                               rto: str) -> None:
        """After changing the address: Personal -> Vehicle -> Terms again."""
        pincode = self.pincode
        self.continue_to_vehicle()
        # The vehicle and terms answers are still in the form; these only
        # re-check them (and re-tick consent if the app cleared it).
        self.continue_to_terms(insurer, rto)
        self.fill_terms(who)
        missing = self.missing_required()
        if missing:
            raise LookupError(
                f"After changing the pincode to {pincode}, the terms step "
                f"still wants: {', '.join(missing)}")

    def wait_for_address_lookup(self, timeout_ms: int = 12_000) -> dict[str, str]:
        """
        Wait for the app to fill state and city from the pincode.

        Returns what it settled on, so the run can report values that came from
        the app rather than from us - the same reason KYC's pre-filled fields
        are printed. If the lookup never runs (no pincode, or it found nothing)
        this simply returns what is there and the normal filling takes over.
        """
        waited = 0
        latest: dict[str, str] = {}
        while waited < timeout_ms:
            try:
                latest = self.page.evaluate(r"""
                () => {
                  const out = {};
                  for (const el of document.querySelectorAll(
                         '[formcontrolname]')) {
                    const name = (el.getAttribute('formcontrolname') || '').toLowerCase();
                    if (!/state|city|district/.test(name)) continue;
                    const value = el.tagName === 'MAT-SELECT'
                                ? (el.innerText || '').trim()
                                : (el.value || '').trim();
                    if (value && !/^select/i.test(value)) out[name] = value;
                  }
                  return out;
                }
                """)
            except Exception:
                return latest

            # Both halves present means the lookup answered.
            if any("state" in n for n in latest) and any("city" in n for n in latest):
                return latest
            self.page.wait_for_timeout(800)
            waited += 800
        return latest

    # The portal's red pop-up is an ngx-toastr error, e.g.
    #   #toast-container > .ngx-toastr.toast-error > .toast-message
    #   "Vehicle sub class is not matching with vehicle registration number data."
    # The message part only, so the close button's "×" is not read as text.
    ERROR_TOAST = ("#toast-container .toast-error:not([data-harness-old]) "
                   ".toast-message")

    def _mark_old_answers(self) -> None:
        """
        Remember what the app has ALREADY said, just before a click.

        A toast lingers for several seconds, so without this the refusal of the
        previous number would be read as the answer to the next one.
        """
        self._hidden_mark = len(self.hidden_errors)
        try:
            self.page.evaluate("""() => document
                .querySelectorAll('#toast-container .ngx-toastr')
                .forEach(t => t.setAttribute('data-harness-old', '1'))""")
        except Exception:
            pass

    def error_toast(self) -> str:
        """Text of a red pop-up that appeared since the last click, or ''."""
        try:
            toast = self.page.locator(self.ERROR_TOAST)
            if toast.count():
                return " ".join(toast.last.inner_text(timeout=800).split())[:200]
        except Exception:
            pass
        return ""

    def app_said_no(self) -> str:
        """
        The app's refusal since the last click, as soon as it exists: a red
        pop-up, or an error buried in an HTTP 200 that the screen never shows
        (NATIONAL's "No district found ..." at Preview is one of those).
        """
        toast = self.error_toast()
        if toast:
            return toast
        # Skip the quote fan-out's late answers. Those endpoints are named after
        # an insurer in capitals ("DIGIT: Status=Error" lands about 30 s after
        # the quotes start) and have nothing to do with the click just made.
        fresh = [e for e in self.hidden_errors[self._hidden_mark:]
                 if not e.split(":", 1)[0].isupper()]
        return f"{fresh[-1]} (not shown on screen)" if fresh else ""

    def notice_on_screen(self) -> str:
        """Any toast, snackbar or inline alert the app is showing right now."""
        for selector in ("simple-snack-bar", "mat-snack-bar-container",
                         ".toast-message", ".toast", ".alert-danger",
                         ".mat-mdc-snack-bar-label", ".swal2-html-container"):
            try:
                found = self.page.locator(selector)
                if found.count():
                    text = " ".join(found.first.inner_text(timeout=1000).split())
                    if text:
                        return text[:200]
            except Exception:
                continue
        return ""

    def hidden_blockers(self) -> list[str]:
        """
        What is blocking the form that missing_required() cannot see.

        missing_required only reports VISIBLE invalid controls, which is right
        for fields a person types into and useless for the three things that
        actually stall this wizard: a consent tickbox whose real <input> Material
        hides behind a styled box, a radio group with nothing chosen, and a
        control that is invalid but scrolled out of the layout.

        Exactly this gap cost a long detour on the KYC screen, where a run
        reported a complete form and a dead button at the same time.
        """
        try:
            return self.page.evaluate(r"""
            () => {
              const out = [];
              const name = el => el.getAttribute('formcontrolname')
                               || el.getAttribute('name')
                               || el.getAttribute('placeholder') || el.id || 'unnamed';

              for (const el of document.querySelectorAll(
                     'input.ng-invalid, mat-select.ng-invalid, textarea.ng-invalid')) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) continue;   // already reported
                out.push(`invalid but not visible: ${name(el)}`);
              }

              for (const el of document.querySelectorAll('input[type=checkbox]')) {
                out.push(`checkbox ${name(el)}: ${el.checked ? 'ticked' : 'NOT ticked'}`
                         + (el.required ? ' (required)' : ''));
              }

              const groups = {};
              for (const el of document.querySelectorAll('input[type=radio]')) {
                const g = el.getAttribute('name') || name(el);
                groups[g] = groups[g] || el.checked;
              }
              for (const [g, chosen] of Object.entries(groups)) {
                if (!chosen) out.push(`radio group '${g}': nothing chosen`);
              }

              // The control Angular is actually unhappy about.
              //
              // Angular puts ng-invalid on the invalid control AND on every
              // ancestor up to the form, so listing them all buries the answer.
              // The one that matters is the LEAF - the invalid element with no
              // invalid descendant. Looking only at input/mat-select/textarea
              // misses it whenever the control is a radio group, a datepicker
              // or any other component, which is exactly the case that left a
              // run reporting "the form is invalid" and nothing else.
              const invalid = [...document.querySelectorAll('.ng-invalid')];
              for (const el of invalid) {
                if (el.tagName === 'FORM') continue;
                if (invalid.some(other => other !== el && el.contains(other))) continue;
                const label = el.getAttribute('formcontrolname')
                            || el.getAttribute('formgroupname')
                            || el.getAttribute('name') || el.id || '';
                const r = el.getBoundingClientRect();
                const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
                out.push(`INVALID <${el.tagName.toLowerCase()}>`
                         + (label ? ` ${label}` : '')
                         + ` [${(r.width > 0 && r.height > 0) ? 'visible' : 'hidden'}]`
                         + (text ? ` "${text.slice(0, 40)}"` : ''));
              }
              if (document.querySelector('form.ng-invalid') && !invalid.length) {
                out.push('Angular marks the form invalid but names no control');
              }
              return out.slice(0, 14);
            }
            """)
        except Exception:
            return []

    # ----------------------------------------------------------------- preview

    def summary(self) -> dict[str, str]:
        """Read back what the portal says it is about to buy."""
        return self.page.evaluate(r"""
        () => {
          const t = document.body.innerText.replace(/\s+/g, ' ');
          const grab = (label) => {
            const m = t.match(new RegExp(label + '\\s*:?\\s*([^\\s].{0,38}?)(?=\\s{2}|$)'));
            return m ? m[1].trim() : '';
          };
          return {
            quotation_number: grab('Quotation Number'),
            net_premium: grab('Net Premium'),
            gst: grab('GST'),
            final_premium: grab('Final Premium'),
            policy_start: grab('Policy Start Date'),
            policy_end: grab('Policy End Date'),
            otp_mobile: grab('OTP will be sent to Mobile'),
          };
        }
        """)

    def payment_is_waiting(self) -> bool:
        """Have we reached the point where only a human can continue?"""
        return (self.page.get_by_text("OTP", exact=False).count() > 0
                or self.page.get_by_text("Pay Now", exact=False).count() > 0)

    # The OTP boxes are ng-otp-input: one <input class="otp-input"> per digit,
    # five on this screen, and the component moves the focus to the next box
    # itself as each digit arrives.
    OTP_BOXES = "ng-otp-input input.otp-input"
    OTP_SENT_TOAST = ("#toast-container .toast-success:not([data-harness-old]) "
                      ".toast-message")

    def enter_otp(self, otp: str, timeout_ms: int = 60_000) -> "ProposalPage":
        """
        Ask for the OTP and type it into the boxes. Proceed is a separate
        step - proceed_to_payment() - because it is the one that sends the
        proposal to the insurer.

        The order is forced by the portal: "Click here to get OTP" CLEARS the
        boxes before it sends (sendOtpToNumber() resets the field), so it is
        clicked first and the digits are typed after it.
        """
        boxes = self.page.locator(self.OTP_BOXES)
        try:
            boxes.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            ui.raise_if_stuck(self.page, "waiting for the OTP boxes")
            raise LookupError(
                f"The OTP boxes never appeared on the Preview.\n"
                f"  Now on      : {self.page.url}\n"
                f"  Buttons here: "
                f"{', '.join(self.buttons_on_screen()) or '(none)'}")

        self._ask_for_otp()

        wanted = boxes.count()
        if wanted != len(otp):
            raise LookupError(
                f"The Preview has {wanted} OTP boxes but the OTP "
                f"'{otp}' has {len(otp)} digits - change DEV_OTP in "
                f"config/settings.py.")

        # Type it the way a person does: click the first box and type. Each
        # digit moves the focus on, so the next one lands in the next box.
        try:
            boxes.first.click(timeout=8000)
        except Exception:
            boxes.first.focus()
        self.page.keyboard.type(otp, delay=120)
        self.page.wait_for_timeout(400)

        typed = self._otp_on_screen()
        if typed != otp:
            # The whole OTP in the first box is spread over all five by the
            # component itself - the same path a paste takes.
            self.otp_log.append(f"typing gave '{typed}' - filling it in one go")
            boxes.first.fill(otp)
            self.page.wait_for_timeout(400)
            typed = self._otp_on_screen()
        if typed != otp:
            raise LookupError(
                f"Typed the OTP '{otp}' but the boxes show '{typed}'.")

        try:
            complaint = self.page.get_by_text("OTP is required", exact=False)
            if complaint.count() and complaint.first.is_visible():
                raise LookupError(
                    f"The boxes show '{typed}' but the page still says "
                    f"'OTP is required' - the form did not take the digits.")
        except LookupError:
            raise
        except Exception:
            pass

        self.otp_log.append(f"typed OTP {typed}")
        return self

    def _ask_for_otp(self, timeout_ms: int = 20_000) -> None:
        """Click "Click here to get OTP" and note what the portal answers."""
        link = self.page.get_by_text("here to get OTP", exact=False)
        if not link.count():
            self.otp_log.append("no 'Click here to get OTP' link - typing anyway")
            return
        self.close_overlays()
        self._mark_old_answers()
        link.first.click(timeout=8000)

        waited = 0
        while waited < timeout_ms:
            try:
                sent = self.page.locator(self.OTP_SENT_TOAST)
                if sent.count():
                    text = " ".join(sent.last.inner_text(timeout=800).split())
                    self.otp_log.append(f"portal said: {text}")
                    return
            except Exception:
                pass
            # A failed send does not stop the run: the developer OTP is
            # accepted for any number, so it is still worth typing.
            refusal = self.app_said_no()
            if refusal:
                self.otp_log.append(
                    f"sending the OTP failed ({refusal}) - typing it anyway")
                return
            self.page.wait_for_timeout(500)
            waited += 500
        self.otp_log.append(
            f"no answer to 'get OTP' in {timeout_ms // 1000}s - typing it anyway")

    # How the portal asks a question after Proceed: a SweetAlert box (the
    # "Premium Mismatch Detected!" one) or a Material dialog (the KYC form some
    # insurers ask for at this point).
    POPUPS = (".swal2-popup", "mat-dialog-container")
    INFO_TOAST = ("#toast-container .toast-info:not([data-harness-old]) "
                  ".toast-message")

    def proceed_to_payment(self, otp: str, timeout_ms: int = 180_000) -> str:
        """
        Press Proceed and wait for the payment page. Returns its address.

        One click makes three calls in a row (proposal-otp.component.ts):
        VerifyOtp, then SendCompanyProposal - the proposal goes to the
        insurer - then CompanyPaymentLink, whose answer the browser is sent
        to. So arriving means the browser has LEFT the Preview. Nothing on the
        payment page is touched: paying stays a human's job.

        Every other ending stops the run with the portal's own reason: a
        refused OTP, an insurer refusing the proposal, no payment link, or a
        question the portal asks - including a changed premium, which is a
        finding to report, never something to agree to on someone's behalf.

        While it works the portal swaps the whole page for its loader, so a
        bare "Loading" screen here is the insurer being slow, not a hang, and
        it is waited out rather than reported as stuck.
        """
        self.close_overlays()
        self._mark_old_answers()
        button = ui.live_button(self.page, "Proceed")
        if button is None:
            raise LookupError(
                f"No clickable Proceed button on the Preview.\n"
                f"  Buttons here: "
                f"{', '.join(self.buttons_on_screen()) or '(none)'}")
        button.scroll_into_view_if_needed(timeout=4000)
        button.click(timeout=12_000)
        self.advanced_by = "Proceed"
        self.otp_log.append("pressed Proceed - waiting for the insurer")

        waited, step = 0, 500
        while waited < timeout_ms:
            if not routes.on(self.page.url, "proposal-payment"):
                return self._arrived_after_proceed()
            refusal = self.app_said_no() or self._info_toast()
            # "KYC Bypass" arrives dressed as an error, but the portal treats
            # it as a go-ahead and carries on to the payment link.
            if refusal and "KYC Bypass" not in refusal:
                raise LookupError(self._proceed_verdict(refusal, otp))
            question = self._popup_text()
            if question:
                raise LookupError(
                    f"After Proceed the portal stopped to ask a question, and "
                    f"the tool does not answer it for you.\n"
                    f"  It asked: {question}\n"
                    + ("  The premium changed between the quote and the "
                       "proposal - that is worth reporting as it is.\n"
                       if "premium" in question.lower() else ""))
            self.page.wait_for_timeout(step)
            waited += step

        raise LookupError(
            f"Pressed Proceed and waited {timeout_ms // 1000}s - the browser "
            f"never reached a payment page and the portal gave no reason.\n"
            f"  Still on: {self.page.url}")

    def _arrived_after_proceed(self) -> str:
        """The browser left the Preview - where to, and is it payment?"""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        # Payment gateways often bounce through a redirect or two.
        self.page.wait_for_timeout(3000)
        url = self.page.url
        if "/kyc" in url.lower():
            raise LookupError(
                f"After Proceed the portal sent the browser back to KYC - the "
                f"insurer did not accept this KYC for the proposal.\n"
                f"  Now on: {url}")
        self.otp_log.append(f"reached the payment page: {url}")
        return url

    def _proceed_verdict(self, refusal: str, otp: str) -> str:
        """Turn the portal's refusal after Proceed into what to do about it."""
        low = refusal.lower()
        if "invalid otp" in low or "valid otp" in low:
            return (f"The portal refused the OTP {otp}.\n"
                    f"  The developer OTP has probably changed - put the new "
                    f"one in DEV_OTP in config/settings.py.\n"
                    f"  The portal said: {refusal}")
        if "verifying otp" in low:
            return (f"The OTP check itself failed - the back end's VerifyOtp "
                    f"call errored. That is the environment, not the OTP.\n"
                    f"  The portal said: {refusal}")
        if "payment link" in low:
            return (f"The insurer accepted the proposal, but the portal could "
                    f"not get a payment link for it.\n"
                    f"  The portal said: {refusal}")
        return (f"The OTP was accepted, but the insurer did not accept the "
                f"proposal.\n"
                f"  The portal said: {refusal}")

    def _info_toast(self) -> str:
        """A blue pop-up since the last click - some insurers refuse in one."""
        try:
            toast = self.page.locator(self.INFO_TOAST)
            if toast.count():
                return " ".join(toast.last.inner_text(timeout=800).split())[:200]
        except Exception:
            pass
        return ""

    def _popup_text(self) -> str:
        """The text of a question box the portal has opened, or ''."""
        for selector in self.POPUPS:
            try:
                box = self.page.locator(selector)
                if box.count() and box.first.is_visible():
                    return " ".join(box.first.inner_text(timeout=1000).split())[:300]
            except Exception:
                continue
        return ""

    def _otp_on_screen(self) -> str:
        """The digits in the OTP boxes, joined up."""
        try:
            return "".join(self.page.locator(self.OTP_BOXES).evaluate_all(
                "boxes => boxes.map(b => b.value || '')"))
        except Exception:
            return ""

    # ----------------------------------------------------------------- helpers

    def _click_forward(self, *labels: str) -> None:
        """
        Click whichever 'next' button this step uses.

        The wizard does not use one consistent label - it is "Continue To
        Vehicle Details" on one step and "Continue to Preview" on another - and
        the exact wording of the middle step is not yet confirmed. Trying a few
        beats hardcoding one and failing on a label we guessed wrong.
        """
        # A dropdown left open puts a full-screen transparent backdrop over the
        # page, and Angular Material's backdrop swallows every click beneath it.
        # Playwright reports that honestly - "cdk-overlay-backdrop intercepts
        # pointer events" - but only after retrying for the whole timeout, by
        # which point it reads like the button is broken. It is not: the page is
        # covered. Clear it first, every time.
        self.close_overlays()
        # Anything the app has said so far belongs to an EARLIER click.
        self._mark_old_answers()

        for label in labels:
            try:
                button = ui.live_button(self.page, label)
                if button is not None:
                    button.scroll_into_view_if_needed(timeout=4000)
                    button.click(timeout=12_000)
                    self.advanced_by = label
                    return
            except Exception:
                continue

        # None of the guessed labels exist. Rather than fail on the wording,
        # find the button by what it DOES: the one visible, enabled control
        # that moves the wizard forward. The exact captions were taken from
        # screenshots and have already been wrong once ("Continue To Vehicle
        # Details" is not what this step actually says), so treating the label
        # as a hint rather than a requirement is the honest design.
        captions: list[str] = []
        try:
            buttons = self.page.locator("button")
            for i in range(min(buttons.count(), 40)):
                button = buttons.nth(i)
                try:
                    caption = " ".join(
                        (button.inner_text(timeout=800) or "").split())
                    if not caption:
                        continue
                    captions.append(caption)
                    # "Back" must never win. It is checked first and on word
                    # boundaries, so the icon text inside "keyboard_backspace
                    # Proceed" cannot be mistaken for a back button.
                    if BACKWARD.search(caption) or not FORWARD.search(caption):
                        continue
                    if not (button.is_visible(timeout=800)
                            and button.is_enabled(timeout=800)):
                        continue
                    button.scroll_into_view_if_needed(timeout=4000)
                    button.click(timeout=12_000)
                    self.advanced_by = caption
                    return
                except Exception:
                    continue
        except Exception:
            pass

        raise LookupError(
            f"Nothing on '{self.current_step()}' moves the form forward.\n"
            f"  Looked for: {', '.join(labels)}\n"
            f"  Buttons actually on screen: "
            f"{', '.join(repr(c) for c in captions[:12]) or '(none found)'}\n"
            f"  If one of those is the right button, the form is fine and the "
            f"label list above just needs it adding. If they are all disabled, "
            f"the form is incomplete - check missing_required() first."
        )
