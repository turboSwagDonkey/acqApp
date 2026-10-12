"""Draw the DMD ROI set for a just-saved FOV, check it lit on the live camera,
and save it under the paired name ("<base>_roi"; see `routines/pairs.py`).

Hardware arrives as callables, so this never knows which camera or projector
it drives. Illuminate is the one control that emits light, and closing the
dialog always turns it off.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout)

from acqApp import style
from acqApp.devices.dmd import roi_store
from acqApp.devices.dmd.roi import RoiSet
from acqApp.devices.dmd.roi_live import LiveFrame, attach_live_view
from acqApp.devices.dmd.roi_panel import RoiEditor

_REPROJECT_MS = 150        # coalesces a drag's burst of edits into one upload


class PairRoiDialog(QDialog):
    """Modal. `.path` is the saved ROI set after Save, else None."""

    def __init__(self, editor: RoiEditor, name: str, *,
                 live_source: Callable[[], LiveFrame | None],
                 illuminate: Callable[[RoiSet | None], None],
                 set_live: Callable[[bool], bool] | None = None, parent=None):
        """`editor` already shows the FOV snapshot; the live camera goes under
        it. `illuminate(rois)` projects them, `illuminate(None)` stops.
        `set_live` starts/stops the camera and returns its previous state."""
        super().__init__(parent)
        self._ed = editor
        self._name = name
        self._illuminate = illuminate
        self._set_live = set_live
        self._was_live: bool | None = None     # set once we started the camera
        self._lit = False
        self.path: Path | None = None

        self.setWindowTitle(f"DMD ROIs for {name}")
        self.setStyleSheet(style.accent_panel("dmd"))
        self.resize(1000, 980)
        self._live = attach_live_view(editor, live_source)
        self._build()

        self._reproject = QTimer(self)
        self._reproject.setSingleShot(True)
        self._reproject.setInterval(_REPROJECT_MS)
        self._reproject.timeout.connect(self._project)
        self._ed.rois_changed.connect(self._on_rois_changed)
        self._on_rois_changed()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        msg = QLabel(
            "Draw where the DMD should light up on the full-field snapshot "
            "(top). Below, the live camera shows them over what it captures "
            "now. Illuminate to see them lit; keep tweaking until it's right, "
            f'then Save as "{self._name}". Skip saves the FOV alone.')
        msg.setWordWrap(True)
        root.addWidget(msg)
        root.addWidget(self._ed, 1)

        row = QHBoxLayout()
        self._btn_light = QPushButton("Illuminate ROIs")
        self._btn_light.setCheckable(True)
        self._btn_light.setStyleSheet(style.toggle_btn("dmd"))
        self._btn_light.toggled.connect(self._on_light_toggled)
        row.addWidget(self._btn_light)
        row.addStretch(1)
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
    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._set_live is not None and self._was_live is None:
            self._was_live = self._set_live(True)
        self._live.start()

    # ── light ────────────────────────────────────────────────────────────────
    def _on_light_toggled(self, on: bool) -> None:
        if on:
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
        self._live.stop()
        self._reproject.stop()
        self._stop_light()
        if self._was_live is not None and self._set_live is not None:
            self._set_live(self._was_live)
        super().done(result)
