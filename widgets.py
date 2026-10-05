"""Shared panel widgets. Qt only; knows nothing about devices."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QComboBox,
                             QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QListWidget,
                             QListWidgetItem, QPushButton, QSpinBox, QStyle,
                             QStyleOptionGroupBox, QToolTip, QVBoxLayout,
                             QWidget)

_GEOM_ORG, _GEOM_APP = "acqApp", "acqApp"

OPEN, SHUT = "▾", "▸"

_PATH_ROLE = Qt.ItemDataRole.UserRole


def compact(w: QWidget, chars: int | None = None) -> QWidget:
    """Keep an input as narrow as its content: Qt's form layouts otherwise
    stretch every spin box, combo and line edit across the whole panel.
    `chars` caps a free-text box at about that many characters.

    The panel rules: compact() every combo, line edit and spin box not made
    with `spin()` (which does it already); put closely related pairs on one
    row; end a row with addStretch() so its compact widgets stay left. Paths
    and table-cell editors stay full width."""
    from PyQt6.QtWidgets import QSizePolicy
    w.setSizePolicy(QSizePolicy.Policy.Fixed, w.sizePolicy().verticalPolicy())
    if chars:
        w.setFixedWidth(w.fontMetrics().horizontalAdvance("0" * chars) + 16)
    return w


def spin(lo, hi, value=None, *, decimals: int | None = None, step=None,
         suffix: str = "", prefix: str = "", tooltip: str = "",
         track: bool = True) -> QSpinBox | QDoubleSpinBox:
    """QSpinBox, or QDoubleSpinBox once `decimals` is given.

    Range is set BEFORE the value (the other order clamps to Qt's 0-99).
    `track=False` emits once per typed number, not per keystroke.
    As wide as its range and suffix need, never stretched across the panel
    (see `compact`).
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
    compact(s)
    return s


def hrow(*items) -> QHBoxLayout:
    """One line of controls, kept left. A str becomes the label of the item
    after it; a QLayout is nested."""
    from PyQt6.QtWidgets import QLayout
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    for it in items:
        if isinstance(it, str):
            lay.addWidget(QLabel(it))
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    lay.addStretch()
    return lay


def pairs_grid(*rows, per_row: int | None = None,
               gap: int = 28) -> QGridLayout:
    """Items in aligned columns: each row is (label, widget, label, widget, ...)
    and each pair is one item. A None label lets the widget (a check box, say)
    take the item's whole width; a None widget leaves it empty; a QLayout is
    nested. `gap` is the space between items; `per_row` wraps a row after that
    many items, keeping the columns lined up (a narrow host). In a column the
    labels share one width, and the inputs and the check boxes/buttons share
    the item's width: inputs widen to match a check box above them. A label
    shows its input's tooltip, so hovering either explains the control."""
    from PyQt6.QtWidgets import QLayout, QSizePolicy
    if per_row:
        n = 2 * per_row
        rows = tuple(row[k:k + n] for row in rows
                     for k in range(0, len(row), n))
    rows = tuple(row for row in rows if row)
    items = max(len(row) for row in rows) // 2
    g = QGridLayout()
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(4)
    g.setVerticalSpacing(6)
    grows = False                       # a column that wants the spare width
    cols = [{"lab": [], "inp": [], "btn": []} for _ in range(items)]
    for r, row in enumerate(rows):
        for k in range(len(row) // 2):
            label, w = row[2 * k], row[2 * k + 1]
            if w is None:
                continue
            base = 3 * k                # label, input, gap
            if label is not None:
                lbl = QLabel(label)
                first = w.itemAt(0).widget() if isinstance(w, QLayout) else w
                lbl.setToolTip(first.toolTip() if first is not None else "")
                lbl.setProperty("tipMirror", True)
                g.addWidget(lbl, r, base)
                cols[k]["lab"].append(lbl)
                col, span = base + 1, 1
            else:
                col, span = base, 2
            if isinstance(w, QLayout):
                g.addLayout(w, r, col, 1, span)
                continue
            g.addWidget(w, r, col, 1, span)
            if w.sizePolicy().horizontalPolicy() in (
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.MinimumExpanding):
                g.setColumnStretch(col, 1)
                grows = True
            elif label is not None and isinstance(
                    w, (QAbstractSpinBox, QComboBox)):
                cols[k]["inp"].append(w)
            elif isinstance(w, QAbstractButton):
                cols[k]["btn"].append(w)
    sp = g.horizontalSpacing()
    for k, c in enumerate(cols):
        lw = max((x.sizeHint().width() for x in c["lab"]), default=0)
        if lw:
            g.setColumnMinimumWidth(3 * k, lw)
        bw = max((x.sizeHint().width() for x in c["btn"]), default=0)
        iw = max((x.sizeHint().width() for x in c["inp"]), default=0)
        if c["inp"]:
            iw = max(iw, bw - lw - sp)
            for x in c["inp"]:
                x.setFixedWidth(iw)
            bw = lw + sp + iw
        for x in c["btn"]:
            x.setMinimumWidth(bw)
    for k in range(items - 1):
        g.setColumnMinimumWidth(3 * k + 2, gap)
    if not grows:
        g.setColumnStretch(3 * items, 1)
    return g


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
    # In a row of several inputs, the label just before it names it.
    from PyQt6.QtWidgets import QLayout
    for lay in box.findChildren(QLayout):
        i = lay.indexOf(w)
        if i > 0:
            prev = lay.itemAt(i - 1).widget()
            if isinstance(prev, QLabel) and prev.text():
                return prev.text().rstrip(":")
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


def section_help(box: QGroupBox, keep: bool = False) -> str:
    """Move the help off the controls in `box` onto its title: one tooltip,
    shown only while hovering the title, explaining each control by name.
    The box's own tooltip leads. With `keep`, the controls keep their tooltips
    as well. Returns the help text (rich text)."""
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
        if w.property("tipMirror"):         # a label echoing its input's tip
            if not keep:
                w.setToolTip("")
            continue
        name = _field_label(w, box)
        found[id(w)] = (f"<li><b>{html.escape(name)}</b> — {_html(tip)}</li>"
                        if name else f"<li>{_html(tip)}</li>")
        if not keep:
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


def sections_help(panel: QWidget, keep: bool = False) -> None:
    """`section_help` on every group box in `panel`. Call once the panel is
    built; any module's panel can opt in with this one line. `keep` leaves
    each control's own tooltip too, besides the list on the title."""
    for box in panel.findChildren(QGroupBox):
        section_help(box, keep)


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


# ── a bar between two limits ──────────────────────────────────────────────────

class RangeBar(QWidget):
    """A slider whose ends are limits that move (e.g. exposure: the camera's
    minimum to the longest the frame rate allows), with the value beside it.
    Logarithmic, so short values are as easy to pick as long ones.
    `valueChanged(v)` fires while dragging; `editingFinished` once, on
    release (or after a key/wheel step)."""

    valueChanged = pyqtSignal(float)
    editingFinished = pyqtSignal()

    _STEPS = 1000

    def __init__(self, lo: float, hi: float, value: float, *,
                 fmt=lambda v: f"{v:g}", parent=None) -> None:
        super().__init__(parent)
        from PyQt6.QtWidgets import QSlider
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._sld = QSlider(Qt.Orientation.Horizontal)
        self._sld.setRange(0, self._STEPS)
        self._lbl = QLabel()
        self._lbl.setMinimumWidth(50)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._sld, 1)
        lay.addWidget(self._lbl)
        self._fmt = fmt
        self._lo, self._hi = float(lo), float(hi)
        self._v = min(max(float(value), self._lo), self._hi)
        self._sync = False
        self._sld.valueChanged.connect(self._moved)
        self._sld.sliderReleased.connect(self.editingFinished)
        self._place()

    # value <-> slider position, on a log scale
    def _to_pos(self, v: float) -> int:
        if self._hi <= self._lo:
            return 0
        import math
        f = math.log(v / self._lo) / math.log(self._hi / self._lo)
        return int(round(f * self._STEPS))

    def _from_pos(self, pos: int) -> float:
        if self._hi <= self._lo:
            return self._lo
        return self._lo * (self._hi / self._lo) ** (pos / self._STEPS)

    def _place(self) -> None:
        self._sync = True
        try:
            self._sld.setValue(self._to_pos(self._v))
        finally:
            self._sync = False
        self._lbl.setText(self._fmt(self._v))

    def _moved(self, pos: int) -> None:
        if self._sync:
            return
        self._v = self._from_pos(pos)
        self._lbl.setText(self._fmt(self._v))
        self.valueChanged.emit(self._v)
        if not self._sld.isSliderDown():    # a key or wheel step
            self.editingFinished.emit()

    def value(self) -> float:
        return self._v

    def setValue(self, v: float) -> None:
        """Clamped to the limits; emits like a user change if it moved."""
        v = min(max(float(v), self._lo), self._hi)
        if abs(v - self._v) < 1e-9:
            return
        self._v = v
        self._place()
        self.valueChanged.emit(v)
        self.editingFinished.emit()

    def setRange(self, lo: float, hi: float) -> None:
        """New limits; a value outside them is pulled in (and emitted)."""
        self._lo, self._hi = float(lo), max(float(hi), float(lo))
        old = self._v
        self._v = min(max(self._v, self._lo), self._hi)
        self._place()
        if abs(self._v - old) > 1e-9:
            self.valueChanged.emit(self._v)
            self.editingFinished.emit()

    def minimum(self) -> float:
        return self._lo

    def maximum(self) -> float:
        return self._hi

