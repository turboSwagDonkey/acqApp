"""Shared panel widgets. Qt only; knows nothing about devices."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFileDialog, QGroupBox, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton,
                             QSpinBox, QVBoxLayout, QWidget)

_GEOM_ORG, _GEOM_APP = "acqApp", "acqApp"

OPEN, SHUT = "▾", "▸"

_PATH_ROLE = Qt.ItemDataRole.UserRole


def spin(lo, hi, value=None, *, decimals: int | None = None, step=None,
         suffix: str = "", prefix: str = "", tooltip: str = "",
         track: bool = True) -> QSpinBox | QDoubleSpinBox:
    """QSpinBox, or QDoubleSpinBox once `decimals` is given.

    Range is set BEFORE the value (the other order clamps to Qt's 0-99).
    `track=False` emits once per typed number, not per keystroke.
    """
    s = QDoubleSpinBox() if decimals is not None else QSpinBox()
    s.setRange(lo, hi)
    if decimals is not None:
        s.setDecimals(decimals)
    if step is not None:
        s.setSingleStep(step)
    if suffix:
        s.setSuffix(suffix)
    if prefix:
        s.setPrefix(prefix)
    if not track:
        s.setKeyboardTracking(False)
    if value is not None:
        s.setValue(value if decimals is not None else int(value))
    if tooltip:
        s.setToolTip(tooltip)
    return s


class SessionPicker(QDialog):
    """Pick one of this session's saved files; older ones behind Browse.

    `store` exposes `list_session()`, `list_archive()` and `ARCHIVE_DIR`
    (duck-typed, so this knows no devices). Subclasses supply `row()` and may
    override `chose()`. `self.path` is the choice after `exec()`, else None.
    """

    def __init__(self, parent, store, *, title: str, empty: str,
                 browse_tip: str, browse_caption: str, browse_filter: str,
                 icon_size=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.path: Path | None = None
        self._store = store
        self._browse_caption, self._browse_filter = browse_caption, browse_filter

        v = QVBoxLayout(self)
        v.addWidget(QLabel("This session:"))
        self._list = QListWidget()
        if icon_size is not None:
            self._list.setIconSize(icon_size)
        records = list(reversed(store.list_session()))        # newest first
        for rec in records:
            text, icon = self.row(rec)
            it = QListWidgetItem(text)
            it.setData(_PATH_ROLE, str(rec.path))
            if icon is not None:
                it.setIcon(icon)
            self._list.addItem(it)
        if records:
            self._list.setCurrentRow(0)
        else:
            self._list.addItem(empty)
            self._list.setEnabled(False)
        self._list.itemDoubleClicked.connect(lambda _it: self._accept_selected())
        v.addWidget(self._list)

        row = QHBoxLayout()
        btn_browse = QPushButton("Browse older…")
        btn_browse.setToolTip(browse_tip)
        btn_browse.clicked.connect(self._browse)
        row.addWidget(btn_browse)
        row.addStretch(1)
        v.addLayout(row)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                               QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept_selected)
        box.rejected.connect(self.reject)
        v.addWidget(box)

    def row(self, rec) -> tuple[str, Any]:
        """(label, icon or None) for one record."""
        return (f"{rec.name}  ({rec.saved_at})", None)

    def chose(self, path: Path) -> None:
        self.path = path
        self.accept()

    def _accept_selected(self) -> None:
        if not self._list.isEnabled():
            return
        items = self._list.selectedItems()
        if items:
            self.chose(Path(items[0].data(_PATH_ROLE)))

    def _browse(self) -> None:
        self._store.list_archive()    # ensures the folder (+ rotation) exist
        path, _ = QFileDialog.getOpenFileName(
            self, self._browse_caption, str(self._store.ARCHIVE_DIR),
            self._browse_filter)
        if path:
            self.chose(Path(path))


def _arrow(box: QGroupBox, on: bool) -> None:
    """A disclosure triangle in the title (Qt's tick box reads as "enable";
    `style.accent_panel` hides it). The base title is kept on the box."""
    base = getattr(box, "_base_title", None)
    if base is None:
        base = box.title()
        box._base_title = base          # type: ignore[attr-defined]
    box.setTitle(f"{OPEN if on else SHUT} {base}")


def collapsible(box: QGroupBox, expanded: bool = True) -> QGroupBox:
    """Fold the box by hiding its direct children; it shrinks to its title on
    its own, and Qt restores each child's own enabled state."""
    kids = [c for c in box.children() if isinstance(c, QWidget)]

    def _show(on: bool) -> None:
        for w in kids:
            w.setVisible(on)
        _arrow(box, on)

    box.setCheckable(True)
    box.setChecked(expanded)
    _show(expanded)
    box.toggled.connect(_show)
    if not box.toolTip():
        box.setToolTip("Click the title to fold this section away.")
    return box


def collapsible_groups(panel: QWidget, key: str) -> list[QGroupBox]:
    """Make every group box collapsible and remember which are shut. A box
    already checkable keeps its own wiring and only gets the arrow."""
    s = QSettings(_GEOM_ORG, _GEOM_APP)
    done: list[QGroupBox] = []
    for box in panel.findChildren(QGroupBox):
        if box.isCheckable():
            _arrow(box, box.isChecked())
            box.toggled.connect(lambda on, b=box: _arrow(b, on))
            continue
        # Keyed by the plain title (read before the arrow), not position.
        setting = f"collapse/{key}/{box.title()}"
        collapsible(box, s.value(setting, "1") not in (False, "false", "0", 0))
        box.toggled.connect(
            lambda on, k=setting: QSettings(_GEOM_ORG, _GEOM_APP).setValue(
                k, "1" if on else "0"))
        done.append(box)
    return done
