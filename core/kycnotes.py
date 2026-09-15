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
