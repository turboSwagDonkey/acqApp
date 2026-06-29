"""
DMD toy — load a pattern, display it, watch frame counter.

  ..\..\.venv\Scripts\python.exe dmd\_toy.py
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

from pathlib import Path
import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QSpinBox, QDoubleSpinBox,
    QVBoxLayout, QWidget,
)
import pyqtgraph as pg
pg.setConfigOptions(imageAxisOrder="row-major")

from dmd.control import MockDmdController, DmdSettings


class ToyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DMD toy")
        self.resize(700, 500)
        self._dmd = MockDmdController()
        self._dmd.frame_displayed.connect(self._on_frame)
        self._pattern: np.ndarray | None = None
        self._build()

    def _build(self):
        w = QWidget(); self.setCentralWidget(w)
        v = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        btn_load = QPushButton("Load pattern"); btn_load.clicked.connect(self._load)
        ctrl.addWidget(btn_load)
        self._lbl_pat = QLabel("No pattern"); ctrl.addWidget(self._lbl_pat)

        btn_disp = QPushButton("Display"); btn_disp.clicked.connect(self._dmd.display)
        btn_stop = QPushButton("Stop");    btn_stop.clicked.connect(self._dmd.stop)
        ctrl.addWidget(btn_disp); ctrl.addWidget(btn_stop)
        cw = QWidget(); cw.setLayout(ctrl); v.addWidget(cw)

        self._img_item = pg.ImageItem()
        gv = pg.GraphicsView(); vb = pg.ViewBox(lockAspect=True, invertY=False)
        gv.setCentralItem(vb); vb.addItem(self._img_item)
        v.addWidget(gv)

        self._lbl_frame = QLabel("Frame: —")
        v.addWidget(self._lbl_frame)

    def _load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pattern", "", "Images (*.png *.bmp *.tif)"
        )
        p = Path(path) if path else None
        self._dmd.load_pattern(p)
        if self._dmd._pattern is not None:
            self._pattern = self._dmd._pattern
            self._img_item.setImage(self._pattern.astype("float32"), autoLevels=True)
            self._lbl_pat.setText(p.name if p else "fallback checkerboard")

    def _on_frame(self, idx: int):
        self._lbl_frame.setText(f"Frame: {idx}")

    def closeEvent(self, e):
        self._dmd.close(); e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion")
    w = ToyWindow(); w.show()
    sys.exit(app.exec_())
