"""The module cards: an accent header over a small live view of the module's
state, with its settings folded under it."""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QEvent, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                             QVBoxLayout, QWidget, QWIDGETSIZE_MAX)

from acqApp import style
from acqApp.widgets import _GEOM_APP, _GEOM_ORG, OPEN, SHUT

REFRESH_MS = 250                # a glance, not a preview
CARD_MIN_W = 190


def _rgba(c: QColor, a: int) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{a})"


def _line(lbl: QLabel) -> None:
    """A long text clips rather than widening the whole dock."""
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)


class MiniView(QWidget):
    """A card's live body: polls its source while shown."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._failed = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._safe_refresh)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._safe_refresh()
        self._timer.start()

    def hideEvent(self, e) -> None:
        self._timer.stop()
        super().hideEvent(e)

    def _safe_refresh(self) -> None:
        # An escaping exception in a slot aborts the process.
        try:
            self.refresh()
        except Exception as e:                      # noqa: BLE001
            if not self._failed:
                self._failed = True
                print(f"[sidebar] {type(self).__name__}: "
                      f"{type(e).__name__}: {e}")

    def refresh(self) -> None:
        """Re-read the source; cheap when nothing changed."""


class MiniText(MiniView):
    """A bold value then a muted detail, on one line; `source()` -> (value,
    detail). Clipped text is in the tooltip."""

    def __init__(self, source: Callable[[], tuple[str, str]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._shown: tuple[str, str] | None = None
        self._value = QLabel()
        self._value.setStyleSheet("font-weight:bold")
        self._detail = QLabel()
        self._detail.setStyleSheet(f"color:{style.muted()}")
        _line(self._detail)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self._value)
        lay.addWidget(self._detail, 1)

    def refresh(self) -> None:
        got = self._source()
        if got == self._shown:
            return                                  # each set is a relayout
        self._shown = got
        value, detail = got
        self._value.setText(value)
        self._value.setVisible(bool(value))
        self._detail.setText(detail)
        tone = style.WARN if detail.startswith("⚠") else style.muted()
        self._detail.setStyleSheet(f"color:{tone}")
        self.setToolTip(" · ".join(t for t in got if t))


class _Card(QFrame):
    """One line: an accent chip and title, then the module's mini view. A
    click on it, not on the view's own controls, is `clicked`."""

    clicked = pyqtSignal()

    def __init__(self, key: str, label: str, mini: QWidget | None) -> None:
        super().__init__()
        self.setObjectName("moduleCard")
        self.setMinimumWidth(CARD_MIN_W)
        c = QColor(style.HEX.get(key, "#888"))
        self.setStyleSheet(
            f"QFrame#moduleCard{{border:1px solid {_rgba(c, 110)};"
            "border-radius:5px}"
            f"QFrame#moduleCard:hover{{background:{_rgba(c, 28)}}}"
            f'QFrame#moduleCard[open="true"]{{border:2px solid {c.name()};'
            f"background:{_rgba(c, 45)}}}")

        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background:{c.name()};border-radius:2px")
        self.title = QLabel(label)
        self.title.setStyleSheet("font-weight:bold")

        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(8, 3, 8, 3)
        self._lay.setSpacing(8)
        self._lay.addWidget(chip)
        self._lay.addWidget(self.title)
        self.mini = None
        self.set_mini(mini)

    def set_mini(self, mini: QWidget | None) -> None:
        if self.mini is not None:
            self.mini.setParent(None)
            self.mini.deleteLater()     # its view polls a module
        self.mini = mini
        if mini is not None:
            self._lay.addWidget(mini, 1)

    def set_open(self, on: bool) -> None:
        self.setProperty("open", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e) -> None:
        e.accept()                      # or the release goes elsewhere

    def mouseReleaseEvent(self, e) -> None:
        if (e.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(e.position().toPoint())):
            self.clicked.emit()


class Fold(QWidget):
    """A module's card with its settings folded under it: a click on the card
    opens or shuts them, remembered per key. No body: an inert card, there
    only for its view's own controls."""

    toggled = pyqtSignal(bool)

    def __init__(self, key: str, label: str, mini: QWidget | None,
                 body: QWidget | None, *, default_open: bool = False) -> None:
        super().__init__()
        self.key = key
        self.body = body
        self._label = label
        self._open = False
        self._setting = f"fold/{key}"
        self.card = _Card(key, label, mini)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.card)
        if body is None:
            return
        lay.addWidget(body, 1)
        self.card.setToolTip(f"Click to show or hide the {label} settings.")
        self.card.clicked.connect(lambda: self.set_open(not self._open))
        saved = QSettings(_GEOM_ORG, _GEOM_APP).value(self._setting)
        self.set_open(default_open if saved is None
                      else saved not in (False, "false", "0", 0), remember=False)

    def is_open(self) -> bool:
        return self._open

    def set_open(self, on: bool, *, remember: bool = True) -> None:
        if self.body is None:
            return
        self._open = on
        self.body.setVisible(on)
        self.card.set_open(on)
        self.card.title.setText(f"{OPEN if on else SHUT} {self._label}")
        self._clamp()
        if remember:
            QSettings(_GEOM_ORG, _GEOM_APP).setValue(self._setting,
                                                     "1" if on else "0")
        self.toggled.emit(on)

    def sizeHint(self) -> QSize:
        """Open over a scroll area, as tall as what it holds: a QScrollArea
        asks ~24 lines whatever its content, and a splitter gives it that."""
        hint = super().sizeHint()
        inner = getattr(self.body, "widget", lambda: None)()
        if self._open and isinstance(inner, QWidget):
            lay = self.layout()
            hint.setHeight(self.card.sizeHint().height() + lay.spacing()
                           + inner.sizeHint().height()
                           + 2 * self.body.frameWidth())
        return hint

    def _clamp(self) -> None:
        """Shut, no taller than the card: a splitter ignores size policies."""
        self.setMaximumHeight(QWIDGETSIZE_MAX if self._open or self.body is None
                              else self.card.sizeHint().height())

    def event(self, e) -> bool:
        # The card's mini view grows and shrinks a line as its text comes and goes.
        if e.type() == QEvent.Type.LayoutRequest:
            self._clamp()
        return super().event(e)
