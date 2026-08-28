"""Tracking as the app runs it: off the GUI thread, drawn, pinned, recorded.

`test_pupil_eyeloop.py` checks the tracker itself against the rig clips. This
file checks the four things that stand between that tracker and an operator,
none of which involve EyeLoop being any good:

  * the fit happens on its own thread, and the frame the GUI paints is the one
    the fit belongs to;
  * a settings edit reaches the running worker;
  * every tracked frame reaches the recorder — NaN when there was no fit, so a
    gap in the trace is a gap and not a missing row;
  * the preview draws the fit and clears it, and pins go on and come off.

**None of it needs an EyeLoop clone.** Without one every fit is None, which is
exactly the case the NaN contract exists for — so these checks mean the same
thing on a machine that has never set EyeLoop up.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_track.py
"""
from __future__ import annotations

import math
import sys
import threading
import time

import numpy as np

from _harness import Report, isolate_user_state, pump, qt_app

from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.track_worker import PupilTrackWorker


REGION = dict(limit_x0=60.0, limit_y0=20.0, limit_x1=260.0, limit_y1=220.0)


def eye_frame(w=320, h=240, r=40) -> np.ndarray:
    """The mock camera's frame: a dark disc on mid-grey, with a glint in it."""
    img = np.full((h, w), 180, np.uint8)
    yy, xx = np.ogrid[:h, :w]
    img[(xx - w // 2) ** 2 + (yy - h // 2) ** 2 <= r ** 2] = 20
    img[(xx - (w // 2 + 12)) ** 2 + (yy - (h // 2 - 10)) ** 2 <= 25] = 245
    return img


def npoints(item) -> int:
    xs = item.getData()[0]
    return 0 if xs is None else len(xs)


class FakeRec:
    """Stands in for the Recorder: remembers what was offered, and to which
    stream."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, float, float | None]] = []

    def put(self, stream, data, at=None) -> None:
        self.puts.append((stream, float(data), at))


def run_worker(app, r: Report, st: PupilSettings, *, frames: int = 6,
               reconfigure: PupilSettings | None = None):
    """Drive a worker over `frames` synthetic frames. Returns what it saw."""
    frame = eye_frame()
    # `gate` holds the source at half the frames until the reconfigure has
    # happened — the worker polls faster than this loop pumps, so without it
    # every frame is served before the edit and the edit proves nothing.
    served = {"n": 0, "thread": None,
              "gate": frames if reconfigure is None else frames // 2}

    def source():
        served["thread"] = threading.get_ident()
        if served["n"] >= served["gate"]:
            return None
        served["n"] += 1
        return frame

    w = PupilTrackWorker(source, st)
    sink: list[tuple] = []
    w.set_fit_sink(lambda fit, is_blink, at: sink.append((fit, is_blink, at)))
    seen: list = []
    crashed: list[str] = []
    w.error.connect(crashed.append)     # the Qt signal, NOT `track_error`

    w.start()
    deadline = time.perf_counter() + 4.0
    while time.perf_counter() < deadline:
        pump(app, 0.05)
        tr = w.get_latest()
        if tr is not None:
            seen.append(tr)
        if reconfigure is not None and served["n"] >= served["gate"]:
            w.configure(reconfigure)
            reconfigure = None
            served["gate"] = frames
            deadline = time.perf_counter() + 4.0
        if served["n"] >= frames and reconfigure is None:
            break
    radii = [radius for radius, _blink in w.take_tracked()]
    w.stop()
    r.check(not w.isRunning(), "the worker stops when told to")
    r.check(crashed == [], f"…without an exception escaping its thread ({crashed})")
    return w, served, seen, sink, radii


def main() -> int:
    r = Report("pupil-track")
    isolate_user_state()
    app = qt_app()

    main_thread = threading.get_ident()

    # ── 1. it runs somewhere else ────────────────────────────────────────────
    st_on = PupilSettings(track=True, **REGION)
    w, served, seen, sink, radii = run_worker(app, r, st_on)
    r.check(served["n"] > 0, f"the worker pulled frames ({served['n']})")
    r.check(served["thread"] not in (None, main_thread),
            "frames are pulled on the worker's thread, not the GUI's")
    # CONTROL: the same call made here reports this thread — so the check above
    # is comparing against something that can differ, not against None.
    inline = threading.get_ident()
    r.check(inline == main_thread,
            "control: the same reading taken inline is the GUI thread")
    r.info(f"EyeLoop: {w.track_error or 'available'}")

    # ── 2. the frame and its fit travel together ─────────────────────────────
    r.check(seen and all(t.frame is not None for t in seen),
            f"every published item carries its frame ({len(seen)} seen)")
    r.check(all(t.fit is None or t.box is not None for t in seen),
            "a fit always comes with the crop it was made in")

    # ── 3. every tracked frame reaches the recorder ──────────────────────────
    r.check(len(sink) == w.frames_seen,
            f"one sink call per tracked frame ({len(sink)} vs {w.frames_seen})")
    r.check(len(radii) == w.frames_seen,
            f"…and one trace point per tracked frame ({len(radii)})")
    r.check(all(isinstance(at, float) and at > 0 for _f, _b, at in sink),
            "each carries the time the frame was pulled")
    r.check(w.fits == sum(1 for f, _b, _at in sink if f is not None),
            f"the fit counter matches the fits ({w.fits}/{w.frames_seen})")

    # CONTROL: with tracking off nothing is recorded and nothing is traced,
    # while frames still flow — the camera must be untouched by any of this.
    st_off = PupilSettings(track=False, **REGION)
    w2, served2, seen2, sink2, radii2 = run_worker(app, r, st_off)
    r.check(served2["n"] > 0 and seen2, "control: frames still flow with tracking off")
    r.check(sink2 == [] and radii2 == [],
            f"control: …but nothing is recorded or traced ({len(sink2)}, "
            f"{len(radii2)})")
    r.check(all(t.fit is None for t in seen2),
            "control: …and no frame carries a fit")

    # ── 4. a settings edit reaches the running worker ────────────────────────
    w3, _s3, _v3, sink3, _r3 = run_worker(app, r, PupilSettings(track=False,
                                                                **REGION),
                                          frames=10, reconfigure=st_on)
    r.check(len(sink3) > 0,
            f"turning tracking on mid-run starts the trace ({len(sink3)} frames)")
    r.check(len(sink3) < w3.frames_seen,
            f"…and only from the edit onwards ({len(sink3)} of "
            f"{w3.frames_seen} frames)")

    # ── 5. `error` is still the Qt signal that carries a crash out ───────────
    r.check(hasattr(w.error, "connect"),
            "`error` is the PullWorker signal, not shadowed by a property")
    r.check(w.track_error is None or isinstance(w.track_error, str),
            f"`track_error` is the message ({w.track_error!r})")

    # ── 5b. the fit smoother — a rolling mean, with a circular angle mean ────
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.track_worker import _FitSmoother

    sm = _FitSmoother()
    f1 = sm.apply(PupilFit(100.0, 100.0, 40.0, 30.0, 10.0), window=1)
    r.check(f1 == PupilFit(100.0, 100.0, 40.0, 30.0, 10.0),
            "window=1 is a no-op — the raw fit, unchanged")

    sm = _FitSmoother()
    fits = [PupilFit(100.0 + d, 100.0, 40.0, 30.0, 10.0) for d in (0.0, 10.0, 20.0)]
    out = [sm.apply(f, window=3) for f in fits]
    r.check(abs(out[0].center_x - 100.0) < 1e-9,
            f"the first fit in a run is unaveraged, one value in ({out[0].center_x})")
    r.check(abs(out[1].center_x - 105.0) < 1e-9,
            f"two fits in, the mean of both ({out[1].center_x})")
    r.check(abs(out[2].center_x - 110.0) < 1e-9,
            f"three fits in (=window), the mean of all three ({out[2].center_x})")

    sm = _FitSmoother()
    for f in fits:
        sm.apply(f, window=3)
    f4 = sm.apply(PupilFit(140.0, 100.0, 40.0, 30.0, 10.0), window=3)
    r.check(abs(f4.center_x - (110.0 + 120.0 + 140.0) / 3.0) < 1e-9,
            f"a fourth fit drops the oldest — mean of the last 3 (110,120,140), "
            f"not all 4 ({f4.center_x})")

    # CONTROL: a lost frame must clear the buffer, not be skipped over — else
    # the first fit after a loss would blend in a pupil position from before it.
    sm = _FitSmoother()
    for f in fits:
        sm.apply(f, window=3)
    r.check(sm.apply(None, window=3) is None,
            "a lost frame reports no fit, same as unsmoothed")
    f_after = sm.apply(PupilFit(500.0, 500.0, 40.0, 30.0, 10.0), window=3)
    r.check(f_after.center_x == 500.0,
            f"control: the fit right after a loss is raw, not blended with "
            f"pre-loss history ({f_after.center_x})")

    # An ellipse's angle repeats every 180 deg, so 179 and 1 deg are 2 deg
    # apart, not ~178 — a plain mean gets this wrong at the wrap.
    sm = _FitSmoother()
    sm.apply(PupilFit(0.0, 0.0, 40.0, 30.0, 179.0), window=2)
    wrapped = sm.apply(PupilFit(0.0, 0.0, 40.0, 30.0, 1.0), window=2)
    r.check(wrapped.angle_deg < 5.0 or wrapped.angle_deg > 175.0,
            f"the angle mean wraps at 180 deg, not through the middle "
            f"({wrapped.angle_deg:.1f})")

    # ── 5c. the blink detector — a sudden drop against a rolling baseline ────
    from acqApp.devices.pupil_cam.track_worker import _BlinkDetector

    bd = _BlinkDetector()
    steady = [bd.check(30.0, drop_frac=0.35, window=10) for _ in range(6)]
    r.check(not any(steady),
            f"a steady radius never flags, warm-up included ({steady})")

    bd = _BlinkDetector()
    for _ in range(6):
        bd.check(30.0, drop_frac=0.35, window=10)
    r.check(not bd.check(25.0, drop_frac=0.35, window=10),
            "a 17% dip under a 35% threshold does not flag")
    r.check(bd.check(15.0, drop_frac=0.35, window=10),
            "a 50% drop under the same threshold does")
    r.check(bd.check(14.0, drop_frac=0.35, window=10),
            "…and stays flagged while the radius stays down")
    r.check(not bd.check(29.0, drop_frac=0.35, window=10),
            "…and clears once the radius recovers")

    # CONTROL: a run of blink frames must not drag the baseline down — or a
    # blink long enough would talk itself into looking normal.
    bd = _BlinkDetector()
    for _ in range(6):
        bd.check(30.0, drop_frac=0.35, window=10)
    for _ in range(20):                 # a long blink, well past `window`
        bd.check(10.0, drop_frac=0.35, window=10)
    r.check(bd.check(28.0, drop_frac=0.35, window=10) is False,
            "control: the baseline held at ~30 through a long blink, so the "
            "eventual recovery reads as recovery, not as a fresh baseline")

    r.check(bd.check(None, drop_frac=0.35, window=10) is False,
            "no radius (no fit) is never itself a flagged blink")

    # ══ the app half ═════════════════════════════════════════════════════════
    sys.argv = ["main.py", "--mock"]
    import acqApp.main as M

    win = M.MainWindow(cam_info=None, mock=True, enabled={"pupil_cam"},
                       cam_handle=None)
    mod = win._modules[0]
    panel = mod.panel

    # ── 6. what the recorder is offered, stream by stream ────────────────────
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    all_streams = list(mod.FIT_STREAMS) + [mod.BLINK_STREAM]
    rec = FakeRec()
    mod._record_fit(rec, PupilFit(101.0, 202.0, 30.0, 20.0, 45.0), False, 1.5)
    r.check([p[0] for p in rec.puts] == all_streams,
            f"a fit writes the five ellipse streams plus the blink flag "
            f"({[p[0] for p in rec.puts]})")
    r.check([p[1] for p in rec.puts] == [101.0, 202.0, 30.0, 20.0, 45.0, 0.0],
            f"…with the ellipse in them, and 0.0 = not flagged "
            f"({[p[1] for p in rec.puts]})")
    r.check(all(p[2] == 1.5 for p in rec.puts),
            "…all stamped at the frame's own time, not the write's")

    rec1b = FakeRec()
    mod._record_fit(rec1b, PupilFit(0.0, 0.0, 10.0, 10.0, 0.0), True, 1.6)
    r.check(rec1b.puts[-1][:2] == (mod.BLINK_STREAM, 1.0),
            f"a flagged frame records 1.0, not just True ({rec1b.puts[-1]})")

    rec2 = FakeRec()
    mod._record_fit(rec2, None, False, 2.5)
    r.check([p[0] for p in rec2.puts] == all_streams,
            "a LOST frame writes the same six streams")
    r.check(all(math.isnan(p[1]) for p in rec2.puts),
            f"…all NaN including the blink flag — a blink cannot be judged "
            f"without a radius ({[p[1] for p in rec2.puts]})")

    # ── 7. the settings behind the number are recorded ───────────────────────
    panel._chk_track.setChecked(True)
    panel._spn_thr.setValue(57)
    panel.set_pins([(11.0, 22.0, 3.0), (44.0, 55.0, 6.0)])
    md = mod.metadata()
    r.check(md.get("pupil_track_threshold") == 57,
            f"the threshold is in the metadata ({md.get('pupil_track_threshold')})")
    r.check(md.get("pupil_tracker") == "eyeloop",
            f"…and which tracker produced it ({md.get('pupil_tracker')!r})")
    panel._chk_smooth.setChecked(True)
    panel._spn_smooth_win.setValue(9)
    md = mod.metadata()
    r.check(md.get("pupil_smooth") is True and md.get("pupil_smooth_window") == 9,
            f"stabilization travels with the trace, like threshold does "
            f"({md.get('pupil_smooth')}, {md.get('pupil_smooth_window')})")
    panel._chk_smooth.setChecked(False)
    r.check(md.get("pupil_cr_pins") == [11.0, 22.0, 3.0, 44.0, 55.0, 6.0],
            f"…and the pins, flattened for HDF5 ({md.get('pupil_cr_pins')})")
    r.check(mod.final_metadata() == {},
            "a session-less adapter invents no frame counts")

    # ── 8. the preview draws the fit, and clears it ──────────────────────────
    r.check(npoints(mod._fit_curve) == 0, "nothing tracked yet: no outline")
    mod._draw_fit(PupilFit(160.0, 120.0, 40.0, 30.0, 0.0))
    r.check(npoints(mod._fit_curve) > 8,
            f"a fit is drawn ({npoints(mod._fit_curve)} points)")
    xs, ys = mod._fit_curve.getData()
    r.check(abs(0.5 * (xs.min() + xs.max()) - 160.0) < 1.0
            and abs(0.5 * (xs.max() - xs.min()) - 40.0) < 1.0
            and abs(0.5 * (ys.max() - ys.min()) - 30.0) < 1.0,
            f"…as the ellipse it was given (centre {0.5*(xs.min()+xs.max()):.0f}, "
            f"semi-axes {0.5*(xs.max()-xs.min()):.0f}/"
            f"{0.5*(ys.max()-ys.min()):.0f})")
    # The angle is drawn, not dropped: a 90 degree turn swaps the two extents.
    mod._draw_fit(PupilFit(160.0, 120.0, 40.0, 30.0, 90.0))
    xs, ys = mod._fit_curve.getData()
    r.check(abs(0.5 * (xs.max() - xs.min()) - 30.0) < 1.0
            and abs(0.5 * (ys.max() - ys.min()) - 40.0) < 1.0,
            f"control: turning it 90° swaps the axes "
            f"({0.5*(xs.max()-xs.min()):.0f}/{0.5*(ys.max()-ys.min()):.0f})")
    # THE one that matters: a lost frame must not leave the last good outline
    # standing. `fit()` upstream returns stale params on failure — this is the
    # display half of the same trap.
    mod._draw_fit(None)
    r.check(npoints(mod._fit_curve) == 0,
            "a frame with no fit clears the outline rather than leaving a stale one")

    # ── 8b. blink runs are shaded on the radius plot ──────────────────────────
    blink = [False, False, True, True, True, False, False, True, False]
    mod._trace = [(0.0, b) for b in blink]
    mod._update_blink_overlay()
    r.check(sum(reg.isVisible() for reg in mod._blink_regions) == 2,
            f"two separate runs of True become two shaded regions "
            f"({sum(reg.isVisible() for reg in mod._blink_regions)})")
    spans = sorted(reg.getRegion() for reg in mod._blink_regions if reg.isVisible())
    r.check(abs(spans[0][0] - 1.5) < 1e-9 and abs(spans[0][1] - 4.5) < 1e-9,
            f"the first run (indices 2-4) spans (1.5, 4.5) ({spans[0]})")
    r.check(abs(spans[1][0] - 6.5) < 1e-9 and abs(spans[1][1] - 7.5) < 1e-9,
            f"the second, one-frame run (index 7) spans (6.5, 7.5) ({spans[1]})")

    pool_after_two = len(mod._blink_regions)
    mod._trace = [(0.0, False) for _ in mod._trace]      # the blink passes
    mod._update_blink_overlay()
    r.check(all(not reg.isVisible() for reg in mod._blink_regions),
            "no runs left: every region is hidden")
    r.check(len(mod._blink_regions) == pool_after_two,
            "…but the pool is kept, not torn down and rebuilt next time")

    mod._trace = [(0.0, True)] * 5
    mod._update_blink_overlay()
    r.check(len(mod._blink_regions) == pool_after_two,
            "one run reuses a pooled region rather than growing the pool")
    r.check(sum(reg.isVisible() for reg in mod._blink_regions) == 1,
            "…and exactly one of them is shown")

    # ── 9. pins go on and come off, on the preview ───────────────────────────
    win._btn_run.setChecked(True)
    pump(app, 1.0)
    for _ in range(4):
        win._display_tick()
        pump(app, 0.05)
    r.check(mod._img.image is not None, "frames still reach the preview")
    r.check(mod._last_frame is not None, "…and the newest one is kept for pinning")

    mod._gv.resize(400, 300)
    mod._vb.setRange(xRange=(0, 320), yRange=(0, 240), padding=0)
    pump(app, 0.05)

    class _Ev:
        def __init__(self, pt): self._p = pt
        def scenePos(self): return self._p

    panel.clear_pins()
    rect = mod._vb.sceneBoundingRect()
    at = rect.center()
    at_view = mod._vb.mapSceneToView(at)

    # CONTROL first: with the tool off, a click on the preview pins nothing.
    mod._on_click(_Ev(at))
    r.check(panel.settings.cr_pins == [],
            f"control: with pin mode off, a click pins nothing "
            f"({panel.settings.cr_pins})")

    mod._btn_pin.setChecked(True)
    r.check("pin or unpin" in mod._lbl_limit.text(),
            f"arming says what to do next ({mod._lbl_limit.text()!r})")
    mod._on_click(_Ev(at))
    pins = panel.settings.cr_pins
    r.check(len(pins) == 1, f"a click pins one reflection ({pins})")
    r.check(abs(pins[0][0] - at_view.x()) < 1 and abs(pins[0][1] - at_view.y()) < 1,
            "…where it was clicked, in frame pixels")
    r.check(pins[0][2] > 0, f"…with an extent, so it can be seen and hit ({pins[0][2]})")
    r.check(npoints(mod._pin_curve) > 8,
            f"…and it is drawn ({npoints(mod._pin_curve)} points)")

    mod._on_click(_Ev(at))              # the same place again
    r.check(panel.settings.cr_pins == [],
            f"clicking a pinned reflection unpins it ({panel.settings.cr_pins})")
    r.check(npoints(mod._pin_curve) == 0, "…and it stops being drawn")

    # The two placement tools are exclusive: arming one disarms the other, or
    # the next click means two things at once.
    mod._btn_pin.setChecked(True)
    mod._btn_limit.setChecked(True)
    r.check(not mod._btn_pin.isChecked(),
            "arming the region tool disarms pinning")
    mod._btn_pin.setChecked(True)
    r.check(not mod._btn_limit.isChecked(), "…and the other way round")
    mod._btn_pin.setChecked(False)

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
