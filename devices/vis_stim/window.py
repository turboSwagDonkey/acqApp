"""The full-screen stimulus display: a QWidget + QPainter repainted on a
QTimer, in place of PTB's OpenWindow/DrawTexture/Flip. PTB-grade vsync
precision was traded away deliberately for simplicity.

VisStimController sets the trial (`set_trial`), the phase per tick
(`set_visible`) and the drift per frame (`set_offset`); this paints what it
was last told and emits `painted` so the controller can advance.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget

from . import grating as grating_mod
from .settings import StimParams

DEFAULT_HZ = 60.0
_WHITE = QColor(255, 255, 255)
_BLACK = QColor(0, 0, 0)


class StimDisplay(QWidget):
    painted = pyqtSignal()
    escape_pressed = pyqtSignal()   # abort the whole run
    skip_pressed = pyqtSignal()     # 'n' — skip the current trial

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Window)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._mode = "grating"          # "grating" | "map"
        self._img: QImage | None = None
        self._period = 1.0
        self._offset = 0.0
        self._orientation = 0.0
        self._visible = False
        self._bg = QColor(128, 128, 128)
        self._center = (0.0, 0.0)
        self._radius = 0.0
        self._solid = False             # sweep pretrials: white aperture
        # map mode
        self._map_ignored: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._map_regions: list[tuple[float, float, float, float]] = []
        self._map_grey = QColor(128, 128, 128)
        self._map_active = -1
        self._map_white = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)

    # ── driven by the controller ──────────────────────────────────────────
    def open_on(self, screen, hz: float = DEFAULT_HZ) -> None:
        self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._timer.start(max(1, int(round(1000.0 / max(hz, 1.0)))))

    def close_display(self) -> None:
        self._timer.stop()
        self.close()

    def set_trial(self, p: StimParams, screen_w: int, screen_h: int) -> None:
        """Texture and aperture, built once per trial (not per frame)."""
        self._mode = "grating"
        self._solid = False
        row = grating_mod.build_grating(p)
        w = int(row.shape[0])
        img = QImage(row.tobytes(), w, 1, w, QImage.Format.Format_Grayscale8)
        self._img = img.copy()      # detach from the numpy buffer
        self._period = max(float(p.WaveSpPeriod), 1e-6)
        self._offset = float(p.Phase) % self._period
        self._orientation = float(p.Orientation)
        cx, cy, r = grating_mod.aperture_geometry(p, screen_w, screen_h)
        self._center, self._radius = (cx, cy), r
        bkg = max(0.0, min(1.0, float(p.BKGColor)))
        self._bg = QColor.fromRgbF(bkg, bkg, bkg)

    def set_offset(self, offset: float) -> None:
        self._offset = offset % self._period

    def set_visible(self, on: bool) -> None:
        self._visible = on

    def set_orientation(self, deg: float) -> None:
        """No rebuild: orientation is only the paint-time rotation."""
        self._orientation = deg

    def set_solid(self, on: bool) -> None:
        """White aperture instead of the grating (sweep pretrials)."""
        self._solid = on

    # ── map trial ────────────────────────────────────────────────────────
    def set_map_trial(self, ignored: tuple[float, float, float, float],
                      regions: list[tuple[float, float, float, float]],
                      grey_level: float) -> None:
        """Once per trial: region geometry and the inactive-region grey
        (BKGColor)."""
        self._mode = "map"
        self._map_ignored = ignored
        self._map_regions = regions
        g = max(0.0, min(1.0, grey_level))
        self._map_grey = QColor.fromRgbF(g, g, g)
        self._map_active = -1
        self._map_white = True

    def set_map_state(self, active_index: int, white: bool) -> None:
        self._map_active = active_index
        self._map_white = white

    # ── painting ──────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._mode == "map":
            self._paint_map(painter)
        else:
            self._paint_grating(painter)
        painter.end()
        self.painted.emit()

    def _paint_grating(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), self._bg)
        if not self._visible or self._radius <= 0:
            return
        if not self._solid and self._img is None:
            return
        painter.save()
        cx, cy = self._center
        path = QPainterPath()
        path.addEllipse(QPointF(cx, cy), self._radius, self._radius)
        painter.setClipPath(path)
        if self._solid:
            painter.fillRect(self.rect(), _WHITE)
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            side = self._radius * 2.0
            painter.translate(cx, cy)
            painter.rotate(self._orientation)
            painter.translate(-side / 2.0, -side / 2.0)
            painter.drawImage(QRectF(0.0, 0.0, side, side), self._img,
                              QRectF(self._offset, 0.0, side, 1.0))
        painter.restore()

    def _paint_map(self, painter: QPainter) -> None:
        # Black first: covers rounding gaps between regions.
        painter.fillRect(self.rect(), _BLACK)
        painter.fillRect(QRectF(*self._map_ignored), _BLACK)
        active = _WHITE if self._map_white else _BLACK
        for i, rect in enumerate(self._map_regions):
            color = active if i == self._map_active else self._map_grey
            painter.fillRect(QRectF(*rect), color)

    # ── keyboard: ESC aborts, 'n' skips a trial (KbCheck in the .m code) ──
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
        elif event.key() == Qt.Key.Key_N:
            self.skip_pressed.emit()
        else:
            super().keyPressEvent(event)
