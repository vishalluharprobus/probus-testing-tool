"""
Remember what each insurer's KYC actually did.

config/insurers.py holds EXPECTATIONS - what we believe an insurer does. This
file holds OBSERVATIONS - what it did on a real run, with a date. The two are
kept apart on purpose: an expectation that stops matching reality is a finding,
and merging the two would quietly erase it.

It answers the question that keeps coming up in this project - "which insurers
are inline and which redirect?" - with evidence rather than memory, and it fills
itself in as runs happen, so nobody has to maintain a list by hand.

One insurer can legitimately show more than one shape. NATIONAL has been seen
going straight to the proposal on one run and handing off to an external portal
on the next, which usually means the CKYC lookup found the customer one time and
not the other. So every observation is appended and counted, never overwritten:
"redirect x3, inline x1" is the honest answer, and it is a more useful one than
either value alone.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

NOTES_FILE = Path(__file__).resolve().parent.parent / "reports" / "kyc_styles.json"


def _load() -> dict:
    try:
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record(insurer: str, style: str, host: str = "") -> None:
    """Add one observation. Never raises - a notes file is not worth a run."""
    try:
        data = _load()
        key = insurer.strip().upper()
        entry = data.setdefault(key, {"seen": {}, "hosts": [], "last_seen": ""})
        entry["seen"][style] = entry["seen"].get(style, 0) + 1
        entry["last_seen"] = f"{date.today().isoformat()} {style}"
        if host and host not in entry["hosts"]:
            entry["hosts"].append(host)

        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True),
                              encoding="utf-8")
    except Exception:
        pass


def summary(insurer: str) -> str:
    """One line of history for a run report, or '' if we have never seen it."""
    entry = _load().get(insurer.strip().upper())
    if not entry:
        return ""
    seen = entry.get("seen", {})
    counts = ", ".join(f"{style} x{n}"
                       for style, n in sorted(seen.items(), key=lambda kv: -kv[1]))
    hosts = ", ".join(entry.get("hosts", []))
    return counts + (f" (via {hosts})" if hosts else "")


def is_reliably(insurer: str, style: str) -> bool:
    """
    Has this insurer ONLY ever shown this shape?

    Used to order candidates. "Only ever inline" is worth trying first; "inline
    once, redirect three times" is not, and a single lucky run should not be
    allowed to look like a rule.
    """
    seen = _load().get(insurer.strip().upper(), {}).get("seen", {})
    return bool(seen) and set(seen) == {style}


def never(insurer: str, style: str) -> bool:
    """Has this insurer never shown this shape? (Unknown counts as never.)"""
    return style not in _load().get(insurer.strip().upper(), {}).get("seen", {})


# ------------------------------------------------------------ redirect trips
#
# A redirect insurer has more worth remembering than "it redirects": WHERE the
# link goes, whether it opens in the same tab or a new one, the page it sends
# the customer back to, and how each round-trip test ended. That is the
# difference between "RELIANCE redirects" and something a developer can act on.

def record_round_trip(insurer: str, mode: str, passed: bool, headline: str,
                      facts: dict) -> None:
    """Remember one redirect test. Never raises."""
    try:
        data = _load()
        key = insurer.strip().upper()
        entry = data.setdefault(key, {"seen": {}, "hosts": [], "last_seen": ""})
        trip = entry.setdefault("redirect", {})
        # Facts only overwrite with something. A run that died before the
        # return must not erase the return path an earlier run learned.
        for name, value in facts.items():
            if value:
                trip[name] = value
        tests = trip.setdefault("tests", {})
        tally = tests.setdefault(mode, {"pass": 0, "fail": 0, "last": ""})
        tally["pass" if passed else "fail"] += 1
        tally["last"] = f"{date.today().isoformat()} {headline[:140]}"

        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True),
                              encoding="utf-8")
    except Exception:
        pass


def round_trip(insurer: str) -> dict:
    """What we know about this insurer's redirect, or {}."""
    return _load().get(insurer.strip().upper(), {}).get("redirect", {})


def has_redirected(insurer: str) -> bool:
    return not never(insurer, "redirect") or bool(round_trip(insurer))


def redirect_share(insurer: str) -> float | None:
    """
    How often this insurer redirected when we drove it, or None if never driven.

    "Has redirected" alone is not enough to choose by: NATIONAL redirected 3
    times in 21, so picking it for a redirect test mostly produced inline runs
    that tested nothing.
    """
    seen = _load().get(insurer.strip().upper(), {}).get("seen", {})
    total = sum(seen.values())
    return seen.get("redirect", 0) / total if total else None


def report() -> str:
    """Every insurer we have seen, as a table a person can read."""
    data = _load()
    if not data:
        return "No KYC observations yet - run run_proposal_test.py first."
    rows = [f"{'INSURER':<14}{'SEEN':<28}{'REDIRECT GOES TO':<30}{'OPENS IN':<10}"
            f"RETURN TESTS"]
    for insurer in sorted(data):
        entry = data[insurer]
        seen = ", ".join(f"{s} x{n}" for s, n in
                         sorted(entry.get("seen", {}).items(), key=lambda kv: -kv[1]))
        trip = entry.get("redirect", {})
        goes = trip.get("link_host") or ", ".join(entry.get("hosts", [])) or "-"
        tests = "; ".join(f"{mode}: {t['pass']} pass / {t['fail']} fail"
                          for mode, t in sorted(trip.get("tests", {}).items())) or "-"
        rows.append(f"{insurer:<14}{seen[:27]:<28}{goes[:29]:<30}"
                    f"{trip.get('opens_in', '-'):<10}{tests}")
        for mode, t in sorted(trip.get("tests", {}).items()):
            rows.append(f"{'':<14}last {mode}: {t.get('last', '')}")
    return "\n".join(rows)


if __name__ == "__main__":
    # python -m core.kycnotes   ->  "which insurers redirect, and where?"
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print(report())
