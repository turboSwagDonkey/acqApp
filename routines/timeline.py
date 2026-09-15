"""A visual timeline of a routine — Groups and Recordings made literal.

The table shows a routine as rows; this shows it as **time**, one cycle's
worth, drawn to scale. Three bands, top to bottom:

- A **repeat-group bracket**, spanning every repeat's segments as one block
  labelled "×N" — the group itself doesn't repeat visually (that would just
  draw the same bracket N times); its CONTENTS already do, since this reads
  the **expanded** play order (`play_order`), the same one the engine and
  `recording_run_ids` use.
- The **step blocks** themselves, one per position in that expanded order,
  colored by kind and widened by estimated duration (`estimate.step_seconds`)
  — a 10 s Wait draws wider than a 1 s one. Move/Display/Puff, which cost
  nothing or only a fixed settle, still get a visible minimum width so they
  stay clickable rather than collapsing to a sliver.
- A **recording bar**, drawn as SEPARATE segments — one per run
  `recording_run_ids` reports — rather than one bar spanning a whole
  repeated group's range. That gap between two bars over the same bracket
  *is* the answer to "how are recordings handled for repeated steps":
  each repeat gets its own file boundary, never one merged capture.

Only one cycle is drawn — `routine.cycles` repeating the whole thing is
named in the summary line instead of drawn out, so a cycles=50 routine
doesn't need a timeline fifty screens wide.

Qt-only, view-only: this reads a `Routine`, it never drives anything, so it
carries none of the actuation-safety weight `engine.py` does.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QPoint, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                             QScrollArea, QToolTip, QVBoxLayout, QWidget)

from acqApp import style
from acqApp.routines.estimate import clock, estimate, step_seconds
from acqApp.routines.settings import (Recording, Routine, group_region_at,
                                      play_order, recording_region_at,
                                      recording_run_ids)
from acqApp.routines.table import KIND_LABELS, GROUP_TINT, REC_TINT

PX_PER_SEC = 30.0
MIN_SEG_PX = 30.0
MAX_SEG_PX = 420.0
SEG_GAP = 2.0
ROW_Y = 26          # top of the step-block row
ROW_H = 34
GROUP_BAND_H = 20
REC_BAND_Y = ROW_Y + ROW_H + 4
REC_BAND_H = 12
MARGIN = 12

# Tied to the same per-subsystem colors the rest of the app uses for these
# instruments, so the timeline's palette is one the operator already knows.
_KIND_COLOR = {
    "move":    QColor(style.HEX["stage"]),
    "display": QColor(style.HEX["dmd"]),
    "wait":    QColor(style.HEX["sync"]),
    "puff":    QColor(style.HEX["puffer"]),
}


@dataclass(frozen=True)
class _Seg:
    pos: int             # position in the expanded play order
    step_index: int       # row in routine.steps
    kind: str
    label: str
    duration_s: float | None    # None = unknown (frames, no frame rate)
    x: float
    width: float


def _layout(routine: Routine, hz: float | None,
           order: list[int] | None = None) -> tuple[list[_Seg], float]:
    """One cycle's segments, left to right, plus the total width."""
    if order is None:
        order = play_order(routine)
    segs: list[_Seg] = []
    x = float(MARGIN)
    for pos, step_i in enumerate(order):
        step = routine.steps[step_i]
        secs, frames = step_seconds(step, hz)
        unknown = frames > 0
        dur = None if unknown else secs
        width = (MIN_SEG_PX if dur is None
                 else max(MIN_SEG_PX, min(MAX_SEG_PX, dur * PX_PER_SEC)))
        segs.append(_Seg(pos=pos, step_index=step_i, kind=step.kind,
                         label=step.label or step.describe(),
                         duration_s=dur, x=x, width=width))
        x += width + SEG_GAP
    return segs, x + MARGIN - SEG_GAP


def _group_spans(routine: Routine, order: list[int]) -> list[tuple[int, int, int]]:
    """(first_pos, last_pos, repeats) for each Group's FULL run — every
    repeat merged into one bracket, since the repeats already draw out as
    separate segments underneath it; see the module docstring."""
    spans: list[tuple[int, int, int]] = []
    active: tuple[int, int] | None = None      # (group_index, start_pos)
    for pos, step_i in enumerate(order):
        gi = group_region_at(routine, step_i)
        cur = None if active is None else active[0]
        if gi != cur:
            if active is not None:
                spans.append((active[1], pos - 1, routine.groups[active[0]].repeats))
            active = None if gi is None else (gi, pos)
    if active is not None:
        spans.append((active[1], len(order) - 1, routine.groups[active[0]].repeats))
    return spans


def _recording_runs(routine: Routine, order: list[int]) -> list[tuple[int, int, int]]:
    """(first_pos, last_pos, region) per SEPARATE recording run — the whole
    point of drawing these apart rather than merged; see `recording_run_ids`."""
    ids = recording_run_ids(routine, order)
    runs: list[tuple[int, int, int]] = []
    start = None
    prev = None
    for pos, rid in enumerate(ids):
        if rid != prev:
            if prev is not None:
                runs.append((start, pos - 1, recording_region_at(routine, order[start])))
            start = pos if rid is not None else None
        prev = rid
    if prev is not None:
        runs.append((start, len(order) - 1, recording_region_at(routine, order[start])))
    return runs


