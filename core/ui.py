"""
Angular Material helpers.

Everything in this file exists because of one discovery during portal mapping:
Angular Material only reacts to REAL browser events. Script-generated clicks
(element.click() in JS) are silently ignored - the radio stays unselected and
the autocomplete never opens.

Playwright dispatches real events through the debug protocol, so the app behaves
exactly as it does for a human. These wrappers just add the waiting and the
option-picking that Material's cascading forms need.
"""
import re

from playwright.sync_api import Page, expect

# The portal's forms cascade: each field only appears once the previous one is
# chosen, and each choice triggers a server call. These are generous on purpose -
# a slow insurer lookup is not a test failure.
FIELD_TIMEOUT_MS = 20_000
OPTION_TIMEOUT_MS = 24_000

# How many times to retype into an autocomplete before giving up. Three quick
# attempts beat one long wait, because the failure is an EMPTY result list
# rather than a slow one - waiting longer does not help, retyping does.
AUTOCOMPLETE_ATTEMPTS = 3


class LookupTimedOut(RuntimeError):
    """
    A master-data lookup (RTO, make, model, variant) never returned anything.

    Its own type because it is almost always the environment being busy rather
    than a broken selector, and the two deserve different reactions: retry the
    first, investigate the second.
    """


class PageStuckLoading(RuntimeError):
    """
    The app never finished rendering a screen.

    Its own category because the cause is almost never the test: usually a dead
    session token or an app dependency that is unavailable. Reporting it as a
    generic "element not found" sends someone hunting for a selector bug that
    does not exist.
    """


def wait_for_screen(page: Page, form_control: str, timeout_ms: int = FIELD_TIMEOUT_MS) -> None:
    """
    Wait for a screen's first field, and explain properly if it never arrives.

    The portal shows a bare "Loading" while it waits on its own back end, and
    stays there indefinitely rather than timing out or showing an error - so
    "Loading is still on screen after 20 seconds" is a real, nameable diagnosis.
    """
    try:
        page.locator(f'input[formcontrolname="{form_control}"]').wait_for(
            state="visible", timeout=timeout_ms)
    except Exception as exc:
        body = ""
        try:
            body = page.inner_text("body")[:400]
        except Exception:
            pass

        if "Loading" in body:
            raise PageStuckLoading(
                f"The page is still showing 'Loading' after "
                f"{timeout_ms / 1000:.0f}s and never rendered the form.\n"
                f"  Most likely: the login token has expired (it only lasts a "
                f"few minutes), or the app's Firebase connection pool is full.\n"
                f"  What to do: just run the command again - a fresh login is "
                f"taken automatically."
            ) from exc
        raise


def autocomplete(page: Page, form_control: str, type_text: str, choose: str) -> None:
    """
    Fill a Material autocomplete and pick an option from the dropdown.

    form_control : the formcontrolname attribute, e.g. "rtoName"
    type_text    : what to type to trigger the search, e.g. "GJ-01"
    choose       : the exact option text to click, e.g. "GJ-01 Ahmedabad"

    We type a short prefix rather than the full value because the portal's
    search matches on prefix, and typing the full text sometimes returns
    nothing (e.g. variant strings containing brackets).
    """
    field = page.locator(f'input[formcontrolname="{form_control}"]')
    field.wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)

    # Each keystroke triggers a server lookup for master data (RTO, make, model,
    # variant). On a loaded environment that lookup sometimes returns nothing at
    # all - not an error, just an empty list - and the dropdown never opens.
    #
    # Retyping reliably fixes it, so we retry rather than failing. This is not
    # papering over a defect: a slow master-data lookup is a real condition the
    # app lives with, and a human hitting it would simply retype too.
    option = page.get_by_role("option", name=choose, exact=True)
    last_error: Exception | None = None

    for attempt in range(1, AUTOCOMPLETE_ATTEMPTS + 1):
        try:
            field.click()
            field.fill("")                       # clear before retyping
            page.wait_for_timeout(300)
            field.fill(type_text)

            # Shorter wait per attempt - three quick tries beat one long one,
            # because the failure mode is "empty list", not "slow list".
            option.first.wait_for(state="visible",
                                  timeout=OPTION_TIMEOUT_MS // AUTOCOMPLETE_ATTEMPTS)
            option.first.click()

            # Confirm the selection landed. Material will happily leave the typed
            # prefix in the box if the click missed, and every later step would
            # then fail somewhere confusing instead of here.
            expect(field).to_have_value(choose, timeout=FIELD_TIMEOUT_MS)
            return
        except Exception as exc:
            last_error = exc
            if attempt < AUTOCOMPLETE_ATTEMPTS:
                page.wait_for_timeout(2000)

    raise LookupTimedOut(
        f"The '{form_control}' lookup never offered {choose!r}.\n"
        f"  Typed {type_text!r} and waited, {AUTOCOMPLETE_ATTEMPTS} times over.\n"
        f"  This is usually the environment being busy rather than a broken test - "
        f"the master-data lookup returned an empty list.\n"
        f"  What to do: wait a few minutes and run again. If it keeps happening "
        f"for the same field, check whether that master data still exists."
    ) from last_error


def options_for(page: Page, form_control: str, prefix: str,
                wait_ms: int = 6000) -> list[str]:
    """
    Type a prefix into an autocomplete and read back what it offers.

    This is how we learn valid master data instead of guessing it. The portal's
    dropdowns ARE the master data - every RTO, make, model and variant the plan
    master holds is reachable by typing a prefix and reading the list. Inventing
    values fails at screen one with an empty dropdown and teaches us nothing;
    harvesting them gives us combinations that are real by construction.

    Returns [] when nothing comes back, which is a normal answer (no RTO starts
    "ZZ") and not an error.
    """
    field = page.locator(f'input[formcontrolname="{form_control}"]')
    try:
        field.wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)
        field.click()
        field.fill("")
        page.wait_for_timeout(250)
        field.fill(prefix)
        page.wait_for_timeout(wait_ms)
        return [t.strip() for t in
                page.get_by_role("option").all_inner_texts() if t.strip()]
    except Exception:
        return []


