"""
AcqMainWindow — top-level QMainWindow with dockable device panels.

Panels:
  - DMD (placeholder until Phase 1 copy from dmdGUI_project)
  - Camera (placeholder until Phase 2)
  - Encoder (placeholder until Phase 3)

Layout persists across runs via QSettings (key "acqApp/dockState").
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import (
    QDockWidget, QLabel, QMainWindow, QStatusBar, QWidget,
)

from acq.clock import SessionClock
from app.camera_panel import CameraPanel


def _placeholder(title: str) -> QWidget:
    lbl = QLabel(f"[{title} — not yet connected]")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet("color: gray; font-style: italic;")
    return lbl


class AcqMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("acqApp")
        self.resize(1400, 900)
        self._clock = SessionClock()
        self._build_docks()
        self._build_status_bar()
        self._restore_layout()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _build_docks(self) -> None:
        self.dmd_dock = self._make_dock("DMD", _placeholder("DMD"),
                                        Qt.DockWidgetArea.LeftDockWidgetArea)

        self._cam_panel = CameraPanel(self._clock)
        self.cam_dock = self._make_dock("Camera", self._cam_panel,
                                        Qt.DockWidgetArea.RightDockWidgetArea)

        self.enc_dock = self._make_dock("Encoder", _placeholder("Encoder"),
                                        Qt.DockWidgetArea.BottomDockWidgetArea)

    def _make_dock(
        self,
        title: str,
        widget: QWidget,
        area: Qt.DockWidgetArea,
    ) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title}")
        dock.setWidget(widget)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(area, dock)
        return dock

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self._drop_label = QLabel("drops: 0")
        bar.addPermanentWidget(self._drop_label)
        self.setStatusBar(bar)

    # ------------------------------------------------------------------
    # Layout persistence
    # ------------------------------------------------------------------
    def _restore_layout(self) -> None:
        settings = QSettings("acqApp", "acqApp")
        state = settings.value("dockState")
        if state is not None:
            self.restoreState(state)

    def closeEvent(self, event) -> None:  # noqa: N802
        settings = QSettings("acqApp", "acqApp")
        settings.setValue("dockState", self.saveState())
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Public helpers (called by controller later)
    # ------------------------------------------------------------------
    def update_drop_count(self, count: int) -> None:
        self._drop_label.setText(f"drops: {count}")
