"""Pupil Auto: suggested threshold/blur/reflection (and eye region) from
a few frames.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_auto.py [-v] [--part NAME]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from _harness import Report, isolate_user_state, pump, qt_app, run_parts
from _pupil_helpers import face_frame
from acqApp.devices.pupil_cam.settings import PupilSettings


# ═══ Auto (suggested parameters) ═════════════════════════════════════

def _part_autotune() -> int:  # noqa: PLR0915 — one linear scenario
    r = Report("pupil-autotune")
    from acqApp.devices.pupil_cam.autotune import autotune

    frames = [face_frame(cx=200 + 3 * i, seed=i) for i in range(8)]
    a = autotune(frames, (100, 60, 320, 240))
    r.check(a is not None, "a dim synthetic eye gets a suggestion")
    if a is not None:
        r.check(20 <= a.threshold < 26,
                f"threshold lands between pupil (20) and iris (26): {a.threshold}")
        r.check(a.blur in (1, 3, 5), f"blur is a sane odd kernel ({a.blur})")
        r.check(a.cr_remove and a.cr_threshold is not None
                and 26 < a.cr_threshold < 235,
                f"a rim reflection turns removal on, threshold between iris and "
                f"glint ({a.cr_threshold})")
        r.check(a.region is None, "a given region is left alone")
        st = a.apply(PupilSettings(limit_x0=100, limit_y0=60, limit_x1=320,
                                   limit_y1=240, track_threshold=99))
        r.check(st.track_threshold == a.threshold and st.limit_x1 == 320,
                "apply() sets the knobs and keeps the drawn region")

    nog = autotune([face_frame(glint=False, seed=i) for i in range(6)],
                   (100, 60, 320, 240))
    r.check(nog is not None and not nog.cr_remove,
            "control: no reflection leaves removal off")

    big = [np.pad(face_frame(seed=i), ((300, 300), (500, 500)),
                  constant_values=150) for i in range(6)]
    b = autotune(big, None)
    r.check(b is not None and b.region is not None,
            "no region: one is estimated on a larger frame")
    if b is not None and b.region is not None:
        x0, y0, x1, y1 = b.region
        r.check(x0 < 710 < x1 and y0 < 450 < y1,
                f"the estimated region holds the pupil ({b.region})")
        r.check((x1 - x0) < 600, f"and is a box around the eye, not the frame "
                                 f"({x1 - x0} px wide)")
        r.check(20 <= b.threshold < 26, f"threshold still right ({b.threshold})")
    r.check(autotune([np.full((200, 200), 128, np.uint8)] * 4, None) is None,
            "a blank frame gives None, not a guess")
    r.check(autotune([], None) is None, "no frames gives None")

    # Real rig clip, when this machine has it.
    real = Path(r"C:\Users\pinkh008\Downloads\VF215.4LL_20260924_FOV1_T16_Pupil"
                r"\VF215.4LL_20260924_FOV1_T16_Pupil.avi")
    if real.is_file():
        from acqApp.devices.pupil_cam.avi import AviReader
        rd = AviReader(real)
        got = autotune([rd.luma(i) for i in range(0, len(rd), 8)], None)
        r.check(got is not None and got.threshold == 21 and got.blur == 3,
                f"real clip: matches the hand-swept best (21, blur 3): "
                f"{got and (got.threshold, got.blur)}")
    else:
        r.info("real clip absent: skipped")

    # ── the live tab: panel Auto -> adapter gathers frames -> one change ──
    app = qt_app()
    isolate_user_state()
    from acqApp.adapters.pupil_cam import PupilCamModule

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    changes: list = []
    m.panel.settings_changed.connect(changes.append)
    m._on_auto_requested()
    r.check(m._auto_frames is None and any("Live view" in x for x in win.messages),
            "Auto with no live feed says to start Live view")
    m.build_session(True)
    m.start()
    pump(app, 0.2)
    m._on_auto_requested()
    r.check(m._auto_frames == [] and not m.panel.tracking._btn_auto.isEnabled(),
            "Auto starts gathering and greys the button")
    for i in range(m._AUTO_FRAMES * m._AUTO_EVERY):
        m._gather_auto(face_frame(cx=200 + i % 5, seed=i))
    for _ in range(100):
        pump(app, 0.05)
        if m._auto_worker is None:
            break
    st = m.panel.settings
    r.check(20 <= st.track_threshold < 26 and st.search_limit() is not None,
            f"the suggestion lands in the panel (threshold {st.track_threshold}, "
            f"region {st.search_limit()})")
    r.check(len(changes) == 1, f"as ONE settings change ({len(changes)})")
    r.check(m.panel.tracking._btn_auto.isEnabled()
            and any("pupil Auto:" in x and "check" in x for x in win.messages),
            "the button comes back and the status says what was chosen")
    m.stop()
    return r.finish()


PARTS = {
    "autotune": _part_autotune,
}

if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
