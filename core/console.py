"""
Make the Windows console able to print rupee amounts.

Windows defaults to a legacy codepage, so printing a premium like Rs.1,247
written with the rupee sign crashes with UnicodeEncodeError. Every premium this
tool reads contains that character, so this is not an edge case - it is the
normal path. Call use_utf8() once at the start of any script that prints.
"""
from __future__ import annotations

import sys


def use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # Older Python, or a stream that is not reconfigurable (a pipe in
            # some CI setups). errors="replace" on the fallback keeps output
            # readable instead of raising.
            pass
