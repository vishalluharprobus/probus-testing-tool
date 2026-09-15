"""
Drive one insurer all the way to the KYC / proposal stage.

    python run_proposal_test.py --insurer ZUNO
    python run_proposal_test.py --insurer ZUNO --submit-kyc     # calls the insurer

THIS GOES FURTHER THAN run_quote_test.py
----------------------------------------
run_quote_test.py stops at the quote list and creates nothing anywhere. This
script continues into the buy journey, so it needs the target's write ceiling
raised to "proposal" in config/settings.local.json.

By default it still stops short of submitting: it fills the KYC form and
reports whether the app accepts it, WITHOUT pressing Proceed. Add --submit-kyc
to actually submit, which calls the insurer's KYC service for real.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

from urllib.parse import urlparse

from config import insurers, settings
from config.insurers import profile_for
from data.customer import DEFAULT as CUSTOMER
from core import (auth, backend, browser, console, health, kycnotes,
                  safety, ui)
from pages.additional_details import AdditionalChoice, AdditionalDetailsPage
from pages.kyc import KycPage
from pages.policy_details import PolicyChoice, PolicyDetailsPage
from pages.proposal import ProposalPage
from pages.quote_list import QuoteListPage
from pages.vehicle_details import Vehicle, VehicleDetailsPage

HONDA_ACTIVA = Vehicle("GJ-01", "GJ-01 Ahmedabad", "HONDA", "ACTIVA",
                       "3G (110 CC) (PETROL)", "2022")


def main() -> int:
    console.use_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--insurer", default="ZUNO",
                    help="insurer code, or AUTO to drive whichever insurer "
                         "quoted and is least likely to redirect")
    ap.add_argument("--target", default=settings.DEFAULT_TARGET)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--slow", type=int, default=0)
    ap.add_argument("--addons", default="")
    ap.add_argument("--submit-kyc", action="store_true",
                    help="actually press Proceed on KYC - calls the insurer")
    ap.add_argument("--proposal", action="store_true",
                    help="after KYC, fill the proposal up to Preview "
                         "(implies --submit-kyc)")
    ap.add_argument("--retries", type=int, default=2,
                    help="how many times to retry when the ENVIRONMENT fails "
                         "(app stuck loading, master data empty). Real test "
                         "failures are never retried.")
    args = ap.parse_args()
    return run_with_retries(args)


def run_with_retries(args) -> int:
    """
    Retry environment failures, never test failures.

    This environment is genuinely unreliable - the app hangs on a bare
    "Loading" screen, master-data lookups come back empty, the login token
    expires within minutes - and roughly one run in two dies for a reason that
    has nothing to do with the code under test.

    Retrying that is not papering over a defect, because the two cases are kept
    strictly apart: exit code 6 means "the environment did not cooperate" and is
    worth another go, while a genuine failure (code 1) is returned immediately
    and untouched. Retrying a real failure would be how a suite starts lying;
    retrying a hung app is just what a human does.
    """
    for attempt_no in range(1, args.retries + 2):
        code = attempt(args)
        if code != 6 or attempt_no > args.retries:
            return code
        pause = 45
        print(f"\n  ENVIRONMENT PROBLEM on attempt {attempt_no} of "
              f"{args.retries + 1}. Waiting {pause}s for the app to recover, "
              f"then trying again.")
        print("  (Nothing was created by the failed attempt.)\n")
        time.sleep(pause)
    return code


def hidden_error(body: str) -> str:
    """
    Find an error the API reported that nobody will ever see.

    This platform answers with HTTP 200 and a clean envelope - Error null,
    Message null, StatusCode 200 - while the real verdict sits INSIDE the
    payload. A stalled proposal turned out to be carrying:

        Status       : "Error"
        ErrorMessage : "No district found matching your state, city & pincode
                        combination."

    The screen showed nothing at all. Someone hitting that by hand would click
    Continue, watch nothing happen, and have no way to find out why - and it is
    the same shape as the "N insurers unavailable" panel on the quote list,
    where per-insurer reasons hide behind a successful response.

    Surfacing it is most of the value of driving a browser rather than an API:
    we get the user's view AND the wire underneath it.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""          # not JSON - nothing to read, and that is fine

    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                low = key.lower()
                if low in ("errormessage", "error_message", "failurereason"):
                    if isinstance(value, str) and value.strip():
                        found.append(value.strip())
                elif low == "status" and isinstance(value, str):
                    if value.strip().lower() in ("error", "failed", "failure"):
                        found.append(f"Status={value.strip()}")
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item)

    walk(data)
    return "; ".join(dict.fromkeys(found))[:300]


