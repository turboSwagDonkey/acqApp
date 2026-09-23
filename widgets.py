"""Shared panel widgets.

The settings panels are per-instrument and own their own layout; this is the
little all of them want. Kept Qt-only and knowing nothing about devices, so a
panel can use it without pulling in the shell.
"""
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
    """A configured spin box: `QSpinBox` by default, `QDoubleSpinBox` once
    `decimals` is given.

    Range is always applied BEFORE the value — the other order silently
    clamps to Qt's default 0–99. Omit `value` to keep Qt's own starting
    point (0, clamped into range), which is what a box the operator is
    expected to fill in wants.

    `track=False` turns off keyboard tracking, so typing "150" emits once
    instead of at 1, 15 and 150. Worth it wherever the signal is expensive
    (a save, or a box that jumps across the frame); the default stays on,
    which is Qt's, so a control only opts out deliberately.
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
    """Choose one of this session's saved files, older runs behind Browse.

    The shape both `devices/dmd/roi_picker.py` and
    `devices/stage/fov_picker.py` want: the quick list is only THIS run's
    saves, so a long history never slows finding today's, and everything
    earlier is one Browse away in the archive folder.

    `store` is any module exposing `list_session()`, `list_archive()` and
    `ARCHIVE_DIR` — duck-typed on purpose, so this file still knows nothing
    about devices. A subclass supplies `row()` and, if it wants more than
    the path, overrides `chose()`. `self.path` is the chosen file after
    `exec()`, else None.
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

    # ── what a subclass fills in ──
    def row(self, rec) -> tuple[str, Any]:
        """One list row for `rec`: its label, and an icon or None."""
        return (f"{rec.name}  ({rec.saved_at})", None)

    def chose(self, path: Path) -> None:
        """Record the choice and close. Override to also load the file."""
        self.path = path
        self.accept()

    # ── the picking itself ──
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
    """Show the section's state in its title, as a disclosure triangle.

    Qt's own indicator is a tick box, which reads as "enable this section"
    rather than "expand it"; `style.accent_panel` hides it. The base title is
    remembered on the box, so toggling twice can't accumulate arrows.
    """
    base = getattr(box, "_base_title", None)
    if base is None:
        base = box.title()
        box._base_title = base          # type: ignore[attr-defined]
    box.setTitle(f"{OPEN if on else SHUT} {base}")


def collapsible(box: QGroupBox, expanded: bool = True) -> QGroupBox:
    """Fold `box`'s contents away behind the disclosure arrow in its title.

    Hiding the direct children is the whole implementation — box shrinks to
    its title on its own (measured: 141 px → 43), so there's no height to
    juggle. Qt also disables them while unticked, and restores each child's
    *own* enabled state on the way back, so a control the panel had deliberately
    greyed out is still greyed out afterwards.
    """
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
    """Make every group box in `panel` collapsible, and remember which are shut.

    Applied centrally rather than in each panel, so a new instrument gets it
    without doing anything — and so the panels stay about their instrument.

    **A box that's already checkable keeps its own wiring.** A tick there means
    something to the panel (the pupil tab's "Advanced tracking" folds itself and
    renames its own title), so it gets the arrow for consistency and nothing
    else — taking its toggle over would fight it.
    """
    s = QSettings(_GEOM_ORG, _GEOM_APP)
    done: list[QGroupBox] = []
    for box in panel.findChildren(QGroupBox):
        if box.isCheckable():
            _arrow(box, box.isChecked())
            box.toggled.connect(lambda on, b=box: _arrow(b, on))
            continue
        # Keyed by title, not position: inserting a group above should not
        # shuffle everyone's saved state. Read before the arrow is added, so
        # the key stays the plain title.
        setting = f"collapse/{key}/{box.title()}"
        collapsible(box, s.value(setting, "1") not in (False, "false", "0", 0))
        box.toggled.connect(
            lambda on, k=setting: QSettings(_GEOM_ORG, _GEOM_APP).setValue(
                k, "1" if on else "0"))
        done.append(box)
    return done
