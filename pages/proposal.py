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
There is no method here that enters an OTP or presses Pay. The OTP goes to a
real mobile and payment moves real money, so the capability does not exist in
the code at all - which is a stronger guarantee than remembering not to call it.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from core import smartfill, ui
from data.customer import Customer, form_values

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

    # ------------------------------------------------------------------ state

    def wait_until_loaded(self, timeout_ms: int = 60_000) -> "ProposalPage":
        try:
            self.page.wait_for_url(f"**{URL_MARKER}*", timeout=timeout_ms)
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
        step = 1000
        while waited < timeout_ms:
            if heading in self.headings_present():
                self.page.wait_for_timeout(600)   # let the fields render
                return self
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

    def fill_owner_details(self, who: Customer) -> "ProposalPage":
        """
        Complete the personal-details step.

        Most of this arrives pre-filled from KYC, so we only write a field when
        it is empty. Overwriting a KYC-verified name or PAN would be actively
        wrong - the whole point of KYC is that those values came from the
        insurer, not from us.
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
            try:
                self._click_forward(*labels)
            except LookupError as exc:
                problem = str(exc)

            try:
                return self.wait_for_step(to_heading, timeout_ms=20_000)
            except LookupError as exc:
                problem = str(exc)
                if attempt < attempts:
                    self.page.wait_for_timeout(2500)

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

    def fill_vehicle_details(self, rto: str = "GJ-01") -> "ProposalPage":
        """
        Engine number, chassis number, registration number and body colour.

        Generated per run and made obviously synthetic - a tester finding
        ENGTW... in a record should be able to tell instantly that a test put it
        there. They must also be unique, because insurers reject a duplicate
        chassis number on a second proposal.

        The registration number is built from the RTO the journey actually
        selected, so it cannot contradict it. A GJ-01 vehicle carrying an MH-01
        plate is the kind of nonsense an insurer may well reject, and chasing
        that rejection would waste a morning on a problem the test invented.
        """
        self._missing.clear()
        from datetime import datetime
        now = datetime.now()
        stamp = now.strftime("%Y%m%d%H%M%S")

        # Pull the code out of whatever form the RTO arrives in. It is passed
        # as "GJ-01" in one place and "GJ-01 Ahmedabad" in another, and simply
        # stripping the hyphen produced the plate "GJ01 AHMEDABADBN1127", which
        # the app quite rightly rejected. Matching the code explicitly means the
        # caller cannot hand us the wrong half of it.
        series = now.strftime("%H%M")
        letters = chr(65 + now.minute % 26) + chr(65 + now.second % 26)
        code = re.match(r"\s*([A-Za-z]{2})[-\s]?(\d{1,2})", rto or "")
        prefix = f"{code.group(1).upper()}{int(code.group(2)):02d}" if code else "GJ01"
        registration = f"{prefix}{letters}{series}"

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
        return self

    def continue_to_terms(self) -> "ProposalPage":
        # "Continue to Terms & Conditions" is what the button actually says -
        # read off the screen by a failing run, not guessed from a screenshot.
        return self.advance("Terms & Conditions",
                            "Continue to Terms & Conditions",
                            "Continue To Terms", "Continue To Proposal")

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

    def continue_to_preview(self, attempts: int = 3) -> "ProposalPage":
        """
        Move to the Preview, which is a different PAGE, not another panel.

        The first three steps are panels on /two-wheeler/proposal, but Preview
        lives on /two-wheeler/proposal-payment. Waiting for a heading therefore
        misses it whenever the new page words its title differently, so arrival
        is judged by the URL as well - and the URL is the stronger signal, since
        it is the app's own routing rather than its copywriting.

        This is also the last move the harness makes. What is on the other side
        is the payment screen, and nothing here touches it.
        """
        problem = ""
        for attempt in range(1, attempts + 1):
            try:
                self._click_forward("Continue to Preview", "Continue To Preview")
            except LookupError as exc:
                problem = str(exc)

            waited = 0
            while waited < 30_000:
                if (PREVIEW_MARKER in self.page.url
                        or "Preview Information" in self.headings_present()):
                    self.page.wait_for_timeout(1500)
                    return self
                if waited >= 15_000:
                    ui.raise_if_stuck(self.page, "waiting for the preview")
                self.page.wait_for_timeout(1000)
                waited += 1000

            problem = (f"Still on {self.page.url} showing "
                       f"'{self.current_step()}'")
            # Apps of this kind usually DO say what went wrong - in a toast
            # that fades after a few seconds, long before anyone looks at a
            # screenshot. Catching it while it is still on screen turns a
            # silent stall into the app's own explanation.
            note = self.notice_on_screen()
            if note:
                problem += f"\n  The app said: {note}"
            if attempt < attempts:
                self.page.wait_for_timeout(2500)

        still = self.missing_required()
        hidden = self.hidden_blockers()
        raise LookupError(
            f"The preview never opened after {attempts} attempts.\n"
            f"  {problem}\n"
            f"  Buttons here : "
            f"{', '.join(self.buttons_on_screen()) or '(none)'}\n"
            f"  Form wants   : {', '.join(still) if still else 'nothing visible'}\n"
            f"  Out of sight : "
            f"{'; '.join(hidden) if hidden else 'nothing'}"
        )

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
