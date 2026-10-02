"""Console output that can never take a device down.

Prints use "→ ≤ ⚠ Δ ─", which cp1252 (Python's fallback for a pipe or
non-UTF-8 terminal) can't encode. The raise lands at the print, inside an
acquisition loop, so `PullWorker.run()` reports it as a device failure.

Called by every entry point, not on import, so importing acqApp for the
preset maths doesn't rewrite someone's stdout. `tests/test_console_safety.py`
enforces the call.
"""
from __future__ import annotations

import sys


def enable_safe_console() -> None:
    """UTF-8 stdout/stderr with errors="replace". Idempotent; streams that
    can't be reconfigured (pythonw, wrapped test/notebook streams) are left
    alone."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass                        # detached, or refuses to be changed


def short_error(e: BaseException) -> str:
    """A driver error as one line: its first sentence, plus the code NI-DAQmx
    gives (its message runs to five lines: device, task name, status)."""
    first = next((ln.strip() for ln in str(e).splitlines() if ln.strip()),
                 type(e).__name__).rstrip(".")
    code = getattr(e, "error_code", None)
    return f"{first} (NI {code})" if isinstance(code, int) else first
