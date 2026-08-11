r"""
Pupil-cam toy — bring-up harness for the Basler + the annulus tracker.

  ..\..\.venv\Scripts\python.exe pupil_cam\_toy.py           # real Basler camera
  ..\..\.venv\Scripts\python.exe pupil_cam\_toy.py --mock    # synthetic

Shows the whole IMAQ-style search, not just the answer: the annulus band the
rays sweep, every edge point they found (green = used in the fit, red =
rejected as an outlier), and the fitted pupil. That is what you tune against —
a wrong radius is usually obvious as rays latching onto an eyelash or a glint.

Click the image to place the annulus by hand when the auto-seed picks the wrong
dark region (the LabVIEW operator workflow).
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2]))

# Diagnostic prints in the device modules use characters a non-UTF-8 console
# cannot encode; unguarded, that raises inside the acquisition thread and
# looks like a device failure. See acqApp/console.py.
from acqApp.console import enable_safe_console
enable_safe_console()

# ── Basler pre-init (before Qt / numpy / pyqtgraph) ──────────────────────────
_mock = "--mock" in sys.argv
_cam = None
if not _mock:
    from acqApp.pupil_cam.acquisition import open_camera
    _cam = open_camera()
    if _cam is None:
        print("[pupil_cam] falling back to the mock worker")
        _mock = True
# ─────────────────────────────────────────────────────────────────────────────

import os
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from collections import deque
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QVBoxLayout, QWidget,
)
import pyqtgraph as pg
pg.setConfigOptions(imageAxisOrder="row-major")

from acqApp.pupil_cam.acquisition import PupilCameraWorker, MockPupilCameraWorker
from acqApp.pupil_cam.control     import LedController, MockLedController
from acqApp.pupil_cam.settings    import SettingsPanel
from acqApp.pupil_cam.tracking    import PupilTracker
from acqApp.pupil_cam.recording   import FrameWriter, TrackingLog

HISTORY = 300
_THETA = np.linspace(0, 2 * np.pi, 64)      # outline vertices, precomputed


class ToyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pupil_cam toy" + ("  [mock]" if _mock else ""))
        self.resize(1180, 620)
        self._worker = None
        try:
            self._led = LedController()
        except Exception as e:
            print(f"[pupil_cam] LED controller failed ({e}) — mock")
            self._led = MockLedController()

        self._tracker   = PupilTracker()
        self._frame_rec = FrameWriter()
        self._track_rec = TrackingLog()
        self._n         = 0
        self._levels_n  = 0
        self._r_buf     = deque(maxlen=HISTORY)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._pull)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self):
        w = QWidget()
        self.setCentralWidget(w)
        h = QHBoxLayout(w)

        # ── Image column ──────────────────────────────────────────────────────
        self._img_item = pg.ImageItem()
        gv = pg.GraphicsView()
        self._vb = pg.ViewBox(lockAspect=True, invertY=True)
        gv.setCentralItem(self._vb)
        self._vb.addItem(self._img_item)

        # annulus band the rays sweep (dashed), then the edge points, then the
        # fitted pupil on top
        dash = pg.mkPen("#ffbf00", width=1, style=pg.QtCore.Qt.PenStyle.DashLine)
        self._ann_in  = pg.PlotCurveItem(pen=dash)
        self._ann_out = pg.PlotCurveItem(pen=dash)
        self._pts_in  = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush("lime"))
        self._pts_out = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush("red"))
        self._outline = pg.PlotCurveItem(pen=pg.mkPen("cyan", width=2))
        for it in (self._ann_in, self._ann_out, self._pts_out, self._pts_in,
                   self._outline):
            self._vb.addItem(it)

        self._vb.scene().sigMouseClicked.connect(self._on_click)

        self._lbl_fps = QLabel("FPS: —")
        self._lbl_fit = QLabel("no detection")

        img_col = QVBoxLayout()
        img_col.addWidget(gv)
        img_col.addWidget(self._lbl_fps)
        img_col.addWidget(self._lbl_fit)

        btn_row = QHBoxLayout()
        self._btn = QPushButton("Start"); self._btn.setCheckable(True)
        self._btn.toggled.connect(self._toggle)
        self._btn_led = QPushButton("LED on"); self._btn_led.setCheckable(True)
        self._btn_led.toggled.connect(self._toggle_led)
        self._btn_rec = QPushButton("Record"); self._btn_rec.setCheckable(True)
        self._btn_rec.toggled.connect(self._toggle_rec)
        self._btn_seed = QPushButton("Re-seed")
        self._btn_seed.setToolTip("Drop the current lock and re-detect from "
                                  "scratch on the next frame")
        self._btn_seed.clicked.connect(self._tracker.reset)
        for b in (self._btn, self._btn_led, self._btn_rec, self._btn_seed):
            btn_row.addWidget(b)
        bw = QWidget(); bw.setLayout(btn_row)
        img_col.addWidget(bw)

        iw = QWidget(); iw.setLayout(img_col)
        h.addWidget(iw, 3)

        # ── Radius plot ───────────────────────────────────────────────────────
        pw = pg.PlotWidget(title="Pupil radius (px)")
        pw.showGrid(x=True, y=True, alpha=0.3)
        pw.setLabel("left", "Radius", units="px")
        pw.setLabel("bottom", "Frame")
        self._curve = pw.plot(pen=pg.mkPen("lime", width=1.5))
        h.addWidget(pw, 2)

        # ── Settings ──────────────────────────────────────────────────────────
        self._panel = SettingsPanel()
        self._panel.exposure_changed.connect(self._on_exposure)
        self._panel.led_toggled.connect(self._btn_led.setChecked)
        h.addWidget(self._panel, 1)

    # ── Acquisition control ───────────────────────────────────────────────────

    def _toggle(self, on: bool):
        if on:
            s = self._panel.settings
            if _mock:
                self._worker = MockPupilCameraWorker(fps=s.fps)
            else:
                self._worker = PupilCameraWorker(_cam, exposure_us=s.exposure_us,
                                                 fps=s.fps)
            self._worker.fps_update.connect(self._on_fps)
            self._tracker.reset()
            self._levels_n = 0
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

    def _on_exposure(self, us: float):
        if self._worker is not None:
            self._worker.set_exposure(us)

    def _on_click(self, ev):
        """Place the annulus by hand — the LabVIEW operator workflow."""
        if self._worker is None or not self._vb.sceneBoundingRect().contains(
                ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        _, min_r, max_r = self._panel.track_params
        r = 0.5 * (min_r + max_r)
        self._tracker.seed(p.x(), p.y(), r)
        print(f"[pupil_cam] annulus seeded at ({p.x():.1f}, {p.y():.1f}) r={r:.1f}")

    # ── Display loop ──────────────────────────────────────────────────────────

    def _pull(self):
        if not self._worker:
            return
        f = self._worker.get_latest()
        if f is None:
            return

        self._n += 1
        # Re-scan the levels only occasionally: on a 1920x1200 Basler frame a
        # full min/max every tick is real work, and the levels barely move.
        self._levels_n += 1
        if self._levels_n <= 3 or self._levels_n % 30 == 0:
            self._img_item.setImage(f, autoLevels=True)
        else:
            self._img_item.setImage(f, autoLevels=False)

        thr, min_r, max_r = self._panel.track_params
        self._tracker.configure(threshold=thr, min_r=min_r, max_r=max_r,
                                **self._panel.track_kwargs)
        result = self._tracker.process(f)
        self._draw(result)

        if result.radius is not None:
            self._r_buf.append(result.radius)
        else:
            self._r_buf.append(float("nan"))
        self._curve.setData(list(range(len(self._r_buf))), list(self._r_buf))

        if self._frame_rec.is_recording:
            self._frame_rec.write(f)
            self._track_rec.write(self._n, result)

    def _draw(self, res):
        """Overlay the annulus, the per-ray edge points and the fitted pupil."""
        if res.edge_x is not None and len(res.edge_x):
            keep = res.inliers if res.inliers is not None else np.ones(
                len(res.edge_x), dtype=bool)
            self._pts_in.setData(res.edge_x[keep], res.edge_y[keep])
            self._pts_out.setData(res.edge_x[~keep], res.edge_y[~keep])
        else:
            self._pts_in.setData([], [])
            self._pts_out.setData([], [])

        if res.radius is None:
            self._outline.setData([], [])
            self._ann_in.setData([], [])
            self._ann_out.setData([], [])
            self._lbl_fit.setText("no detection")
            return

        cx, cy = res.center_x, res.center_y
        if res.axes is not None:
            a, b = res.axes
            t = np.radians(res.angle)
            ca, sa = np.cos(t), np.sin(t)
            u, v = a * np.cos(_THETA), b * np.sin(_THETA)
            self._outline.setData(cx + u * ca - v * sa, cy + u * sa + v * ca)
            shape = f"ellipse {a:.1f}x{b:.1f} px @ {res.angle:.0f}°"
        else:
            self._outline.setData(cx + res.radius * np.cos(_THETA),
                                  cy + res.radius * np.sin(_THETA))
            shape = f"circle r={res.radius:.2f} px"

        for item, rr in ((self._ann_in, res.radius * 0.45),
                         (self._ann_out, res.radius * 1.55)):
            item.setData(cx + rr * np.cos(_THETA), cy + rr * np.sin(_THETA))

        self._lbl_fit.setText(
            f"{shape}   centre ({cx:.1f}, {cy:.1f})   "
            f"conf {res.confidence:.2f}   rays {res.n_rays}   "
            f"rms {res.rms:.2f} px")

    def _toggle_led(self, on: bool):
        self._led.set(on)
        self._btn_led.setText("LED off" if on else "LED on")

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_rec(self, on: bool):
        if on:
            self._frame_rec.start(Path("toy_output"), "pupil")
            self._track_rec.start(Path("toy_output"), "pupil")
            self._btn_rec.setText("Stop rec")
        else:
            n = self._frame_rec.stop()
            self._track_rec.stop()
            self._btn_rec.setText("Record")
            print(f"[pupil_cam] saved {n} frames")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, e):
        self._timer.stop()
        if self._worker:
            self._worker.stop()
            self._worker = None
        self._frame_rec.stop()
        self._track_rec.stop()
        self._led.close()
        # The toy opened the camera in pre-init, so the toy closes it.
        if _cam is not None:
            try:
                _cam.Close()
                print("[pupil_cam] camera closed")
            except Exception as ex:
                print(f"[pupil_cam] camera close failed: {ex}")
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = ToyWindow()
    w.show()
    sys.exit(app.exec())
