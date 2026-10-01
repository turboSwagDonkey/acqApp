"""A large colour-coded banner for the routine's state, readable from across the
rig. Shows only what the panel already decided to say; owns no routine logic.
Click it to dismiss."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from acqApp.routines.engine import Phase

# phase -> (headline, background)
_LOOK = {
    Phase.RUNNING: ("RUNNING",               "#2e7d32"),
    Phase.ARMED:   ("ARMED",                 "#1565c0"),
    Phase.WAITING: ("WAITING FOR TRIGGER",   "#1565c0"),
    Phase.PAUSED:  ("PAUSED",                "#e65100"),
    Phase.DONE:    ("FINISHED",              "#455a64"),
}
_ABORTED = ("ABORTED", "#b71c1c")
_REARMING = ("RE-ARMING — DON'T TRIGGER", "#b26a00")


def look_for(phase: str, text: str) -> tuple[str, str] | None:
    """(headline, colour) for a state, None for idle. An abort is a pause with
    the fault "aborted", and a re-arm a wait whose edge would be lost; each
    reads as its own state."""
    if phase == Phase.PAUSED and "abort" in text.lower():
        return _ABORTED
    if phase == Phase.WAITING and text.startswith("RE-ARMING"):
        return _REARMING
    return _LOOK.get(phase)


class RoutineBanner(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        # Never take focus from whatever the operator is doing on the rig.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._head = QLabel()
        self._body = QLabel()
        self._head.setStyleSheet("color:white;font-size:34px;font-weight:700;")
        self._body.setStyleSheet("color:white;font-size:18px;")
        self._body.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 14)
        lay.addWidget(self._head)
        lay.addWidget(self._body)
        self.setMinimumWidth(520)
        self._painted: tuple | None = None

    def show_state(self, phase: str, text: str) -> None:
        look = look_for(phase, text)
        if look is None:                 # idle: nothing to announce
            self.hide()
            self._painted = None
            return
        if (look, text) != self._painted:
            self._painted = (look, text)
            head, colour = look
            self._head.setText(head)
            self._body.setText(text)
            self.setStyleSheet(f"RoutineBanner{{background:{colour};"
                               f"border-radius:10px;}}")
            self.adjustSize()
        if not self.isVisible():
            self._place()
            self.show()
        # On every update, not just on show: StaysOnTop alone doesn't keep an
        # unattended run's banner above windows the operator raised since.
        self.raise_()

    def _place(self) -> None:
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is not None:
            self.move(scr.center().x() - self.width() // 2, scr.top() + 24)

    def mousePressEvent(self, ev) -> None:      # noqa: N802
        self.hide()
