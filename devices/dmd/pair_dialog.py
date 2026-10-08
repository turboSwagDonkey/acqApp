"""Draw the DMD ROI set for a just-saved FOV, check it lit on the live camera,
and save it under the paired name ("<base>_roi"; see `routines/pairs.py`).

Hardware arrives as callables, so this never knows which camera or projector
it drives. Illuminate is the one control that emits light, and closing the
dialog always turns it off.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout)

from acqApp import style
from acqApp.devices.dmd import roi_store
from acqApp.devices.dmd.roi import RoiSet
from acqApp.devices.dmd.roi_panel import RoiEditor

_LIVE_MS = 50
_REPROJECT_MS = 150        # coalesces a drag's burst of edits into one upload


class PairRoiDialog(QDialog):
    """Modal. `.path` is the saved ROI set after Save, else None."""

    def __init__(self, editor: RoiEditor, snapshot: np.ndarray, name: str, *,
                 live_source: Callable[[], object],
                 illuminate: Callable[[RoiSet | None], None],
                 set_live: Callable[[bool], bool] | None = None, parent=None):
        """`editor` already shows `snapshot`. `illuminate(rois)` projects
        them, `illuminate(None)` stops. `set_live` starts/stops the camera
        and returns its previous state."""
        super().__init__(parent)
        self._ed = editor
        self._snapshot = snapshot
        self._name = name
        self._live_source = live_source
        self._illuminate = illuminate
        self._set_live = set_live
        self._was_live: bool | None = None     # set once we started the camera
        self._last_frame = None
        self._lit = False
        self.path: Path | None = None

        self.setWindowTitle(f"DMD ROIs for {name}")
        self.setStyleSheet(style.accent_panel("dmd"))
        self.resize(1000, 800)
        self._build()

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(_LIVE_MS)
        self._live_timer.timeout.connect(self._refresh_live)
        self._reproject = QTimer(self)
        self._reproject.setSingleShot(True)
        self._reproject.setInterval(_REPROJECT_MS)
        self._reproject.timeout.connect(self._project)
        self._ed.rois_changed.connect(self._on_rois_changed)
        self._on_rois_changed()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        msg = QLabel(
            "Draw where the DMD should light up in this FOV. Illuminate to see "
            "it on the live camera; keep tweaking until it's right, then Save "
            f'as "{self._name}". Skip saves the FOV alone.')
        msg.setWordWrap(True)
        root.addWidget(msg)
        root.addWidget(self._ed, 1)

        row = QHBoxLayout()
        self._chk_live = QCheckBox("Live camera")
        self._chk_live.setToolTip(
            "Show the camera's live frames instead of the FOV snapshot "
            "(starts Live view if it isn't running).")
        self._chk_live.toggled.connect(self._on_live_toggled)
        row.addWidget(self._chk_live)
        self._btn_light = QPushButton("Illuminate ROIs")
        self._btn_light.setCheckable(True)
        self._btn_light.setStyleSheet(style.toggle_btn("dmd"))
        self._btn_light.toggled.connect(self._on_light_toggled)
        row.addWidget(self._btn_light)
        self._lbl_live = QLabel()
        self._lbl_live.setStyleSheet(f"color:{style.muted()};")
        row.addWidget(self._lbl_live, 1)
        root.addLayout(row)

        self._bb = QDialogButtonBox()
        self._btn_save = self._bb.addButton(
            "Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self._bb.addButton("Skip", QDialogButtonBox.ButtonRole.RejectRole)
        self._bb.accepted.connect(self._save)
        self._bb.rejected.connect(self.reject)
        root.addWidget(self._bb)

        if self._ed.can_project:
            self._btn_light.setToolTip(
                "THIS PROJECTS LIGHT: shows these ROIs on the DMD, and keeps "
                "them in step with your edits until switched off.")
        else:
            self._btn_light.setEnabled(False)
            self._btn_light.setToolTip(
                "No DMD calibration loaded, so ROIs can't be projected.")

    # ── live camera ──────────────────────────────────────────────────────────
    def _on_live_toggled(self, on: bool) -> None:
        if on:
            if self._set_live is not None and self._was_live is None:
                self._was_live = self._set_live(True)
            self._live_timer.start()
            self._refresh_live()
        else:
            self._live_timer.stop()
            self._lbl_live.setText("")
            self._ed.set_image(self._snapshot, keep_view=True)

    def _refresh_live(self) -> None:
        f = self._live_source()
        if f is None or f is self._last_frame:
            return
        self._last_frame = f
        self._ed.set_image(np.asarray(f), keep_view=True)
        self._lbl_live.setText("live")

    # ── light ────────────────────────────────────────────────────────────────
    def _on_light_toggled(self, on: bool) -> None:
        if on:
            self._chk_live.setChecked(True)   # lighting is only for looking
            self._project()
        else:
            self._reproject.stop()
            self._stop_light()

    def _project(self) -> None:
        if self._btn_light.isChecked():
            self._illuminate(self._ed.roi_set)
            self._lit = True

    def _stop_light(self) -> None:
        if self._lit:
            self._lit = False
            self._illuminate(None)

    def _on_rois_changed(self, *_a) -> None:
        self._btn_save.setEnabled(len(self._ed.roi_set) > 0)
        if self._btn_light.isChecked():
            self._reproject.start()

    # ── close ────────────────────────────────────────────────────────────────
    def _save(self) -> None:
        rois = self._ed.roi_set
        if not len(rois):
            return
        self.path = roi_store.save(self._name, rois)
        self.accept()

    def done(self, result: int) -> None:
        # Every exit (Save, Skip, Esc, the title-bar X) turns the light off.
        self._live_timer.stop()
        self._reproject.stop()
        self._stop_light()
        if self._was_live is not None and self._set_live is not None:
            self._set_live(self._was_live)
        super().done(result)
