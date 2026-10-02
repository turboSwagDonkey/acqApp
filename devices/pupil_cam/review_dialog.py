"""Offline pupil review window: load a clip, tune the tracker, fit every frame,
fix the bad ones by hand. The model is `review.py`; this is only the view.

Drag the ellipse (move / resize / rotate handles) to correct a frame; the radius
trace and the saved table follow. Hand-edited frames are orange on the trace.
Play (or Space) runs the clip so the fit can be watched; the LUT bar and Auto box
beside the image work as in the live pupil view and change only the display.
Auto beside Threshold suggests parameters from frames spread over the clip.

The left column mirrors the live Pupil tab: the same TrackingControls widget
(eye region, tracking, smoothing and blinks, reflections), with the clip in
place of the camera. "Next suspect" walks the frames worth checking (no fit,
or a radius jumping off its neighbours); they are shaded red on the trace.

`ReviewWidget` is the whole thing in two parts, `side_widget` (the
controls) and `view_widget` (clip, playback, trace): standalone it lays them
side by side; embedded (the Pupil tab's Review mode) the host places them.
`PupilReviewDialog` is the standalone window. This file holds the state, Auto,
tracking, hand edits and saving; the layout, the frame display/playback/trace
and Auto's help are mixins in review_layout.py, review_playback.py and
review_seed.py.

Every slot that reads the clip or writes the sidecar is guarded: an exception
escaping a Qt slot aborts the process, which in the rig is the whole app.
"""
from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QMessageBox, QWidget

from acqApp.acq.worker import PullWorker
from acqApp.devices.pupil_cam.autotune import NEEDS_HELP
from acqApp.devices.pupil_cam.clip import FILE_FILTER as _VIDEO_FILTER
from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
from acqApp.devices.pupil_cam.review import PupilReview
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.track_worker import AutoTuneWorker
from acqApp.devices.pupil_cam.review_layout import _LayoutMixin
from acqApp.devices.pupil_cam.review_playback import _PlaybackMixin
from acqApp.devices.pupil_cam.review_seed import _SeedMixin

_AUTO_FRAMES = 24       # frames spread over the clip for Auto
# Threads that outlived a wait, held until they end: dropping a running
# QThread aborts the process. Module-level, so a deleted dialog can't drop them.
_PARKED: list[PullWorker] = []


class _TrackAllWorker(PullWorker):
    """`PupilReview.track_all` off the GUI thread. Cancelled through its own
    flag: PullWorker.run() clears `_stop` on entry, which would lose a Stop
    pressed before the thread got going."""

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(bool)        # False = stopped early

    def __init__(self, review: PupilReview, job=None) -> None:
        """`job(progress, should_stop) -> bool`; default the whole clip."""
        super().__init__()
        self._job = job or review.track_all
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        done = self._job(self.progress.emit, self._cancel.is_set)
        self.finished_ok.emit(done)


