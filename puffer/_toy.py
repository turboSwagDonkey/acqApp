r"""
Puffer toy — fire button + event log.

  ..\..\.venv\Scripts\python.exe puffer\_toy.py
"""

import sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2]))

# Diagnostic prints in the device modules use characters a non-UTF-8 console
# cannot encode; unguarded, that raises inside the acquisition thread and
# looks like a device failure. See acqApp/console.py.
from acqApp.console import enable_safe_console
enable_safe_console()

import os
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget,
    QMainWindow, QPushButton, QVBoxLayout, QWidget, QDoubleSpinBox,
)
import pyqtgraph as pg

from acqApp.puffer.control   import MockPufferController
from acqApp.puffer.recording import EventLog


class ToyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("puffer toy")
        self.resize(500, 400)
        self._puffer = MockPufferController()
        self._puffer.puff_fired.connect(self._on_puff)
        self._log    = EventLog()
        self._t0     = time.perf_counter()
        self._build()

    def _build(self):
        w = QWidget(); self.setCentralWidget(w)
        v = QVBoxLayout(w)

        row = QHBoxLayout()
        row.addWidget(QLabel("Duration (s):"))
        self._spn = QDoubleSpinBox()
        self._spn.setRange(0.01, 5.0); self._spn.setSingleStep(0.05)
        self._spn.setDecimals(3); self._spn.setValue(0.100)
        row.addWidget(self._spn)
        rw = QWidget(); rw.setLayout(row); v.addWidget(rw)

        btn_fire = QPushButton("🔥 Fire puffer")
        btn_fire.setStyleSheet("font-size:14pt; padding:8px;")
        btn_fire.clicked.connect(lambda: self._puffer.fire(self._spn.value()))
        v.addWidget(btn_fire)

        rec_row = QHBoxLayout()
        self._btn_rec = QPushButton("Start log"); self._btn_rec.setCheckable(True)
        self._btn_rec.toggled.connect(self._toggle_rec)
        rec_row.addWidget(self._btn_rec)
        rrw = QWidget(); rrw.setLayout(rec_row); v.addWidget(rrw)

        self._lst = QListWidget()
        v.addWidget(QLabel("Events:")); v.addWidget(self._lst)

    def _on_puff(self, ts: float, dur: float):
        elapsed = ts - self._t0
        self._lst.addItem(f"t={elapsed:.3f} s   dur={dur:.3f} s")
        self._lst.scrollToBottom()
        if self._log.is_open:
            self._log.log(elapsed, dur)

    def _toggle_rec(self, on):
        if on:
            from pathlib import Path
            p = self._log.start(Path("toy_output"), "puffer")
            self._btn_rec.setText(f"Stop log  [{p.name}]")
        else:
            n = self._log.stop()
            self._btn_rec.setText("Start log")
            print(f"Logged {n} events")


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion")
    w = ToyWindow(); w.show()
    sys.exit(app.exec())