def pick_best(quotes):
    """
    Choose which insurer to drive when --insurer AUTO is used.

    Naming one insurer is the single biggest reason a run produces nothing: the
    panel is different every time, and a run that demands IFFCOTOKIO simply
    stops when IFFCOTOKIO does not price, even though four others did and any of
    them would have exercised the same journey.

    Ordering, best first:

      1. insurers we have driven before WITHOUT ever being redirected - a known
         quantity, most likely to reach the proposal
      2. insurers we have never driven - worth learning about
      3. insurers that redirect to their own portal - last, because the harness
         deliberately stops at the handover, so they cannot finish the journey

    Cheapest first within a band, so a failure costs the least at the insurer.
    """
    ranked = []
    for quote in quotes:
        expected = profile_for(quote.insurer).kyc_style
        redirects = (expected == insurers.REDIRECT
                     or not kycnotes.never(quote.insurer, insurers.REDIRECT))
        if redirects:
            rank = 2
        elif kycnotes.summary(quote.insurer):
            rank = 0
        else:
            rank = 1
        ranked.append((rank, quote.premium or 0, quote))

    ranked.sort(key=lambda row: (row[0], row[1]))
    return ranked[0] if ranked else None


def attempt(args) -> int:
    cfg = settings.load(args.target)
    addons = [a.strip() for a in args.addons.split(",") if a.strip()]
    # Filling the proposal is pointless unless KYC actually ran, so asking
    # for one implies the other rather than failing later with an empty form.
    if args.proposal:
        args.submit_kyc = True

    print(f"\nTarget  : {cfg.name} ({cfg.base_url})")
    print(f"Insurer : {args.insurer}")
    print(f"Vehicle : {HONDA_ACTIVA.make} {HONDA_ACTIVA.model}, {HONDA_ACTIVA.rto}")
    print(f"KYC     : {'WILL BE SUBMITTED' if args.submit_kyc else 'filled but not submitted'}")
    print(f"Proposal: {'filled up to Preview' if args.proposal else 'not entered'}")
    print(f"Ceiling : {cfg.write_ceiling}\n")

    # Ask the API whether it is awake before spending 90 seconds finding out
    # the hard way. A wedged back end is the single most common reason a run
    # dies here, and it is not a test failure - so it gets the environment code
    # and a message aimed at the person who can actually fix it.
    api = backend.check(cfg.api_url)
    if not api:
        print("=" * 62)
        print("THE APP'S BACK END IS NOT ANSWERING - nothing was run")
        print("=" * 62)
        print(f"\n  {api.detail}\n")
        return 6

    with browser.browser_session(cfg, headed=not args.headless,
                                 slow_mo_ms=args.slow) as (context, run_dir):
        page = None
        watcher = health.Watcher.for_context(context)

        # Every API call the app makes, in order. The health watcher records
        # only failures, which was not enough: when the wizard refused to move,
        # nothing had failed, and the open question was whether the button
        # reached the network at all.
        api_calls: list[str] = []
        context.on("request", lambda r: api_calls.append(
            f"{r.method} {r.url.split('?')[0]}") if "/api/" in r.url.lower() else None)

        # ...and what the app got BACK from the ones that change something.
        # A stalled wizard turned out to be a POST answering "no" rather than a
        # dead button, and "no" is only readable in the response body. Limited
        # to POSTs so this stays a short, relevant list rather than every
        # master-data lookup on the journey.
        api_answers: list[str] = []
        # How many hidden errors belong to the QUOTE fan-out rather than to
        # this insurer's own journey. Set once the buy begins.
        quote_noise = 0
        # Errors the app received and said nothing about. See hidden_error().
        hidden_errors: list[str] = []

        def remember_answer(response) -> None:
            try:
                if "/api/" not in response.url.lower():
                    return
                if response.request.method != "POST":
                    return
                body = response.text() or ""
                api_answers.append(
                    f"[{response.status}] {response.url.split('?')[0]} -> "
                    f"{body[:500]}")
                # Keep the WHOLE body on disk. The answer that explained a
                # stalled wizard was a zeroed quotation object, and the part
                # that mattered - whether any error was buried further down -
                # sat well past any sensible console limit.
                # The REQUEST too, not only the answer. When an API rejects a
                # combination of fields, the fields it was given are half the
                # evidence - "no district found for your state, city & pincode"
                # is unanswerable without knowing which three values were sent.
                sent = ""
                try:
                    sent = response.request.post_data or ""
                except Exception:
                    pass

                with (run_dir / "api-responses.log").open(
                        "a", encoding="utf-8") as log:
                    log.write(f"\n=== [{response.status}] {response.url}\n")
                    if sent:
                        log.write(f"--- sent:\n{sent}\n")
                    log.write(f"--- received:\n{body}\n")

                buried = hidden_error(body)
                if buried:
                    hidden_errors.append(
                        f"{response.url.split('?')[0].rsplit('/', 1)[-1]}: {buried}")
            except Exception:
                pass          # a body we cannot read is not worth failing a run

        context.on("response", remember_answer)
        try:
            page = auth.log_in(context, cfg)
            safety.verify_environment(page, cfg)
            print("  logged in, staging confirmed")

            # --- the safe part, identical to run_quote_test -------------------
            safety.allow("quote", cfg)
            VehicleDetailsPage(page).open(cfg.base_url).fill(HONDA_ACTIVA).proceed()
            PolicyDetailsPage(page).wait_until_loaded().fill(
                PolicyChoice()).proceed()
            AdditionalDetailsPage(page).wait_until_loaded().fill(
                AdditionalChoice()).proceed()
            print("  screens 1-3 done, waiting for quotes ...")

            quote_page = QuoteListPage(page)

            if addons:
                # Add-ons re-price every card, so we do need the full list first.
                quote_page.wait_until_loaded()
                quote_page.select_add_ons(addons)
                print(f"  add-ons applied: {', '.join(addons)}")
                target = next((q for q in quote_page.quotes()
                               if args.insurer.upper() in q.insurer.upper()), None)
            elif args.insurer.upper() == "AUTO":
                # Wait for the whole panel, then choose. This costs the full
                # fan-out, which is the point: we want the choice, not the
                # first answer.
                quote_page.wait_until_loaded()
                best = pick_best(quote_page.quotes())
                target = best[2] if best else None
                if target is not None:
                    why = {0: "driven before, never redirected",
                           1: "not tried before",
                           2: "redirects - nothing better on offer"}[best[0]]
                    print(f"  auto-picked {target.insurer} ({why})")
            else:
                # Otherwise stop as soon as OUR insurer prices - no point waiting
                # out the rest of the fan-out for cards we will ignore.
                target = quote_page.wait_for_insurer(args.insurer)

            found = quote_page.quotes()
            print(f"  {len(found)} quotes: "
                  f"{', '.join(f'{q.insurer} Rs{q.premium}' for q in found)}")

            if target is None:
                # NO insurer quoting is a different fact from OUR insurer not
                # quoting. One insurer declining is a real result about that
                # insurer; the whole panel coming back empty means the fan-out
                # never happened - a busy back end or an expired token - and it
                # deserves the environment code so the retry loop covers it
                # instead of reporting "IFFCOTOKIO returned no quote" when the
                # truth is that nobody did.
                if not found:
                    print("\n  No insurer returned a quote at all - not one. That "
                          "is the environment, not this insurer:\n  the quote "
                          "fan-out did not come back. Nothing was created.")
                    return 6
                print(f"\n  {args.insurer} returned no quote this run, so there is "
                      f"nothing to buy. Nothing was created.")
                print(f"  Others did quote: "
                      f"{', '.join(q.insurer for q in found)}")
                print(f"  Try: python find_insurer.py --insurer {args.insurer}")
                return 5

            # --- past here we are changing state ------------------------------
            safety.allow("proposal", cfg)

            print(f"\n  buying {target.insurer} at Rs {target.premium:,} ...")
            quote_page.buy(target.insurer)
            print("  confirmed - entering the buy journey")
            # Everything recorded from here on belongs to this journey.
            quote_noise = len(hidden_errors)

            # KYC is not one flow - the insurer decides its shape at runtime,
            # so we watch where the browser actually goes rather than assuming.
            kyc = KycPage(page)
            our_host = urlparse(cfg.base_url).hostname or ""
            style, detail = kyc.detect_style(our_host)
            expected = profile_for(target.insurer).kyc_style

            print(f"\n  KYC style detected : {style}")
            # Write down what it actually did. config/insurers.py records what
            # we EXPECT; this records what we have SEEN, with a count, so
            # "which insurers redirect?" is answered by evidence that accrues
            # run by run instead of by somebody's memory.
            # Only record what is ALREADY certain here. A redirect is visible
            # immediately and is worth writing down even if the run dies next.
            # "inline" is NOT certain yet - the upload step appears only after
            # the form is submitted, so recording it now counted every
            # documents-style insurer as inline and produced a table that said
            # ZUNO was inline seventeen times over. The real shape is recorded
            # once KYC has actually resolved, below.
            if style in (insurers.REDIRECT, insurers.SKIPPED):
                kycnotes.record(target.insurer, style,
                                detail if style == insurers.REDIRECT else "")
            history = kycnotes.summary(target.insurer)
            if history:
                print(f"  seen before         : {history}")
            # inline-vs-documents cannot be told apart before the form is
            # submitted, so a difference between those two is normal and not
            # worth warning about. A redirect difference genuinely matters.
            confusable = {insurers.INLINE, insurers.DOCUMENTS}
            mismatch = (expected != insurers.UNKNOWN and expected != style
                        and not ({expected, style} <= confusable))
            if mismatch:
                print(f"  NOTE: expected '{expected}' for this insurer - "
                      f"config/insurers.py may need updating")
            elif expected == insurers.UNKNOWN:
                print(f"  (this insurer was not profiled - add kyc_style='{style}' "
                      f"to config/insurers.py)")

            if style == insurers.REDIRECT:
                print(f"\n{'=' * 62}")
                print("KYC HAPPENS ON THE INSURER'S OWN PORTAL")
                print("=" * 62)
                print(f"\n  The browser was sent to: {detail}")
                print("\n  This insurer does not do KYC on our portal - the customer")
                print("  completes it on the insurer's website and is sent back.")
                print("  That is somebody else's UI, unmapped and liable to change,")
                print("  so the harness stops here rather than pretending to drive it.")
                print("\n  What this run DID prove: quote -> Buy Now -> Confirm works,")
                print("  and the handover to the insurer fires correctly.")
                print(f"\n  Add this to config/insurers.py:")
                print(f"      kyc_hosts=(\"{detail}\",)")
                return 0

            if style == insurers.SKIPPED:
                print("  no KYC asked for - went straight to the proposal")
                print(f"  now on: {page.url}")
                return 0

            if style == insurers.UNKNOWN:
                print(f"  could not tell what KYC shape this is. Now on: {detail}")
                return 1

            kyc.wait_until_loaded()
            print("  KYC form reached")

            kyc.fill_details(CUSTOMER)
            ready = kyc.can_proceed()
            print(f"  KYC details filled - form is "
                  f"{'complete' if ready else 'INCOMPLETE'}")
            if not ready:
                # Say WHICH field, not just "incomplete" - the app already knows.
                reported = kyc.missing_required()
                for field in reported:
                    print(f"      still needed: {field}")
                # If no visible field looks wrong, the blocker is something
                # missing_required cannot see - a hidden tickbox, an unchosen
                # radio group, or simply a request still in flight. Printing
                # nothing at all here is what made the last run unexplainable.
                blocked = kyc.why_blocked()
                if blocked:
                    print("      why the Proceed button is dead:")
                    for line in blocked:
                        print(f"        - {line}")
                elif not reported:
                    print("      nothing on the form looks wrong - see the "
                          "screenshot; this may be a timing problem")

            if not args.submit_kyc:
                print("\n  STOPPING HERE. Filled but not submitted.")
                print("  Re-run with --submit-kyc to call the insurer for real.")
                return 0 if ready else 1
            if not ready:
                print("\n  Refusing to submit an incomplete form.")
                # A picture of the blocked form is worth more than the field
                # list when the field list is the thing that came up empty.
                print(f"  Screenshot: "
                      f"{browser.capture_failure(page, run_dir, 'kyc-blocked')}")
                return 1

            kyc.proceed()
            # The KYC call can answer in two ways - the upload step appears, or
            # we go straight to the proposal. Waiting for EITHER is faster than
            # sleeping long enough to cover both.
            kyc.wait_for_kyc_response()
            print(f"  KYC submitted, now on: {page.url}")

            # Clear the response dialog BEFORE asking what the screen shows.
            # The order matters: NATIONAL answers with a "KYC verification
            # success." popup that covers the whole step, so asking first
            # answered "no documents wanted" while the upload step was sitting
            # underneath it, and the run then waited for a proposal page that
            # was never going to arrive.
            message = kyc.settle()
            if message:
                print(f"  KYC result: {message}")

            # Not every insurer asks for documents - ZUNO does, others verify
            # from the PAN alone and go straight on to the proposal.
            if kyc.upload_step_showing():
                print("  document upload requested - attaching files")
                kyc.upload_documents(CUSTOMER, cfg.test_documents_dir)
                result = kyc.finish()
                print(f"  uploaded: {', '.join(kyc.uploaded) or 'nothing'}")
                for note in kyc.notes:
                    print(f"  NOTE: {note}")
                print(f"  KYC result: {result or message or '(no message shown)'}")
            elif not message:
                print("  no document upload for this insurer - verified from PAN")

            # NOW we know what this insurer really does, because it has done it:
            # handed us to another site, asked for documents, or verified from
            # the PAN alone. This is the answer to "which insurers redirect?",
            # and it is evidence rather than expectation.
            observed = (insurers.REDIRECT if kyc.redirected_to
                        else insurers.DOCUMENTS if kyc.uploaded
                        else insurers.INLINE)
            kycnotes.record(target.insurer, observed, kyc.redirected_to)
            print(f"  KYC shape confirmed: {observed}")

            print(f"  now on: {page.url}")
            safety.assert_never_pays(page)

            # Some insurers look inline and only redirect AFTER the form is
            # submitted, so this is checked here as well as up front.
            if kyc.redirected_to:
                kycnotes.record(target.insurer, insurers.REDIRECT,
                                kyc.redirected_to)
                print(f"\n{'=' * 62}")
                print("KYC CONTINUES ON AN EXTERNAL PORTAL")
                print("=" * 62)
                print(f"\n  After submitting, the browser was sent to:")
                print(f"    {kyc.redirected_to}")
                print(f"\n  So {target.insurer} does NOT finish KYC on our portal - it")
                print("  hands the customer to an external site and expects them back.")
                print("  That site is not ours, is unmapped, and usually has its own")
                print("  OTP, so the harness stops rather than pretending to drive it.")
                print(f"\n  Proven by this run: quote -> Buy Now -> Confirm -> KYC form")
                print(f"  -> submission -> handover all work for {target.insurer}.")
                print(f"\n  Record it in config/insurers.py:")
                print(f'      "{target.insurer}": InsurerProfile(')
                print(f'          code="{target.insurer}", kyc_style=REDIRECT,')
                print(f'          kyc_hosts=("{kyc.redirected_to}",)),')
                return 0

            if not args.proposal:
                print("\n  KYC done. Re-run with --proposal to continue into the "
                      "proposal form.")
                return 0

            # ---- the proposal itself -----------------------------------------
            proposal = ProposalPage(page).wait_until_loaded()
            print(f"\n  proposal reached - {proposal.current_step()}")

            # What KYC already gave us. This is the evidence that KYC worked, so
            # it is printed rather than merely relied upon.
            filled = {k: v for k, v in proposal.prefilled().items() if v}
            print("  prefilled by KYC: " +
                  (", ".join(f"{k}={v}" for k, v in filled.items()) or "nothing"))

            address = proposal.wait_for_address_lookup()
            if address:
                print("  app derived from pincode: " +
                      ", ".join(f"{k}={v}" for k, v in address.items()))
            proposal.fill_owner_details(CUSTOMER)
            for item in proposal.filled:
                print(f"      filled {item}")
            missing = proposal.missing_required()
            if missing:
                print(f"  owner details incomplete: {', '.join(missing)}")
                return 1
            print("  owner details complete")

            proposal.continue_to_vehicle()
            print(f"  -> {proposal.current_step()} "
                  f"(clicked {proposal.advanced_by!r})")

            proposal.fill_vehicle_details(HONDA_ACTIVA.rto)
            for item in proposal.filled:
                print(f"      filled {item}")
            missing = proposal.missing_required()
            if missing:
                print(f"  vehicle details incomplete: {', '.join(missing)}")
                return 1

            proposal.continue_to_terms()
            print(f"  -> {proposal.current_step()} "
                  f"(clicked {proposal.advanced_by!r})")

            proposal.fill_terms(CUSTOMER)
            for item in proposal.filled:
                print(f"      filled {item}")
            missing = proposal.missing_required()
            if missing:
                print(f"  terms incomplete: {', '.join(missing)}")
                print(f"  buttons on screen: "
                      f"{', '.join(proposal.buttons_on_screen()) or '(none)'}")
                return 1
            print("  nominee and previous-policy details complete")

            proposal.continue_to_preview()
            print(f"  -> {proposal.current_step()} "
                  f"(clicked {proposal.advanced_by!r})")

            # Preview is the end of the line. Payment is the next screen and
            # nothing here clicks toward it.
            safety.assert_never_pays(page)
            print(f"\n  JOURNEY COMPLETE - stopped at Preview, before payment.")
            print(f"  now on: {page.url}")
            return 0

        except safety.SafetyRefusal as exc:
            print(f"\nREFUSED (guard rail working as designed):\n  {exc}\n")
            return 3
        except ui.LookupTimedOut as exc:
            print("\n" + "=" * 62)
            print("MASTER-DATA LOOKUP FAILED - usually a busy environment")
            print("=" * 62)
            print(f"\n  {exc}\n")
            return 6
        except ui.PageStuckLoading as exc:
            print(f"\nAPP DID NOT LOAD - not a test failure\n\n  {exc}\n")
            return 6
        except auth.LoginFailed as exc:
            print(f"\nLOGIN FAILED (setup problem):\n  {exc}\n")
            return 4
        except LookupError as exc:
            print(f"\n{exc}\n")
            # Print this FIRST. When the app was told why and showed the user
            # nothing, that message IS the answer; everything else printed by
            # this handler is only context for it.
            # Separate THIS insurer's failure from the quote fan-out's noise.
            # The quote step asks every insurer at once and collects other
            # companies' errors as a matter of course; letting those head the
            # report buried the one error that actually stopped this journey.
            mine = list(dict.fromkeys(hidden_errors[quote_noise:]))
            others = list(dict.fromkeys(hidden_errors[:quote_noise]))
            if mine:
                print("  " + "=" * 58)
                print("  THE APP WAS TOLD WHY, AND SAID NOTHING")
                print("  " + "=" * 58)
                for line in mine:
                    print(f"    {line}")
                print("  HTTP 200 with a clean envelope - the failure is inside")
                print("  the payload, which is why the screen showed nothing.\n")
            if others:
                print(f"  ({len(others)} other insurer error(s) arrived during the "
                      f"quote fan-out - normal, and not why this stopped.)\n")
            # A form that is valid, with a live button, that still will not move
            # is not a UI problem - it is a call failing behind the scenes. The
            # browser already knows; print what it saw rather than leaving
            # someone to reproduce the whole journey by hand to find out.
            diag = watcher.diagnose(page) if watcher else None
            if diag and (diag.console_errors or diag.failed_requests):
                # The MOST RECENT entries, not the first. The earliest lines
                # are always the same page-load noise - analytics blocked by
                # SSL, missing marketing images - while the message that
                # explains a stall arrives last, and printing the head of the
                # list buried it completely.
                print("  What the browser reported most recently:")
                for line in diag.failed_requests[-6:]:
                    print(f"    ! {line[:150]}")
                for line in diag.console_errors[-6:]:
                    print(f"    . {line[:150]}")

            # Did the button fire anything at all? Nothing FAILED when the
            # wizard refused to move, so the useful question is not "what
            # broke" but "did the click reach the network". Only a log of
            # successful calls can answer that, and it separates a dead
            # handler from a call that came back unhelpfully.
            print(f"\n  Last API calls ({len(api_calls)} in this run):")
            for line in api_calls[-8:]:
                print(f"    > {line[:130]}")
            if not api_calls:
                print("    (none at all)")

            if api_answers:
                print("\n  What those calls answered:")
                for line in api_answers[-4:]:
                    print(f"    <- {line[:400]}")

            if page:
                print(f"\n  Screenshot: "
                      f"{browser.capture_failure(page, run_dir, 'stalled')}")
            return 5
        except Exception as exc:
            HARNESS_BUGS = (NameError, AttributeError, TypeError,
                            ImportError, KeyError, IndexError)
            diag = watcher.diagnose(page) if watcher else None
            if diag and diag.is_environment_problem and not isinstance(exc, HARNESS_BUGS):
                print(f"\nENVIRONMENT PROBLEM - not a test failure\n\n  {diag.explanation}\n")
                return 6

            print(f"\nERROR: {type(exc).__name__}: {exc}\n")
            traceback.print_exc()
            if page:
                print(f"\nScreenshot: {browser.capture_failure(page, run_dir, 'error')}")
                print(f"Trace     : {run_dir / 'trace.zip'}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