class ReviewWidget(_LayoutMixin, _SeedMixin, _PlaybackMixin, QWidget):
    """`busy()` says whether live tracking is running: EyeLoop's config is
    process-global, so fitting a clip then would corrupt the live fits.
    `embedded`: build the two parts but leave placing them to the host."""

    def __init__(self, video: str = "", settings: PupilSettings | None = None,
                 busy: Callable[[], bool] | None = None, parent=None,
                 embedded: bool = False) -> None:
        super().__init__(parent)
        self._embedded = embedded
        self._busy = busy or (lambda: False)
        self._seed = settings
        self.review: PupilReview | None = None
        self._worker: _TrackAllWorker | None = None
        self._auto_worker: AutoTuneWorker | None = None
        self._gap_done: str | None = None     # a gap re-track's closing line
        self._frame = 0
        self._data = None
        self._dirty = False
        self._loading = False        # programmatic widget/ROI updates
        # The eye region was invented on open, not drawn: Auto may move it.
        self._region_default = False
        self._build()
        self._set_running()
        if video:
            self.open_video(video)

    def _set_running(self) -> None:
        """Lock what must not change under a running fit or Auto."""
        track, auto = self._worker is not None, self._auto_worker is not None
        seed = self._seed_frames is not None
        self._btn_open.setEnabled(not (track or auto or seed))
        self._ctl.set_auto_busy(auto or seed, text="…" if auto else "marking",
                                enabled=not track and self.review is not None)
        self._btn_track.setEnabled(not auto)
        self._btn_track.setText("Stop" if track else "Apply to all frames")
        self._btn_revert.setEnabled(not (track or auto or seed)
                                    and self.review is not None
                                    and bool(self.review.history))
        self._refresh_gap()

    # ── loading ──────────────────────────────────────────────────────────────
    def _pick_video(self) -> None:
        if not self._confirm_discard():
            return
        start = str(self.review.video.parent) if self.review else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Pupil recording", start, _VIDEO_FILTER)
        if path:
            self.open_video(path)

    def open_video(self, path: str) -> bool:
        if self._worker is not None or self._auto_worker is not None:
            self._prog.setText("Stop the running job before opening another clip.")
            return False
        try:
            rev = PupilReview.load(path, self._seed)
        except Exception as e:                          # noqa: BLE001 — bad file
            QMessageBox.warning(self, "Pupil review", f"Can't open {path}:\n{e}")
            return False
        self.pause()
        self._btn_pin_cr.setChecked(False)
        self.review = rev
        n = len(rev)
        h, w = rev.reader.height, rev.reader.width
        self._region_default = rev.settings.search_limit() is None
        if self._region_default:
            # Tracking returns nothing without a region.
            rev.settings = dataclasses.replace(
                rev.settings, limit_x0=w * 0.25, limit_x1=w * 0.75,
                limit_y0=h * 0.25, limit_y1=h * 0.75)
            if rev.tracked_with is not None and rev.tracked_with.search_limit() is None:
                rev.tracked_with = rev.settings
        self._data = None
        self._levels = None
        self._loading = True
        self._spn_rate.setValue(rev.reader.hz or rev.settings.rate_hz or 20.0)
        self._lbl_file.setText(Path(path).name)
        for w_ in (self._sld, self._spn_frame):
            w_.setRange(0, max(0, n - 1))
        self._plot.setXRange(0, max(1, n - 1))
        self._loading = False
        self._show_settings(rev.settings)
        self._draw_pins()
        self._dirty = False
        self._show_stale()
        msg = f"{n} frames" + ("" if rev.tracked else " — not tracked yet")
        if rev.sidecar_note:
            msg += f"\n{rev.sidecar_note} (kept as *.old when you save)"
        self._prog.setText(msg)
        self._set_running()
        self.goto(0, force=True)
        self._refresh_plot()
        self._vb.autoRange()
        return True

    def _show_settings(self, st: PupilSettings) -> None:
        """Put `st` in the controls and the region box, without counting it
        as an edit."""
        self._loading = True
        try:
            self._ctl.show_settings(st)
            self._build_region(st)
        finally:
            self._loading = False

    def _build_region(self, st: PupilSettings) -> None:
        if self._region is not None:
            self._vb.removeItem(self._region)
            self._region = None
        if st.search_limit() is None:       # no clip yet
            return
        x0, y0, x1, y1 = st.search_limit()
        self._region = pg.RectROI((x0, y0), (x1 - x0, y1 - y0),
                                  pen=pg.mkPen("#00e5ff", width=2))
        self._region.sigRegionChangeFinished.connect(self._region_dragged)
        self._region.setVisible(self._view() != "bare")
        self._vb.addItem(self._region)

    def _region_dragged(self) -> None:
        """Box dragged on the clip -> the X0..Y1 numbers (ONE change)."""
        if self._loading:
            return
        p, s = self._region.pos(), self._region.size()
        self._region_default = False
        self._ctl.set_limit(p.x(), p.y(), p.x() + s.x(), p.y() + s.y())

    def _sync_region_box(self) -> None:
        """The controls' region (Auto, Revert, a restored clip) -> the box."""
        x0, y0, x1, y1 = self._ctl.region()
        if x1 <= x0 or y1 <= y0:
            if self._region is not None:
                self._vb.removeItem(self._region)
                self._region = None
            return
        if self._region is None:
            self._loading = True
            try:
                self._build_region(self._read_settings())
            finally:
                self._loading = False
            return
        p, s = self._region.pos(), self._region.size()
        if (abs(p.x() - x0) + abs(p.y() - y0) + abs(s.x() - (x1 - x0))
                + abs(s.y() - (y1 - y0))) < 0.5:
            return
        self._loading = True
        try:
            self._region.setPos((x0, y0), finish=False)
            self._region.setSize((x1 - x0, y1 - y0), finish=False)
        finally:
            self._loading = False

    # ── parameters ───────────────────────────────────────────────────────────
    def _read_settings(self) -> PupilSettings:
        return self._ctl.settings_into(self.review.settings)

    def _params_edited(self, *_a) -> None:
        if self._loading or self.review is None:
            return
        self._sync_region_box()
        new = self._read_settings()
        if new != self.review.settings:
            self.review.settings = new
            self._dirty = True
        self._show_stale()
        # Stabilize changes the table without a re-track; pins are drawn.
        self._draw_pins()
        self._refresh_plot()
        self._preview = None
        self._show_fit(rebuild_roi=not (self.playing or self.seeding))
        self._want_preview()

    def _show_stale(self) -> None:
        rev = self.review
        self._lbl_stale.setText(
            "Settings changed — Apply to all frames to keep them."
            if rev is not None and rev.stale else "")

    # ── Auto ─────────────────────────────────────────────────────────────────
    def _auto(self) -> None:
        rev = self.review
        if rev is None or self._worker is not None or self._auto_worker is not None:
            return
        self.pause()
        n = len(rev)
        idx = np.unique(np.linspace(0, n - 1, min(_AUTO_FRAMES, n)).astype(int))
        try:
            frames = [np.array(rev.reader.luma(int(i))) for i in idx]
        except Exception as e:                          # noqa: BLE001 — bad clip
            self._prog.setText(f"Auto: can't read the clip ({e})")
            return
        st = self._read_settings()
        region = None if self._region_default else st.search_limit()
        self._auto_worker = AutoTuneWorker(frames, region)
        self._auto_worker.done.connect(self._on_auto)
        self._auto_worker.error.connect(self._on_auto_error)
        self._prog.setText("finding parameters…")
        self._set_running()
        self._auto_worker.start()

    def _on_auto(self, res) -> None:
        seeded = self._seed_frames is not None
        self._end_auto()
        if not seeded and (res is None or res.confidence < NEEDS_HELP):
            self._seed_start()
            return
        self._seed_frames = None
        self._set_running()
        if res is None:
            self._prog.setText("Auto: still no pupil found there.")
            return
        new = res.apply(self._read_settings())
        self._show_settings(new)
        if res.region is not None:
            self._region_default = False
        self._params_edited()
        self._prog.setText(f"Auto: {res.notes}. Apply to all frames to keep.")

    def _on_auto_error(self, msg: str) -> None:
        self._seed_frames = None
        self._end_auto()
        self._prog.setText(f"Auto failed: {msg}")

    def _end_auto(self) -> None:
        self._park(self._auto_worker)
        self._auto_worker = None
        self._set_running()

    # ── tracking ─────────────────────────────────────────────────────────────
    def _track_all(self) -> None:
        if self.review is None:
            return
        if self._worker is not None:        # the button doubles as Stop
            self._worker.cancel()
            return
        self._start_job(None)

    def _can_fit(self) -> bool:
        if self._busy():
            self._prog.setText("Live tracking is running — stop it first "
                               "(both use the same EyeLoop state).")
            return False
        if PupilReview.fitting():
            self._prog.setText("Another clip is being tracked — wait for it "
                               "(both use the same EyeLoop state).")
            return False
        return True

    def _start_job(self, job) -> None:
        """Fit off the GUI thread: the whole clip (`job` None) or a gap."""
        if not self._can_fit():
            return
        self.pause()
        self.review.settings = self._read_settings()
        self._worker = _TrackAllWorker(self.review, job)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_tracked)
        self._worker.error.connect(self._on_track_error)
        self._set_running()
        self._worker.start()

    def _revert(self) -> None:
        if self.review is None or not self.review.revert():
            return
        self._show_settings(self.review.settings)
        self._draw_pins()
        self._dirty = True
        self._preview = None
        self._show_stale()
        self._set_running()
        self.goto(self._frame, force=True)
        self._refresh_plot()
        self._prog.setText("reverted to the previous trace")

    def _on_progress(self, i: int, n: int) -> None:
        self._prog.setText(f"tracking {i}/{n}")

    def _on_tracked(self, done: bool) -> None:
        self._end_worker()
        if done:
            self._dirty = True
            self._preview = None
            self._prog.setText(self._gap_done or "done")
        else:
            self._prog.setText("stopped — the previous fits are kept")
        self._gap_done = None
        self._show_stale()
        self.goto(self._frame, force=True)
        self._refresh_plot()

    def _on_track_error(self, msg: str) -> None:
        self._end_worker()
        self._gap_done = None
        self._prog.setText(f"Tracking failed — the previous fits are kept.\n{msg}")

    def _end_worker(self) -> None:
        self._park(self._worker)
        self._worker = None
        self._set_running()

    def _park(self, w: PullWorker | None) -> None:
        """Drop a finished worker; hold one still running until it ends."""
        if w is None or w.wait(3000):
            return
        _PARKED.append(w)
        w.finished.connect(lambda w=w: _PARKED.remove(w) if w in _PARKED else None)

    # ── hand edits ───────────────────────────────────────────────────────────
    def _roi_edited(self) -> None:
        if self._loading or self._roi is None:
            return
        self.edit_frame(self._frame, self._roi_fit(), keep_roi=True)

    def edit_frame(self, i: int, fit: PupilFit, keep_roi: bool = False) -> None:
        """Pin frame `i` to `fit` and redraw what depends on it. `keep_roi`:
        the edit came from dragging the handle, which must not be rebuilt
        under the user's mouse."""
        self.pause()
        self.review.set_manual(i, fit)
        self._dirty = True
        if i == self._frame:
            self._show_fit(rebuild_roi=not keep_roi)
        self._refresh_plot()

    def _pin_current(self) -> None:
        fit = self.review.fit_at(self._frame)
        if fit is not None:
            self.edit_frame(self._frame, fit)

    def _reset_current(self) -> None:
        self.review.clear_manual(self._frame)
        self._dirty = True
        self.goto(self._frame, force=True)
        self._refresh_plot()

    def _place_new(self) -> None:
        """Seed an ellipse at the middle of the eye region for a frame the
        tracker missed; then drag it into place."""
        x0, y0, x1, y1 = self._read_settings().search_limit()
        r = max(5.0, min(x1 - x0, y1 - y0) / 6.0)
        self.edit_frame(self._frame, PupilFit((x0 + x1) / 2, (y0 + y1) / 2,
                                              r, r, 0.0))

    # ── the gap back to the last edit: fill or re-track it ──────────────────
    def _gap(self) -> tuple[int, int] | None:
        return None if self.review is None else self.review.gap_before(self._frame)

    def _refresh_gap(self) -> None:
        g = self._gap()
        idle = self._worker is None and self._auto_worker is None
        fit_here = self.review is not None and self.review.fit_at(self._frame) is not None
        self._btn_fill.setEnabled(idle and g is not None and fit_here)
        self._btn_retrack.setEnabled(idle and g is not None)
        self._btn_undo_gap.setEnabled(idle and self.review is not None
                                      and bool(self.review.edit_undo))
        tip = ("" if g is None else f" Frames {g[0] + 1}–{g[1]}, back to the "
               f"edit at {g[0]}.")
        self._btn_fill.setToolTip(
            "Ease the ellipse from the last edited frame to this one." + tip)
        self._btn_retrack.setToolTip(
            "Re-fit from the last edited frame to this one with the current "
            "settings." + tip)

    def _fill_gap(self) -> None:
        g = self._gap()
        if g is None:
            return
        self.pause()
        try:
            n = self.review.interpolate(*g)
        except ValueError as e:
            self._prog.setText(f"can't fill: {e}")
            return
        self._dirty = True
        self._prog.setText(f"filled {n} frames between {g[0]} and {g[1]}")
        self.goto(self._frame, force=True)
        self._refresh_plot()

    def _retrack_gap(self) -> None:
        g = self._gap()
        if g is None or self._worker is not None:
            return
        self._gap_done = f"re-tracked frames {g[0] + 1}–{g[1]}"
        self._start_job(lambda progress, stop: self.review.retrack_range(
            *g, progress, stop))
        if self._worker is None:            # refused
            self._gap_done = None

    def _undo_gap(self) -> None:
        if self.review is None or not self.review.undo_edits():
            return
        self._dirty = True
        self._prog.setText("undid the last gap fill / re-track")
        self.goto(self._frame, force=True)
        self._refresh_plot()

    def _next_suspect(self) -> None:
        self._jump_suspect(+1)

    def _jump_suspect(self, step: int) -> None:
        if self.review is None:
            return
        sus = self._suspects
        nxt = ([i for i in sus if i > self._frame] if step > 0
               else [i for i in sus if i < self._frame][::-1])
        if nxt:
            self.goto(nxt[0])
        else:
            self._prog.setText("no further suspect frames" if step > 0
                               else "no earlier suspect frames")

    # ── saving / closing ─────────────────────────────────────────────────────
    def save(self) -> bool:
        """False (and says why) when the sidecar can't be written."""
        if self.review is None:
            return True
        self.review.settings = self._read_settings()
        try:
            js, npz = self.review.save()
        except Exception as e:                          # noqa: BLE001 — disk
            QMessageBox.warning(self, "Pupil review",
                                f"Couldn't save next to the clip:\n{e}")
            return False
        self._dirty = False
        self._show_stale()
        self._prog.setText(f"saved {js.name}, {npz.name}")
        return True

    def _confirm_discard(self) -> bool:
        if self.review is None or not self._dirty:
            return True
        ans = QMessageBox.question(
            self, "Pupil review", "Save changes to this recording's tracking?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if ans == QMessageBox.StandardButton.Cancel:
            return False
        if ans == QMessageBox.StandardButton.Save:
            return self.save()
        return True

    def can_close(self) -> bool:
        """Ask about running work and unsaved edits. Ask first: a Cancel must
        leave the running work alone."""
        if self._worker is not None:
            ans = QMessageBox.question(
                self, "Pupil review", "Tracking is still running. Stop it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return False
        return self._confirm_discard()

    def shutdown(self) -> None:
        """Stop playback and any running job (after `can_close`)."""
        self._timer.stop()
        self._preview_timer.stop()
        if self._seed_frames is not None:
            self._seed_cancel()
        if self._worker is not None:
            self._worker.cancel()
            self._end_worker()
        if self._auto_worker is not None:
            self._end_auto()

    def reject(self) -> None:
        """Esc: through closeEvent, so nothing unsaved or running is lost."""
        self.close()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape and self.isWindow():
            self.reject()
            return
        super().keyPressEvent(ev)

    def closeEvent(self, ev) -> None:
        if not self.can_close():
            ev.ignore()
            return
        self.shutdown()
        ev.accept()


class PupilReviewDialog(ReviewWidget):
    """The review as its own window (run_pupil_review.py)."""

    def __init__(self, video: str = "", settings: PupilSettings | None = None,
                 busy: Callable[[], bool] | None = None, parent=None) -> None:
        super().__init__(video, settings, busy, parent)
        self.setWindowTitle("Pupil review")
        self.resize(1200, 800)
