"""The sidebar's module cards: an accent header over a small live view of the
module's state. Right-click opens the module's full page."""
from __future__ import annotations

from typing import Callable

import numpy as np
from PyQt6.QtCore import QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                             QVBoxLayout, QWidget, QWidgetAction)

from acqApp import style

REFRESH_MS = 250                # a glance, not a preview
CARD_MIN_W = 190
THUMB_H = 84
_THUMB_SIDE = 256               # frames are decimated to this before scaling


def _rgba(c: QColor, a: int) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{a})"


def _line(lbl: QLabel) -> None:
    """Centred; a long name clips rather than widening the whole bar."""
    lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
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
    """A value line over a muted detail line; `source()` -> (value, detail)."""

    def __init__(self, source: Callable[[], tuple[str, str]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._shown: tuple[str, str] | None = None
        self._value = QLabel()
        self._value.setStyleSheet("font-size:10pt;font-weight:bold")
        self._detail = QLabel()
        self._detail.setStyleSheet(f"color:{style.muted()}")
        for lbl in (self._value, self._detail):
            _line(lbl)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._value)
        lay.addWidget(self._detail)

    def refresh(self) -> None:
        got = self._source()
        if got == self._shown:
            return                                  # each set is a relayout
        self._shown = got
        value, detail = got
        self._value.setText(value)
        self._value.setVisible(bool(value))
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))


class _Thumb(QWidget):
    """A frame scaled to fit, dimmed unless lit, in a dashed accent box."""

    def __init__(self, color: QColor) -> None:
        super().__init__()
        self._color = color
        self._img: QImage | None = None
        self._lit = False
        self.setFixedHeight(THUMB_H)

    def sizeHint(self) -> QSize:
        return QSize(CARD_MIN_W - 20, THUMB_H)

    def set_frame(self, frame: np.ndarray | None, lit: bool) -> None:
        self._img = None if frame is None else _to_qimage(frame)
        self._lit = lit
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._img is not None:
            size = QSize(self._img.width(), self._img.height()).scaled(
                box.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            box = QRectF(box.x() + (box.width() - size.width()) / 2,
                         box.y() + (box.height() - size.height()) / 2,
                         size.width(), size.height())
            p.fillRect(box, Qt.GlobalColor.black)
            p.setOpacity(1.0 if self._lit else 0.4)
            p.drawImage(box, self._img)
            p.setOpacity(1.0)
        pen = QPen(self._color, 1.2,
                   Qt.PenStyle.SolidLine if self._lit else Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRect(box)
        if self._img is None:
            p.setPen(QColor(style.muted()))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, "nothing loaded")
        p.end()


def _to_qimage(frame: np.ndarray) -> QImage:
    step = max(1, max(frame.shape[:2]) // _THUMB_SIDE)
    small = np.ascontiguousarray(frame[::step, ::step], dtype=np.uint8)
    h, w = small.shape[:2]
    # copy(): the QImage would otherwise point into `small`'s buffer.
    return QImage(small.data, w, h, w, QImage.Format.Format_Grayscale8).copy()


class MiniPattern(MiniView):
    """A projector's frame and its name; `source()` -> (frame, name, lit)."""

    def __init__(self, key: str,
                 source: Callable[[], tuple[np.ndarray | None, str, bool]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._color = QColor(style.HEX.get(key, "#888"))
        # The frame itself, not its id: holding it keeps an id from recycling.
        self._shown: tuple | None = None
        self._thumb = _Thumb(self._color)
        self._caption = QLabel()
        _line(self._caption)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(self._thumb)
        lay.addWidget(self._caption)

    def refresh(self) -> None:
        frame, name, lit = self._source()
        now = (frame, name, lit)
        if (self._shown is not None and self._shown[0] is frame
                and self._shown[1:] == now[1:]):
            return
        self._shown = now
        self._thumb.set_frame(frame, lit)
        self._caption.setText(name)
        self._caption.setVisible(bool(name))     # the empty box says it
        self._caption.setStyleSheet(
            f"color:{self._color.name()};font-weight:bold" if lit
            else f"color:{style.muted()}")


class _CardFrame(QFrame):
    def __init__(self, action: QWidgetAction, key: str, label: str,
                 mini: QWidget | None) -> None:
        super().__init__()
        self._action = action
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
        title = QLabel(label)
        title.setStyleSheet("font-weight:bold")
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addStretch(1)
        head.addWidget(chip)
        head.addWidget(title)
        head.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 6)
        lay.setSpacing(3)
        lay.addLayout(head)
        if mini is not None:
            lay.addWidget(mini)

    def set_open(self, on: bool) -> None:
        self.setProperty("open", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def contextMenuEvent(self, e) -> None:
        # Accepted, or the toolbar's own hide-me menu pops up too.
        e.accept()
        if self._action.isCheckable():
            self._action.trigger()


class ModuleCard(QWidgetAction):
    """A sidebar item: checkable like the button it replaced (checked = its
    page is open), and right-click triggers it. A module with no page gets
    an inert card, there only for its view's own controls."""

    def __init__(self, key: str, label: str, mini: QWidget | None,
                 tip: str, parent, *, page: bool = True) -> None:
        super().__init__(parent)
        self.setText(label)
        self.setCheckable(page)
        self.card = _CardFrame(self, key, label, mini)
        self.card.setToolTip(f"{tip}\nRight-click to open." if page else tip)
        self.setDefaultWidget(self.card)
        self.toggled.connect(self.card.set_open)
