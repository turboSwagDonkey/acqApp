"""
Pupil-cam toy — live image + detected radius trace.

  ..\..\.venv\Scripts\python.exe pupil_cam\_toy.py           # real Basler camera
  ..\..\.venv\Scripts\python.exe pupil_cam\_toy.py --mock    # synthetic
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

# ── Basler pre-init (before Qt / numpy / pyqtgraph) ──────────────────────────
_cam  = None
_mock = "--mock" in sys.argv
if not _mock:
    try:
        from pypylon import pylon as _pylon
        _factory = _pylon.TlFactory.GetInstance()
        _devices = _factory.EnumerateDevices()
        if _devices:
            _cam = _pylon.InstantCamera(_factory.CreateDevice(_devices[0]))
            _cam.Open()
            print(f"[OK] Basler camera: {_cam.GetDeviceInfo().GetModelName()}")
        else:
            print("No Basler cameras found — mock")
            _mock = True
    except Exception as _e:
        print(f"Camera init failed ({_e}) — mock")
        _mock = True
# ─────────────────────────────────────────────────────────────────────────────

from collections import deque
import numpy as np
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QVBoxLayout, QWidget,
)
import pyqtgraph as pg
pg.setConfigOptions(imageAxisOrder="row-major")

from pupil_cam.acquisition import PupilCameraWorker, MockPupilCameraWorker
from pupil_cam.control     import LedController, MockLedController
from pupil_cam.tracking    import detect
from pupil_cam.recording   import FrameWriter, TrackingLog

HISTORY = 300


class ToyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pupil_cam toy" + ("  [mock]" if _mock else ""))
        self.resize(900, 520)
        self._worker    = None
        try:
            self._led = LedController()
        except Exception as _e:
            print(f"LED controller failed ({_e}) — mock")
            self._led = MockLedController()
        self._frame_rec = FrameWriter()
        self._track_rec = TrackingLog()
        self._n         = 0
        self._r_buf     = deque(maxlen=HISTORY)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._pull)

    def _build(self):
        w = QWidget(); self.setCentralWidget(w)
        h = QHBoxLayout(w)

        # ── Image column ──────────────────────────────────────────────────────
        self._img_item = pg.ImageItem()
        gv = pg.GraphicsView()
        vb = pg.ViewBox(lockAspect=True, invertY=True)
        gv.setCentralItem(vb)
        vb.addItem(self._img_item)
        self._roi_circle = pg.CircleROI(
            [0, 0], [10, 10],
            pen=pg.mkPen("lime", width=2),
            movable=False, resizable=False,
        )
        vb.addItem(self._roi_circle)

        self._lbl_fps = QLabel("FPS: —")

        img_col = QVBoxLayout()
        img_col.addWidget(gv)
        img_col.addWidget(self._lbl_fps)

        btn_row = QHBoxLayout()
        self._btn = QPushButton("Start"); self._btn.setCheckable(True)
        self._btn.toggled.connect(self._toggle)
        self._btn_led = QPushButton("LED on"); self._btn_led.setCheckable(True)
        self._btn_led.toggled.connect(self._toggle_led)
        self._btn_rec = QPushButton("Record"); self._btn_rec.setCheckable(True)
        self._btn_rec.toggled.connect(self._toggle_rec)
        for b in (self._btn, self._btn_led, self._btn_rec):
            btn_row.addWidget(b)
        bw = QWidget(); bw.setLayout(btn_row)
        img_col.addWidget(bw)

        iw = QWidget(); iw.setLayout(img_col)
        h.addWidget(iw, 2)

        # ── Radius plot ───────────────────────────────────────────────────────
        pw = pg.PlotWidget(title="Pupil radius (px)")
        pw.showGrid(x=True, y=True, alpha=0.3)
        pw.setLabel("left",   "Radius", units="px")
        pw.setLabel("bottom", "Frame")
        self._curve = pw.plot(pen=pg.mkPen("lime", width=1.5))
        h.addWidget(pw, 1)

    # ── Acquisition control ───────────────────────────────────────────────────

    def _toggle(self, on: bool):
        if on:
            if _mock:
                self._worker = MockPupilCameraWorker()
            else:
                self._worker = PupilCameraWorker(_cam)
            self._worker.fps_update.connect(self._on_fps)
            self._worker.start()
            self._timer.start()
            self._btn.setText("Stop")
        else:
            self._timer.stop()
            if self._worker:
                self._worker.stop()
                self._worker = None
            self._btn.setText("Start")
            self._lbl_fps.setText("FPS: —")

    def _on_fps(self, n: int, fps: float):
        self._lbl_fps.setText(f"FPS: {fps:.1f}   frames: {n}")

    # ── Display loop ──────────────────────────────────────────────────────────

    def _pull(self):
        if not self._worker:
            return
        f = self._worker.get_latest()
        if f is None:
            return

        self._n += 1
        self._img_item.setImage(f.astype("float32"), autoLevels=True)

        result = detect(f)
        if result.radius is not None:
            cx, cy, r = result.center_x, result.center_y, result.radius
            self._roi_circle.setPos([cx - r, cy - r])
            self._roi_circle.setSize([2 * r, 2 * r])
            self._r_buf.append(r)
        else:
            self._r_buf.append(float("nan"))

        self._curve.setData(list(range(len(self._r_buf))), list(self._r_buf))

        if self._frame_rec.is_recording:
            self._frame_rec.write(f)
            self._track_rec.write(self._n, result)

    def _toggle_led(self, on: bool):
        self._led.set(on)
        self._btn_led.setText("LED off" if on else "LED on")

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_rec(self, on: bool):
        from pathlib import Path
        if on:
            self._frame_rec.start(Path("toy_output"), "pupil")
            self._track_rec.start(Path("toy_output"), "pupil")
            self._btn_rec.setText("Stop rec")
        else:
            n = self._frame_rec.stop()
            self._track_rec.stop()
            self._btn_rec.setText("Record")
            print(f"Saved {n} frames")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, e):
        self._timer.stop()
        if self._worker:
            self._worker.stop()
        self._led.close()
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = ToyWindow()
    w.show()
    sys.exit(app.exec_())
