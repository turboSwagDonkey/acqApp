"""
Console output that can never take a device down.

The diagnostic prints throughout this app use characters — "→", "≤", "⚠", "Δ",
"─" — that a legacy Windows console code page cannot encode. Python falls back
to that code page (cp1252 here) whenever stdout is a pipe or a non-UTF-8
terminal, and printing one of those characters there raises UnicodeEncodeError
*at the print statement*.

That is not a cosmetic problem. Those prints sit inside the acquisition loops,
so the exception escapes into `PullWorker.run()`, which reports it as a device
failure: the camera simply does not start, and the status bar blames the
hardware. The voltage cam's own "shorten exposure to ≤N µs" notice trips this on
the DEFAULT configuration (10 ms exposure at full frame), so it is not a corner
case — it just depends on which terminal the app was launched from.

Every entry point calls `enable_safe_console()` before its first print (see the
guard test that enforces this). Doing it explicitly rather than on package
import keeps `import acqApp.…` free of global side effects, so analysis code
that only wants the tracking or preset maths does not have its stdout quietly
rewritten underneath it.
"""
from __future__ import annotations

import sys


def enable_safe_console() -> None:
    """Make stdout/stderr able to encode anything, on any terminal.

    UTF-8, so the symbols come out right on a modern terminal and in a
    redirected log; `errors="replace"` so a stream that still cannot represent
    them degrades to "?" instead of raising. Safe to call more than once.

    Streams that cannot be reconfigured are left alone: under `pythonw` there is
    no stdout at all, and some captured/wrapped streams (notebooks, test
    harnesses) do not implement it. Those are not the case that breaks devices.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue                    # pythonw, or a wrapped/captured stream
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass                        # detached, or refuses to be changed