def dropdown(page: Page, form_control: str, choose: str) -> None:
    """Pick a value from a <mat-select> (not an autocomplete - no typing)."""
    select = page.locator(f'mat-select[formcontrolname="{form_control}"]')
    select.wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)
    select.click()

    option = page.get_by_role("option", name=choose, exact=True)
    option.first.wait_for(state="visible", timeout=OPTION_TIMEOUT_MS)
    option.first.click()
    expect(select).to_contain_text(choose, timeout=FIELD_TIMEOUT_MS)


def radio(page: Page, label: str) -> None:
    """
    Select a radio by its visible label, e.g. "Comprehensive", "Individual".

    We target the label rather than the input's value attribute because the
    group names are auto-generated (mat-radio-group-0) and would shift if the
    developers reorder the form. Labels are what a human reads, so they are
    both more stable and more readable in a failure report.
    """
    page.get_by_role("radio", name=label, exact=True).check(timeout=FIELD_TIMEOUT_MS)


def text_field(page: Page, form_control: str, value: str) -> None:
    """Fill a plain text input by formcontrolname."""
    field = page.locator(f'input[formcontrolname="{form_control}"]')
    field.wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)
    field.fill(value)


def buttons_labelled(page: Page, label: str):
    """
    Every button whose visible text contains `label`, matched on the DOM.

    WHY NOT get_by_role, WHICH READS BETTER
    ---------------------------------------
    get_by_role searches the ACCESSIBILITY tree, and Angular Material takes that
    tree away while a dialog is open: opening a mat-dialog sets aria-hidden on
    the rest of the application so screen readers cannot wander out of it. Every
    button behind the dialog therefore vanishes from get_by_role - including
    ones that are perfectly visible and enabled.

    That is not hypothetical. A KYC run reported "form is INCOMPLETE" while the
    diagnostic, which walks the DOM, reported the very same Proceed button as
    "enabled, visible". The form was fine; the lookup was blind.

    Matching on text keeps working regardless, and handles the other quirk these
    buttons have - an icon ligature glued to the label, so the text reads
    "keyboard_backspace Proceed" rather than "Proceed".
    """
    # Whole words only. A plain substring would be actively dangerous here:
    # these buttons read "keyboard_backspace Proceed", so a search for "Back"
    # would match the icon name inside the PROCEED button and click forward
    # when asked to go back. Word boundaries make "back" miss "backspace".
    return page.locator("button").filter(
        has_text=re.compile(rf"\b{re.escape(label)}\b", re.IGNORECASE))


def live_button(page: Page, label: str, limit: int = 8):
    """The first button with this text that is actually visible and enabled."""
    found = buttons_labelled(page, label)
    for i in range(min(found.count(), limit)):
        button = found.nth(i)
        try:
            if button.is_visible(timeout=1500) and button.is_enabled(timeout=1500):
                return button
        except Exception:
            continue
    return None


def button_enabled(page: Page, label: str) -> bool:
    """Is there a clickable button with this text right now?"""
    try:
        return live_button(page, label) is not None
    except Exception:
        return False


