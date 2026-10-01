"""The pupil-tracking controls: eye region, fit, blinks, reflections.

One widget for both the live panel and Pupil review, so the two match. What
differs is the `live` flag, not a second copy. The eye region is a check box
here; the box itself is drawn and dragged on the image by the host.
"""
from __future__ import annotations

import dataclasses

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from acqApp.widgets import spin
from acqApp.devices.pupil_cam.settings import PupilSettings

HINT_STYLE = "color:#9aa0a6;"
_NO_REGION = (0.0, 0.0, 0.0, 0.0)


def hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(HINT_STYLE)
    return lbl


def _valid(r) -> bool:
    return r[2] > r[0] and r[3] > r[1]


class TrackingControls(QWidget):
    """`changed` fires on any edit; `settings_into(s)` reads the knobs into a
    copy of `s`; `show_settings(s)` puts `s` back without firing.
    `region_wanted` asks the host for a starting box (the check box was
    ticked with none to restore)."""

    changed = pyqtSignal()
    auto_requested = pyqtSignal()
    region_wanted = pyqtSignal()

    def __init__(self, s: PupilSettings, *, live: bool, parent=None) -> None:
        super().__init__(parent)
        self._live = live
        self._pins = list(s.cr_pins)
        self._region = (s.limit_x0, s.limit_y0, s.limit_x1, s.limit_y1)
        self._last_region = self._region if _valid(self._region) else None
        self._quiet = True          # widgets emit as they're built
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._build_track(s))
        root.addWidget(self._build_more(s))
        root.addWidget(self._build_cr(s))
        self._quiet = False

    # ── the fit ─────────────────────────────────────────────────────────────
    def _build_track(self, s: PupilSettings) -> QGroupBox:
        box = QGroupBox("Pupil tracking")
        box.setToolTip("Start with Auto, then nudge Threshold until the green "
                       "outline hugs the pupil.")
        vb = QVBoxLayout(box)
        vb.setSpacing(4)

        self._chk_track = QCheckBox("Track the pupil")
        self._chk_track.setChecked(s.track)
        self._chk_track.setToolTip("Needs the EyeLoop clone (docs/EYELOOP.md).")
        # Review always tracks. Left out of the layout rather than hidden: a
        # folding group box re-shows every child it holds.
        if self._live:
            vb.addWidget(self._chk_track)

        self._chk_region = QCheckBox("Eye region")
        self._chk_region.setChecked(_valid(self._region))
        self._chk_region.setToolTip(
            ("Draw it with Set eye region above the preview."
             if self._live else "Drag the cyan box on the clip.")
            + " Tracking only looks inside it, and needs one.")
        self._chk_region.toggled.connect(self._region_toggled)
        vb.addWidget(self._chk_region)

        form = QFormLayout()
        form.setSpacing(4)
        self._spn_thr = spin(1, 254, s.track_threshold, track=False)
        self._spn_thr.setToolTip(
            "Darker than this is pupil. Too high spills into the iris, too "
            "low shrinks inside the pupil.")
        self._btn_auto = QPushButton("Auto")
        self._btn_auto.setToolTip("Suggest Threshold, Blur and Reflections. "
                                  "If unsure it asks you to click the pupil.")
        self._btn_auto.clicked.connect(self.auto_requested)
        thr_row = QHBoxLayout()
        thr_row.setContentsMargins(0, 0, 0, 0)
        thr_row.addWidget(self._spn_thr, 1)
        thr_row.addWidget(self._btn_auto)
        form.addRow("Threshold:", thr_row)

        self._spn_blur = spin(1, 21, s.track_blur, step=2, track=False)
        self._spn_blur.setToolTip("Smoothing before the threshold. Raise it "
                                  "for a grainy image.")
        form.addRow("Blur:", self._spn_blur)

        self._cmb_model = QComboBox()
        for label, key in (("Ellipse", "ellipsoid"), ("Circle", "circular")):
            self._cmb_model.addItem(label, key)
        i = self._cmb_model.findData(s.track_model)
        self._cmb_model.setCurrentIndex(i if i >= 0 else 0)
        self._cmb_model.setToolTip("Circle is cheaper and a bit steadier if "
                                   "only size matters.")
        form.addRow("Shape:", self._cmb_model)
        vb.addLayout(form)

        self._chk_track.toggled.connect(self._fire)
        self._cmb_model.currentIndexChanged.connect(self._fire)
        for w in (self._spn_thr, self._spn_blur):
            w.valueChanged.connect(self._fire)
        return box

    # ── steadier output ─────────────────────────────────────────────────────
    def _build_more(self, s: PupilSettings) -> QGroupBox:
        box = QGroupBox("Smoothing and blinks")
        vb = QVBoxLayout(box)
        vb.setSpacing(4)

        self._chk_smooth = QCheckBox("Stabilize outline")
        self._chk_smooth.setChecked(s.smooth)
        self._chk_smooth.setToolTip(
            "Averages recent fits: less jitter, more lag. Also recorded."
            if self._live else
            "Averages neighbouring fits (no lag). Hand edits are kept as drawn.")
        self._spn_smooth_win = spin(1, 30, s.smooth_window, track=False,
                                    suffix=" frames")
        sform = QFormLayout()
        sform.setSpacing(4)
        sform.addRow("Average over:", self._spn_smooth_win)
        vb.addWidget(self._chk_smooth)
        vb.addLayout(sform)

        self._chk_blink = QCheckBox("Detect blinks")
        self._chk_blink.setChecked(s.blink_detect)
        self._chk_blink.setToolTip("Flags sudden radius drops; shaded on "
                                   "the plot and saved.")
        vb.addWidget(self._chk_blink)
        bform = QFormLayout()
        bform.setSpacing(4)
        self._spn_blink_drop = spin(
            0.05, 0.90, s.blink_drop_frac, decimals=2, step=0.05, track=False,
            tooltip="Fraction below the recent baseline that counts.")
        bform.addRow("Drop of:", self._spn_blink_drop)
        self._spn_blink_win = spin(3, 60, s.blink_baseline_window, track=False,
                                   suffix=" frames")
        bform.addRow("Baseline over:", self._spn_blink_win)
        vb.addLayout(bform)

        for w in (self._chk_smooth, self._chk_blink):
            w.toggled.connect(self._fire)
        for w in (self._spn_smooth_win, self._spn_blink_drop, self._spn_blink_win):
            w.valueChanged.connect(self._fire)
        return box

    # ── bright spots on the eye ─────────────────────────────────────────────
    def _build_cr(self, s: PupilSettings) -> QGroupBox:
        box = QGroupBox("Reflections")
        box.setToolTip("Paints over the light's bright spots before fitting; "
                       "what it removes is shown red on the image.")
        vb = QVBoxLayout(box)
        vb.setSpacing(4)

        self._chk_cr = QCheckBox("Remove reflections")
        self._chk_cr.setChecked(s.cr_remove)
        vb.addWidget(self._chk_cr)

        form = QFormLayout()
        form.setSpacing(4)
        self._spn_cr_thr = spin(1, 254, s.cr_threshold, track=False)
        form.addRow("Brighter than:", self._spn_cr_thr)
        self._spn_cr_pad = spin(0, 20, s.cr_pad, track=False, suffix=" px")
        form.addRow("Grow by:", self._spn_cr_pad)
        self._spn_cr_ring = spin(1, 40, s.cr_ring, track=False, suffix=" px")
        form.addRow("Fill from:", self._spn_cr_ring)
        self._spn_cr_reach = spin(
            0.10, 1.20, s.cr_reach, decimals=2, step=0.05, track=False,
            tooltip="Fraction of the ellipse searched. Past ~0.85 it inflates "
                    "the radius.")
        form.addRow("Search out to:", self._spn_cr_reach)
        vb.addLayout(form)


        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        self._lbl_pins = QLabel()
        self._lbl_pins.setStyleSheet(HINT_STYLE)
        self._btn_pins_clear = QPushButton("Clear pins")
        self._btn_pins_clear.setToolTip(
            "Add pins with Pin reflection above the "
            + ("preview" if self._live else "clip")
            + ", then click a reflection that never moves.")
        self._btn_pins_clear.clicked.connect(self.clear_pins)
        prow.addWidget(self._lbl_pins, 1)
        prow.addWidget(self._btn_pins_clear)
        vb.addLayout(prow)
        self._show_pins()

        self._chk_cr.toggled.connect(self._fire)
        for w in (self._spn_cr_thr, self._spn_cr_pad, self._spn_cr_ring,
                  self._spn_cr_reach):
            w.valueChanged.connect(self._fire)
        return box

    # ── API ─────────────────────────────────────────────────────────────────
    def _fire(self, *_a) -> None:
        if not self._quiet:
            self.changed.emit()

    def settings_into(self, s: PupilSettings) -> PupilSettings:
        """`s` with every knob here read back. `track` is only taken from the
        widget live (review always tracks)."""
        x0, y0, x1, y1 = self._region
        kw = dict(
            limit_x0=float(x0), limit_y0=float(y0),
            limit_x1=float(x1), limit_y1=float(y1),
            track_threshold=self._spn_thr.value(),
            track_blur=self._spn_blur.value() | 1,
            track_model=self._cmb_model.currentData(),
            smooth=self._chk_smooth.isChecked(),
            smooth_window=self._spn_smooth_win.value(),
            blink_detect=self._chk_blink.isChecked(),
            blink_drop_frac=self._spn_blink_drop.value(),
            blink_baseline_window=self._spn_blink_win.value(),
            cr_remove=self._chk_cr.isChecked(),
            cr_threshold=self._spn_cr_thr.value(),
            cr_pad=self._spn_cr_pad.value(), cr_ring=self._spn_cr_ring.value(),
            cr_reach=self._spn_cr_reach.value(), cr_pins=list(self._pins))
        if self._live:
            kw["track"] = self._chk_track.isChecked()
        return dataclasses.replace(s, **kw)

    def show_settings(self, s: PupilSettings) -> None:
        """Put `s` in the widgets without firing `changed`. Values are
        clamped by the widgets' own ranges."""
        self._quiet = True
        try:
            for w, v in ((self._spn_blink_drop, s.blink_drop_frac),
                         (self._spn_cr_reach, s.cr_reach)):
                w.setValue(float(v))
            for w, v in ((self._spn_thr, s.track_threshold),
                         (self._spn_blur, s.track_blur),
                         (self._spn_smooth_win, s.smooth_window),
                         (self._spn_blink_win, s.blink_baseline_window),
                         (self._spn_cr_thr, s.cr_threshold),
                         (self._spn_cr_pad, s.cr_pad),
                         (self._spn_cr_ring, s.cr_ring)):
                w.setValue(int(round(float(v))))
            for w, v in ((self._chk_track, s.track), (self._chk_smooth, s.smooth),
                         (self._chk_blink, s.blink_detect),
                         (self._chk_cr, s.cr_remove)):
                w.setChecked(bool(v))
            i = self._cmb_model.findData(s.track_model)
            self._cmb_model.setCurrentIndex(i if i >= 0 else 0)
            self._pins = list(s.cr_pins)
            self._show_pins()
            self._set_region((s.limit_x0, s.limit_y0, s.limit_x1, s.limit_y1))
        finally:
            self._quiet = False

    # ── eye region ──────────────────────────────────────────────────────────
    def region(self) -> tuple[float, float, float, float]:
        return tuple(float(v) for v in self._region)

    def _set_region(self, r) -> None:
        self._region = tuple(float(v) for v in r)
        if _valid(self._region):
            self._last_region = self._region
        self._chk_region.blockSignals(True)
        self._chk_region.setChecked(_valid(self._region))
        self._chk_region.blockSignals(False)

    def _region_toggled(self, on: bool) -> None:
        if on:
            if self._last_region is None:
                self._chk_region.blockSignals(True)
                self._chk_region.setChecked(False)
                self._chk_region.blockSignals(False)
                self.region_wanted.emit()   # the host knows the frame size
                return
            self._region = self._last_region
        else:
            self._region = _NO_REGION
        self._fire()

    def set_limit(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """From the image, as ONE change."""
        self._set_region((x0, y0, x1, y1))
        self._fire()

    def clear_limit(self) -> None:
        self.set_limit(*_NO_REGION)

    # ── pins ────────────────────────────────────────────────────────────────
    def set_pins(self, pins) -> None:
        """From the preview, as ONE change."""
        self._pins = [tuple(float(v) for v in pin) for pin in pins]
        self._show_pins()
        self._fire()

    def clear_pins(self) -> None:
        self.set_pins([])

    def _show_pins(self) -> None:
        n = len(self._pins)
        self._lbl_pins.setText(
            "no pinned reflections" if not n
            else f"{n} pinned reflection{'s' if n > 1 else ''}")
        self._btn_pins_clear.setEnabled(bool(n))

    # ── Auto ────────────────────────────────────────────────────────────────
    def apply_auto(self, s: PupilSettings) -> None:
        """Suggested threshold, blur, reflections and region, as ONE change."""
        self._quiet = True
        try:
            self._spn_thr.setValue(int(s.track_threshold))
            self._spn_blur.setValue(int(s.track_blur))
            self._chk_cr.setChecked(bool(s.cr_remove))
            self._spn_cr_thr.setValue(int(s.cr_threshold))
            self._set_region((s.limit_x0, s.limit_y0, s.limit_x1, s.limit_y1))
            self._pins = list(s.cr_pins)
            self._show_pins()
        finally:
            self._quiet = False
        self.changed.emit()

    def set_auto_busy(self, busy: bool, enabled: bool = True,
                      text: str = "…") -> None:
        self._btn_auto.setEnabled(enabled and not busy)
        self._btn_auto.setText(text if busy else "Auto")
