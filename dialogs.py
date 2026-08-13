"""
The shell's three modal windows.

Split out of `main.py`, which is session wiring: none of these knows about the
clock, the recorder or a device. They are the windows the operator opens *at*
the app rather than the app itself.

`SettingsDialog` reads and writes `QSettings` for its geometry, so
`tests/_harness.isolate_user_state()` substitutes `QSettings` **here** as well
as in `main` — leaving that out would let the suite overwrite the operator's
real window geometry.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from acqApp import config, probe, style


class ModuleSelectDialog(QDialog):
    """Startup popup: a checkbox per subsystem, pre-checked from last use."""

    def __init__(self, enabled: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select modules to load")
        self.setMinimumWidth(300)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Load these instruments this session:"))

        self._boxes: dict[str, QCheckBox] = {}
        for key, label in config.MODULES.items():
            cb = QCheckBox(label)
            cb.setChecked(key in enabled)
            cb.toggled.connect(self._update_ok)
            self._boxes[key] = cb
            root.addWidget(cb)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)
        self._update_ok()

    def _update_ok(self) -> None:
        # Require at least one module to enable OK.
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(any(cb.isChecked() for cb in self._boxes.values()))

    def selected(self) -> list[str]:
        return [k for k, cb in self._boxes.items() if cb.isChecked()]


class SettingsDialog(QDialog):
    """Modeless settings window: the Save tab plus one tab per subsystem.

    A separate window rather than a dock inside the main window. The panels are
    read and edited *while* watching the live view, and a floating window can sit
    beside the app (or on a second screen) without taking width from the camera
    pane. It is built once and hidden on close — the panels inside are live
    objects wired to the running controllers, so it must not be destroyed.
    """

    _GEOM_KEY = "settingsGeometry"
    # First-run floor, in px. The window opens at least this big even if every
    # panel is small; see `default_size()` for what grows it beyond this.
    _MIN_DEFAULT = (900, 820)
    _PAD = 16          # slack round the measured panel, for the frame/scrollbar

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        # A real window (minimise/maximise buttons), not a fixed dialog frame.
        self.setWindowFlag(Qt.WindowType.Window, True)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)                  # tabs reorderable by drag

        # Scroll area so the window can be dragged narrower than the widest
        # panel (content scrolls instead of pinning a minimum width).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tabs)
        scroll.setMinimumWidth(80)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        # Sizing waits for the first show: the panels are added after __init__,
        # and the size is measured from them.
        self._saved_geom = QSettings("acqApp", "acqApp").value(self._GEOM_KEY)
        self._sized = False

    def default_size(self) -> QSize:
        """Big enough to show the largest panel whole, clamped to the screen.

        Measured rather than hard-coded, so a panel that grows a row doesn't
        silently start opening behind a scrollbar. The floor keeps the small
        panels from opening in a cramped window; the clamp matters because the
        rig's display is not this laptop's — a fixed size that is comfortable
        here can open taller than the screen there, which on Windows puts the
        bottom of the window out of reach.

        `QScrollArea` reports a small hint of its own (that is the point of it),
        so this asks the tab widget inside instead."""
        hint = self.tabs.sizeHint()
        w = max(self._MIN_DEFAULT[0], hint.width()  + 2 * self._PAD)
        h = max(self._MIN_DEFAULT[1], hint.height() + 2 * self._PAD)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(w, int(avail.width()  * 0.9))
            h = min(h, int(avail.height() * 0.9))
        return QSize(w, h)

    def showEvent(self, event) -> None:
        # First show only: afterwards the operator's own size wins.
        if not self._sized:
            self._sized = True
            if self._saved_geom is not None:
                self.restoreGeometry(self._saved_geom)
            else:
                self.resize(self.default_size())
        super().showEvent(event)

    def add_panel(self, panel: QWidget, label: str, key: str) -> None:
        """Add a settings tab wearing its subsystem accent (tab + group box)."""
        idx = self.tabs.addTab(panel, label)
        self.tabs.tabBar().setTabTextColor(idx, QColor(style.HEX[key]))
        panel.setStyleSheet(style.accent_panel(key))

    def save_geometry(self) -> None:
        QSettings("acqApp", "acqApp").setValue(self._GEOM_KEY, self.saveGeometry())

    def closeEvent(self, event) -> None:
        # Remember where the operator put it, then hide (never delete).
        self.save_geometry()
        super().closeEvent(event)


class ConnectionMonitor(QDialog):
    """Modeless panel showing whether each loaded device is detected.

    Uses probe.py's enumeration-only checks, so Refresh is safe to hit at any
    time — including mid-session — without disturbing a running worker."""

    _DOT = {"ok": "#2e7d32", "missing": "#c62828", "error": "#e0860a", "stub": "#8a8a8a"}
    _WORD = {"ok": "connected", "missing": "not found", "error": "error", "stub": "stub"}

    def __init__(self, module_keys: list[str], probe_kwargs=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Device connections")
        self.setMinimumWidth(420)
        self._modules = module_keys
        # A callable, not a dict: Refresh then picks up a port edited in the
        # Stage tab after this window was opened.
        self._probe_kwargs = probe_kwargs or (lambda: {})
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}

        root = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setColumnStretch(2, 1)
        grid.setHorizontalSpacing(12)
        for r, key in enumerate(module_keys):
            name = QLabel(config.MODULES.get(key, key))
            name.setStyleSheet(f"color:{style.HEX.get(key, '#ccc')}; font-weight:bold;")
            status = QLabel("…")
            detail = QLabel("")
            detail.setStyleSheet("color:#9aa0a6;")
            detail.setWordWrap(True)
            grid.addWidget(name,   r, 0)
            grid.addWidget(status, r, 1)
            grid.addWidget(detail, r, 2)
            self._rows[key] = (status, detail)
        root.addLayout(grid)

        buttons = QHBoxLayout()
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.clicked.connect(self.refresh)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        buttons.addStretch()
        buttons.addWidget(self._btn_refresh)
        buttons.addWidget(btn_close)
        root.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        results = probe.probe_all(self._modules, **self._probe_kwargs())
        for key, (status, detail) in self._rows.items():
            res = results[key]
            status.setText(f"● {self._WORD.get(res.status, res.status)}")
            status.setStyleSheet(f"color:{self._DOT.get(res.status, '#ccc')}; "
                                 "font-weight:bold;")
            detail.setText(res.detail)
