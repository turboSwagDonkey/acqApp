"""
Voltage-cam toy — live image + ΔF/F trace + acquisition settings.

  python voltage_cam/_toy.py            # real Hamamatsu ORCA-Fire
  python voltage_cam/_toy.py --mock     # synthetic frames
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

# ── DCAM pre-init via pylablib (before Qt to avoid DLL path conflicts) ────────
_mock = "--mock" in sys.argv
if not _mock:
    try:
        from pylablib.devices import DCAM as _D
        if _D.get_cameras_number():
            print("[OK] ORCA-Fire detected via DCAM")
        else:
            print("No DCAM cameras found — mock")
            _mock = True
    except Exception as _e:
        print(f"DCAM unavailable ({_e}) — mock")
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

from voltage_cam.acquisition import OrcaFireWorker, MockCameraWorker
from voltage_cam.recording   import RecordingManager
from voltage_cam.settings    import SettingsPanel

HISTORY  = 400
DS       = 4   # display downsample stride


class ToyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("voltage_cam toy" + ("  [mock]" if _mock else ""))
        self.resize(1200, 620)

        self._worker = None
        self._rec    = RecordingManager()
        self._f0     = None
        self._x_buf  = deque(maxlen=HISTORY)
        self._df_buf = deque(maxlen=HISTORY)
        self._n      = 0

        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._pull)

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        h = QHBoxLayout(root)

        # ── Left: image + histogram + buttons ─────────────────────────────────
        img_item = pg.ImageItem()
        self._img = img_item
        hist = pg.HistogramLUTWidget()
        hist.setImageItem(img_item)
        hist.setFixedWidth(80)

        gv = pg.GraphicsView()
        vb = pg.ViewBox(lockAspect=True, invertY=True)
        gv.setCentralItem(vb)
        vb.addItem(img_item)

        img_row = QHBoxLayout()
        img_row.addWidget(hist)
        img_row.addWidget(gv)
        img_row_w = QWidget(); img_row_w.setLayout(img_row)

        self._lbl_fps  = QLabel("FPS: —")
        self._lbl_size = QLabel("Frame: —")
        info_row = QHBoxLayout()
        info_row.addWidget(self._lbl_fps)
        info_row.addWidget(self._lbl_size)
        info_w = QWidget(); info_w.setLayout(info_row)

        self._btn = QPushButton("Start")
        self._btn.setCheckable(True)
        self._btn.toggled.connect(self._toggle)

        btn_f0 = QPushButton("Set F₀")
        btn_f0.clicked.connect(self._set_f0)

        self._btn_rec = QPushButton("Record")
        self._btn_rec.setCheckable(True)
        self._btn_rec.toggled.connect(self._toggle_rec)

        btn_row = QHBoxLayout()
        for b in (self._btn, btn_f0, self._btn_rec):
            btn_row.addWidget(b)
        btn_w = QWidget(); btn_w.setLayout(btn_row)

        img_col = QVBoxLayout()
        img_col.addWidget(img_row_w)
        img_col.addWidget(info_w)
        img_col.addWidget(btn_w)
        img_w = QWidget(); img_w.setLayout(img_col)
        h.addWidget(img_w, 3)

        # ── Right: ΔF/F plot + settings panel ─────────────────────────────────
        pw = pg.PlotWidget(title="ΔF/F")
        pw.setYRange(-100, 100)
        pw.showGrid(x=True, y=True, alpha=0.3)
        pw.setLabel("left",   "ΔF/F", units="%")
        pw.setLabel("bottom", "Frame")
        self._curve = pw.plot(pen=pg.mkPen("b", width=1.5))

        self._settings = SettingsPanel()
        self._settings.exposure_changed.connect(self._on_exposure_changed)

        right_col = QVBoxLayout()
        right_col.addWidget(pw, 3)
        right_col.addWidget(self._settings, 2)
        right_w = QWidget(); right_w.setLayout(right_col)
        h.addWidget(right_w, 2)

    # ── Acquisition ───────────────────────────────────────────────────────────

    def _toggle(self, on: bool):
        if on:
            cfg = self._settings.get_config()
            self._settings.set_running(True)
            self._lbl_size.setText(
                f"Frame: {cfg.frame_shape[1]}×{cfg.frame_shape[0]}"
            )
            if _mock:
                self._worker = MockCameraWorker(cfg)
            else:
                self._worker = OrcaFireWorker(0, cfg)
            self._worker.fps_update.connect(self._on_fps)
            self._worker.start()
            self._timer.start()
            self._btn.setText("Stop")
        else:
            self._timer.stop()
            if self._worker:
                self._worker.stop()
                self._worker = None
            self._settings.set_running(False)
            self._btn.setText("Start")
            self._lbl_fps.setText("FPS: —")

    def _on_fps(self, n: int, fps: float):
        self._lbl_fps.setText(f"FPS: {fps:.1f}   frames: {n}")

    def _on_exposure_changed(self, us: float):
        if self._worker is not None:
            self._worker.set_exposure(us)

    # ── Display loop ──────────────────────────────────────────────────────────

    def _pull(self):
        if not self._worker:
            return
        f = self._worker.get_latest()
        if f is None:
            return

        self._n += 1
        small = f[::DS, ::DS].astype("float32")
        lo, hi = np.percentile(small, [1, 99])
        self._img.setLevels([lo, hi])
        self._img.setImage(small, autoLevels=False)

        mean = float(small.mean())
        df = (mean - self._f0) / self._f0 * 100 if self._f0 else 0.0
        self._x_buf.append(self._n)
        self._df_buf.append(df)
        self._curve.setData(list(self._x_buf), list(self._df_buf))

        if self._rec.is_recording:
            done = self._rec.write(f)
            if done:
                self._rec.stop()
                self._btn_rec.setChecked(False)

    def _set_f0(self):
        if self._img.image is not None:
            self._f0 = float(self._img.image.mean())

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_rec(self, on: bool):
        if on:
            from pathlib import Path
            self._rec.start(Path("toy_output"), "vcam", n_frames=300)
            self._btn_rec.setText("Stop rec")
        else:
            n = self._rec.stop()
            self._btn_rec.setText("Record")
            print(f"Saved {n} frames")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, e):
        self._timer.stop()
        if self._worker:
            self._worker.stop()
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = ToyWindow()
    w.show()
    sys.exit(app.exec_())
