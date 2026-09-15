"""
Document files for the KYC upload step.

PRIVACY DECISION, ON PURPOSE
----------------------------
The team's test documents are real scans - a real Aadhaar number, a real PAN.
They are therefore NEVER copied into this repository and never referenced by a
committed path. The folder lives wherever the team keeps it, and its location is
read from config/settings.local.json, which is git-ignored.

That means a fresh clone of this repo contains no identity documents at all, and
someone without access to that folder simply gets the generated placeholders
instead of a confusing missing-file error.

Two sources, in order of preference:
  1. the team's real test documents  - needed for KYC to actually pass
  2. generated blank images          - valid files, but no OCR will read them
"""
from __future__ import annotations

import struct
import zlib
import re

from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / ".fixtures"


class DocumentNotFound(RuntimeError):
    """A named test document could not be located - a setup problem, not a test failure."""


def resolve(filename: str, folder: str | None, fallback: str = "identity") -> Path:
    """
    Find a real test document, or fall back to a generated placeholder.

    filename : the file to look for, e.g. "Dilip_aadhar_front.png"
    folder   : where the team keeps them (from settings.local.json); may be None
    fallback : which placeholder to generate if the real file is unavailable
    """
    if folder and filename:
        candidate = Path(folder).expanduser() / filename
        if candidate.is_file():
            return candidate
        raise DocumentNotFound(
            f"Could not find {filename!r} in {folder!r}.\n"
            f"  Check 'test_documents_dir' in config/settings.local.json, or that "
            f"the file has not been renamed."
        )
    return placeholder(fallback)


# --------------------------------------------------------------------------- #
# fallback: a generated blank image
# --------------------------------------------------------------------------- #

def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Build a solid-colour PNG by hand - no image library needed."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def placeholder(name: str = "identity") -> Path:
    """
    A blank image, created on first use.

    Accepted by the browser as a file, but it carries no readable document, so
    an insurer that actually inspects the upload will reject it. Useful for
    proving the upload MECHANISM works; not for proving KYC passes.
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"TEST-NOT-A-REAL-DOCUMENT-{name}.png"
    if not path.exists():
        colour = {"identity": (200, 220, 255)}.get(name, (255, 225, 200))
        path.write_bytes(_png(600, 380, colour))
    return path


# Kept so older call sites keep working.
test_document = placeholder


def mask(number: str) -> str:
    """
    Show a document number safely in a log: 'XXXX XXXX 1168'.

    Run reports get read, pasted into tickets and kept in CI output. A PAN or
    Aadhaar number printed in full there outlives the run and the folder it came
    from, so the log shows only enough to confirm the right value was used.
    """
    digits = "".join(ch for ch in number if ch.isalnum())
    if len(digits) <= 4:
        return "****"
    return "X" * (len(digits) - 4) + digits[-4:]


def aadhaar_number_from(folder: str) -> str:
    """
    Read the Aadhaar number out of the test folder's own file names.

    The team's scans are named `Dilip_aadhar_front_back - 304318171168.pdf`, so
    the number is already sitting in the folder next to the document it belongs
    to. Taking it from there rather than from a constant keeps a real government
    ID out of this repository entirely - the folder is outside it, and the path
    comes from git-ignored settings.

    Returns "" when nothing matches, which callers report as a named gap rather
    than guessing. A wrong Aadhaar number fails KYC in a way that looks like a
    broken test, so a blank is much safer than an invention.
    """
    if not folder:
        return ""
    try:
        directory = Path(folder)
        if not directory.is_dir():
            return ""
        for entry in sorted(directory.iterdir()):
            if "aadh" not in entry.name.lower():
                continue
            # 12 digits, not part of a longer run of digits.
            match = re.search(r"(?<!\d)(\d{12})(?!\d)", entry.name)
            if match:
                return match.group(1)
    except Exception:
        return ""
    return ""


def _photo_in(directory: Path) -> Path | None:
    """First image in this folder that looks like a person's photograph."""
    try:
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if "photo" not in entry.name.lower():
                continue
            if entry.suffix.lower() in (".jpg", ".jpeg", ".png"):
                return entry
    except Exception:
        return None
    return None


def find_photo(folder: str) -> Path | None:
    """
    A customer photograph for the KYC upload step.

    Looks in this customer's own folder first, then in the sibling folders of
    the same test-documents root. The team keeps one folder per test customer
    and only some of them have a photograph, so a run for a customer without one
    would otherwise stall on a slot the insurer insists on.

    Borrowing another test customer's photograph means the face will not match
    the PAN being verified. That is a deliberate, stated trade-off for this test
    environment, not an oversight - the caller reports which folder it came from
    so the substitution is visible in the run log rather than silent.

    Returns None when there is no photograph anywhere, and the caller falls back
    to an obviously-synthetic placeholder.
    """
    if not folder:
        return None
    try:
        directory = Path(folder)
        if not directory.is_dir():
            return None

        own = _photo_in(directory)
        if own:
            return own

        parent = directory.parent
        if parent.is_dir():
            for sibling in sorted(parent.iterdir()):
                if sibling.is_dir() and sibling != directory:
                    borrowed = _photo_in(sibling)
                    if borrowed:
                        return borrowed
    except Exception:
        return None
    return None
