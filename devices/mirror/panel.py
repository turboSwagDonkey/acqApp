"""PMT/camera mirror — settings tab.

Manual only (PLAN.md S6): two switches in ThorImage move together (the
galvo in/out, the visualizer path camera/PMT), driven by ThorImage and held
on a serial port acqApp can't share, so there's nothing here to read
live — one button for both, and the operator tells acqApp which way they
set them, logged to `/mirror` like `/dmd`'s boundaries — a record of what
was asserted, not a measurement.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QButtonGroup, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import pyqtSignal

from acqApp import style
from acqApp.devices.mirror.settings import CAMERA, PMT, MirrorSettings


class SettingsPanel(QWidget):
    state_changed = pyqtSignal(str)   # CAMERA or PMT — the operator clicked

    def __init__(self, settings: MirrorSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or MirrorSettings()
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("PMT / camera mirror")
        lay = QVBoxLayout(grp)

        row = QHBoxLayout()
        self._btn_cam = QPushButton("Camera  (galvo out)")
        self._btn_pmt = QPushButton("PMT  (galvo in)")
        for b in (self._btn_cam, self._btn_pmt):
            b.setCheckable(True)
            b.setStyleSheet(style.solid_btn("mirror"))
            row.addWidget(b)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.addButton(self._btn_cam)
        self._group.addButton(self._btn_pmt)
        lay.addLayout(row)

        self._lbl_status = QLabel()
        self._lbl_status.setStyleSheet("color: gray;")
        lay.addWidget(self._lbl_status)

        note = QLabel("Click the one you just set BOTH ThorImage switches to "
                       "(galvo, and the visualizer path) — acqApp can't read "
                       "either one itself (ThorImage holds the controller's "
                       "serial port while it runs).")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-style: italic;")
        lay.addWidget(note)

        root.addWidget(grp)
        root.addStretch()

        self._btn_cam.clicked.connect(lambda: self._select(CAMERA))
        self._btn_pmt.clicked.connect(lambda: self._select(PMT))
        self._apply_state(self._s.state)

    def _select(self, state: str) -> None:
        self._apply_state(state)
        self.state_changed.emit(state)

    def _apply_state(self, state: str) -> None:
        self._s = MirrorSettings(state=state)
        self._btn_cam.setChecked(state == CAMERA)
        self._btn_pmt.setChecked(state == PMT)
        self._lbl_status.setText(f"acqApp will log: {state}")

    @property
    def settings(self) -> MirrorSettings:
        return self._s
