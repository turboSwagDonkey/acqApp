r"""
Wheel encoder toy — live voltage + speed trace.

  ..\..\.venv\Scripts\python.exe wheel\_toy.py
  ..\..\.venv\Scripts\python.exe wheel\_toy.py --mock
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2]))

# Diagnostic prints in the device modules use characters a non-UTF-8 console
# cannot encode; unguarded, that raises inside the acquisition thread and
# looks like a device failure. See acqApp/console.py.
from acqApp.console import enable_safe_console
enable_safe_console()

import os
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from collections import deque
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow,
    QPushButton, QVBoxLayout, QWidget, QLabel,
)
import pyqtgraph as pg

from acqApp.wheel.acquisition import EncoderWorker, MockEncoderWorker
from acqApp.wheel.recording   import CSVWriter

HISTORY = 500
_mock   = "--mock" in sys.argv


class ToyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("wheel toy")
        self.resize(900, 500)
        self._worker = None
        self._writer = CSVWriter()
        self._t_buf  = deque(maxlen=HISTORY)
        self._v_buf  = deque(maxlen=HISTORY)
        self._dist_buf = deque(maxlen=HISTORY)
        self._build()
        self._timer = QTimer(self); self._timer.setInterval(20)
        self._timer.timeout.connect(self._pull)

    def _build(self):
        w = QWidget(); self.setCentralWidget(w)
        vb = QVBoxLayout(w)

        self._lbl = QLabel("voltage: –  speed: –")
        vb.addWidget(self._lbl)

        plots = QWidget(); ph = QHBoxLayout(plots)
        self._pw_v    = pg.PlotWidget(title="Voltage (V)")
        self._pw_dist = pg.PlotWidget(title="Distance (m)")
        self._pw_v.showGrid(x=True, y=True, alpha=0.3)
        self._pw_dist.showGrid(x=True, y=True, alpha=0.3)
        self._curve_v    = self._pw_v.plot(pen=pg.mkPen("orange", width=1.5))
        self._curve_dist = self._pw_dist.plot(pen=pg.mkPen("cyan", width=1.5))
        ph.addWidget(self._pw_v); ph.addWidget(self._pw_dist)
        vb.addWidget(plots)

        btn_row = QHBoxLayout()
        self._btn = QPushButton("Start"); self._btn.setCheckable(True)
        self._btn.toggled.connect(self._toggle)
        btn_rec = QPushButton("Record CSV"); btn_rec.setCheckable(True)
        btn_rec.toggled.connect(self._toggle_rec)
        for b in (self._btn, btn_rec): btn_row.addWidget(b)
        bw = QWidget(); bw.setLayout(btn_row); vb.addWidget(bw)

    def _toggle(self, on):
        if on:
            self._worker = MockEncoderWorker() if _mock else EncoderWorker()
            # Surface worker failures — without this, a hardware error just leaves
            # a blank window with no clue (the QThread guard swallows the crash).
            self._worker.error.connect(self._on_error)
            self._worker.start(); self._timer.start()
            self._btn.setText("Stop")
        else:
            self._timer.stop()
            if self._worker: self._worker.stop(); self._worker = None
            self._btn.setText("Start")

    def _on_error(self, msg: str):
        self._lbl.setText(f"WORKER ERROR: {msg}")
        print(f"[wheel toy] worker error: {msg}", file=sys.stderr)

    def _pull(self):
        if not self._worker: return
        sample = self._worker.get_latest()
        if sample is None: return
        v, spd, dist, t = sample       # worker derives speed (mm/s) + distance (mm)
        self._t_buf.append(t); self._v_buf.append(v)
        self._dist_buf.append(dist / 1000.0)     # plot net distance in metres
        self._lbl.setText(
            f"voltage: {v:.4f} V   speed: {spd:+.1f} mm/s   "
            f"distance: {dist / 1000:.2f} m")
        x = list(self._t_buf)
        self._curve_v.setData(x, list(self._v_buf))
        self._curve_dist.setData(x, list(self._dist_buf))
        if self._writer.is_recording:
            self._writer.write(t, v)

    def _toggle_rec(self, on):
        if on:
            from pathlib import Path
            self._writer.start(Path("toy_output"), "wheel")
        else:
            n = self._writer.stop()
            print(f"Saved {n} samples")

    def closeEvent(self, e):
        if self._worker: self._worker.stop()
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion")
    w = ToyWindow(); w.show()
    sys.exit(app.exec())