class _TimelineCanvas(QWidget):
    """The painted strip itself — a QScrollArea's child, not scrollable on
    its own, so it can just be as wide as the routine needs."""

    def __init__(self, routine: Routine, hz: float | None, parent=None) -> None:
        super().__init__(parent)
        self._routine = routine
        order = play_order(routine)
        self._segs, total_w = _layout(routine, hz, order)
        self._groups = _group_spans(routine, order)
        self._recs = _recording_runs(routine, order)
        self.setMinimumSize(max(200, int(total_w)),
                            REC_BAND_Y + REC_BAND_H + MARGIN)
        self.setMouseTracking(True)
        self.setToolTipDuration(60_000)

    def _seg_at(self, x: float) -> _Seg | None:
        for s in self._segs:
            if s.x <= x <= s.x + s.width:
                return s
        return None

    def event(self, ev) -> bool:
        if ev.type() == QEvent.Type.ToolTip:
            seg = self._seg_at(ev.pos().x())
            if seg is not None:
                dur = ("? (no frame rate set)" if seg.duration_s is None
                       else clock(seg.duration_s))
                QToolTip.showText(
                    ev.globalPos(),
                    f"{KIND_LABELS.get(seg.kind, seg.kind)} — {seg.label}\n"
                    f"step {seg.step_index + 1} · {dur}")
                return True
            QToolTip.hideText()
        return super().event(ev)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_groups(p)
            self._paint_steps(p)
            self._paint_recordings(p)
        finally:
            p.end()

    def _paint_groups(self, p: QPainter) -> None:
        if not self._segs:
            return
        pen = QPen(QColor(GROUP_TINT.red(), GROUP_TINT.green(),
                          GROUP_TINT.blue(), 200))
        pen.setWidth(2)
        p.setPen(pen)
        f = p.font()
        f.setPointSize(max(7, f.pointSize() - 1))
        p.setFont(f)
        for first, last, repeats in self._groups:
            x0, x1 = self._segs[first].x, self._segs[last].x + self._segs[last].width
            y = GROUP_BAND_H - 4
            p.drawLine(int(x0), y, int(x1), y)
            p.drawLine(int(x0), y - 5, int(x0), y)
            p.drawLine(int(x1), y - 5, int(x1), y)
            p.drawText(QRectF(x0, 2, x1 - x0, GROUP_BAND_H - 6),
                      Qt.AlignmentFlag.AlignCenter, f"×{repeats}")

    def _paint_steps(self, p: QPainter) -> None:
        for s in self._segs:
            color = _KIND_COLOR.get(s.kind, QColor("#888888"))
            rect = QRectF(s.x, ROW_Y, s.width, ROW_H)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color.darker(160) if s.duration_s is None else color)
            p.drawRoundedRect(rect, 3, 3)
            if s.duration_s is None:      # unknown duration — say so, not to scale
                p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.PenStyle.DashLine))
                p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            if s.width >= 34:
                p.setPen(QColor("#ffffff"))
                p.drawText(rect.adjusted(3, 0, -3, 0),
                          Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                          KIND_LABELS.get(s.kind, s.kind))

    def _paint_recordings(self, p: QPainter) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(REC_TINT.red(), REC_TINT.green(), REC_TINT.blue(), 210))
        for first, last, _region in self._recs:
            x0 = self._segs[first].x
            x1 = self._segs[last].x + self._segs[last].width
            # A real gap either side — this is a SEPARATE run from its
            # neighbour, even one drawn from the same Recording bracket.
            p.drawRoundedRect(QRectF(x0 + 1, REC_BAND_Y, x1 - x0 - 2, REC_BAND_H),
                              2, 2)


class TimelineDialog(QDialog):
    """One cycle of `routine`, to scale. `.exec()` it; nothing is editable
    here — double-clicking a step in the table is still how you change it."""

    def __init__(self, routine: Routine, hz: float | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Timeline — {routine.name}")
        self.resize(900, 260)
        v = QVBoxLayout(self)

        if not routine.steps:
            v.addWidget(QLabel("No steps yet — add one, then open the "
                               "timeline again."))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(False)
            scroll.setWidget(_TimelineCanvas(routine, hz))
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            v.addWidget(scroll, 1)

        v.addWidget(_legend())

        note = QLabel(_summary(routine, hz))
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa0a6;")
        v.addWidget(note)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.clicked.connect(self.accept)
        v.addWidget(box)


def _legend() -> QWidget:
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    for kind, color in _KIND_COLOR.items():
        h.addWidget(_swatch(color, KIND_LABELS[kind]))
    h.addWidget(_swatch(QColor(GROUP_TINT.red(), GROUP_TINT.green(),
                               GROUP_TINT.blue()), "repeat group"))
    h.addWidget(_swatch(QColor(REC_TINT.red(), REC_TINT.green(),
                               REC_TINT.blue()), "recording"))
    h.addStretch(1)
    return row


def _swatch(color: QColor, text: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 10, 0)
    h.setSpacing(4)
    chip = QLabel()
    chip.setFixedSize(12, 12)
    chip.setStyleSheet(f"background:{color.name()}; border-radius:2px;")
    h.addWidget(chip)
    h.addWidget(QLabel(text))
    return w


def _summary(routine: Routine, hz: float | None) -> str:
    est = estimate(routine, hz)
    bits = [f"one cycle shown, to scale"
            + (f" — the whole thing repeats ×{routine.cycles}"
               if routine.cycles > 1 else "")]
    bits.append(est.text() + (f" total (at {est.hz:g} Hz)" if est.hz else
                              " total"))
    if any(s.kind == "wait" and s.unit == "frames" for s in routine.steps) \
            and not hz:
        bits.append("dashed blocks: duration unknown, no camera frame rate set")
    return " · ".join(bits)
