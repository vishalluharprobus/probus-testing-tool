"""
Fill a form by READING it, instead of guessing what the fields are called.

THE PROBLEM WITH GUESSING
-------------------------
The first version of the proposal page hardcoded field names taken from
screenshots - "mobileNo", "gender", "maritalStatus". Screenshots show LABELS,
not the names Angular uses internally, so several guesses were wrong and the
run stopped with "owner details incomplete: Mobile Number, gender".

Guessing does not scale either: five products x several screens each is a lot of
names to guess, keep correct, and re-guess whenever the front end changes.

THE APPROACH HERE
-----------------
Ask the page what it contains, then match each field to our test data by
MEANING rather than by name:

    a field labelled "Mobile Number"  ->  the customer's mobile
    a field labelled "Select Gender"  ->  the customer's gender

Matching looks at the formcontrolname, the placeholder, the surrounding label
and the section heading, so it works whether the developers called it
"mobileNo", "mobile_number" or "contactNo".

Two rules keep this honest rather than merely clever:

  1. It only fills fields that are EMPTY or that Angular marks invalid. A value
     the insurer pre-filled from KYC is never overwritten - that data came from
     the insurer and is the whole point of doing KYC.
  2. It reports exactly what it filled and what it could not match, so a gap
     surfaces as a named field rather than a vague failure.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from core import ui

# Every visible control, with enough context to work out what it is for.
DESCRIBE_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const labelFor = el => {
    const bits = [];
    const field = el.closest('mat-form-field, .form-group, .col, .field');
    if (field) {
      // The visible caption, minus whatever the control itself already shows.
      const own = (el.value || el.innerText || '').trim();
      let text = (field.innerText || '').replace(/\s+/g, ' ').trim();
      if (own) text = text.split(own).join(' ');
      bits.push(text);
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${el.id}"]`);
      if (lab) bits.push(lab.innerText);
    }
    return bits.join(' ').replace(/\s+/g, ' ').trim().slice(0, 60);
  };

  const out = [];
  for (const el of document.querySelectorAll('input, mat-select, textarea')) {
    if (!visible(el)) continue;
    if (el.type === 'hidden') continue;
    const tag = el.tagName.toLowerCase();
    const cls = el.className.toString();
    out.push({
      kind: tag === 'mat-select' ? 'select'
          : (el.type === 'radio' || el.type === 'checkbox') ? el.type : 'text',
      name: el.getAttribute('formcontrolname') || '',
      placeholder: el.getAttribute('placeholder') || '',
      label: labelFor(el),
      value: tag === 'mat-select' ? (el.innerText || '').trim() : (el.value || ''),
      required: cls.includes('ng-invalid') || el.required === true,
      invalid: cls.includes('ng-invalid'),
    });
  }
  return out;
}
"""


@dataclass
class Field:
    kind: str
    name: str
    placeholder: str
    label: str
    value: str
    required: bool
    invalid: bool

    @property
    def haystack(self) -> str:
        """Everything we know about this field, for keyword matching."""
        return f"{self.name} {self.placeholder} {self.label}".lower()

    @property
    def needs_filling(self) -> bool:
        # A select showing its own prompt ("Select Gender") is still empty.
        placeholder_text = self.value.lower().startswith("select")
        return self.invalid or not self.value.strip() or placeholder_text


def describe(page: Page) -> list[Field]:
    return [Field(**row) for row in page.evaluate(DESCRIBE_JS)]


def autofill(page: Page, wanted: dict[str, str],
             only_empty: bool = True) -> tuple[list[str], list[str]]:
    """
    Fill what the form is missing, by matching keywords to values.

    `wanted` maps a keyword to the value to use, most specific first:

        {"mobile": "9512296502", "gender": "Male", "occupation": "Farmer"}

    Returns (filled, unmatched) - what we set, and which still-empty fields we
    had no data for. The second list is the useful one: it names exactly what a
    human needs to add, instead of leaving a vague "incomplete".
    """
    filled: list[str] = []
    unmatched: list[str] = []

    for field in describe(page):
        if only_empty and not field.needs_filling:
            continue          # already has a value - usually from KYC. Leave it.
        if field.kind in ("radio", "checkbox"):
            continue          # handled explicitly, never guessed

        value = _match(field, wanted)
        if value is None:
            if field.needs_filling and field.required:
                unmatched.append(field.label or field.name or field.placeholder)
            continue

        # An empty value is not data. Writing "" into GSTIN achieves nothing,
        # reports a fill that did not happen, and hides the fields that DO need
        # attention behind noise.
        usable = ([value] if isinstance(value, str) else list(value))
        if not any(str(v).strip() for v in usable):
            continue

        worked, reason = _apply(page, field, value)
        if worked:
            # `reason` carries which candidate won, which matters when the data
            # offered alternatives: "occupation = OTHER" is a different run from
            # "occupation = Agriculturist".
            filled.append(f"{field.name or field.label} = {reason}")
        else:
            # Say WHY it could not be set, not merely that it was not.
            tried = value if isinstance(value, str) else " / ".join(map(str, value))
            unmatched.append(
                f"{field.name or field.label} (tried '{tried}' - {reason})")

    return filled, unmatched


