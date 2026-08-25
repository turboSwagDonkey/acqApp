"""Console output that can never take a device down.

Diagnostic prints use "→ ≤ ⚠ Δ ─", which cp1252 cannot encode — and Python
falls back to it whenever stdout is a pipe or a non-UTF-8 terminal. The raise
lands *at the print*, inside an acquisition loop, so `PullWorker.run()` reports
it as a device failure and the status bar blames the hardware. The voltage
cam's "shorten exposure to ≤N µs" notice trips it on the default config.

Called by every entry point rather than on import, so `import acqApp.…` for the
preset maths does not silently rewrite someone's stdout.
`tests/test_console_safety.py` enforces the call.
"""
from __future__ import annotations

import sys


def enable_safe_console() -> None:
    """UTF-8 stdout/stderr with errors="replace". Idempotent.

    Streams that cannot be reconfigured (pythonw has no stdout; notebooks and
    test harnesses wrap theirs) are left alone — not the case that breaks
    devices.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue                    # pythonw, or a wrapped/captured stream
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass                        # detached, or refuses to be changed
