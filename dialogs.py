"""The shell's own windows; none knows about the clock, recorder or devices.

Geometry lives in QSettings, which `tests/_harness.isolate_user_state()`
substitutes here too — or the suite overwrites the operator's layout.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QGridLayout,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QTabWidget, QVBoxLayout,
    QWidget,
)

from acqApp import config, probe, style, widgets

_GEOM_ORG, _GEOM_APP = "acqApp", "acqApp"


def _qsettings() -> QSettings:
    return QSettings(_GEOM_ORG, _GEOM_APP)


class ModuleSelectDialog(QDialog):
    """A checkbox per module, at startup and from the sidebar. ALWAYS_ON
    modules get no box and are added back by `selected()`."""

    def __init__(self, enabled: list[str], parent=None, *,
                 title: str = "Select modules to load",
                 prompt: str = "Load these instruments this session:"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(300)

        root = QVBoxLayout(self)
        root.addWidget(QLabel(prompt))

        self._boxes: dict[str, QCheckBox] = {}
        for key, label in config.MODULES.items():
            if key in config.ALWAYS_ON:
                continue
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
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(any(cb.isChecked() for cb in self._boxes.values()))

    def selected(self) -> list[str]:
        return config.order_modules(
            k for k, cb in self._boxes.items() if cb.isChecked())


def _looks_sane(size: QSize, floor: tuple[int, int]) -> bool:
    """A restored size can be garbage (seen: a few px, saved mid-drag)."""
    return size.width() >= floor[0] // 2 and size.height() >= floor[1] // 2


def _fits_on_screen(hint: QSize, floor: tuple[int, int], pad: int,
                    screen) -> QSize:
    """`hint` + padding, at least `floor`, at most 90% of the screen — or the
    bottom buttons can open off-screen on the rig."""
    w = max(floor[0], hint.width() + 2 * pad)
    h = max(floor[1], hint.height() + 2 * pad)
    screen = screen or QGuiApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        w = min(w, int(avail.width() * 0.9))
        h = min(h, int(avail.height() * 0.9))
    return QSize(w, h)


def _restore_or_fit(win: QDialog, saved, floor: tuple[int, int]) -> None:
    """Saved geometry if it restores AND looks sane (a restore can succeed on
    a garbage size), else `win.default_size()`."""
    if not (saved is not None and win.restoreGeometry(saved)
            and _looks_sane(win.size(), floor)):
        win.resize(win.default_size())


class PanelWindow(QDialog):
    """One module's panel in its own window (`ModuleAdapter.own_window`).
    Hidden, never destroyed: the panel is wired to a live controller.
    Geometry is saved per module key."""

    visibility_changed = pyqtSignal(bool)

    _MIN_DEFAULT = (900, 700)
    _PAD = 16

    def __init__(self, panel: QWidget, label: str, key: str, parent=None,
                 size: tuple[int, int] | None = None):
        super().__init__(parent)
        self._size = size
        self._geom_key = f"panelGeometry/{key}"
        self.setWindowTitle(label)
        self.setWindowFlag(Qt.WindowType.Window, True)

        # Scrolls, so the window can be narrower than the panel.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(panel)
        self._scroll.setMinimumWidth(80)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll)

        panel.setStyleSheet(style.accent_panel(key))
        widgets.collapsible_groups(panel, key)

        self._panel = panel
        self._saved_geom = _qsettings().value(self._geom_key)
        self._sized = False

    def default_size(self) -> QSize:
        if self._size is not None:
            return _fits_on_screen(QSize(*self._size), (0, 0), 0, self.screen())
        return _fits_on_screen(self._panel.sizeHint(), self._MIN_DEFAULT,
                               self._PAD, self.screen())

    def showEvent(self, event) -> None:
        if not self._sized:
            self._sized = True
            _restore_or_fit(self, self._saved_geom,
                            (0, 0) if self._size else self._MIN_DEFAULT)
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def save_geometry(self) -> None:
        _qsettings().setValue(self._geom_key, self.saveGeometry())

    def closeEvent(self, event) -> None:
        self.save_geometry()
        super().closeEvent(event)       # hides; the adapter owns the panel

    def release(self) -> QWidget | None:
        """Hand the panel back before this window dies: a QScrollArea deletes
        its widget, and the adapter disposes of the panel itself."""
        self.save_geometry()
        self.hide()
        panel = self._scroll.takeWidget()
        self._panel = None
        return panel


class SettingsDialog(QDialog):
    """Modeless settings window, a page per module. Hidden on close, never
    destroyed: the panels are wired to live controllers."""

    _GEOM_KEY = "settingsGeometry"
    _MIN_DEFAULT = (900, 820)
    _PAD = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setWindowFlag(Qt.WindowType.Window, True)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tabs)
        scroll.setMinimumWidth(80)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        # Sized on first show, once the panels exist.
        self._saved_geom = _qsettings().value(self._GEOM_KEY)
        self._sized = False

    def default_size(self) -> QSize:
        """Fits the largest panel (asks the tabs; a scroll area's hint is small)."""
        return _fits_on_screen(self.tabs.sizeHint(), self._MIN_DEFAULT,
                               self._PAD, self.screen())

    def showEvent(self, event) -> None:
        if not self._sized:
            self._sized = True
            _restore_or_fit(self, self._saved_geom, self._MIN_DEFAULT)
        super().showEvent(event)

    def add_panel(self, panel: QWidget, label: str, key: str,
                  index: int | None = None) -> None:
        """A tab in the module's accent, its group boxes made collapsible."""
        idx = (self.tabs.addTab(panel, label) if index is None
               else self.tabs.insertTab(index, panel, label))
        self.tabs.tabBar().setTabTextColor(idx, QColor(style.HEX[key]))
        panel.setStyleSheet(style.accent_panel(key))
        widgets.collapsible_groups(panel, key)

    def remove_panel(self, panel: QWidget) -> None:
        """Not deleted: the adapter disposes of it."""
        idx = self.tabs.indexOf(panel)
        if idx >= 0:
            self.tabs.removeTab(idx)

    def panel_index(self, panel: QWidget) -> int:
        return self.tabs.indexOf(panel)

    def show_panel(self, panel: QWidget) -> bool:
        idx = self.tabs.indexOf(panel)
        if idx < 0:
            return False
        self.tabs.setCurrentIndex(idx)
        return True

    def current_panel(self) -> QWidget | None:
        return self.tabs.currentWidget()

    def save_geometry(self) -> None:
        _qsettings().setValue(self._GEOM_KEY, self.saveGeometry())

    def closeEvent(self, event) -> None:
        self.save_geometry()
        super().closeEvent(event)


class ConnectionMonitor(QDialog):
    """Whether each loaded device is detected. Enumeration-only probes, safe
    mid-session."""

    _DOT = {"ok": "#2e7d32", "missing": "#c62828", "error": "#e0860a", "stub": "#8a8a8a"}
    _WORD = {"ok": "connected", "missing": "not found", "error": "error", "stub": "stub"}

    visibility_changed = pyqtSignal(bool)

    def __init__(self, module_keys: list[str], probe_kwargs=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Device connections")
        self.setMinimumWidth(420)
        # A callable, so Refresh sees a port edited after opening.
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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def refresh(self) -> None:
        """One module at a time, painting each row as it lands. Refresh is
        disabled meanwhile: these touch hardware, and must not re-enter."""
        kwargs = self._probe_kwargs()
        self._btn_refresh.setEnabled(False)
        try:
            for key, (status, detail) in self._rows.items():
                status.setText("● checking…")
                status.setStyleSheet("color:#9aa0a6; font-weight:bold;")
                detail.setText("")
                QApplication.processEvents()
                res = probe.probe(key, **kwargs)
                status.setText(f"● {self._WORD.get(res.status, res.status)}")
                status.setStyleSheet(f"color:{self._DOT.get(res.status, '#ccc')}; "
                                     "font-weight:bold;")
                detail.setText(res.detail)
                QApplication.processEvents()
        finally:
            self._btn_refresh.setEnabled(True)