def _match(field: Field, wanted: dict[str, str]) -> str | None:
    """
    Find the value meant for this field.

    Longest keyword first, so "mobile number" wins over a bare "mobile" and
    "nominee name" is not captured by "name".
    """
    # Compare with spaces removed, so a readable keyword matches Angular's
    # camelCase control names: "nominee name" finds `nomineeName`. Without this
    # the nominee fields silently went unfilled, and worse, `nomineeDob` matched
    # the short key "dob" and was given the PROPOSER's date of birth - wrong
    # data that the form accepts and nobody would notice.
    squeeze = field.haystack.replace(" ", "")
    for keyword in sorted(wanted, key=len, reverse=True):
        needle = keyword.lower().replace(" ", "")
        if needle and needle in squeeze:
            return wanted[keyword]
    return None


def _apply(page: Page, field: Field, value) -> tuple[bool, str]:
    """
    Set a value, using whichever mechanism this control needs.

    `value` may be a single string OR a list of acceptable answers, tried in
    order. The list form exists because insurers do not share a vocabulary: the
    occupation dropdown offers "Agriculturist" at NATIONAL and
    "BUISNESSMAN / HOUSEWIFE / OTHER / STUDENT" at IFFCOTOKIO, so any single
    value is wrong somewhere. Offering alternatives lets one set of test data
    drive every insurer, instead of a per-insurer table of magic strings.

    Returns (worked, note). On success the note is the value actually chosen -
    worth printing, since "occupation = OTHER" and "occupation = Agriculturist"
    are different test runs. On failure it is the reason, including the list the
    dropdown really offered.
    """
    candidates = [value] if isinstance(value, str) else [v for v in value if v]
    try:
        if field.kind == "select":
            target = (page.locator(f'mat-select[formcontrolname="{field.name}"]')
                      if field.name else
                      page.locator("mat-select").filter(has_text=field.value))
            # Scroll it into view first. A select below the fold can be clicked
            # by Playwright but its option panel may render off-screen, and the
            # option click then silently misses.
            target.first.scroll_into_view_if_needed(timeout=4000)
            target.first.click(timeout=6000)
            page.wait_for_timeout(700)

            # Exact first, then a forgiving match, for each candidate in turn.
            # Option text is written by humans: "Farmer" can appear as "Farmer ",
            # "FARMER" or "Farmer / Agriculturist", and refusing all three
            # because none is byte-equal would be pedantry rather than
            # correctness. (One insurer's list even reads "BUISNESSMAN".)
            for candidate in candidates:
                for matcher in (
                    page.get_by_role("option", name=candidate, exact=True),
                    page.get_by_role("option", name=candidate, exact=False),
                    page.locator("mat-option").filter(has_text=candidate),
                ):
                    if matcher.count():
                        matcher.first.click(timeout=6000)
                        page.wait_for_timeout(400)
                        ui.close_overlays(page)
                        return True, candidate

            # Nothing matched. Report what the list ACTUALLY offered, because
            # "no matching option" alone just moves the guessing one step along
            # - the next person still has to open the dropdown by hand to find
            # out that "Mr." is spelled "Mr" or that "Farmer" is "Agriculturist".
            offered = ui.options_on_screen(page)
            ui.close_overlays(page)                   # never leave it covering the page
            if offered:
                shown = ", ".join(offered[:10])
                more = f" (+{len(offered) - 10} more)" if len(offered) > 10 else ""
                return False, f"offers: {shown}{more}"
            return False, "the dropdown opened but offered nothing"

        value = candidates[0] if candidates else ""
        locator = (page.locator(f'input[formcontrolname="{field.name}"]')
                   if field.name else
                   page.locator(f'input[placeholder="{field.placeholder}"]'))
        locator.first.scroll_into_view_if_needed(timeout=4000)
        locator.first.fill(value, timeout=6000)
        page.wait_for_timeout(200)

        # Confirm it stuck. Angular can reject or reformat a value - a date in
        # the wrong format simply vanishes - and reporting a fill that did not
        # survive is worse than reporting a failure.
        try:
            if locator.first.input_value(timeout=2000).strip() == "":
                return False, "the value did not stick - the app rejected it"
        except Exception:
            pass
        # Return the value, not "". The caller prints this as what it set, and
        # returning blank made every text field report `address1 = ` - a filled
        # form that reads like an empty one.
        return True, value
    except Exception as exc:
        return False, f"{type(exc).__name__} while filling"
