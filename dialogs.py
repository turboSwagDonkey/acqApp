"""The shell's own dialogs; none knows about the clock, recorder or devices."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QLabel, QVBoxLayout, QWidget,
)

from acqApp import config, probe, style


class ModuleSelectDialog(QDialog):
    """A checkbox per module, at startup and from General's Modules button,
    in one column per `config.module_groups()` (Path 1 | Path 2 | Shared).
    Each column's title is a checkbox for the whole bundle: ticked when all
    are, partial when some. With `probe_kwargs` each row also says whether
    its device is detected (enumeration-only probes, safe mid-session).
    ALWAYS_ON modules get no box and are added back by `selected()`."""

    _DOT = {"ok": "#2e7d32", "missing": "#c62828", "error": "#e0860a", "stub": "#8a8a8a"}
    _WORD = {"ok": "connected", "missing": "not found", "error": "error", "stub": "stub"}
    _DETAIL_W = 210             # one line under each row; the tooltip has it all
    # A column's accent: the path's camera; shared things get no device's hue.
    _ACCENT = {"Path 1": "voltage_cam", "Path 2": "voltage_cam_2"}

    def __init__(self, enabled: list[str], parent=None, *,
                 title: str = "Select modules to load",
                 prompt: str = "Load these instruments this session:",
                 probe_kwargs=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        # A callable, so Refresh sees a port edited after opening.
        self._probe_kwargs = probe_kwargs
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}
        self._boxes: dict[str, QCheckBox] = {}
        self._heads: list[tuple[QCheckBox, list[str]]] = []
        self._busy = False
        self._closed = False

        root = QVBoxLayout(self)
        root.addWidget(QLabel(prompt))
        cols = QHBoxLayout()
        cols.setSpacing(10)
        for group, keys in config.module_groups():
            cols.addWidget(self._column(group, keys, enabled), 1)
        root.addLayout(cols)

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
        self._on_toggled()

    # ── construction ──
    def _column(self, group: str, keys: list[str], enabled: list[str]) -> QFrame:
        accent = style.HEX[self._ACCENT.get(group, "sync")]
        col = QFrame()
        col.setObjectName("moduleColumn")
        col.setStyleSheet(
            f"QFrame#moduleColumn{{border:1px solid {style.line()};"
            f"border-top:3px solid {accent};border-radius:4px}}")
        lay = QVBoxLayout(col)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(4)

        head = QCheckBox(group)
        # Slate text reads as disabled: Shared keeps its hue on the border only.
        color = f"color:{accent}" if group in self._ACCENT else ""
        head.setStyleSheet(f"QCheckBox{{font-weight:bold;{color}}}")
        head.setToolTip(f"Tick or untick all of {group} at once.")
        head.clicked.connect(lambda _on, ks=keys: self._on_head(ks))
        lay.addWidget(head)
        self._heads.append((head, keys))
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet(f"color:{style.line()};")
        lay.addWidget(rule)

        for key in keys:
            lay.addWidget(self._row(group, key, key in enabled))
        lay.addStretch(1)               # a short column stays at the top
        return col

    def _row(self, group: str, key: str, checked: bool) -> QWidget:
        # The column already says which path: "Camera (path 2)" -> "Camera".
        cb = QCheckBox(config.MODULES[key].removesuffix(f" ({group.lower()})"))
        cb.setToolTip(config.MODULES[key])
        cb.setChecked(checked)
        cb.toggled.connect(self._on_toggled)
        self._boxes[key] = cb

        row = QWidget()
        lay = QVBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        line = QHBoxLayout()
        line.setSpacing(8)
        swatch = QLabel("■")
        swatch.setStyleSheet(f"color:{style.HEX.get(key, '#888')};")
        line.addWidget(swatch)
        line.addWidget(cb)
        line.addStretch(1)
        lay.addLayout(line)
        if self._probe_kwargs is not None:
            status, detail = QLabel(""), QLabel("")
            line.addWidget(status)
            detail.setStyleSheet(f"color:{style.muted()}; font-size:10px;")
            detail.setFixedWidth(self._DETAIL_W)
            detail.setContentsMargins(22, 0, 0, 2)  # under the label, past the swatch
            lay.addWidget(detail)
            self._rows[key] = (status, detail)
        return row

    # ── ticking ──
    def _on_head(self, keys: list[str]) -> None:
        """All on, unless all already were: then all off."""
        want = not all(self._boxes[k].isChecked() for k in keys)
        for k in keys:
            self._boxes[k].setChecked(want)
        self._on_toggled()

    def _on_toggled(self, *_a) -> None:
        for head, keys in self._heads:
            n = sum(self._boxes[k].isChecked() for k in keys)
            head.setCheckState(Qt.CheckState.Unchecked if n == 0
                               else Qt.CheckState.Checked if n == len(keys)
                               else Qt.CheckState.PartiallyChecked)
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(any(cb.isChecked() for cb in self._boxes.values()))

    def selected(self) -> list[str]:
        return config.order_modules(
            k for k, cb in self._boxes.items() if cb.isChecked())

    # ── device checks ──
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
                    self._DETAIL_W - 22))
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
