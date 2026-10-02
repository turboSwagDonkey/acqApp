"""Pupil review: showing a frame (image, levels, fit overlay, the
unapplied-settings preview), playback and the radius trace. Split out of
review_dialog.py."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt

from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit

_LEVELS_EVERY = 10      # while playing, re-derive Auto levels this often
_SUSPECT_BANDS = 300    # most red bands drawn; the rest still get a dot
_PREVIEW_MS = 150       # settle time before re-fitting the shown frame


def _ellipse_xy(fit: PupilFit, n: int = 64):
    th = np.linspace(0, 2 * np.pi, n)
    t = np.radians(fit.angle_deg)
    u, v = fit.semi_major * np.cos(th), fit.semi_minor * np.sin(th)
    return (fit.center_x + u * np.cos(t) - v * np.sin(t),
            fit.center_y + u * np.sin(t) + v * np.cos(t))


class _PlaybackMixin:
    """Frame navigation and display, playback, the preview, the trace."""

    def goto(self, i: int, force: bool = False) -> None:
        rev = self.review
        if rev is None:
            return
        i = max(0, min(int(i), len(rev) - 1))
        if i == self._frame and not force:
            return
        try:
            data = np.ascontiguousarray(rev.reader.luma(i))
        except Exception as e:                          # noqa: BLE001 — bad frame
            self.pause()
            self._prog.setText(f"frame {i} unreadable ({e})")
            return
        self._frame = i
        self._loading = True
        for w in (self._sld, self._spn_frame):
            w.setValue(i)
        self._cursor.setValue(i)
        self._loading = False
        self._data = data
        self._repaint()
        if self._preview is not None and self._preview[0] != i:
            self._preview = None
        if self._mask is not None and self._mask[0] != i:
            self._mask = None
            self._mask_img.clear()
        # No handle while playing: rebuilding it per frame is wasted work.
        self._show_fit(rebuild_roi=not (self.playing or self.seeding))
        if self.playing:
            # Every frame gets its overlay, straight away: the tracker walks
            # on from the frame before, so no warm-up is needed.
            sequential = self._overlay_at is not None and i == self._overlay_at + 1
            self._overlay(i, warmup=0 if sequential else 2)
        else:
            self._want_preview()

    def _repaint(self) -> None:
        """The current frame through the LUT. Auto re-derives percentiles each
        frame when still, every few while playing (as the live view does);
        manual re-passes the LUT bar's own levels."""
        data = self._data
        if data is None:
            return
        if self._chk_auto.isChecked():
            if (self._levels is None or not self.playing
                    or self._level_ctr % _LEVELS_EVERY == 0):
                lo, hi = np.percentile(data[::4, ::4], (1, 99))
                self._levels = (float(lo), float(hi))
            self._level_ctr += 1
            levels = self._levels
        else:
            levels = self._hist.item.getLevels()
        h, w = data.shape
        rect = QRectF(0, 0, w, h)
        if self._view() == "crop" and self.review is not None:
            box = self._read_settings().crop_box(data.shape)
            if box is not None:
                x0, y0, x1, y1 = box
                data = data[y0:y1, x0:x1]
                rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        self._img.setImage(data, autoLevels=False, levels=levels)
        self._img.setRect(rect)

    def _view(self) -> str:
        return self._cmb_view.currentData() or "full"

    def _view_changed(self, *_a) -> None:
        bare = self._view() == "bare"
        for item in (self._fit_curve, self._pin_curve, self._mask_img,
                     self._region, self._roi):
            if item is not None:
                item.setVisible(not bare)
        self._repaint()
        self._vb.autoRange()

    # ── playback ─────────────────────────────────────────────────────────────
    @property
    def playing(self) -> bool:
        return self._timer.isActive()

    def toggle_play(self) -> None:
        if self.review is None:
            return
        if self.playing:
            self.pause()
            return
        if self._frame >= len(self.review) - 1:
            self.goto(0)                    # play from the top after the end
        self._btn_play.setText("Pause")
        self._timer.start(self._interval_ms())
        self.goto(self._frame, force=True)  # drops the handle

    def pause(self) -> None:
        if not self.playing:
            return
        self._timer.stop()
        self._btn_play.setText("Play")
        if self.review is not None:         # the handle comes back
            self.goto(self._frame, force=True)

    def _interval_ms(self) -> int:
        return max(1, int(round(1000.0 / self._spn_rate.value())))

    def _rate_changed(self, *_a) -> None:
        if self.playing:
            self._timer.setInterval(self._interval_ms())

    def _tick(self) -> None:
        last = len(self.review) - 1
        if self._frame >= last:
            if self._chk_loop.isChecked():
                self.goto(0)
            else:
                self.pause()
            return
        self.goto(self._frame + 1)

    # ── the shown frame, with settings not yet applied ───────────────────────
    def _needs_preview(self) -> bool:
        rev = self.review
        return (rev is not None and not rev.is_edited(self._frame)
                and (rev.stale or not rev.tracked)
                and self._read_settings().search_limit() is not None)

    def _wants_mask(self) -> bool:
        """What removal blanks is always shown while it is on."""
        st = self._read_settings() if self.review is not None else None
        return (st is not None and st.cr_remove
                and st.search_limit() is not None)

    def _want_preview(self) -> None:
        if self._needs_preview() or self._wants_mask():
            self._preview_timer.start(_PREVIEW_MS)
        else:
            self._preview_timer.stop()
        if not self._wants_mask():
            self._mask = None
            self._draw_mask()

    def _run_preview(self) -> None:
        if not self.playing:
            self._overlay(self._frame)

    def _overlay(self, i: int, warmup: int = 2) -> None:
        """Fit frame `i` with the current settings and draw it (when they
        aren't applied yet), and what reflection removal blanked there."""
        needs, mask = self._needs_preview(), self._wants_mask()
        if not (needs or mask) or self.seeding or self._worker is not None:
            self._overlay_at = None
            return
        if self._busy():
            self._lbl_state.setText(f"frame {i}: no preview while live "
                                    f"tracking runs")
            return
        try:
            fit = self.review.preview_fit(i, self._read_settings(), warmup=warmup)
        except Exception as e:                          # noqa: BLE001 — no EyeLoop
            self._overlay_at = None
            self._lbl_state.setText(f"frame {i}: no preview ({e})")
            return
        self._overlay_at = i
        if needs:
            self._preview = (i, fit)
            self._show_fit(rebuild_roi=False)
        self._mask = ((i, self.review.last_mask, self.review.last_box)
                      if mask else None)
        self._draw_mask()

    def _draw_mask(self) -> None:
        m = self._mask
        if m is None or m[0] != self._frame or m[1] is None or m[2] is None:
            self._mask_img.clear()
            return
        _i, mask, box = m
        rgba = np.zeros(mask.shape + (4,), np.uint8)
        rgba[..., 0] = 255
        rgba[..., 3] = np.where(mask, 140, 0)
        x0, y0, x1, y1 = box
        self._mask_img.setImage(rgba, autoLevels=False)
        self._mask_img.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    def _show_fit(self, rebuild_roi: bool = True) -> None:
        rev = self.review
        i = self._frame
        fit = rev.fit_at(i)
        edited = rev.is_edited(i)
        if self._preview is not None and self._preview[0] == i and not edited:
            # Unapplied settings: show what they'd give here, no edit handle.
            if self._roi is not None:
                self._vb.removeItem(self._roi)
                self._roi = None
            pfit = self._preview[1]
            self._fit_curve.setPen(pg.mkPen("#ffd166", width=2,
                                            style=Qt.PenStyle.DashLine))
            self._fit_curve.setData(*(_ellipse_xy(pfit) if pfit is not None
                                      else ([], [])))
            self._lbl_state.setText(
                f"frame {i}: preview — " + ("no fit" if pfit is None else
                                            "Apply to all frames to keep"))
            self._btn_reset.setEnabled(False)
            self._btn_pin.setEnabled(False)
            self._btn_new.setEnabled(pfit is None)
            return
        self._fit_curve.setPen(pg.mkPen("#ff9d3d" if edited else "#7fff6a",
                                        width=2))
        if (rebuild_roi or self.playing or self.seeding) and self._roi is not None:
            self._vb.removeItem(self._roi)
            self._roi = None
        if fit is None:
            self._fit_curve.setData([], [])
        else:
            self._fit_curve.setData(*_ellipse_xy(fit))
            if rebuild_roi:
                self._make_roi(fit)
        self._lbl_state.setText(
            f"frame {i}: " + ("hand-edited" if edited
                              else "no fit" if fit is None else "auto"))
        self._btn_reset.setEnabled(edited)
        self._btn_pin.setEnabled(fit is not None and not edited)
        self._btn_new.setEnabled(fit is None)

    def _make_roi(self, fit: PupilFit) -> None:
        """An ellipse handle at `fit`. ROI pos is the unrotated box's corner,
        rotated about itself: centre = pos + R(angle)(a, b)."""
        a, b = fit.semi_major, fit.semi_minor
        t = np.radians(fit.angle_deg)
        c, s = np.cos(t), np.sin(t)
        pos = (fit.center_x - (a * c - b * s), fit.center_y - (a * s + b * c))
        self._roi = pg.EllipseROI(pos, (2 * a, 2 * b), angle=fit.angle_deg,
                                  pen=pg.mkPen("#ffffff", width=1))
        self._roi.sigRegionChangeFinished.connect(self._roi_edited)
        self._roi.setVisible(self._view() != "bare")
        self._vb.addItem(self._roi)

    def _roi_fit(self) -> PupilFit:
        a, b = self._roi.size()[0] / 2.0, self._roi.size()[1] / 2.0
        ang = float(self._roi.angle())
        t = np.radians(ang)
        p = self._roi.pos()
        return PupilFit(float(p.x() + a * np.cos(t) - b * np.sin(t)),
                        float(p.y() + a * np.sin(t) + b * np.cos(t)),
                        float(a), float(b), ang)

    # ── plot ─────────────────────────────────────────────────────────────────
    def _refresh_plot(self) -> None:
        rev = self.review
        if rev is None:
            return
        n = len(rev)
        x = np.arange(n)
        r_auto = np.where(np.isnan(rev.auto[:, 0]), np.nan,
                          (rev.auto[:, 2] + rev.auto[:, 3]) / 2.0)
        self._auto_curve.setData(x, r_auto, connect="finite")
        self._final_curve.setData(x, rev.radius(), connect="finite")
        ed = np.flatnonzero(rev.edited)
        radius = rev.radius()
        self._edit_pts.setData(ed, radius[ed])
        self._draw_suspects(radius)

    def _draw_suspects(self, radius: np.ndarray) -> None:
        """Red bands over runs of suspect frames, a red x on each. Nothing
        before the first track: every frame would be 'no fit'."""
        rev = self.review
        sus = rev.suspects() if rev.tracked or rev.edited.any() else []
        self._suspects = sus
        runs: list[tuple[int, int]] = []
        for i in sus:
            if runs and i == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], i)
            else:
                runs.append((i, i))
        while len(self._sus_bands) < min(len(runs), _SUSPECT_BANDS):
            band = pg.LinearRegionItem(movable=False,
                                       brush=pg.mkBrush(255, 77, 77, 60),
                                       pen=pg.mkPen(None))
            band.setZValue(-10)
            self._plot.addItem(band)
            self._sus_bands.append(band)
        for k, band in enumerate(self._sus_bands):
            if k < len(runs):
                a, b = runs[k]
                band.setRegion((a - 0.5, b + 0.5))
                band.show()
            else:
                band.hide()
        sus_arr = np.asarray(sus, dtype=int)
        y = radius[sus_arr] if sus_arr.size else np.array([])
        # A frame with no fit has no radius: put its x on the axis floor.
        floor = np.nanmin(radius) if np.isfinite(radius).any() else 0.0
        self._sus_pts.setData(sus_arr, np.where(np.isnan(y), floor, y))
        self._lbl_sus.setText(f"{len(sus)} to check" if sus else
                              ("none to check" if rev.tracked else ""))
