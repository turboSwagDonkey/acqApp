"""
`presets.readout_fps` — the datasheet readout ceiling for an ROI.

This is the number behind every pre-Start frame-rate label and the initial ring
buffer sizing, and it is a table lookup with log-log interpolation between rows
plus two clamps and a binning divisor — four chances to be quietly wrong on a
preset nobody checks by hand. #11 was exactly this going unnoticed: the panel
dropped `link` on the way out, the estimate reverted to the other column, and a
USB3 rig was told it could do 115 fps instead of 15.7.

No hardware and no Qt: the real rate comes from the camera at run time
(`get_frame_timings`), so what is checked here is that the *estimate* obeys the
table it claims to come from.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_readout_fps.py
"""
from __future__ import annotations

import math
import sys

from _harness import Report      # noqa: F401  (also puts acqApp on sys.path)

from acqApp.voltage_cam import presets as P        # noqa: E402


def main() -> int:
    r = Report("readout-fps")
    rows_usb = P._ROWS_FPS
    r.note(f"table: {len(rows_usb)} rows, {rows_usb[0][0]}–{rows_usb[-1][0]} "
           f"rows, default link {P.LINK_LABEL[P.DEFAULT_LINK]}")

    # ── every table row comes back exactly ───────────────────────────────────
    exact = True
    for rws, f_usb, f_cxp in P._ROWS_FPS_BOTH:
        exact &= (abs(P.readout_fps(rws, link=P.USB) - f_usb) < 1e-9
                  and abs(P.readout_fps(rws, link=P.CXP) - f_cxp) < 1e-9)
    r.check(exact, f"every one of the {len(P._ROWS_FPS_BOTH)} table rows is "
                   f"returned exactly, on both links")

    # ── the two links are genuinely different columns ────────────────────────
    r.check(abs(P.readout_fps(2368, link=P.USB) - 15.7) < 1e-9,
            f"full frame on USB3 is 15.7 fps (got "
            f"{P.readout_fps(2368, link=P.USB):.1f})")
    r.check(abs(P.readout_fps(2368, link=P.CXP) - 115.0) < 1e-9,
            f"full frame on CoaXPress is 115 fps (got "
            f"{P.readout_fps(2368, link=P.CXP):.1f})")
    ratio = P.readout_fps(2368, link=P.CXP) / P.readout_fps(2368, link=P.USB)
    r.check(ratio > 5.0,
            f"the link choice is worth {ratio:.1f}x — losing it (#11) is not a "
            f"rounding error")
    r.check(P.readout_fps(1500, link="nonsense") == P.readout_fps(1500, link=P.USB),
            "an unknown link falls back to the USB column, not to CoaXPress "
            "— an estimate that is too low only oversizes a buffer")

    # ── interpolation ────────────────────────────────────────────────────────
    # Log-log means the geometric midpoint of two rows maps to the geometric
    # mean of their rates. 512→524, 1024→264 on CXP.
    mid = math.sqrt(512 * 1024)
    want = math.sqrt(524.0 * 264.0)
    got = P.readout_fps(round(mid), link=P.CXP)
    r.check(abs(got - want) < 0.5,
            f"interpolates in log-log: {round(mid)} rows -> {got:.1f} fps, "
            f"geometric mean of the neighbours is {want:.1f}")
    r.check(P.readout_fps(700, link=P.CXP) < P.readout_fps(600, link=P.CXP),
            "fewer rows is never slower")
    mono = all(P.readout_fps(a, link=P.CXP) >= P.readout_fps(b, link=P.CXP)
               for a, b in zip(range(4, 2400, 37), range(41, 2437, 37)))
    r.check(mono, "monotonic across the whole range (65 sample points)")

    # Readout is row-by-row, so rows x fps is ~constant over the mid range —
    # this is the physical claim the table is standing in for.
    k = [rws * P.readout_fps(rws, link=P.CXP) for rws in (2368, 2048, 1024, 512, 256)]
    r.check(max(k) / min(k) < 1.1,
            f"rows x fps is constant to {100 * (max(k) / min(k) - 1):.0f} % over "
            f"the mid range (readout really is row-by-row)")

    # ── clamps, so a silly ROI cannot extrapolate off the end ────────────────
    r.check(P.readout_fps(999_999, link=P.CXP) == 115.0,
            "more rows than the sensor has clamps to the full-frame rate")
    r.check(P.readout_fps(1, link=P.CXP) == 19500.0,
            "fewer rows than the smallest table entry clamps to its rate")
    r.check(P.readout_fps(0, link=P.CXP) == 19500.0 and
            P.readout_fps(-5, link=P.CXP) == 19500.0,
            "zero or negative rows clamps instead of dividing by zero")

    # ── binning reads out fewer lines ────────────────────────────────────────
    r.check(abs(P.readout_fps(2048, binning=2) - P.readout_fps(1024)) < 1e-9,
            "binning 2 over 2048 rows reads out like 1024 rows")
    r.check(abs(P.readout_fps(2048, binning=4) - P.readout_fps(512)) < 1e-9,
            "binning 4 over 2048 rows reads out like 512 rows")
    r.check(P.readout_fps(2368, binning=0) == P.readout_fps(2368, binning=1),
            "binning 0 is treated as 1, not as a division by zero")
    r.check(all(P.readout_fps(2368, binning=b) >= P.readout_fps(2368)
                for b in P.BINNING_OPTIONS),
            f"every offered binning option ({P.BINNING_OPTIONS}) is at least as "
            f"fast as unbinned")

    # ── the presets the panel actually offers ────────────────────────────────
    bad = [k for k in P.PRESET_KEYS
           if not (0.0 < P.readout_fps(P.PRESETS[k].vsize) < 1e6)]
    r.check(not bad, f"every preset in the dropdown yields a sane rate "
                     f"(bad: {bad})")
    r.check(P.DEFAULT_PRESET in P.PRESETS,
            f"the default preset {P.DEFAULT_PRESET!r} exists")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
