"""Pupil review: the offline review window, its safety fixes, the live
mirror, seeding, Apply, the Live | Review mode, recorded sessions and
the standalone launcher.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_review.py [-v] [--part NAME]
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from _harness import (Report, isolate_user_state, npoints, pump, qt_app,
                      run_parts)
from _pupil_helpers import DiscTracking, face_frame, video_eye_frame, write_avi
from acqApp.devices.pupil_cam.settings import PupilSettings
from PyQt6.QtCore import QPointF, Qt


# ═══ offline review ═════════════════════════════════════════════════════

def _part_review() -> int:  # noqa: PLR0915 — one linear scenario
    r = Report("pupil-review")
    tmp = Path(tempfile.mkdtemp(prefix="pupil_review_"))
    H, W, N = 120, 160, 12
    radii = [14 + (i % 3) for i in range(N)]
    frames = [video_eye_frame(H, W, 80, 60, rad) for rad in radii]
    frames[7] = np.full((H, W), 190, np.uint8)       # nothing to fit
    clip = write_avi(tmp / "eye.avi", [f.tobytes() for f in frames], W, H,
                     b"Y800", 8)

    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.review import PupilReview, sidecar_paths

    review_mod.PupilTracking = DiscTracking

    st = PupilSettings(limit_x0=40, limit_y0=30, limit_x1=120, limit_y1=90,
                       cr_remove=False)
    rev = PupilReview(clip, st)
    seen: list[int] = []
    done = rev.track_all(lambda i, n: seen.append(i))
    r.check(done and seen[-1] == N, f"tracked every frame ({seen[-1:]})")
    r.check(rev.tracked, "marked tracked")
    good = [i for i in range(N) if i != 7]
    r.check(all(not np.isnan(rev.auto[i, 0]) for i in good),
            "every real frame got a fit")
    r.check(np.isnan(rev.auto[7, 0]), "the blank frame has no fit")
    r.check(all(abs(rev.radius()[i] - radii[i]) < 4 for i in good),
            f"radius follows the disc ({np.round(rev.radius(), 1)})")
    r.check(7 in rev.suspects(), "a no-fit frame is a suspect")

    # ── hand edits ──
    before = rev.table().copy()
    fix = PupilFit(82.0, 58.0, 25.0, 25.0, 0.0)
    rev.set_manual(3, fix)
    r.check(rev.is_edited(3) and not rev.is_edited(4), "only frame 3 is edited")
    r.check(abs(rev.radius()[3] - 25.0) < 1e-9, "the reported radius is the edit")
    r.check(np.array_equal(rev.auto[3], before[3]),
            "the auto fit under the edit is untouched")
    r.check(np.array_equal(rev.table()[4:], before[4:], equal_nan=True),
            "other frames unchanged")
    rev.set_manual(7, fix)
    r.check(rev.fit_at(7) == fix, "a frame with no fit can be given one")
    r.check(7 not in rev.suspects(), "an edited frame stops being a suspect")
    # CONTROL: or a table that always returned manual would pass.
    rev.clear_manual(3)
    r.check(np.array_equal(rev.table()[3], before[3]),
            "control: clearing the edit restores the auto fit")
    rev.set_manual(3, fix)

    # ── re-track keeps edits; new parameters take effect ──
    rev.settings = PupilSettings(**{**vars(rev.settings), "track_threshold": 5})
    rev.track_all()
    r.check(rev.is_edited(3) and rev.is_edited(7),
            "re-tracking keeps hand edits")
    r.check(np.isnan(rev.auto[:, 0]).all(),
            "a threshold that selects nothing re-tracks to no fits")
    rev.settings = PupilSettings(**{**vars(rev.settings), "track_threshold": 45})
    rev.track_all()

    # ── sidecar ──
    js, npz = rev.save()
    r.check(js == sidecar_paths(clip)[0] and js.is_file() and npz.is_file(),
            "sidecar files sit beside the clip")
    back = PupilReview.load(clip)
    r.check(np.array_equal(back.table(), rev.table(), equal_nan=True),
            "the final table round-trips")
    r.check(np.array_equal(back.edited, rev.edited), "edited flags round-trip")
    r.check(back.settings.track_threshold == 45
            and back.settings.limit_x1 == 120, "parameters round-trip")
    z = np.load(npz)
    r.check(z["final"].shape == (N, 5) and z["radius"].shape == (N,),
            "npz carries the final table and radius")
    other = write_avi(tmp / "short.avi", [f.tobytes() for f in frames[:5]],
                      W, H, b"Y800", 8)
    for src, dst in zip(sidecar_paths(clip), sidecar_paths(other)):
        dst.write_bytes(src.read_bytes())
    r.check(not PupilReview.load(other).edited.any(),
            "a sidecar for a different frame count is ignored")

    # ── the dialog ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    clip2 = write_avi(tmp / "eye2.avi", [f.tobytes() for f in frames],
                      W, H, b"Y800", 8)
    live = [True]
    dlg = PupilReviewDialog(str(clip2), busy=lambda: live[0])
    r.check(dlg.review is not None and len(dlg.review) == N,
            "dialog opens the clip")
    r.check(dlg.review.settings.search_limit() is not None,
            "a clip with no region gets a default one")
    # Fit the frames: a default region is the central half, which holds the disc.
    r.check(dlg._btn_track.text() == "Apply to all frames", "idle button label")
    dlg._track_all()
    r.check(dlg._worker is None and "Live tracking" in dlg._prog.text(),
            "tracking refused while live tracking runs, and says why")
    live[0] = False
    dlg._track_all()
    for _ in range(100):
        pump(app, 0.1)
        if dlg._worker is None:
            break
    r.check(dlg._worker is None and dlg.review.tracked, "tracked in the worker")
    dlg.goto(5)
    fit = dlg.review.fit_at(5)
    r.check(fit is not None, "frame 5 has a fit to edit")
    # The handle must reproduce the fit it was built from.
    dlg._make_roi(PupilFit(70.0, 50.0, 20.0, 10.0, 30.0))
    got = dlg._roi_fit()
    r.check(all(abs(a - b) < 1e-6 for a, b in
                zip((got.center_x, got.center_y, got.semi_major,
                     got.semi_minor, got.angle_deg), (70, 50, 20, 10, 30))),
            "ellipse handle round-trips centre, axes and angle")
    # An edge handle moves its own side; the opposite side stays put.
    from PyQt6.QtCore import QPointF as _P
    roi = dlg._roi
    kinds = sorted((h["type"], round(h["pos"].x(), 1), round(h["pos"].y(), 1),
                    round(h["center"].x(), 1), round(h["center"].y(), 1))
                   for h in roi.handles)
    r.check([k for k in kinds if k[0] == "s"] == [
                ("s", 0.0, 0.5, 1.0, 0.5), ("s", 0.5, 0.0, 0.5, 1.0),
                ("s", 0.5, 1.0, 0.5, 0.0), ("s", 1.0, 0.5, 0.0, 0.5)]
            and sum(k[0] == "r" for k in kinds) == 1,
            f"one handle per edge, each pinned at the opposite edge, plus a "
            f"rotate handle ({kinds})")
    from pyqtgraph.graphicsItems.ROI import Handle
    drawn = [c for c in roi.childItems() if isinstance(c, Handle)]
    r.check(len(drawn) == len(roi.handles) == 5,
            f"…and nothing else drawn: no leftover default handles "
            f"({len(drawn)} handle items)")
    far = roi.mapToParent(_P(20.0, 0.0))        # the edge opposite the drag
    top = next(h["item"] for h in roi.handles
               if h["type"] == "s" and h["pos"] == _P(0.5, 1.0))
    roi.movePoint(top, roi.mapToParent(_P(20.0, 26.0)), finish=True)
    got = dlg._roi_fit()
    far2 = roi.mapToParent(_P(20.0, 0.0))
    r.check(abs(got.semi_minor - 13.0) < 1e-6 and abs(got.semi_major - 20.0) < 1e-6
            and abs(far2.x() - far.x()) < 1e-6 and abs(far2.y() - far.y()) < 1e-6,
            f"dragging an edge out 6 px grows that axis by 3 and leaves the "
            f"opposite edge where it was (minor {got.semi_minor:.2f})")
    r.check(dlg.review.is_edited(5), "…and the drag is an edit")
    dlg.review.clear_manual(5)
    dlg.edit_frame(5, PupilFit(70.0, 50.0, 20.0, 10.0, 30.0))
    r.check(dlg.review.is_edited(5) and abs(dlg.review.radius()[5] - 15) < 1e-9,
            "a dialog edit lands in the data")
    dlg._next_suspect()
    r.check(dlg._frame == 7, f"Next suspect jumps to the no-fit frame ({dlg._frame})")
    dlg._reset_current()
    dlg.goto(5)
    dlg._reset_current()
    r.check(not dlg.review.is_edited(5), "Reset returns the frame to auto")
    # ── playback ──
    dlg.goto(0)
    dlg._spn_rate.setValue(200.0)
    dlg.toggle_play()
    r.check(dlg.playing and dlg._btn_play.text() == "Pause" and dlg._roi is None,
            "Play starts the timer and drops the edit handle")
    for _ in range(60):
        pump(app, 0.05)
        if not dlg.playing:
            break
    r.check(not dlg.playing and dlg._frame == N - 1,
            f"playback runs to the last frame and stops ({dlg._frame})")
    r.check(dlg._btn_play.text() == "Play", "the button reads Play again")
    dlg.toggle_play()                       # from the end: starts over
    r.check(dlg._frame <= 2, f"Play at the end restarts from the top ({dlg._frame})")
    dlg.pause()
    dlg.goto(0)
    dlg._chk_loop.setChecked(True)
    dlg.toggle_play()
    wraps, prev = 0, dlg._frame
    for _ in range(40):
        pump(app, 0.05)
        wraps += dlg._frame < prev
        prev = dlg._frame
    r.check(dlg.playing and wraps >= 1, f"Loop wraps to the top and keeps going ({wraps})")
    dlg.edit_frame(2, PupilFit(70.0, 50.0, 20.0, 10.0, 30.0))
    r.check(not dlg.playing, "editing a frame stops playback")
    dlg.review.clear_manual(2)
    dlg._chk_loop.setChecked(False)

    # ── display levels (display only) ──
    dlg.goto(0, force=True)
    auto = tuple(dlg._img.levels)
    dlg._chk_auto.setChecked(False)
    dlg._hist.item.setLevels(60, 130)
    pump(app, 0.05)
    r.check(tuple(dlg._img.levels) == (60, 130),
            f"Auto off: the LUT bar's handles set the image levels ({tuple(dlg._img.levels)})")
    before = dlg.review.table().copy()
    dlg._chk_auto.setChecked(True)
    r.check(tuple(dlg._img.levels) == auto,
            "Auto on again re-derives the levels from the frame")
    r.check(np.array_equal(dlg.review.table(), before, equal_nan=True),
            "changing the display never touches the data")
    dlg.save()
    r.check(not dlg._dirty, "saving clears the unsaved flag")
    dlg._dirty = False
    dlg.close()
    return r.finish()


# ═══ the standalone launcher ═══════════════════════════════════════════

def _part_launcher() -> int:
    r = Report("pupil-launcher")
    import ast
    import subprocess
    from _harness import APP_DIR

    launcher = APP_DIR / "run_pupil_review.py"
    reqs = (APP_DIR / "requirements-pupil.txt").read_text(encoding="utf-8")
    want = {ln.split("#")[0].strip().lower() for ln in reqs.splitlines()}
    want.discard("")

    # The tool's third-party imports must all be installable from its own
    # requirements, or a fresh machine dies at the first import.
    stdlib = set(sys.stdlib_module_names)
    third, seen, todo = set(), set(), ["devices/pupil_cam/review_app.py"]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for n in ast.walk(ast.parse((APP_DIR / rel).read_text(encoding="utf-8"))):
            mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                    else [n.module] if isinstance(n, ast.ImportFrom) and n.module
                    else [])
            for m in mods:
                top = m.split(".")[0]
                if top == "acqApp":
                    parts = m.split(".")[1:]
                    for cand in (parts, parts[:-1]):
                        if cand and APP_DIR.joinpath(*cand).with_suffix(".py").is_file():
                            todo.append("/".join(cand) + ".py")
                elif top not in stdlib:
                    third.add(top)
    pip_name = {"cv2": "opencv-python", "yaml": "pyyaml"}
    need = {pip_name.get(m, m).lower() for m in third - {"eyeloop"}}  # the clone, not pip
    r.check(need <= want, f"requirements-pupil.txt covers the tool's imports "
                          f"(missing {sorted(need - want)})")
    # EyeLoop is imported lazily from a clone, but its own cv2/yaml need listing.
    r.check({"opencv-python", "pyyaml"} <= want, "EyeLoop's cv2 and yaml are listed")
    r.check("nidaqmx" not in want and "pypylon" not in want,
            "no hardware package in the tool's requirements")

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, str(launcher), "--check"],
                         capture_output=True, text=True, timeout=180, env=env)
    r.check(out.returncode == 0,
            f"the launcher builds the review window headless ({out.stderr[-300:]})")
    return r.finish()


# ═══ the review's safety fixes, the mirror, seeding, Apply, mode ════════

def _part_review_safety() -> int:  # noqa: PLR0915 — one linear scenario
    r = Report("pupil-review-safety")
    import json
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.review import PupilReview, sidecar_paths

    review_mod.PupilTracking = DiscTracking
    tmp = Path(tempfile.mkdtemp(prefix="pupil_safety_"))
    H, W, N = 120, 160, 10
    frames = [video_eye_frame(H, W, 80, 60, 14).tobytes() for _ in range(N)]
    clip = write_avi(tmp / "a.avi", frames, W, H, b"Y800", 8)
    st = PupilSettings(limit_x0=40, limit_y0=30, limit_x1=120, limit_y1=90)

    rev = PupilReview(clip, st)
    rev.track_all()
    good = rev.auto.copy()
    r.check(not rev.stale and not PupilReview.fitting(),
            "fresh fit: not stale, not fitting")
    # A stopped re-track keeps the previous fits.
    rev.settings = PupilSettings(**{**vars(st), "track_threshold": 5})
    r.check(rev.stale, "changed parameters make the fits stale")
    calls: list = []
    r.check(rev.track_all(should_stop=lambda: calls.append(1) or len(calls) > 3)
            is False, "a stop returns False")
    r.check(np.array_equal(rev.auto, good), "and the previous fits are kept")

    class Broken(DiscTracking):
        def track(self, frame, st):
            raise ValueError("bad frame")

    review_mod.PupilTracking = Broken
    try:
        rev.track_all()
        r.check(False, "a failing tracker raises")
    except ValueError:
        r.check(np.array_equal(rev.auto, good) and not PupilReview.fitting(),
                "a failed re-track keeps the fits and clears the fitting flag")
    review_mod.PupilTracking = DiscTracking

    # Saved while stale: reopened, it still says stale.
    rev.save()
    back = PupilReview.load(clip)
    r.check(back.stale and back.tracked_with.track_threshold == st.track_threshold,
            "the settings the fits came from are saved; stale survives a reload")

    # Another clip under the same name, same frame count: not applied.
    rev.set_manual(2, PupilFit(80, 60, 30, 30, 0))
    rev.save()
    js, npz = sidecar_paths(clip)
    meta = json.loads(js.read_text())
    meta["video_bytes"] += 1
    js.write_text(json.dumps(meta))
    other = PupilReview.load(clip)
    r.check(not other.edited.any() and bool(other.sidecar_note)
            and "changed" in other.sidecar_note,
            f"a sidecar for a changed clip is not applied ({other.sidecar_note})")
    other.save()
    r.check((tmp / "a.avi.pupil.json.old").is_file()
            and (tmp / "a.avi.pupil.npz.old").is_file(),
            "and is kept as *.old when the new one is saved")

    # A truncated npz: ignored, not a crash.
    npz.write_bytes(npz.read_bytes()[:50])
    r.check(PupilReview.load(clip).sidecar_note is not None,
            "a truncated sidecar is ignored with a note, not raised")
    js.write_text("[1, 2]")
    r.check(PupilReview.load(clip).sidecar_note is not None,
            "a json that isn't an object is ignored too")
    r.check(not list(tmp.glob("*.tmp")), "no temp files left behind")

    # ── the dialog ──
    app = qt_app()
    isolate_user_state()
    from PyQt6.QtWidgets import QMessageBox
    from acqApp.devices.pupil_cam import review_dialog as rd
    for p in sidecar_paths(clip):
        p.unlink(missing_ok=True)
    answers: list = []
    real_q, real_warn = QMessageBox.question, QMessageBox.warning
    warned: list = []
    QMessageBox.question = staticmethod(lambda *a, **k: answers.pop(0))
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    real_save = PupilReview.save
    try:
        dlg = rd.PupilReviewDialog(str(clip))
        dlg.show()
        dlg.edit_frame(1, PupilFit(80, 60, 20, 20, 0))
        answers[:] = [QMessageBox.StandardButton.Cancel]
        dlg.reject()                                    # Esc
        r.check(dlg.isVisible() and not answers,
                "Esc asks about unsaved edits, and Cancel keeps the window")
        dlg._ctl._spn_thr.setValue(200)
        dlg._auto()
        for _ in range(100):
            pump(app, 0.05)
            if dlg._auto_worker is None:
                break
        r.check(dlg._ctl._spn_thr.value() < 190 and "Auto:" in dlg._prog.text(),
                f"Auto in the review window sets the threshold "
                f"({dlg._ctl._spn_thr.value()}): {dlg._prog.text()[:70]}")
        r.check(dlg.review.settings.track_threshold == dlg._ctl._spn_thr.value(),
                "and the review's settings follow")
        dlg._worker = object()
        r.check(not dlg.open_video(str(clip)) and "Stop" in dlg._prog.text(),
                "opening a clip mid-job is refused")
        dlg._worker = None

        def ro(self):
            raise PermissionError("read-only folder")

        PupilReview.save = ro
        r.check(dlg.save() is False and bool(warned) and dlg._dirty,
                "a failed save warns and leaves the edits unsaved")
        answers[:] = [QMessageBox.StandardButton.Save]
        dlg.close()
        r.check(dlg.isVisible(), "a failed save on close keeps the window open")
        PupilReview.save = real_save
        answers[:] = [QMessageBox.StandardButton.Discard]
        dlg.close()
        r.check(not dlg.isVisible(), "Discard closes")
    finally:
        QMessageBox.question, QMessageBox.warning = real_q, real_warn
        PupilReview.save = real_save
    return r.finish()

def _part_mirror() -> int:  # noqa: PLR0915 — one linear scenario
    """The review window shows the live tab's own tracking controls, the
    region box and the numbers stay in step, suspects are drawn, and the
    sidecar survives case, half-writes and odd values."""
    r = Report("pupil-mirror")
    import json
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.review import PupilReview, sidecar_paths
    from acqApp.devices.pupil_cam.tracking_panel import TrackingControls

    review_mod.PupilTracking = DiscTracking
    tmp = Path(tempfile.mkdtemp(prefix="pupil_mirror_"))
    H, W, N = 120, 160, 20
    frames = [video_eye_frame(H, W, 80, 60, 14) for _ in range(N)]
    for i in (6, 7, 15):
        frames[i] = np.full((H, W), 190, np.uint8)      # nothing to fit
    clip = write_avi(tmp / "Eye.avi", [f.tobytes() for f in frames], W, H,
                     b"Y800", 8)

    # ── persistence ──
    rev = PupilReview(clip, PupilSettings(limit_x0=40, limit_y0=30,
                                          limit_x1=120, limit_y1=90))
    rev.track_all()
    rev.set_manual(3, PupilFit(80, 60, 20, 20, 0))
    rev.save()
    lower = tmp / "eye.avi"
    back = PupilReview.load(lower)
    r.check(back.sidecar_note is None and back.is_edited(3),
            f"the same clip opened in another case keeps its results "
            f"({back.sidecar_note})")
    js, npz = sidecar_paths(clip)
    meta = json.loads(js.read_text())
    meta["settings"]["track_threshold"] = 21.0
    js.write_text(json.dumps(meta))
    back = PupilReview.load(clip)
    r.check(back.settings.track_threshold == 21
            and isinstance(back.settings.track_threshold, int),
            "a float written for an int setting is read as an int")
    meta["settings"]["cr_pins"] = [[1, "a", 3]]
    js.write_text(json.dumps(meta))
    back = PupilReview.load(clip)
    r.check(back.sidecar_note is not None and not back.edited.any(),
            f"a sidecar with unusable values is set aside, not raised "
            f"({back.sidecar_note})")
    rev.save()
    meta = json.loads(js.read_text())
    meta["generation"] = "someone-else"
    js.write_text(json.dumps(meta))
    back = PupilReview.load(clip)
    r.check(back.stale and back.sidecar_note and "re-track" in back.sidecar_note,
            "a half-written pair (ids differ) loads as stale, with a note")
    for i in range(2):                      # two set-asides, two backups
        meta = json.loads(js.read_text())
        meta["video_bytes"] = 1
        js.write_text(json.dumps(meta))
        PupilReview.load(clip).save()
    olds = sorted(p.name for p in tmp.glob("Eye.avi.pupil.json.old*"))
    r.check(len(olds) == 2, f"an earlier backup is never overwritten ({olds})")

    # ── the window mirrors the live tab ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.panel import SettingsPanel
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    for p in list(sidecar_paths(clip)) + list(tmp.glob("*.old*")):
        p.unlink(missing_ok=True)
    panel = SettingsPanel(PupilSettings())
    dlg = PupilReviewDialog(str(clip))
    r.check(isinstance(panel.tracking, TrackingControls)
            and isinstance(dlg._ctl, TrackingControls),
            "both use the one TrackingControls widget")

    def titles(w):
        from PyQt6.QtWidgets import QGroupBox
        return [b._base_title if hasattr(b, "_base_title") else b.title()
                for b in w.findChildren(QGroupBox)]

    r.check(titles(panel.tracking) == titles(dlg._ctl),
            f"same sections in the same order ({titles(dlg._ctl)})")
    r.check(not dlg._ctl._chk_track.isVisibleTo(dlg)
            and dlg._ctl._spn_smooth_win.isEnabled(),
            "review hides 'Track the pupil'; smoothing is usable")

    # Region: box dragged -> numbers; numbers typed -> box.
    dlg._region.setPos((30, 20))
    dlg._region.setSize((90, 70))           # finish=True fires the drag slot
    r.check(dlg._ctl.region() == (30.0, 20.0, 120.0, 90.0),
            f"dragging the box updates X0..Y1 ({dlg._ctl.region()})")
    dlg._ctl.set_limit(35, 20, 120, 90)
    p = dlg._region.pos()
    r.check(abs(p.x() - 35) < 1e-6, f"a set region moves the box ({p.x()})")
    r.check(dlg.review.settings.limit_x0 == 35.0, "and the review follows")
    r.check(not hasattr(dlg._ctl, "_chk_region"),
            "no Eye region check box: the box is always on")
    dlg._ctl.set_limit(0, 0, 0, 0)
    r.check(dlg._region is not None and abs(dlg._region.pos().x() - 35) < 1e-6
            and dlg.review.settings.limit_x0 == 35.0,
            "an empty region is ignored; the box stays")

    # Display row and the LUT's own Auto box are one setting, as live.
    dlg._chk_auto.setChecked(False)
    r.check(not dlg._chk_auto_contrast.isChecked(),
            "the box over the LUT and 'Auto contrast' move together")
    dlg._chk_lut.setChecked(False)
    r.check(not dlg._hist.isVisibleTo(dlg), "Show LUT hides the bar")
    dlg._chk_auto.setChecked(True)
    dlg._chk_lut.setChecked(True)

    # ── suspects are highlighted ──
    r.check(not dlg._suspects and dlg._lbl_sus.text() == "",
            "nothing is flagged before the first track")
    dlg._ctl.set_limit(40, 30, 120, 90)
    dlg._track_all()
    for _ in range(100):
        pump(app, 0.05)
        if dlg._worker is None:
            break
    r.check(dlg._suspects == [6, 7, 15], f"suspects found ({dlg._suspects})")
    shown = [b.getRegion() for b in dlg._sus_bands if b.isVisible()]
    r.check(shown == [(5.5, 7.5), (14.5, 15.5)],
            f"one red band per run of suspect frames ({shown})")
    r.check(len(dlg._sus_pts.data) == 3, "and a mark on each")
    r.check("3 to check" in dlg._lbl_sus.text(), dlg._lbl_sus.text())
    # ── Stabilize: auto fits only, edits kept as drawn ──
    rev2 = dlg.review
    rev2.auto[4:6, 2:4] += 6.0                  # a jumpy pair of fits
    rev2.set_manual(5, PupilFit(80, 60, 30, 30, 0))
    before = rev2.radius().copy()
    dlg._ctl._chk_smooth.setChecked(True)
    dlg._ctl._spn_smooth_win.setValue(5)
    after = rev2.radius()
    r.check(abs(after[4] - before[4]) > 0.5 and after[5] == 30.0,
            f"Stabilize smooths auto fits ({before[4]:.1f}->{after[4]:.1f}) and "
            f"leaves a hand edit alone ({after[5]})")
    r.check(not rev2.stale, "and needs no re-track")
    dlg._ctl._chk_smooth.setChecked(False)
    r.check(np.allclose(rev2.radius(), before, equal_nan=True), "off restores")
    rev2.clear_manual(5)
    rev2.auto[4:6, 2:4] -= 6.0

    # ── pins in review ──
    dlg._btn_pin_cr.setChecked(True)
    dlg.toggle_pin(90.0, 55.0)
    pins = dlg.review.settings.cr_pins
    r.check(len(pins) == 1 and dlg.review.stale
            and dlg._ctl._btn_pins_clear.isEnabled()
            and npoints(dlg._pin_curve) > 0,
            f"Pin reflection pins one, draws it, and asks for a re-track ({pins})")
    dlg.toggle_pin(90.0, 55.0)
    r.check(not dlg.review.settings.cr_pins and npoints(dlg._pin_curve) == 0,
            "clicking it again unpins")
    dlg._btn_pin_cr.setChecked(False)

    dlg.goto(10)
    dlg._jump_suspect(-1)
    r.check(dlg._frame == 7, f"◀ Suspect goes back ({dlg._frame})")
    dlg._next_suspect()
    r.check(dlg._frame == 15, f"Suspect ▶ goes forward ({dlg._frame})")
    dlg._place_new()
    shown = [b.getRegion() for b in dlg._sus_bands if b.isVisible()]
    r.check(15 not in dlg._suspects and shown == [(5.5, 7.5)],
            f"fixing a frame clears its band ({shown})")
    dlg._dirty = False
    dlg.close()
    return r.finish()


def _part_seed() -> int:  # noqa: PLR0915 — one linear scenario
    """When Auto is unsure, the user marks the pupil on a few frames."""
    r = Report("pupil-seed")
    from acqApp.devices.pupil_cam.autotune import autotune

    # Auto pins a reflection that stays put while the eye moves, not one
    # that moves with it.
    def glinted(i, moving):
        f = face_frame(cx=200 + 2 * i, seed=i, glint=False)
        Y, X = np.ogrid[:f.shape[0], :f.shape[1]]
        gx = (175 + 2 * i) if moving else 178
        f[(X - gx) ** 2 + (Y - 125) ** 2 < 16] = 235
        return f
    fixed = autotune([glinted(i, False) for i in range(8)], (100, 60, 320, 240))
    r.check(fixed is not None and len(fixed.pins) == 1
            and abs(fixed.pins[0][0] - 178) < 3 and abs(fixed.pins[0][1] - 125) < 3,
            f"a fixed reflection is pinned ({fixed and fixed.pins})")
    st = fixed.apply(PupilSettings(cr_pins=[(1.0, 2.0, 3.0)]))
    r.check(list(st.cr_pins) == [tuple(fixed.pins[0])], "apply() sets the pins")
    moving = autotune([glinted(i, True) for i in range(8)], (100, 60, 320, 240))
    r.check(moving is not None and not moving.pins
            and moving.apply(PupilSettings(cr_pins=[(1.0, 2.0, 3.0)])).cr_pins
            == [(1.0, 2.0, 3.0)],
            "one that moves is not, and hand-placed pins are kept")

    # A dark distractor (a round shadow, darker than the pupil) wins unaided.
    def tricky(seed):
        f = face_frame(seed=seed)
        Y, X = np.ogrid[:f.shape[0], :f.shape[1]]
        f[(X - 360) ** 2 + (Y - 60) ** 2 < 18 ** 2] = 8
        return f

    frames = [tricky(i) for i in range(5)]
    alone = autotune(frames, (0, 0, 420, 300))
    r.info(f"unaided: {alone and (alone.threshold, alone.notes)}")
    clicked = autotune(frames, (0, 0, 420, 300), [(210, 150, None)] * 5)
    r.check(clicked is not None and 20 <= clicked.threshold < 26,
            f"a clicked centre steers it to the pupil "
            f"({clicked and clicked.threshold})")
    drawn = autotune(frames, None, [(210, 150, 30.0)] * 5)
    r.check(drawn is not None and 20 <= drawn.threshold < 26
            and drawn.region is not None,
            f"a drawn circle sets the threshold and the region around it "
            f"({drawn and (drawn.threshold, drawn.region)})")
    if drawn is not None and drawn.region is not None:
        x0, y0, x1, y1 = drawn.region
        r.check(x0 < 210 < x1 and y0 < 150 < y1 and x1 - x0 < 300,
                f"…a box around the marks ({drawn.region})")
    skipped = autotune(frames, (0, 0, 420, 300),
                       [None, (210, 150, None), None, (210, 150, None), None])
    r.check(skipped is not None and 20 <= skipped.threshold < 26,
            "skipped frames (None) are left out")

    # ── the review window asks, the user marks, Auto applies ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam import review_dialog as rd
    tmp = Path(tempfile.mkdtemp(prefix="pupil_seed_"))
    clip = write_avi(tmp / "s.avi", [tricky(i).tobytes() for i in range(10)],
                     420, 300, b"Y800", 8)
    rd.NEEDS_HELP = 2.0                 # any unaided answer counts as unsure
    try:
        dlg = rd.PupilReviewDialog(str(clip))
        dlg.show()
        dlg._ctl._spn_thr.setValue(200)
        dlg._auto()
        for _ in range(100):
            pump(app, 0.05)
            if dlg._auto_worker is None:
                break
        r.check(dlg.seeding and dlg._seed_bar.isVisible()
                and "click" in dlg._lbl_seed.text(),
                f"an unsure Auto asks for marks ({dlg._lbl_seed.text()[:40]})")
        r.check(dlg._frame == dlg._seed_frames[0] and dlg._roi is None,
                "it shows the first frame to mark, without the edit handle")
        r.check(not dlg._btn_seed_next.isEnabled(), "Next waits for a click")

        class Click:
            def __init__(self, x, y):
                self._p = dlg._vb.mapViewToScene(QPointF(x, y))

            def button(self):
                return Qt.MouseButton.LeftButton

            def scenePos(self):
                return self._p

            def accept(self):
                pass

        frames_seen = []
        for k in range(len(dlg._seed_frames)):
            frames_seen.append(dlg._frame)
            if k == 1:
                dlg._seed_next(skip=True)
                continue
            dlg._on_image_click(Click(210, 150))
            if k == 2:      # draw this one: resize the circle
                dlg._seed_roi.setSize((60, 60))
            r.check(dlg._btn_seed_next.isEnabled() or k == 1, f"click {k} placed")
            dlg._seed_next()
        r.check(frames_seen == dlg._seed_frames,
                f"each marked frame is shown in turn ({frames_seen})")
        r.check(dlg._seed_marks[1] is None and dlg._seed_marks[0][2] is None
                and dlg._seed_marks[2][2] is not None,
                "a skip, a click and a drawn circle are told apart")
        for _ in range(100):
            pump(app, 0.05)
            if dlg._auto_worker is None:
                break
        r.check(20 <= dlg._ctl._spn_thr.value() < 26 and not dlg.seeding
                and "Auto:" in dlg._prog.text(),
                f"the marks give a threshold ({dlg._ctl._spn_thr.value()}): "
                f"{dlg._prog.text()[:50]}")
        r.check(dlg._ctl._btn_auto.text() == "Auto"
                and dlg._ctl._btn_auto.isEnabled(), "Auto is back")
        # Cancel leaves everything as it was.
        thr = dlg._ctl._spn_thr.value()
        dlg._seed_start()
        dlg._seed_cancel()
        r.check(not dlg.seeding and dlg._ctl._spn_thr.value() == thr
                and dlg._btn_open.isEnabled(), "Cancel changes nothing")
        dlg._dirty = False
        dlg.close()
    finally:
        rd.NEEDS_HELP = 0.25

    # ── the live tab: an unsure answer asks for clicks; Auto again cancels ──
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
    m._on_auto_done(None)
    r.check(m._seed_clicks == [] and "click" in win.messages[-1]
            and m.panel.tracking._btn_auto.text() == "click pupil",
            f"live: no answer asks for clicks ({win.messages[-1][:50]})")
    m._on_auto_requested()
    r.check(m._seed_clicks is None and m.panel.tracking._btn_auto.text() == "Auto",
            "live: Auto again cancels")
    return r.finish()

def _part_apply() -> int:
    """A changed setting shows on the current frame at once; Apply to all
    frames keeps it; Revert goes back to the previous trace."""
    r = Report("pupil-apply")
    from acqApp.devices.pupil_cam import review as review_mod

    review_mod.PupilTracking = DiscTracking
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    tmp = Path(tempfile.mkdtemp(prefix="pupil_apply_"))
    # A soft-edged disc, so the threshold moves the radius.
    H, W = 120, 160
    Y, X = np.ogrid[:H, :W]
    d = np.sqrt((X - 80) ** 2 + (Y - 60) ** 2)
    frame = np.clip(20 + np.maximum(0, d - 10) * 6, 0, 190).astype(np.uint8)
    clip = write_avi(tmp / "a.avi", [frame.tobytes()] * 8, W, H, b"Y800", 8)
    dlg = PupilReviewDialog(str(clip))
    dlg._ctl.set_limit(40, 30, 120, 90)
    dlg._ctl._spn_thr.setValue(60)

    def settle():
        for _ in range(40):
            pump(app, 0.05)
            if dlg._worker is None and not dlg._preview_timer.isActive():
                return

    settle()
    r.check(dlg._preview is not None and dlg._preview[0] == dlg._frame
            and "preview" in dlg._lbl_state.text(),
            f"before any Apply, the shown frame is fitted ({dlg._lbl_state.text()})")
    dlg._track_all()
    settle()
    r1 = float(dlg.review.radius()[3])
    r.check(dlg._preview is None and not dlg._btn_revert.isEnabled(),
            "after Apply: no preview, nothing to revert yet")
    dlg.goto(3)
    dlg._ctl._spn_thr.setValue(100)
    settle()
    prev = dlg._preview
    r.check(prev is not None and prev[0] == 3 and prev[1].radius > r1 + 1,
            f"a new threshold shows on the current frame at once "
            f"({r1:.1f} -> {prev and prev[1].radius:.1f})")
    r.check(float(dlg.review.radius()[3]) == r1,
            "…without touching the saved trace")
    dlg.goto(5)
    settle()
    r.check(dlg._preview is not None and dlg._preview[0] == 5,
            "moving to another frame previews that one")
    r.check(dlg._btn_track.text() == "Apply to all frames", "the button's name")
    dlg._track_all()
    settle()
    r2 = float(dlg.review.radius()[3])
    r.check(r2 > r1 + 1 and dlg._btn_revert.isEnabled(),
            f"Apply keeps it for every frame ({r2:.1f}); Revert is offered")
    # The live view's overlays: removed reflections in red, and the views.
    class MaskTracking(DiscTracking):
        def __init__(self):
            self.last_mask = np.ones((60, 80), bool)
            self.last_box = (40, 30, 120, 90)
    review_mod.PupilTracking = MaskTracking
    dlg._ctl._chk_cr.setChecked(False)      # on by default: toggle to redraw
    dlg._ctl._chk_cr.setChecked(True)
    settle()
    r.check(dlg._mask is not None and dlg._mask_img.image is not None,
            "with removal on, what it removed is painted on the clip")
    r.check(not hasattr(dlg._ctl, "_chk_cr_mask"), "…with no check box for it")
    # Playing: every frame gets its overlay, not just where it stops.
    dlg._spn_rate.setValue(200.0)
    dlg.goto(0)
    painted = []
    real_draw = dlg._draw_mask
    dlg._draw_mask = lambda: (painted.append(dlg._frame), real_draw())[1]
    dlg.toggle_play()
    for _ in range(40):
        pump(app, 0.05)
        if not dlg.playing:
            break
    dlg._draw_mask = real_draw
    r.check(set(range(1, 8)) <= set(painted),
            f"playing overlays every frame ({sorted(set(painted))})")
    dlg._ctl._chk_cr.setChecked(False)
    settle()
    r.check(dlg._wants_mask(),
            "reflections off, whiskers still painted: the overlay stays wanted")
    dlg._ctl._chk_whiskers.setChecked(False)
    settle()
    r.check(dlg._mask is None and dlg._mask_img.image is None,
            "reflection and whisker removal off: nothing painted")
    review_mod.PupilTracking = DiscTracking
    dlg._cmb_view.setCurrentIndex(dlg._cmb_view.findData("crop"))
    rect = dlg._img.mapRectToParent(dlg._img.boundingRect())
    r.check(abs(rect.x() - 40) < 1 and abs(rect.width() - 80) < 1,
            f"Cropped to region shows just the region ({rect})")
    dlg._cmb_view.setCurrentIndex(dlg._cmb_view.findData("bare"))
    r.check(not dlg._fit_curve.isVisible(), "Full, no overlay hides the fit")
    dlg._cmb_view.setCurrentIndex(0)
    dlg._ctl._chk_cr.setChecked(False)
    settle()

    dlg._revert()
    r.check(float(dlg.review.radius()[3]) == r1
            and dlg._ctl._spn_thr.value() == 60 and not dlg.review.stale,
            "Revert restores the previous trace and its settings")
    r.check(not dlg._btn_revert.isEnabled(), "…once; then there is nothing older")
    dlg._dirty = False
    dlg.close()
    return r.finish()

def _part_mode() -> int:
    """Live and Review share the Pupil tab, switched by a segmented control."""
    r = Report("pupil-mode")
    app = qt_app()
    isolate_user_state()
    from acqApp.adapters.pupil_cam import PupilCamModule
    from acqApp.devices.pupil_cam.review_dialog import ReviewWidget
    from acqApp.widgets import SegmentedSwitch

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.docks: list = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

        def register_pg_view(self, _v) -> None:
            pass

        def add_dock(self, title, widget, *_a, **_k) -> None:
            self.docks.append((title, widget))

        def is_recording(self) -> bool:
            return False

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    m.build_views()
    r.check(isinstance(m.panel.mode, SegmentedSwitch)
            and m.panel.mode.value() == "live" and m.mode() == "live",
            "the tab opens in Live, with a Live | Review switch")
    m.panel.mode.button("review").click()
    r.check(m.mode() == "review" and isinstance(m._review, ReviewWidget)
            and m.panel._pages.currentWidget() is m.panel.review_page
            and m._view_stack.currentWidget() is m._review.view_widget,
            "Review shows the clip's controls in the panel and the clip in "
            "the same dock")
    r.check(m._review.side_widget.parent() is not None
            and m._review._ctl.isVisibleTo(m.panel.review_page),
            "…the same tracking controls, now for the clip")
    m.panel.mode.button("live").click()
    r.check(m.mode() == "live" and m.panel._pages.currentIndex() == 0,
            "back to Live")
    review = m._review
    m.panel.mode.button("review").click()
    r.check(m._review is review, "the review (and its clip) is kept between visits")

    # Live view takes the tab back; Review is refused while the camera runs.
    m.build_session(True)
    m.start()
    pump(app, 0.2)
    r.check(m.mode() == "live", "starting Live view returns to Live")
    m.panel.mode.button("review").click()
    r.check(m.mode() == "live" and m.panel.mode.value() == "live"
            and any("stop Live view" in x for x in win.messages),
            "Review is refused while the camera runs, and says why")
    m.stop()
    m.panel.mode.button("review").click()
    r.check(m.mode() == "review", "…and allowed once it stops")
    r.check(m.busy_reason() == "", "nothing fitting: the module set may change")
    m.close_controller()
    return r.finish()

def _part_recorded() -> int:
    """Review reads what acqApp records (the session folder's .avi, or the
    .tiff older sessions have) and opens the newest recording by itself."""
    r = Report("pupil-recorded")
    from acqApp.acq.writer import SessionWriter
    from acqApp.devices.pupil_cam.clip import open_clip, recorded_clip

    tmp = Path(tempfile.mkdtemp(prefix="pupil_rec_"))
    frames = [video_eye_frame(64, 96, 40 + i, 30, 10) for i in range(6)]

    sess = tmp / "sess"
    w = SessionWriter({"pupil_cam": "avi"})
    w.open(sess, {})
    for i, f in enumerate(frames):
        w.write("pupil_cam", i / 30.0, f)
    w.write("wheel", 0.0, 1.0)
    w.close()
    clip_avi = recorded_clip(sess)
    rd = open_clip(clip_avi)
    r.check(clip_avi is not None and clip_avi.name == "sess_pupil_cam.avi"
            and len(rd) == 6 and abs(rd.hz - 30.0) < 0.5,
            f"a session's pupil .avi is found and reads ({rd.describe()})")
    r.check(np.array_equal(rd.luma(3), frames[3]), "frames come back exact")

    split = tmp / "sess2"                       # an older session: TIFF
    w = SessionWriter()
    w.open(split, {})
    for i, f in enumerate(frames):
        w.write("pupil_cam", i / 30.0, f)
    w.close()
    clip = recorded_clip(split)
    r.check(clip is not None and clip.name == "sess2_pupil_cam.tiff",
            f"an older session's pupil TIFF is found ({clip})")
    rd = open_clip(clip)
    r.check(len(rd) == 6 and np.array_equal(rd.luma(5), frames[5]),
            "and reads")
    r.check(recorded_clip(None) is None and recorded_clip(tmp / "nope") is None,
            "recorded_clip edge cases")

    # The module remembers the recording and Review opens it.
    app = qt_app()
    isolate_user_state()
    from acqApp.adapters.pupil_cam import PupilCamModule

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.path = None

        def status(self, msg): self.messages.append(msg)
        def on_worker_error(self, _m): pass
        def register_pg_view(self, _v): pass
        def add_dock(self, *_a, **_k): pass
        def is_recording(self): return False
        def recording_path(self): return self.path

    class FakeRec:
        def put(self, *_a, **_k): pass

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    m.build_views()
    m.build_session(True)
    win.path = sess
    m.attach_sink(FakeRec())
    m.detach_sink()
    m.stop()
    r.check(m._last_recording == sess, "recording with the pupil camera is remembered")
    m.panel.mode.button("review").click()
    rv = m._review
    r.check(rv.review is not None and rv.review.video == clip_avi
            and any("newest recording" in x for x in win.messages),
            "Review opens it by itself")
    rv._dirty = True
    m._last_recording = split
    m.panel.mode.button("live").click()
    m.panel.mode.button("review").click()
    r.check(rv.review.video == clip_avi and "save or discard" in win.messages[-1],
            "unsaved edits on the open clip are not thrown away for it")
    rv._dirty = False
    m.panel.mode.button("live").click()
    m.panel.mode.button("review").click()
    r.check(rv.review.video == clip, "…and once saved/discarded the newest opens")
    m.close_controller()
    return r.finish()


def _part_gap() -> int:  # noqa: PLR0915 — one linear scenario
    """Many frames at once: fill or re-track the gap back to the last edit,
    and undo it."""
    r = Report("pupil-gap")
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.review import PupilReview
    review_mod.PupilTracking = DiscTracking

    tmp = Path(tempfile.mkdtemp(prefix="pupil_gap_"))
    H, W, N = 120, 160, 12
    frames = [video_eye_frame(H, W, 50 + 4 * i, 60, 14) for i in range(N)]
    clip = write_avi(tmp / "eye.avi", [f.tobytes() for f in frames], W, H,
                     b"Y800", 8)
    st = PupilSettings(limit_x0=20, limit_y0=20, limit_x1=140, limit_y1=100,
                       cr_remove=False)

    # ── interpolate between two edits ──
    rev = PupilReview(clip, st)
    r.check(rev.gap_before(6) is None, "control: no edit before, no gap")
    rev.set_manual(2, PupilFit(10.0, 20.0, 10.0, 8.0, 170.0))
    rev.set_manual(6, PupilFit(30.0, 40.0, 20.0, 16.0, 10.0))
    r.check(rev.gap_before(6) == (2, 6) and rev.gap_before(3) is None,
            f"the gap runs back to the last edit ({rev.gap_before(6)}); "
            f"none with nothing between")
    n = rev.interpolate(2, 6)
    t = rev.table()
    r.check(n == 3 and rev.edited[3:6].all(),
            f"the 3 frames between become edits ({n})")
    r.check(np.allclose(t[4, :4], [20.0, 30.0, 15.0, 12.0]),
            f"midway is halfway in centre and size ({np.round(t[4, :4], 2)})")
    r.check(abs(t[3, 4] - 175.0) < 1e-6 and abs(t[4, 4]) < 1e-6
            and abs(t[5, 4] - 5.0) < 1e-6,
            f"the angle turns the short way, through 180/0 "
            f"({np.round(t[3:6, 4], 1)}), not back through 90")
    r.check(rev.undo_edits() and not rev.edited[3:6].any()
            and rev.edited[2] and rev.edited[6],
            "Undo takes the fill back, leaving both anchors")
    r.check(not rev.undo_edits(), "…and with nothing left, says so")
    try:
        rev.interpolate(6, 9)               # frame 9: never tracked, no fit
        r.check(False, "an end with no ellipse must be refused")
    except ValueError:
        r.check(not rev.edited[7:10].any(),
                "an end with no ellipse is refused, nothing changed")

    # ── re-track the gap with other settings; kept through a full track ──
    rev.track_all()
    rev.manual[:] = np.nan
    rev.set_manual(2, rev.fit_at(2))        # Keep auto fit: the anchor
    before = rev.manual.copy()
    r.check(not rev.retrack_range(2, 8, should_stop=lambda: True)
            and np.array_equal(rev.manual, before, equal_nan=True),
            "a stopped re-track changes nothing")
    seen: list = []
    r.check(rev.retrack_range(2, 8, lambda i, n: seen.append((i, n))),
            "re-track runs to the end")
    r.check(rev.edited[3:9].all() and not rev.edited[9:].any()
            and seen[-1] == (6, 6),
            f"frames 3-8 (8 not an edit) are re-fitted as edits ({seen[-1:]})")
    r.check(all(abs(rev.table()[i, 0] - (50 + 4 * i)) < 1.5 for i in range(3, 9)),
            "…each on its own frame's pupil")
    rev.settings = PupilSettings(**{**vars(st), "track_threshold": 5})
    rev.track_all()                         # selects nothing
    r.check(not np.isnan(rev.table()[3:9, 0]).any()
            and np.isnan(rev.table()[9:, 0]).all(),
            "a later Apply to all keeps the re-tracked gap (control: the "
            "frames after it lose their fits)")
    r.check(rev.undo_edits() and not rev.edited[3:9].any(),
            "Undo takes the re-track back too")

    # ── the buttons ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    clip2 = write_avi(tmp / "eye2.avi", [f.tobytes() for f in frames], W, H,
                      b"Y800", 8)
    dlg = PupilReviewDialog(str(clip2), settings=st, busy=lambda: False)
    dlg._track_all()
    for _ in range(100):
        pump(app, 0.05)
        if dlg._worker is None:
            break
    dlg.goto(5)
    r.check(not dlg._btn_fill.isEnabled() and not dlg._btn_retrack.isEnabled(),
            "with no edit before, Fill and Re-track gap are off")
    dlg.goto(1)
    dlg._pin_current()
    dlg.goto(5)
    r.check(dlg._btn_fill.isEnabled() and dlg._btn_retrack.isEnabled()
            and "Frames 2–5" in dlg._btn_fill.toolTip(),
            f"after an edit they're on, naming the gap ({dlg._btn_fill.toolTip()!r})")
    dlg._btn_fill.click()
    r.check(dlg.review.edited[2:6].all() and dlg._dirty
            and "filled 3 frames" in dlg._prog.text(),
            f"Fill gap fills it ({dlg._prog.text()!r})")
    r.check(dlg._btn_undo_gap.isEnabled(), "…and Undo is offered")
    dlg._btn_undo_gap.click()
    r.check(not dlg.review.edited[2:5].any(), "Undo empties it again")
    dlg.goto(8)
    dlg._btn_retrack.click()
    r.check(dlg._worker is not None and not dlg._btn_fill.isEnabled(),
            "Re-track gap runs off the GUI thread, the gap buttons locked")
    for _ in range(100):
        pump(app, 0.05)
        if dlg._worker is None:
            break
    r.check(dlg.review.edited[2:9].all()
            and "re-tracked frames 2–8" in dlg._prog.text(),
            f"…and re-fits frames 2-8 ({dlg._prog.text()!r})")
    dlg._dirty = False
    dlg.close()
    pump(app, 0.05)
    return r.finish()


def _part_glare() -> int:
    """The red over removed reflections, on a frame jumped to, is what a run
    through the clip removed there — not a guess from a cold tracker. Real
    EyeLoop: reflections are searched around the previous fit."""
    r = Report("pupil-glare")
    from acqApp.devices.pupil_cam.eyeloop_tracker import EYELOOP_DIR
    if not (EYELOOP_DIR / "eyeloop").is_dir():
        print(f"[pupil-glare] no EyeLoop clone at {EYELOOP_DIR} — skipped")
        return r.finish()
    from acqApp.devices.pupil_cam.review import PupilReview
    from acqApp.devices.pupil_cam.tracking import PupilTracking

    tmp = Path(tempfile.mkdtemp(prefix="pupil_glare_"))
    H, W, N, AT = 120, 220, 24, 20

    def eye(cx: int, cy: int = 60, r: int = 16) -> np.ndarray:
        """A dim eye (under the reflection threshold) whose pupil holds one
        small bright reflection; it starts where EyeLoop's walk does."""
        Y, X = np.ogrid[:H, :W]
        f = np.full((H, W), 90, np.uint8)
        f[(X - cx) ** 2 + (Y - cy) ** 2 < r * r] = 20
        f[(X - cx - 5) ** 2 + (Y - cy + 3) ** 2 < 9] = 250
        return f

    frames = [eye(110 + 3 * i) for i in range(N)]    # drifts 3 px a frame
    clip = write_avi(tmp / "eye.avi", [f.tobytes() for f in frames], W, H,
                     b"Y800", 8)
    st = PupilSettings(limit_x0=10, limit_y0=10, limit_x1=210, limit_y1=110,
                       cr_remove=True, track=True)

    run = PupilTracking()
    for i in range(AT + 1):
        truth_fit = run.track(frames[i], st)
    truth = run.last_mask
    if not r.check(truth_fit is not None and truth is not None and truth.any(),
                   f"fixture: a run through the clip fits frame {AT} and "
                   f"blanks its reflection"):
        return r.finish()

    rev = PupilReview(clip, st)
    rev.track_all()
    r.check(not np.isnan(rev.auto[:, 0]).any(), "fixture: every frame tracked")
    rev.preview_fit(2)                       # the preview was elsewhere...
    rev.preview_fit(AT)                      # ...then a jump
    r.check(rev.last_mask is not None and np.array_equal(rev.last_mask, truth),
            f"after a jump the red is exactly what the run removed "
            f"({0 if rev.last_mask is None else int(rev.last_mask.sum())} vs "
            f"{int(truth.sum())} px)")

    cold = PupilReview(clip, st)             # no run to start from
    cold.preview_fit(2)
    cold.preview_fit(AT)
    got = 0 if cold.last_mask is None else int(cold.last_mask.sum())
    r.check(cold.last_mask is None or not np.array_equal(cold.last_mask, truth),
            f"control: warming up from where the preview last was misses it "
            f"({got} px)")
    return r.finish()


PARTS = {
    "review": _part_review,
    "glare": _part_glare,
    "gap": _part_gap,
    "launcher": _part_launcher,
    "safety": _part_review_safety,
    "mirror": _part_mirror,
    "seed": _part_seed,
    "apply": _part_apply,
    "mode": _part_mode,
    "recorded": _part_recorded,
}

if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
