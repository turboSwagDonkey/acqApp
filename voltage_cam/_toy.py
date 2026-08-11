"""
Voltage-cam toy — live image + ΔF/F trace + acquisition settings.

  python voltage_cam/_toy.py            # real Hamamatsu ORCA-Fire
  python voltage_cam/_toy.py --mock     # synthetic frames
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2]))

# Diagnostic prints in the device modules use characters a non-UTF-8 console
# cannot encode; unguarded, that raises inside the acquisition thread and
# looks like a device failure. See acqApp/console.py.
from acqApp.console import enable_safe_console
enable_safe_console()

# ── DCAM pre-init via pylablib (before Qt to avoid DLL path conflicts) ────────
# Opening the ORCA is slow (~7 s), so open it ONCE here and reuse the handle for
# every Start/Stop — otherwise each Start would re-pay that cost.
_mock = "--mock" in sys.argv
_cam = None
if not _mock:
    try:
        import time as _time
        from pylablib.devices import DCAM as _D
        if _D.get_cameras_number():
            print("[OK] ORCA-Fire detected via DCAM — opening (one-time, ~7 s)…")
            _t0 = _time.perf_counter()
            _cam = _D.DCAMCamera(idx=0)
            print(f"[voltage_cam] camera opened in {_time.perf_counter() - _t0:.1f}s")
        else:
            print("No DCAM cameras found — mock")
            _mock = True
    except Exception as _e:
        print(f"DCAM unavailable ({_e}) — mock")
        _mock = True
# ─────────────────────────────────────────────────────────────────────────────

import os
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from collections import deque

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QVBoxLayout, QWidget,
)
import pyqtgraph as pg
pg.setConfigOptions(imageAxisOrder="row-major")

from acqApp.voltage_cam.acquisition import OrcaFireWorker, MockCameraWorker
from acqApp.voltage_cam.recording   import RecordingManager
from acqApp.voltage_cam.settings    import SettingsPanel

HISTORY      = 400
DS           = 4    # display downsample stride
LEVELS_EVERY = 15   # recompute contrast levels every N display ticks


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
        self._levels: tuple[float, float] | None = None
        self._lvl_ctr = 0
        self._achievable = 0.0      # camera-reported max fps for this config
        self._rec_done = False      # set from the capture thread, cleared in _pull

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

        self._lbl_fps   = QLabel("FPS: —")
        self._lbl_size  = QLabel("Frame: —")
        self._lbl_limit = QLabel("Max: —")
        info_row = QHBoxLayout()
        info_row.addWidget(self._lbl_fps)
        info_row.addWidget(self._lbl_size)
        info_row.addWidget(self._lbl_limit)
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
            self._levels = None
            self._lvl_ctr = 0
            if _mock:
                self._worker = MockCameraWorker(cfg)
            else:
                self._worker = OrcaFireWorker(config=cfg, cam=_cam)
            self._worker.fps_update.connect(self._on_fps)
            if hasattr(self._worker, "timing_update"):
                self._worker.timing_update.connect(self._on_timing)
            if hasattr(self._worker, "drops_update"):
                self._worker.drops_update.connect(self._on_drops)
            if self._rec.is_recording:      # recording survives a Stop/Start
                self._worker.set_sink(self._on_recorded)
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
            # Clear the camera-reported figures — they describe the run that
            # just ended, and a stale "max fps" reads as if it still applies.
            self._achievable = 0.0
            self._lbl_limit.setText("Max: —")

    def _on_fps(self, n: int, fps: float):
        txt = f"FPS: {fps:.1f}   frames: {n}"
        if self._achievable:
            txt += f"   (max {self._achievable:.1f})"
        self._lbl_fps.setText(txt)

    def _on_timing(self, fps: float, exposure_limited: bool):
        """The camera's own answer for the running config — the number to beat."""
        self._achievable = fps
        note = " ⚠ exposure-limited" if exposure_limited else ""
        self._lbl_limit.setText(f"Max: {fps:.1f} fps{note}")

    def _on_drops(self, skipped: int, buffer_size: int):
        """Camera-side frame loss: we are not draining its buffer fast enough."""
        self._lbl_limit.setText(
            f"⚠ DROPPED {skipped} frames (buffer {buffer_size})")

    def _on_exposure_changed(self, us: float):
        if self._worker is not None:
            self._worker.set_exposure(us)

    # ── Display loop ──────────────────────────────────────────────────────────

    def _pull(self):
        if not self._worker:
            return
        # Recording finishes on the capture thread; finalise it here so the
        # widget work stays on the GUI thread.
        if self._rec_done:
            self._rec_done = False
            self._btn_rec.setChecked(False)
        f = self._worker.get_latest()
        if f is None:
            return

        self._n += 1
        small = f[::DS, ::DS]                        # strided uint16 view (no copy)
        # Contrast levels (the costly percentile) refresh a couple of times a
        # second, not every frame.
        if self._levels is None or self._lvl_ctr % LEVELS_EVERY == 0:
            lo, hi = np.percentile(small, [1, 99])
            self._levels = (float(lo), float(hi))
        self._lvl_ctr += 1
        self._img.setImage(small, autoLevels=False, levels=self._levels)

        mean = float(small.mean())
        df = (mean - self._f0) / self._f0 * 100 if self._f0 else 0.0
        self._x_buf.append(self._n)
        self._df_buf.append(df)
        self._curve.setData(list(self._x_buf), list(self._df_buf))

    def _set_f0(self):
        if self._img.image is not None:
            self._f0 = float(self._img.image.mean())

    # ── Recording ─────────────────────────────────────────────────────────────

    def _on_recorded(self, frame):
        """Worker sink — runs on the CAPTURE thread, so it only enqueues."""
        if self._rec.write(frame):
            self._rec_done = True

    def _toggle_rec(self, on: bool):
        if on:
            from pathlib import Path
            self._rec.start(Path("toy_output"), "vcam", n_frames=300)
            self._rec_done = False
            # Record from the worker sink (every frame), NOT from the ~30 Hz
            # display pull, which would sample only 1 frame in N.
            if self._worker is not None:
                self._worker.set_sink(self._on_recorded)
            self._btn_rec.setText("Stop rec")
        else:
            if self._worker is not None:
                self._worker.set_sink(None)
            dropped = self._rec.dropped      # read before stop() releases the buffer
            n = self._rec.stop()
            self._btn_rec.setText("Record")
            print(f"Saved {n} frames"
                  + (f" ({dropped} dropped — disk too slow)" if dropped else ""))

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, e):
        self._timer.stop()
        if self._worker:
            self._worker.set_sink(None)      # stop feeding a closing recorder
            self._worker.stop()
        # Close the recording first: the writer thread is a daemon and the HDF5
        # file is only valid once stop() drains and closes it, so quitting
        # mid-recording would otherwise abandon an open, truncated file.
        if self._rec.is_recording:
            n = self._rec.stop()
            print(f"Saved {n} frames (closed on exit)")
        if _cam is not None:                 # close the shared handle on exit
            try:
                _cam.close()
            except Exception:
                pass
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = ToyWindow()
    w.show()
    sys.exit(app.exec())
