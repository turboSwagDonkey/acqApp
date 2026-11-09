"""The shell's own dialogs; none knows about the clock, recorder or devices."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QLabel,
    QVBoxLayout,
)

from acqApp import config, probe


class ModuleSelectDialog(QDialog):
    """A checkbox per module, at startup and from General's Modules button.
    With `probe_kwargs` each row also says whether its device is detected
    (enumeration-only probes, safe mid-session). ALWAYS_ON modules get no box
    and are added back by `selected()`."""

    _DOT = {"ok": "#2e7d32", "missing": "#c62828", "error": "#e0860a", "stub": "#8a8a8a"}
    _WORD = {"ok": "connected", "missing": "not found", "error": "error", "stub": "stub"}
    _DETAIL_W = 300

    def __init__(self, enabled: list[str], parent=None, *,
                 title: str = "Select modules to load",
                 prompt: str = "Load these instruments this session:",
                 probe_kwargs=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        if probe_kwargs is None:        # else the detail column sets the width
            self.setMinimumWidth(300)
        # A callable, so Refresh sees a port edited after opening.
        self._probe_kwargs = probe_kwargs
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}
        self._busy = False
        self._closed = False

        root = QVBoxLayout(self)
        root.addWidget(QLabel(prompt))

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(2, 1)
        self._boxes: dict[str, QCheckBox] = {}
        keys = [k for k in config.MODULES if k not in config.ALWAYS_ON]
        for r, key in enumerate(keys):
            cb = QCheckBox(config.MODULES[key])
            cb.setChecked(key in enabled)
            cb.toggled.connect(self._update_ok)
            self._boxes[key] = cb
            grid.addWidget(cb, r, 0)
            if probe_kwargs is not None:
                status, detail = QLabel(""), QLabel("")
                detail.setStyleSheet("color:#9aa0a6;")
                detail.setFixedWidth(self._DETAIL_W)     # one line; tooltip has it all
                grid.addWidget(status, r, 1)
                grid.addWidget(detail, r, 2)
                self._rows[key] = (status, detail)
        root.addLayout(grid)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._btn_refresh = None
        if probe_kwargs is not None:
            self._btn_refresh = self._buttons.addButton(
                "Check devices", QDialogButtonBox.ButtonRole.ActionRole)
            self._btn_refresh.setToolTip("Look again for each instrument's "
                                         "hardware (enumeration only).")
            self._btn_refresh.clicked.connect(self.refresh)
            QTimer.singleShot(0, self.refresh)      # once the dialog is up
        root.addWidget(self._buttons)
        self._update_ok()

    def _update_ok(self) -> None:
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(any(cb.isChecked() for cb in self._boxes.values()))

    def selected(self) -> list[str]:
        return config.order_modules(
            k for k, cb in self._boxes.items() if cb.isChecked())

    def refresh(self) -> None:
        """One module at a time, painting each row as it lands. Not
        re-entrant: these touch hardware. Stops once the dialog is closed."""
        if self._busy or self._probe_kwargs is None:
            return
        kwargs = self._probe_kwargs()
        self._busy = True
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
                detail.setText(detail.fontMetrics().elidedText(
                    " ".join(res.detail.split()), Qt.TextElideMode.ElideRight,
                    self._DETAIL_W))
                detail.setToolTip(res.detail)
                QApplication.processEvents()
                if self._closed:
                    break                   # OK/Cancel pressed mid-check
        finally:
            self._busy = False
            self._btn_refresh.setEnabled(True)

    def done(self, r: int) -> None:
        self._closed = True
        super().done(r)
