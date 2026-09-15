"""
KYC - two steps on one route: /two-wheeler/kyc-insurance

    Step 1/2  EKYC Details      PAN, name, gender, DOB, mobile, email, pincode
    Step 2/2  Upload Documents  identity + address proof files
              -> "KYC verification success." popup
              -> on to /two-wheeler/proposal

Reached from the quote list via Buy Now -> Confirm. This confirms what the
InsureBridge code review predicted: KYC sits INSIDE the buy journey, not as a
separate step afterwards.

Whether step 2 appears at all is INSURER-SPECIFIC - some verify from the PAN
alone and skip straight to the proposal. So the upload step is treated as
optional: we look for it, handle it if present, and move on if not. Assuming it
is always there would break every insurer that skips it.

Submitting step 1 calls the insurer's real KYC service with a real PAN.
Callers must clear it with core.safety.allow("proposal", cfg) first.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from urllib.parse import urlparse

from config.insurers import (DOCUMENTS as config_DOCUMENTS,
                             INLINE as config_INLINE,
                             REDIRECT as config_REDIRECT,
                             SKIPPED as config_SKIPPED,
                             UNKNOWN as config_UNKNOWN)
from core import documents, ui
from data.customer import Customer


# Find the button that closes whatever dialog is on screen, WITHOUT knowing what
# the dialog is built from.
#
# Two attempts were made at matching the container instead - mat-dialog-container
# and .cdk-overlay-pane, then Bootstrap's .modal-content and .modal-body - and
# neither matched the "KYC verification success." popup, which sat on screen for
# a full minute while the code reported no dialog present. Guessing a third
# framework would have been the same mistake again.
#
# So this matches the thing we can actually describe: a small, visible, enabled
# button whose WHOLE caption is a dismissal word. Whole-caption matching is what
# makes that safe - "OK" as an entire label is a dismiss button, whereas "OK" as
# a fragment would also match "Book Now" and "Lookup".
#
# The dialog's message is read by climbing to the nearest positioned ancestor,
# which is the popup box in every layout, rather than from a class name.
FIND_DISMISS_JS = r"""
() => {
  // Dismissal words ONLY. "Proceed" and "Continue" are deliberately absent:
  // this runs from can_proceed() before the form is submitted, so listing them
  // would make "close the popup" quietly press SUBMIT - calling the insurer's
  // KYC service twice with a real PAN. A dismiss helper must never be able to
  // advance the journey.
  const DISMISS = /^(okay|ok|got it|close|dismiss)$/i;
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  for (const button of document.querySelectorAll('button')) {
    if (button.disabled || !visible(button)) continue;
    const caption = (button.innerText || '').replace(/\s+/g, ' ').trim();
    if (!DISMISS.test(caption)) continue;

    // Climb to the popup box: the nearest ancestor the browser has taken out
    // of normal flow. That is what a dialog is, in any framework.
    let box = button.parentElement;
    for (let i = 0; i < 8 && box; i++) {
      const position = getComputedStyle(box).position;
      if (position === 'fixed' || position === 'absolute') break;
      box = box.parentElement;
    }

    let text = ((box || button.parentElement).innerText || '')
                 .replace(/\s+/g, ' ').trim();
    // Drop the button's own caption, so the message reads "KYC verification
    // success." and not "KYC verification success. Okay".
    text = text.replace(new RegExp('\\s*' + caption + '\\s*$', 'i'), '').trim();

    button.setAttribute('data-harness-dismiss', '1');
    return { found: true, caption, text: text.slice(0, 200) };
  }
  return { found: false };
}
"""

# Find the file input and number box that belong to ONE document card.
#
# Positional indexing was wrong here, and wrong in a way that looked right. The
# screen has THREE file inputs - user photograph, identity proof, address proof -
# so "identity is input 0, address is input 1" actually put the PAN scan into the
# photograph slot and the Aadhaar into identity, leaving address empty. Every
# upload reported success.
#
# So we start from the control we DID identify - the document-type dropdown, which
# has a formcontrolname - and climb until we reach the box that also contains a
# file input. That box is the card, and whatever is inside it belongs together.
MARK_PHOTO_JS = r"""
() => {
  document.querySelectorAll('[data-harness-photo]')
          .forEach(el => el.removeAttribute('data-harness-photo'));

  for (const input of document.querySelectorAll('input[type=file]')) {
    let box = input.parentElement;
    for (let i = 0; i < 8 && box; i++) {
      const text = (box.innerText || '').toLowerCase();
      if (text.includes('photograph')) {
        // A proof card also mentions its document type and carries a dropdown;
        // the photograph card has no dropdown. That is what tells them apart.
        if (box.querySelector('mat-select')) break;
        const waiting = [...box.querySelectorAll('button')]
            .some(b => /upload/i.test(b.innerText || ''));
        input.setAttribute('data-harness-photo', '1');
        for (const b of box.querySelectorAll('button')) {
          if (/upload/i.test(b.innerText || '')) {
            b.setAttribute('data-harness-photo-button', '1');
            break;
          }
        }
        return {found: true, waiting};
      }
      box = box.parentElement;
    }
  }
  return {found: false};
}
"""

MARK_CARD_JS = r"""
(formControl) => {
  document.querySelectorAll('[data-harness-file], [data-harness-number]')
          .forEach(el => {
            el.removeAttribute('data-harness-file');
            el.removeAttribute('data-harness-number');
          });

  const select = document.querySelector(
      `mat-select[formcontrolname="${formControl}"]`);
  if (!select) return {card: false};

  let box = select.parentElement;
  for (let i = 0; i < 8 && box; i++) {
    const file = box.querySelector('input[type=file]');
    if (file) {
      file.setAttribute('data-harness-file', '1');
      // The number box lives in the same card - "Enter PAN Card Number".
      for (const input of box.querySelectorAll('input')) {
        const hint = ((input.getAttribute('placeholder') || '') + ' '
                    + (input.getAttribute('formcontrolname') || '')).toLowerCase();
        if (/number|no/.test(hint) && input.type !== 'file') {
          input.setAttribute('data-harness-number', '1');
          break;
        }
      }
      return {card: true, number: !!box.querySelector('[data-harness-number]')};
    }
    box = box.parentElement;
  }
  return {card: false};
}
"""


class KycPage:
    URL_MARKER = "/two-wheeler/kyc-insurance"

    # Every wrapper the portal uses for a modal. Listed in one place
    # because "is a dialog covering the screen?" is asked from several
    # directions - before typing, after submitting, and when explaining
    # a dead button - and they must all agree on the answer.
    DIALOG_SELECTOR = ("mat-dialog-container, .cdk-overlay-pane, "
                       ".modal-content, .modal-body")

    PAN = "pan"
    FIRST = "firstname"
    MIDDLE = "middlename"
    LAST = "lastname"
    DOB = "dob"
    MOBILE = "mobileno"
    EMAIL = "email"
    PINCODE = "pincode"
    GENDER = "gender"          # mat-select

    def __init__(self, page: Page):
        self.page = page
        # Recorded so the report can say WHICH files were sent - "KYC failed" is
        # far less useful than "KYC failed with Dilip_aadhar_front.png".
        self.uploaded: list[str] = []
        # Things that went wrong without stopping the run - a document type the
        # app did not offer, a dropdown that would not open. Collected rather
        # than printed, so the runner decides how to report them, and never
        # silently dropped.
        self._notes: list[str] = []
        # Set when KYC hands us off to an external portal. Recorded rather than
        # merely detected, because the host is the fact worth writing down.
        self.redirected_to: str = ""
        self._our_host: str = ""

    # ------------------------------------------------------- which shape is it?

    def detect_style(self, our_host: str, timeout_ms: int = 45_000) -> tuple[str, str]:
        """
        Watch where the browser lands after Confirm, and name the KYC shape.

        Returns (style, detail). We DETECT rather than trust configuration,
        because the insurer decides at runtime and the honest answer is whatever
        actually happened. Config says what we expected, so a mismatch gets
        flagged instead of silently passing.

            redirect  the browser left our host entirely - we are now on the
                      insurer's own portal, driving a UI we have not mapped
            documents inline form, and an upload step is waiting
            inline    inline form, no upload step
            skipped   we went straight to the proposal; no KYC was asked for
            unknown   none of the above inside the timeout

        Note this can only see the shape the journey has RIGHT NOW. Some
        insurers look inline here and redirect later, after the form is
        submitted - settle() catches those.
        """
        self._our_host = our_host
        elapsed = 0
        step = 1500

        while elapsed < timeout_ms:
            url = self.page.url
            host = _host_of(url)

            if host and our_host and host != our_host:
                return config_REDIRECT, host

            if "/two-wheeler/proposal" in url:
                return config_SKIPPED, url

            if self.URL_MARKER in url and ui.field_exists(
                    self.page, self.PAN, timeout_ms=1200):
                return (config_DOCUMENTS if self.upload_step_showing(timeout_ms=1500)
                        else config_INLINE), url

            self.page.wait_for_timeout(step)
            elapsed += step

        return config_UNKNOWN, self.page.url

    # ------------------------------------------------------------------ state

    def is_showing(self) -> bool:
        return (self.URL_MARKER in self.page.url
                or ui.field_exists(self.page, self.PAN, timeout_ms=8000))

    def wait_until_loaded(self, timeout_ms: int = 45_000) -> "KycPage":
        self.page.wait_for_url(f"**{self.URL_MARKER}*", timeout=timeout_ms)
        ui.wait_for_screen(self.page, self.PAN, timeout_ms=timeout_ms)
        self.dismiss_popup()
        return self

    def dismiss_popup(self) -> str:
        """
        Close whatever dialog is sitting over the screen, and report its text.

        Arriving at KYC opens an informational modal, and finishing KYC opens a
        "KYC verification success." one. Both sit in an overlay above the form,
        so Playwright will wait the full timeout trying to click a field beneath
        one and then report "element intercepts pointer events" - which reads
        like a selector bug rather than "a dialog is in the way".

        Returns the dialog's text (useful evidence: a KYC failure message is
        exactly what a test wants to record), or "" if there was no dialog.
        """
        try:
            found = self.page.evaluate(FIND_DISMISS_JS)
        except Exception:
            return ""
        if not found or not found.get("found"):
            return ""

        # Click it with Playwright, not from JavaScript. This is the oldest
        # lesson in this project: Angular ignores script-generated clicks, so a
        # JS click here would report success and change nothing. The JS above
        # only MARKS the button; the click is a real browser event.
        try:
            self.page.locator('[data-harness-dismiss="1"]').first.click(timeout=8000)
            self.page.wait_for_timeout(1200)
        except Exception:
            return ""
        finally:
            try:
                self.page.evaluate(
                    """() => document.querySelectorAll('[data-harness-dismiss]')
                             .forEach(el => el.removeAttribute('data-harness-dismiss'))""")
            except Exception:
                pass

        # Never return "" after actually closing something. The caller reads ""
        # as "there was no dialog", and a dialog whose text we could not parse
        # is still a dialog - conflating the two is what made settle() sit and
        # wait for 60 seconds with the popup plainly on screen.
        return found.get("text") or f"(dialog closed via '{found.get('caption')}')"

    def dialog_text(self) -> str:
        """Whatever the dialog on screen says, or '' if there is none."""
        for selector in (".modal-body", ".modal-content",
                         "mat-dialog-container", ".cdk-overlay-pane"):
            try:
                found = self.page.locator(selector)
                if found.count():
                    text = found.first.inner_text(timeout=1500).strip()
                    # Drop the button caption so "KYC verification success.
                    # Okay" is reported as the message, not the message plus
                    # the thing we are about to click.
                    text = re.sub(r"\s*\b(Okay|OK|Got it|Close)\b\s*$", "", text)
                    if text:
                        return " ".join(text.split())
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------- step 1 / 2

    def fill_details(self, who: Customer, attempts: int = 2) -> "KycPage":
        """
        Fill the KYC form, and check the app agrees it is complete.

        Filling is retried because it is measurably flaky: the same code on the
        same data reports "complete" one run and "INCOMPLETE" the next. The
        cause is the form re-rendering while we type - a pincode lookup or a
        gender change can rebuild part of the screen and quietly discard a value
        that was already entered.

        So we fill, ask the app whether it is satisfied, and if not, fill the
        fields it is still complaining about. A human hitting this would simply
        retype the empty box; this does the same thing.
        """
        for attempt in range(1, attempts + 1):
            self._fill_once(who)
            if self.can_proceed():
                return self
            if attempt < attempts:
                # Let any in-flight re-render settle before trying again,
                # otherwise the retry races the same rebuild that lost the value.
                self.page.wait_for_timeout(2000)
        return self

    def _fill_once(self, who: Customer) -> None:
        self.dismiss_popup()
        for fc, value in (
            (self.PAN, who.pan),
            (self.FIRST, who.first_name),
            (self.MIDDLE, who.middle_name),
            (self.LAST, who.last_name),
            (self.DOB, who.dob_kyc),
            (self.MOBILE, who.mobile),
            (self.EMAIL, who.email),
            (self.PINCODE, who.pincode),
        ):
            if value:
                ui.text_field(self.page, fc, value)
                self.page.wait_for_timeout(250)

        if who.gender:
            # Gender last, and tolerantly: selecting it can trigger a re-render,
            # and if the dropdown is briefly unavailable that is a reason to
            # retry the whole fill, not to abort the run.
            try:
                ui.dropdown(self.page, self.GENDER, who.gender)
            except Exception:
                pass

    def can_proceed(self) -> bool:
        """
        Is there a live Proceed button?

        ANY of them, not the first one. The portal renders duplicate mobile and
        desktop layouts - the same trap that made the quote list click a Buy Now
        that was present but unclickable - so `.first` can easily be a hidden
        copy that is permanently disabled while the real button is fine.
        """
        # Clear any dialog first. It is not only that a dialog swallows clicks -
        # while one is open Angular Material hides the rest of the page from the
        # accessibility tree, which is what made an earlier version of this
        # method report a perfectly good form as incomplete.
        self.dismiss_popup()
        return ui.button_enabled(self.page, "Proceed")

    def why_blocked(self) -> list[str]:
        """
        Explain a dead Proceed button when no visible field looks wrong.

        A run reported "form is INCOMPLETE" and then listed nothing, because
        `missing_required` only looks at VISIBLE inputs. That is a fair rule for
        fields a human types into, but three common blockers are invisible to it:

          - a required tickbox, whose real <input> Angular Material hides behind
            a styled box, so it has no size and gets skipped
          - a radio group with nothing chosen, which shows as several unchecked
            inputs rather than as one empty field
          - a control that is invalid but scrolled out of the layout entirely

        So this asks a different question - not "which field looks empty" but
        "what does Angular think is wrong, anywhere on this form" - and reports
        the Proceed buttons themselves, since a button disabled while a request
        is in flight is a wait, not a missing field.
        """
        return self.page.evaluate(r"""
        () => {
          const out = [];
          const name = el => el.getAttribute('formcontrolname')
                           || el.getAttribute('name')
                           || el.getAttribute('placeholder') || el.id || 'unnamed';

          // 1. Angular's own verdict on the form as a whole.
          for (const form of document.querySelectorAll('form')) {
            if (form.className.toString().includes('ng-invalid')) {
              out.push('form: Angular marks the whole form invalid');
              break;
            }
          }

          // 2. Every invalid control, INCLUDING ones with no size.
          for (const el of document.querySelectorAll(
                 'input.ng-invalid, mat-select.ng-invalid, textarea.ng-invalid')) {
            const r = el.getBoundingClientRect();
            const where = (r.width > 0 && r.height > 0) ? 'visible' : 'HIDDEN';
            const value = el.tagName === 'MAT-SELECT'
                        ? (el.innerText || '').trim() : (el.value || '');
            out.push(`invalid (${where}): ${name(el)}${value ? ` = "${value}"` : ' (empty)'}`);
          }

          // 3. Tickboxes that are required and not ticked.
          for (const el of document.querySelectorAll('input[type=checkbox]')) {
            const req = el.required || el.className.toString().includes('ng-invalid');
            if (req && !el.checked) out.push(`unticked checkbox: ${name(el)}`);
          }

          // 4. Radio groups where nothing is chosen.
          const groups = {};
          for (const el of document.querySelectorAll('input[type=radio]')) {
            const g = el.getAttribute('name') || name(el);
            groups[g] = groups[g] || false;
            if (el.checked) groups[g] = true;
          }
          for (const [g, chosen] of Object.entries(groups)) {
            if (!chosen) out.push(`nothing chosen in radio group: ${g}`);
          }

          // 5. The buttons themselves. A disabled button with a spinner beside
          //    it means "busy", which is a completely different problem from
          //    "incomplete" and should never be reported as the same thing.
          for (const b of document.querySelectorAll('button')) {
            if (!/proceed/i.test(b.innerText || '')) continue;
            const r = b.getBoundingClientRect();
            out.push(`button "${(b.innerText || '').trim().slice(0, 24)}": `
                     + `${b.disabled ? 'DISABLED' : 'enabled'}, `
                     + `${(r.width > 0 && r.height > 0) ? 'visible' : 'hidden'}`);
          }
          if (document.querySelector('mat-spinner, .mat-mdc-progress-spinner')) {
            out.push('a spinner is on screen - the app may still be working');
          }

          // 6. An open dialog. This both swallows clicks AND removes the rest
          //    of the page from the accessibility tree, so it can make a good
          //    form look broken to anything that searches by role.
          const dialog = document.querySelector('mat-dialog-container, .cdk-overlay-pane');
          if (dialog) {
            out.push(`a dialog is open: "`
                     + `${(dialog.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60)}"`);
          }
          if (document.querySelector('[aria-hidden="true"] input[formcontrolname]')) {
            out.push('the form is inside an aria-hidden region - '
                     + 'role-based lookups cannot see it');
          }
          return out;
        }
        """)

    def missing_required(self) -> list[str]:
        """
        Which fields the APP considers still invalid or empty.

        "Form is INCOMPLETE" on its own is a useless message - it tells you
        something is wrong but not what, so the next step is guessing. Angular
        marks every invalid control with its own ng-invalid class, so the app
        already knows the answer; we just have to ask it.

        INVALID fields come first, because those are the ones actually blocking
        the form. Merely-empty fields are listed afterwards and flagged as
        "may be optional" - an empty middle name is not necessarily a problem,
        and reporting it with the same weight as a genuinely invalid field sends
        people to fix the wrong thing.
        """
        return self.page.evaluate(r"""
        () => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const blocking = [];
          const maybe = [];

          for (const el of document.querySelectorAll('input, mat-select, textarea')) {
            if (!visible(el)) continue;
            const name = el.getAttribute('formcontrolname')
                       || el.getAttribute('placeholder') || el.id || 'unnamed';
            const cls = el.className.toString();
            const invalid = cls.includes('ng-invalid');
            const value = el.tagName === 'MAT-SELECT'
                        ? (el.innerText || '').trim()
                        : (el.value || '');

            if (invalid) {
              // Angular says this one is wrong - it IS blocking the form.
              blocking.push(`${name}${value ? ` = "${value}"` : ' (empty)'} [BLOCKING]`);
            } else if (!value) {
              // Empty but not flagged invalid, so probably optional.
              maybe.push(`${name} (empty - may be optional)`);
            }
          }
          return blocking.concat(maybe);
        }
        """)

    def proceed(self) -> None:
        """Submit. THIS CALLS THE INSURER'S KYC SERVICE."""
        ui.click_button(self.page, "Proceed")

    # ------------------------------------------------------------- step 2 / 2

    def wait_for_kyc_response(self, timeout_ms: int = 40_000) -> str:
        """
        Wait for the KYC call to come back, however it chooses to answer.

        Three possible outcomes, and we return as soon as ANY of them happens
        rather than sleeping long enough to cover the slowest:

            "documents"  the upload step appeared
            "proposal"   we moved on - KYC passed with no documents needed
            "dialog"     a message came back (often a failure reason)
            "timeout"    nothing happened in time
        """
        waited = 0
        while waited < timeout_ms:
            if "/two-wheeler/proposal" in self.page.url:
                return "proposal"
            try:
                if self.page.get_by_text("Upload Documents", exact=False
                                         ).first.is_visible(timeout=600):
                    return "documents"
            except Exception:
                pass
            try:
                if self.page.locator(".modal-body").first.is_visible(timeout=600):
                    return "dialog"
            except Exception:
                pass
            self.page.wait_for_timeout(800)
            waited += 800
        return "timeout"

    def upload_step_showing(self, timeout_ms: int = 20_000) -> bool:
        """
        Did the insurer ask for documents? Not all of them do.

        Waits a little rather than checking instantly, because this screen
        appears only after the KYC call comes back.
        """
        try:
            self.page.get_by_text("Upload Documents", exact=False).first.wait_for(
                state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _document_number(self, choice: str, who: Customer, docs_dir: str) -> str:
        """
        The number that belongs to a chosen document type.

        The PAN is ordinary test data and lives with the customer. The Aadhaar
        number is a real government ID, so it is NEVER stored in this repository -
        it is read from the file names in the team's own document folder, whose
        path comes from git-ignored settings. Same rule as the scans themselves.
        """
        low = choice.lower()
        if "pan" in low:
            return who.pan
        if "aadh" in low:
            return documents.aadhaar_number_from(docs_dir)
        return ""

    def _set_number(self, form_control: str, value: str) -> bool:
        """
        Type a document number into the box belonging to this card.

        Re-marks the card first. The mark is an attribute on a live DOM node, and
        these cards are rebuilt whenever their contents change, so a mark taken
        even a second earlier may point at an element that is already gone.
        """
        try:
            self.page.evaluate(MARK_CARD_JS, form_control)
            box = self.page.locator('[data-harness-number="1"]').first
            if not box.count():
                return False
            box.scroll_into_view_if_needed(timeout=3000)
            box.click(timeout=4000)
            box.fill(value, timeout=6000)
            self.page.wait_for_timeout(400)
            return bool(box.input_value(timeout=1500).strip())
        except Exception:
            return False

    def upload_documents(self, who: Customer, docs_dir: str = "") -> "KycPage":
        """
        Choose a document type this insurer accepts, and attach the matching file.

        The type is NOT hardcoded. Insurers differ about what they will take -
        IFFCOTOKIO's identity dropdown offers only "PAN Card", while NATIONAL
        lists CKYC, PAN, Voter ID, Driving Licence, Passport and Aadhaar - so
        asking for "Aadhaar Card" everywhere selected nothing at IFFCOTOKIO. The
        file still uploaded, the type stayed blank, and KYC could not complete,
        with nothing on screen to say why.

        So each slot is handled on its own terms: open the dropdown, read what is
        actually offered, pick the first type we hold a file for, and upload THAT
        file. Type and file are chosen together, because sending an Aadhaar scan
        labelled "PAN Card" would be worse than failing.

        docs_dir is the folder holding the team's real scans. Without it we fall
        back to generated blanks, which the browser accepts but no OCR can read -
        fine for proving the upload works, useless for passing KYC.
        """
        self.dismiss_popup()

        slots = (
            ("selectedIdentityProof", 0, "identity"),
            ("selectedAddressProof", 1, "address"),
        )
        selects = self.page.locator("mat-select")

        for form_control, index, kind in slots:
            # Always start from a clean page. A dropdown left open from the
            # previous slot covers everything with its backdrop, and the next
            # click then times out blaming a control that works perfectly -
            # which is exactly how this step used to fail.
            ui.close_overlays(self.page)

            # By formcontrolname where the app gives one ("selectedAddressProof"
            # is right there in the markup), positionally only as a fallback.
            by_name = self.page.locator(f'mat-select[formcontrolname="{form_control}"]')
            target = by_name.first if by_name.count() else (
                selects.nth(index) if selects.count() > index else None)
            if target is None:
                self._notes.append(f"no {kind} document dropdown on screen")
                continue

            try:
                target.scroll_into_view_if_needed(timeout=4000)
                target.click(timeout=15_000)
                self.page.wait_for_timeout(600)
            except Exception:
                ui.close_overlays(self.page)
                self._notes.append(f"could not open the {kind} document dropdown")
                continue

            offered = ui.options_on_screen(self.page)
            choice, filename = who.document_for(kind, offered)

            if not choice:
                # A real finding, not a crash: this insurer wants a proof we do
                # not have. Naming both sides tells someone exactly which file
                # to add to the test folder.
                ui.close_overlays(self.page)
                self._notes.append(
                    f"{kind} proof: this insurer accepts "
                    f"{', '.join(offered[:8]) or '(nothing listed)'} - "
                    f"we hold none of those")
                continue

            picked, _ = ui.pick_option(self.page, choice)
            if not picked:
                self._notes.append(
                    f"{kind} proof: '{choice}' was listed but could not be selected")
                continue

            # Now the file and the number, for THIS card. File inputs sit hidden
            # behind a styled button, so files are set on the input directly -
            # clicking the button opens the operating system's file dialog, which
            # Playwright cannot drive because Windows owns that window.
            ui.close_overlays(self.page)
            found = self.page.evaluate(MARK_CARD_JS, form_control)
            if not found.get("card"):
                self._notes.append(f"{kind} proof: no file input in its card")
                continue

            # The NUMBER first, then the file. Order matters: attaching a file
            # makes Angular rebuild the card to show a preview, which destroys
            # the element we marked a moment earlier - so typing afterwards hit
            # a node that no longer existed and reported "could not type" for a
            # box that was sitting there perfectly.
            number = self._document_number(choice, who, docs_dir)
            typed = False
            if found.get("number") and number:
                typed = self._set_number(form_control, number)

            path = documents.resolve(filename, docs_dir, kind)
            self.page.evaluate(MARK_CARD_JS, form_control)      # re-mark, post-typing
            self.page.locator('[data-harness-file="1"]').first.set_input_files(str(path))
            self.page.wait_for_timeout(800)

            # Some screens only enable the number box once a file is attached,
            # so a failure before the upload is worth one more try after it.
            if found.get("number") and number and not typed:
                typed = self._set_number(form_control, number)

            shown = f"{choice} = {path.name}"
            if found.get("number"):
                if not number:
                    self._notes.append(
                        f"{kind} proof: this insurer wants the {choice} number and "
                        f"none is available - for Aadhaar it is read from the "
                        f"document folder's own file names")
                elif typed:
                    shown += f" (#{documents.mask(number)})"
                else:
                    self._notes.append(
                        f"{kind} proof: could not type the {choice} number")
            self.uploaded.append(shown)
            # Uploads are processed server-side; giving each one a moment stops
            # the second from cancelling the first on a slow environment.
            self.page.wait_for_timeout(2500)

        self._upload_photograph(docs_dir)
        return self

    def _upload_photograph(self, docs_dir: str = "") -> None:
        """
        Attach the customer photograph, which is its own slot with no dropdown.

        Easy to miss, and expensive to miss: with both proofs complete and no
        error text anywhere, Proceed simply did nothing, because this third card
        was still empty. It is handled separately because it has no document-type
        dropdown to key off - it is found by its caption instead.

        Two ways of attaching are tried, because this card does not behave like
        the proof cards. Setting the hidden input directly works for those, but
        left this one still showing its Upload button, so we then drive the
        button's own file chooser - which is what a person clicking it triggers.
        """
        try:
            found = self.page.evaluate(MARK_PHOTO_JS)
        except Exception:
            return
        if not found.get("found"):
            return                      # this insurer does not ask for one
        if not found.get("waiting"):
            return                      # already attached

        photo = documents.find_photo(docs_dir)
        path = photo or documents.placeholder("photo")

        # First try: set the hidden input, as the proof cards accept.
        attached = False
        try:
            self.page.locator('[data-harness-photo="1"]').first.set_input_files(str(path))
            self.page.wait_for_timeout(1500)
            attached = not self.page.evaluate(MARK_PHOTO_JS).get("waiting", True)
        except Exception:
            attached = False

        # Second try: click Upload and answer the file chooser it opens.
        # Playwright intercepts that chooser, so the operating system's own
        # dialog - which it cannot drive - never appears.
        if not attached:
            try:
                self.page.evaluate(MARK_PHOTO_JS)
                button = self.page.locator('[data-harness-photo-button="1"]').first
                with self.page.expect_file_chooser(timeout=10_000) as chooser:
                    button.click(timeout=8000)
                chooser.value.set_files(str(path))
                self.page.wait_for_timeout(2000)
                attached = not self.page.evaluate(MARK_PHOTO_JS).get("waiting", True)
            except Exception:
                attached = False

        if not attached:
            self._notes.append(
                f"photograph: {path.name} would not attach - the slot still asks "
                f"for one, so Proceed will not move")
            return

        self.uploaded.append(f"Photograph = {path.name}")
        if not photo:
            self._notes.append(
                "photograph: no customer photo in the document folder, so a "
                "generated placeholder was sent - fine for proving the upload "
                "works, but an insurer checking the face will reject it")
        elif docs_dir and photo.parent.name.lower() not in docs_dir.lower():
            # Say so plainly. A face that does not match the PAN is a real
            # difference between this run and a genuine customer journey, and it
            # belongs in the log rather than in somebody's memory.
            self._notes.append(
                f"photograph: borrowed {photo.name} from the "
                f"'{photo.parent.name}' folder - this customer has no photo, so "
                f"the face does not match the PAN being verified")

    @property
    def notes(self) -> list[str]:
        """Non-fatal problems worth printing in the run report."""
        return list(self._notes)

    def finish(self, timeout_ms: int = 60_000) -> str:
        """
        Submit the documents, clear whatever dialogs appear, and get us moving.

        Returns the verification message - "KYC verification success." on the
        happy path, or the failure reason. That text is the most useful thing
        this screen produces, so it is returned rather than swallowed.

        WHY THIS LOOPS INSTEAD OF WAITING A FIXED TIME
        ----------------------------------------------
        The app puts a dialog up when KYC comes back, and it does NOT leave this
        screen until that dialog is dismissed. The original version clicked
        Proceed, slept nine seconds, then dismissed once - which worked only if
        the dialog happened to arrive inside those nine seconds. When the
        insurer answered slower, we dismissed nothing, navigation never
        happened, and the run died waiting for a proposal page that was never
        coming. Same code, different day, different result.

        So: click, then keep watching. Dismiss every dialog that appears, and
        stop as soon as we have actually left the page. That works whether the
        insurer answers in two seconds or thirty.
        """
        ui.click_button(self.page, "Proceed")
        # stop_on_upload_step=False is essential here. We are ON the upload step,
        # so a settle that treats "the upload step is showing" as an arrival
        # returns instantly - before the insurer has answered and before its
        # dialog exists. That is exactly what happened: the run reported "no
        # message shown" while "Your KYC request is successfully logged" sat on
        # screen with its Okay button unclicked, and the journey stopped there.
        return self.settle(timeout_ms, stop_on_upload_step=False)

    def settle(self, timeout_ms: int = 60_000,
               stop_on_upload_step: bool = True) -> str:
        """
        Clear dialogs until we have actually left the KYC screen.

        Used after ANY KYC submission, with or without a document upload -
        which matters, because insurers differ. ZUNO asks for documents and
        finishes via that screen; RELIANCE verifies from the PAN alone and
        finishes right here. The first version only ran this dialog loop on the
        upload path, so RELIANCE submitted KYC successfully, put its
        confirmation dialog up, and then sat there forever with nobody to
        dismiss it - looking exactly like a failure when KYC had in fact passed.

        Returns the verification message the app showed, or a note saying we
        never got past this screen.
        """
        message = ""
        waited = 0
        while waited < timeout_ms:
            url = self.page.url

            if "/two-wheeler/proposal" in url:
                return message                  # we are through

            # A REDIRECT can happen HERE, after the form is submitted - not only
            # before it. RELIANCE looks inline right up until you press Proceed,
            # then hands the browser to an external KYC portal. Detecting only
            # at the start therefore mislabels it, and the run sits waiting for
            # a proposal page that was never coming because we are not even on
            # the same site any more.
            host = _host_of(url)
            if host and self._our_host and host != self._our_host:
                self.redirected_to = host
                return f"REDIRECTED to {host}"

            text = self.dismiss_popup()
            if text:
                message = text
                # A dialog usually gates the navigation, so give the app a
                # moment to move now that it is out of the way.
                self.page.wait_for_timeout(2500)
                continue

            # Step 2/2 appearing is also "settled" - it means KYC came back and
            # now wants documents. This is checked AFTER the dialog loop on
            # purpose: the success popup sits on top of the upload step, so
            # asking first (as the runner used to) reliably answered "no upload
            # step" while the upload step was right there underneath.
            #
            # Only when we are WAITING for that step, though. Callers already on
            # it - finish(), after submitting the documents - must keep waiting
            # for the insurer's answer instead of treating their own screen as
            # the destination.
            if stop_on_upload_step and self.upload_step_showing(timeout_ms=1200):
                return message

            # Stop early rather than burning the whole budget on a page that is
            # never going to render. Checked only after a grace period, because
            # a brief "Loading" between screens is completely normal - it is
            # staying there that means something is wrong.
            if waited >= 15_000:
                ui.raise_if_stuck(self.page, "waiting for KYC to come back")

            self.page.wait_for_timeout(1500)
            waited += 1500

        return message or "(no message, and never left the KYC screen)"


def _host_of(url: str) -> str:
    """Hostname of a URL, or "" if it has none (about:blank, data: and friends)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
