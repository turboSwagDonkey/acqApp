"""Shared panel widgets. Qt only; knows nothing about devices."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (QEvent, QObject, QSettings, QSize, Qt, pyqtProperty,
                          pyqtSignal)
from PyQt6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QCheckBox,
                             QComboBox,
                             QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLayout, QListWidget,
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


class ElidedLabel(QLabel):
    """One line that never widens its panel: a long path is cut in the middle
    to fit, and the full text is the tooltip."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PyQt6.QtWidgets import QSizePolicy
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 1)))


class _EnabledMirror(QObject):
    """Keeps a label's enabled state in step with its input's, so a greyed
    input greys its caption too."""

    def __init__(self, src: QWidget, lbl: QLabel) -> None:
        super().__init__(src)
        self._lbl = lbl
        src.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.Type.EnabledChange:
            self._lbl.setEnabled(obj.isEnabled())
        return False


def gate(switches, *dependents: QWidget) -> None:
    """Grey out `dependents` while none of the check boxes in `switches` is
    ticked: the numbers behind a switch look dead when the switch is off.
    Follows the switch however it changes, including code setting it."""
    if isinstance(switches, QAbstractButton):
        switches = [switches]

    def sync(*_a) -> None:
        on = any(s.isChecked() for s in switches)
        for w in dependents:
            w.setEnabled(on)

    for s in switches:
        s.toggled.connect(sync)
    sync()


def show_item(w: QWidget, on: bool) -> None:
    """Show or hide one `pairs_grid` input together with its caption."""
    w.setVisible(on)
    lbl = getattr(w, "_grid_label", None)
    if lbl is not None:
        lbl.setVisible(on)


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
                if first is not None:       # for _field_label
                    first.setProperty("gridLabel", label)
                g.addWidget(lbl, r, base)
                cols[k]["lab"].append(lbl)
                if not isinstance(w, QLayout):
                    _EnabledMirror(w, lbl)
                    w._grid_label = lbl     # for show_item
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
    # For align_grids; a wide combo or a one-column grid must not push every
    # section's next column right, so only spin boxes in multi-column grids.
    g._pair_widths = [(g.columnMinimumWidth(3 * k),
                       max((x.width() for x in c["inp"]
                            if items > 1 and isinstance(x, QAbstractSpinBox)),
                           default=0))
                      for k, c in enumerate(cols)]
    return g


def align_grids(panel: QWidget) -> None:
    """Line up every `pairs_grid` in `panel`: column k of each section gets the
    widest label and input of any section's column k, so the boxes start at
    one x down the whole panel instead of per section."""
    grids = [g for g in panel.findChildren(QGridLayout)
             if getattr(g, "_pair_widths", None)]
    n = max((len(g._pair_widths) for g in grids), default=0)
    for k in range(n):
        cols = [g._pair_widths[k] for g in grids if k < len(g._pair_widths)]
        lw = max(c[0] for c in cols)
        iw = max(c[1] for c in cols)
        for g in grids:
            if k < len(g._pair_widths):
                g.setColumnMinimumWidth(3 * k, lw)
                g.setColumnMinimumWidth(3 * k + 1, iw)


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
        # A nested grid caches its size; without this the box keeps it.
        for lay in box.findChildren(QLayout):
            lay.invalidate()
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
    if w.property("gridLabel"):             # set by pairs_grid
        return str(w.property("gridLabel")).rstrip(":")
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


# ── pills: the ROI editor's tool-strip look, shared by every panel ────────────
PILL_GAP = 6        # between pills in one group
ROW_GAP = 8         # between buttons, and between groups (twice this)


def check(label: str, *, checked: bool = False, tip: str = "") -> QCheckBox:
    """A plain on/off option: the default. Keep `pill` for the few that
    matter (a light, a mode, the panel's main switch)."""
    b = QCheckBox(label)
    b.setChecked(checked)
    if tip:
        b.setToolTip(tip)
    return b