def click_button(page: Page, label: str) -> None:
    """
    Click a button by its visible text, e.g. "Proceed", "Back".

    The portal renders duplicate mobile and desktop layouts, so the first match
    is not reliably the usable one - we click the first that is genuinely
    visible AND enabled, and say so plainly if there is none.
    """
    button = live_button(page, label)
    if button is None:
        # Fall back to the accessible-name lookup rather than giving up: if the
        # text has been restyled into something our match misses, the a11y name
        # may still carry it.
        fallback = page.get_by_role("button", name=label)
        if fallback.count():
            fallback.first.click(timeout=FIELD_TIMEOUT_MS)
            return
        # Before blaming the button: is there a screen at all? A hung app has
        # no buttons either, and calling that "no Proceed button" sends someone
        # hunting a selector bug instead of restarting a back end. This raises
        # PageStuckLoading, which the runners treat as environment (retried)
        # rather than as a test failure (not retried).
        raise_if_stuck(page, f"looking for the '{label}' button")
        raise LookupError(
            f"No visible, enabled button reading '{label}' on this screen.")
    button.scroll_into_view_if_needed(timeout=4000)
    button.click(timeout=FIELD_TIMEOUT_MS)


def field_exists(page: Page, form_control: str, timeout_ms: int = 3000) -> bool:
    """
    Has a cascading field appeared yet? Used to tell 'the form moved on' from
    'the form is still waiting', without turning a slow lookup into a failure.
    """
    try:
        page.locator(f'input[formcontrolname="{form_control}"]').wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def stuck_loading(page: Page) -> bool:
    """
    Is the app showing its bare "Loading" screen and nothing else?

    Worth asking explicitly, because this state is indistinguishable from a
    broken test if you only watch for a timeout. The app puts up a spinner and
    the word "Loading" and then stays there indefinitely - it never errors, never
    times out, and never renders - so a caller waiting for a field sees only
    "element not found after 60 seconds" and goes hunting for a selector bug.

    The tell is that a real screen always has form controls and the loading
    screen has none, so we ask about those rather than trying to interpret the
    text on a page that is deliberately almost empty.
    """
    try:
        if "Loading" not in page.inner_text("body"):
            return False
        return page.locator(
            "input[formcontrolname], mat-select[formcontrolname]").count() == 0
    except Exception:
        return False


def raise_if_stuck(page: Page, doing: str) -> None:
    """Turn a silent hang into the named diagnosis it actually is."""
    if not stuck_loading(page):
        return
    raise PageStuckLoading(
        f"The app is stuck on its 'Loading' screen while {doing}.\n"
        f"  Nothing rendered, and it will sit there indefinitely rather than "
        f"time out or show an error.\n"
        f"  Most likely: the login token expired (they last only a few "
        f"minutes), or the app's own back end is not answering.\n"
        f"  What to do: run the command again - it takes a fresh login "
        f"automatically. This is not a test failure."
    )


def close_overlays(page: Page, attempts: int = 4) -> None:
    """
    Make sure no dropdown panel is left covering the page.

    An open mat-select paints a full-screen transparent backdrop, and that
    backdrop absorbs every later click anywhere on the page. So a dropdown whose
    option never matched does not just fail itself - it disables everything that
    comes after it, and the symptom surfaces somewhere else entirely:

        <div class="cdk-overlay-backdrop ..."> intercepts pointer events

    That is what broke the KYC document step. The identity dropdown was left
    open, and the click on the ADDRESS dropdown timed out 15 seconds later,
    pointing the blame at a control that was working perfectly.

    Escape first, because that is what a person presses. Then a click on the
    backdrop itself, which works even when focus is not inside the panel -
    Escape alone was measurably not enough.
    """
    for attempt in range(attempts):
        try:
            backdrop = page.locator(".cdk-overlay-backdrop")
            if not backdrop.count():
                return
            if attempt == 0:
                page.keyboard.press("Escape")
            else:
                # force=True is right here rather than a smell: the backdrop IS
                # the topmost element, so Playwright's "something is covering
                # this" check is warning us about the very thing we are aiming at.
                backdrop.first.click(timeout=3000, force=True)
            page.wait_for_timeout(400)
        except Exception:
            return


def options_on_screen(page: Page) -> list[str]:
    """Read the option list of whichever dropdown is currently open."""
    try:
        return [t.strip() for t in
                page.locator("mat-option").all_inner_texts() if t.strip()]
    except Exception:
        return []


def pick_option(page: Page, choose: str) -> tuple[bool, list[str]]:
    """
    Click an option in the panel that is already open, tolerantly.

    Returns (picked, what_was_offered). Exact first, then a forgiving match,
    because option text is written by humans: "Aadhaar Card" turns up as
    "AADHAAR CARD" or "Aadhaar card (front)" and refusing all of them because
    none is byte-equal is pedantry, not correctness.

    On failure it closes the panel. Leaving it open is what turns one missed
    option into a page where nothing can be clicked at all.
    """
    for matcher in (page.get_by_role("option", name=choose, exact=True),
                    page.get_by_role("option", name=choose, exact=False),
                    page.locator("mat-option").filter(has_text=choose)):
        try:
            if matcher.count():
                matcher.first.click(timeout=8000)
                page.wait_for_timeout(400)
                close_overlays(page)
                return True, []
        except Exception:
            continue

    offered = options_on_screen(page)
    close_overlays(page)
    return False, offered
