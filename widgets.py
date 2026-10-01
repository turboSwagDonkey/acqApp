"""Shared panel widgets. Qt only; knows nothing about devices."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractButton, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QSpinBox, QStyle,
                             QStyleOptionGroupBox, QToolTip, QVBoxLayout,
                             QWidget)

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
    # A box whose help lives on its title (section_help) keeps that instead.
    if not box.toolTip() and not hasattr(box, "_help_text"):
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


# ── help on the section title only ────────────────────────────────────────────

def _field_label(w: QWidget, box: QGroupBox) -> str:
    """What a control is called: its own text (check box, button), else
    its form-row label, looked up through any row layout it sits in."""
    if isinstance(w, QAbstractButton) and w.text():
        return w.text()
    for form in box.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            lbl = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            fld = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if lbl is None or fld is None or lbl.widget() is None:
                continue
            if fld.widget() is w or (fld.layout() is not None
                                     and fld.layout().indexOf(w) >= 0):
                return lbl.widget().text().rstrip(":")
    return ""


def _own_widgets(box: QGroupBox) -> list[QWidget]:
    """Descendants of `box` that aren't inside a nested group box."""
    out = []
    for w in box.findChildren(QWidget):
        p = w.parentWidget()
        while p is not None and p is not box and not isinstance(p, QGroupBox):
            p = p.parentWidget()
        if p is box:
            out.append(w)
    return out


class _TitleTip(QObject):
    """Shows the box's help only while the pointer is on its title."""

    def __init__(self, box: QGroupBox, text: str) -> None:
        super().__init__(box)
        self._box, self._text = box, text

    def _on_title(self, pos) -> bool:
        box = self._box
        opt = QStyleOptionGroupBox()
        box.initStyleOption(opt)
        style = box.style()
        rect = style.subControlRect(QStyle.ComplexControl.CC_GroupBox, opt,
                                    QStyle.SubControl.SC_GroupBoxLabel, box)
        rect = rect.united(style.subControlRect(
            QStyle.ComplexControl.CC_GroupBox, opt,
            QStyle.SubControl.SC_GroupBoxCheckBox, box))
        if rect.isEmpty():       # a style that won't say: the top strip
            return pos.y() <= box.fontMetrics().height() + 6
        return rect.adjusted(-2, -2, 2, 2).contains(pos)

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._box and ev.type() == QEvent.Type.ToolTip:
            if self._on_title(ev.pos()):
                QToolTip.showText(ev.globalPos(), self._text, self._box)
            else:
                QToolTip.hideText()
            return True
        return False


def section_help(box: QGroupBox) -> str:
    """Move the help off the controls in `box` onto its title: one tooltip,
    shown only while hovering the title, explaining each control by name.
    The box's own tooltip leads. Returns the help text (rich text)."""
    parts = []
    intro = getattr(box, "_help_intro", None)
    if intro is None:
        intro = box.toolTip()
        box._help_intro = intro             # type: ignore[attr-defined]
    if intro and not intro.startswith("Click the title"):
        parts.append(f"<p>{_html(intro)}</p>")
    # Kept on the box, so applying again (after new controls or new tips)
    # adds to the help instead of losing what was already moved.
    found: dict = getattr(box, "_help_items", {})
    for w in _own_widgets(box):
        tip = w.toolTip()
        if not tip:
            continue
        name = _field_label(w, box)
        found[id(w)] = (f"<li><b>{html.escape(name)}</b> — {_html(tip)}</li>"
                        if name else f"<li>{_html(tip)}</li>")
        w.setToolTip("")
    box._help_items = found                 # type: ignore[attr-defined]
    items = list(found.values())
    if items:
        parts.append("<ul style='margin-left:-20px'>" + "".join(items) + "</ul>")
    text = "<qt>" + "".join(parts) + "</qt>" if parts else ""
    box.setToolTip("")          # the filter shows it; Qt's own would show anywhere
    old = getattr(box, "_title_tip", None)
    if old is not None:
        box.removeEventFilter(old)
    if text:
        tip = _TitleTip(box, text)
        box.installEventFilter(tip)
        box._title_tip = tip                # type: ignore[attr-defined]
    box._help_text = text                   # type: ignore[attr-defined]
    return text


def sections_help(panel: QWidget) -> None:
    """`section_help` on every group box in `panel`. Call once the panel is
    built; any module's panel can opt in with this one line."""
    for box in panel.findChildren(QGroupBox):
        section_help(box)


def _html(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


# ── a segmented switch ────────────────────────────────────────────────────────

class SegmentedSwitch(QWidget):
    """Joined buttons, exactly one lit: a mode switch that reads as one,
    unlike a check box. `changed(key)` fires on a click, not on `set_value`."""

    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str]], color: str,
                 parent=None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._buttons: dict[str, QPushButton] = {}
        n = len(options)
        for i, (label, key) in enumerate(options):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setAutoExclusive(True)
            left = "6px" if i == 0 else "0px"
            right = "6px" if i == n - 1 else "0px"
            b.setStyleSheet(
                "QPushButton{"
                f"border:1px solid {color};padding:4px 14px;"
                f"border-top-left-radius:{left};border-bottom-left-radius:{left};"
                f"border-top-right-radius:{right};border-bottom-right-radius:{right};"
                "background:transparent}"
                f"QPushButton:checked{{background:{color};color:white;"
                "font-weight:bold}"
                "QPushButton:disabled{color:#777;border-color:#555}")
            b.clicked.connect(lambda _c, k=key: self.changed.emit(k))
            lay.addWidget(b, 1)
            self._buttons[key] = b
        self.set_value(options[0][1])

    def value(self) -> str:
        return next(k for k, b in self._buttons.items() if b.isChecked())

    def set_value(self, key: str) -> None:
        self._buttons[key].setChecked(True)

    def button(self, key: str) -> QPushButton:
        return self._buttons[key]
