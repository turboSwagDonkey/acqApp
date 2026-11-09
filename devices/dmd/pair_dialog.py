"""ROI editor dialogs that light what is drawn on the live camera.

`RoiLightDialog` is the shared shape: the editor over the live camera and an
Illuminate toggle that re-projects on every edit. `PairRoiDialog` draws the
set for a just-saved FOV and saves it under the paired name ("<base>_roi";
see `routines/pairs.py`).

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


class RoiLightDialog(QDialog):
    """Modal. Accepting calls `save(rois)`; `save` raising OSError keeps the
    dialog open with the error shown."""

    def __init__(self, editor: RoiEditor, *, title: str, message: str,
                 live_source: Callable[[], LiveFrame | None] | None,
                 illuminate: Callable[[RoiSet | None], None] | None,
                 save: Callable[[RoiSet], None],
                 set_live: Callable[[bool], bool] | None = None,
                 lit: bool = False, reject_label: str = "Cancel",
                 allow_empty: bool = False, parent=None):
        """`editor` already shows the snapshot; the live camera goes under
        it. `illuminate(rois)` projects them, `illuminate(None)` stops.
        `set_live` starts/stops the camera and returns its previous state.
        `lit` starts with Illuminate on (the DMD was already lit).
        No `live_source`: the stage is elsewhere, so the snapshot alone and
        no light (it would land on another field)."""
        super().__init__(parent)
        self._ed = editor
        self._snapshot_only = live_source is None
        self._illuminate = illuminate or (lambda _r: None)
        self._save_fn = save
        self._set_live = None if self._snapshot_only else set_live
        self._allow_empty = allow_empty
        self._was_live: bool | None = None     # set once we started the camera
        self._lit = False

        self.setWindowTitle(title)
        self.setStyleSheet(style.accent_panel("dmd"))
        self.resize(1000, 980)
        self._live = (None if live_source is None
                      else attach_live_view(editor, live_source))
        self._build(message, reject_label)

        self._reproject = QTimer(self)
        self._reproject.setSingleShot(True)
        self._reproject.setInterval(_REPROJECT_MS)
        self._reproject.timeout.connect(self._project)
        self._ed.rois_changed.connect(self._on_rois_changed)
        self._on_rois_changed()
        if lit and self._btn_light.isEnabled():
            self._btn_light.setChecked(True)

    def _build(self, message: str, reject_label: str) -> None:
        root = QVBoxLayout(self)
        msg = QLabel(message)
        msg.setWordWrap(True)
        root.addWidget(msg)
        root.addWidget(self._ed, 1)

        row = QHBoxLayout()
        self._btn_light = QPushButton("Illuminate ROIs")
        self._btn_light.setCheckable(True)
        self._btn_light.setStyleSheet(style.toggle_btn("dmd"))
        self._btn_light.toggled.connect(self._on_light_toggled)
        row.addWidget(self._btn_light)
        self._lbl_err = QLabel()
        self._lbl_err.setWordWrap(True)
        self._lbl_err.setStyleSheet(f"color:{style.WARN};")
        row.addWidget(self._lbl_err, 1)
        root.addLayout(row)

        self._bb = QDialogButtonBox()
        self._btn_save = self._bb.addButton(
            "Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self._bb.addButton(reject_label, QDialogButtonBox.ButtonRole.RejectRole)
        self._bb.accepted.connect(self._save)
        self._bb.rejected.connect(self.reject)
        root.addWidget(self._bb)

        if self._snapshot_only:
            self._btn_light.hide()
        elif self._ed.can_project:
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
        if self._live is not None:
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
            self._lit = True            # before: a failed upload may have lit it
            try:
                self._illuminate(self._ed.roi_set)
            except Exception as e:      # noqa: BLE001 — escaping a slot aborts the app
                self._fail("Could not illuminate", e)
                self._btn_light.setChecked(False)   # -> _stop_light
            else:
                self._lbl_err.clear()   # a retry worked: no stale "off" message by a lit DMD

    def _stop_light(self) -> None:
        if self._lit:
            self._lit = False
            try:
                self._illuminate(None)
            except Exception as e:      # noqa: BLE001
                self._fail("Could not turn the light off", e)

    def _fail(self, what: str, e: Exception) -> None:
        self._lbl_err.setText(f"{what}: {type(e).__name__}: {e}")

    def _on_rois_changed(self, *_a) -> None:
        self._btn_save.setEnabled(self._allow_empty or len(self._ed.roi_set) > 0)
        if self._btn_light.isChecked():
            self._reproject.start()

    # ── close ────────────────────────────────────────────────────────────────
    def _save(self) -> None:
        rois = self._ed.roi_set
        if not (self._allow_empty or len(rois)):
            return
        try:
            self._save_fn(rois)
        except OSError as e:
            self._fail("Could not save", e)
            return
        self.accept()

    def done(self, result: int) -> None:
        # Every exit (Save, Skip, Esc, the title-bar X) turns the light off.
        if self._live is not None:
            self._live.stop()
        self._reproject.stop()
        self._stop_light()
        if self._was_live is not None and self._set_live is not None:
            self._set_live(self._was_live)
        super().done(result)


class PairRoiDialog(RoiLightDialog):
    """`.path` is the saved ROI set after Save, else None."""

    def __init__(self, editor: RoiEditor, name: str, *,
                 live_source: Callable[[], LiveFrame | None] | None,
                 illuminate: Callable[[RoiSet | None], None] | None,
                 set_live: Callable[[bool], bool] | None = None, parent=None):
        super().__init__(
            editor, title=f"DMD ROIs for {name}",
            message=("Draw where the DMD should light up on the FOV's saved "
                     f'snapshot, then Save as "{name}".'
                     if live_source is None else
                     "Draw where the DMD should light up on the full-field "
                     "snapshot (top). Below, the live camera shows them over "
                     "what it captures now. Illuminate to see them lit; keep "
                     f'tweaking until it\'s right, then Save as "{name}". '
                     "Skip saves the FOV alone."),
            live_source=live_source, illuminate=illuminate,
            save=lambda rois: self._store(name, rois), set_live=set_live,
            reject_label="Skip", parent=parent)
        editor.hide_save()              # the name is already fixed by the FOV
        self.path: Path | None = None

    def _store(self, name: str, rois: RoiSet) -> None:
        self.path = roi_store.save(name, rois,
                                   png_bytes=self._ed.render_png())