def pill(label: str, key: str, *, checked: bool = False,
         tip: str = "") -> QPushButton:
    """An on/off option as a rounded accent toggle (`style.toggle_btn`): pale
    off, full accent on. Same isChecked/setChecked/toggled as a QCheckBox.
    Sparingly: only for the controls that matter (see `check`)."""
    from acqApp import style
    b = QPushButton(label)
    b.setCheckable(True)
    b.setChecked(checked)
    b.setStyleSheet(style.toggle_btn(key))
    if tip:
        b.setToolTip(tip)
    # Room for the bold "on" label, so lighting a pill never clips its text.
    from PyQt6.QtGui import QFont, QFontMetrics
    b.ensurePolished()
    bold = QFont(b.font())
    bold.setBold(True)
    b.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(label) + 2 * 8 + 2 + 8)
    return b


class PillGroup(QWidget):
    """Separate rounded pills, exactly one lit: a mode or tool choice, the
    ROI editor's Rectangle/Circle/… strip. API as `SegmentedSwitch`;
    `options` are (label, key) or (label, key, tooltip)."""

    changed = pyqtSignal(str)

    def __init__(self, options, key: str, parent=None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(PILL_GAP)
        self._buttons: dict[str, QPushButton] = {}
        for label, k, *tip in options:
            b = pill(label, key, tip=tip[0] if tip else "")
            b.setAutoExclusive(True)
            b.clicked.connect(lambda _c, k=k: self.changed.emit(k))
            lay.addWidget(b)
            self._buttons[k] = b
        self.set_value(options[0][1])

    def value(self) -> str:
        return next(k for k, b in self._buttons.items() if b.isChecked())

    def set_value(self, key: str) -> None:
        self._buttons[key].setChecked(True)

    def button(self, key: str) -> QPushButton:
        return self._buttons[key]


class SlideSwitch(QAbstractButton):
    """A physical two-position switch: a knob in the accent colour slides
    between `off_text` (left, unchecked) and `on_text` (right, checked).
    Checkable button API: isChecked/setChecked/toggled/click."""

    SLIDE_MS = 140

    def __init__(self, off_text: str, on_text: str, key: str,
                 parent=None) -> None:
        from PyQt6.QtCore import QPropertyAnimation
        super().__init__(parent)
        self._texts = (off_text, on_text)
        self._color = key
        self._pos = 0.0                    # knob: 0 = left, 1 = right
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(self.SLIDE_MS)
        self.toggled.connect(self._slide)

    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = pyqtProperty(float, _get_knob, _set_knob)   # what the animation drives

    def _slide(self, on: bool) -> None:
        self._anim.stop()
        if not self.isVisible():           # nothing to watch: jump there
            self._set_knob(1.0 if on else 0.0)
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _bold(self):
        from PyQt6.QtGui import QFont
        f = QFont(self.font())
        f.setBold(True)
        return f

    def sizeHint(self) -> QSize:
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self._bold())
        half = max(fm.horizontalAdvance(t) for t in self._texts) + 24
        return QSize(2 * half + 4, fm.height() + 14)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _ev) -> None:
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter, QPen
        from acqApp import style
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(style.HEX[self._color])
        if not self.isEnabled():
            accent = QColor("#555")
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rad = r.height() / 2
        p.setPen(QPen(accent, 1.5))
        p.setBrush(QColor(style.line()).darker(160))
        p.drawRoundedRect(r, rad, rad)
        half = r.width() / 2
        knob = QRectF(r.left() + 2 + self._pos * (half - 2), r.top() + 2,
                      half - 2, r.height() - 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(knob, knob.height() / 2, knob.height() / 2)
        p.setFont(self._bold())
        for i, text in enumerate(self._texts):
            cell = QRectF(r.left() + i * half, r.top(), half, r.height())
            lit = abs(self._pos - i) < 0.5
            p.setPen(QColor("white") if lit else QColor(style.muted()))
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


def button_row(*left, right=()) -> QHBoxLayout:
    """Buttons at their natural width, ROW_GAP apart, kept left; `right` ones
    pushed to the far edge. A None in `left` is a group break (2×ROW_GAP); a
    QLayout is nested. Use instead of buttons stretched across the panel."""
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(ROW_GAP)
    for it in left:
        if it is None:
            lay.addSpacing(ROW_GAP)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    lay.addStretch()
    for it in right:
        lay.addWidget(it)
    return lay


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

