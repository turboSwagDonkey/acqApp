#!/usr/bin/env python
r"""
In vivo voltage imaging — single-camera acquisition.
Emulates the Hamamatsu / LabVIEW front panel.

Run:
  ..\..\.venv\Scripts\python.exe cam_app.py
  ..\..\.venv\Scripts\python.exe cam_app.py --mock          (no hardware needed)
  ..\..\.venv\Scripts\python.exe cam_app.py --exposure 0.005
"""

import sys

# ── Camera pre-init — must run before Qt or scipy load their DLLs ─────────────
# scipy (OpenBLAS) and Qt both add directories to the Windows DLL search path at
# import time. If they load first, DCAM's dcamapi_init() gets null function
# pointers from a mismatched DLL and AVs at 0x0. Initialising DCAM here, while
# only stdlib is loaded, guarantees it finds the right DLL.
_g_cam = _g_cam_info = None
_g_mock = "--mock" in sys.argv
if not _g_mock:
    try:
        from pylablib.devices import DCAM as _DCAM
        _n = _DCAM.get_cameras_number()
        if _n == 0:
            print("No DCAM camera found — falling back to --mock mode")
            _g_mock = True
        else:
            _g_cam = _DCAM.DCAMCamera(idx=0)
            _g_cam_info = _g_cam.get_device_info()
            print(f"Pre-init connected: {_g_cam_info}")
    except Exception as _e:
        print(f"Camera pre-init failed ({_e}) — falling back to --mock mode")
        if _g_cam is not None:
            try:
                _g_cam.close()
            except Exception:
                pass
        _g_cam = _g_cam_info = None
        _g_mock = True
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMainWindow, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

pg.setConfigOptions(imageAxisOrder="row-major")

PLOT_HISTORY = 600  # rolling frames kept in the plot buffers


# ── Camera workers ─────────────────────────────────────────────────────────────

class CameraWorker(QThread):
    fps_update = pyqtSignal(int, float)

    def __init__(self, cam):
        super().__init__()
        self._cam   = cam
        self._stop  = False
        self._lock  = threading.Lock()
        self.latest_frame: np.ndarray | None = None
        self.total_frames = 0

    def get_latest(self):
        with self._lock:
            f = self.latest_frame
            self.latest_frame = None
        return f

    def run(self):
        self._stop = False
        self._cam.start_acquisition()
        n, t0 = 0, time.perf_counter()
        try:
            while not self._stop:
                try:
                    self._cam.wait_for_frame(timeout=0.5)
                except Exception:
                    continue
                imgs = self._cam.read_multiple_images()
                if imgs:
                    n += 1
                    with self._lock:
                        self.latest_frame = imgs[-1]
                        self.total_frames = n
                    if n % 15 == 0:
                        self.fps_update.emit(n, n / (time.perf_counter() - t0))
        finally:
            self._cam.stop_acquisition()

    def stop(self):
        self._stop = True
        self.wait(3000)


class MockCameraWorker(QThread):
    fps_update = pyqtSignal(int, float)
    H, W, FPS = 512, 512, 30.0

    def __init__(self):
        super().__init__()
        self._stop = False
        self._lock = threading.Lock()
        self.latest_frame: np.ndarray | None = None
        self.total_frames = 0

    def get_latest(self):
        with self._lock:
            f = self.latest_frame
            self.latest_frame = None
        return f

    def run(self):
        self._stop = False
        rng = np.random.default_rng(0)
        n, t0 = 0, time.perf_counter()
        period = 1.0 / self.FPS
        cy, cx = self.H // 2, self.W // 2
        y, x = np.ogrid[-cy:self.H - cy, -cx:self.W - cx]
        blob = (x * x + y * y) < 60 ** 2
        while not self._stop:
            t = time.perf_counter() - t0
            frame = rng.integers(1500, 2500, (self.H, self.W), dtype=np.uint16)
            sig = int(300 * np.sin(2 * np.pi * 0.5 * t))
            frame[blob] = np.clip(frame[blob].astype(np.int32) + sig, 0, 65535).astype(np.uint16)
            n += 1
            with self._lock:
                self.latest_frame = frame
                self.total_frames = n
            if n % 30 == 0:
                self.fps_update.emit(n, n / (time.perf_counter() - t0))
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)

    def stop(self):
        self._stop = True
        self.wait(2000)


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self, cam=None, cam_info=None, mock=False, downsample=4):
        super().__init__()
        self._cam        = cam
        self._cam_info   = cam_info
        self._mock       = mock
        self._worker     = None
        self._downsample = downsample

        # state
        self._acquiring  = False
        self._recording  = False
        self._frame_idx  = 0
        self._rec_count  = 0
        self._rec_target = 0
        self._bg_frame   = None   # downsampled float32 snapshot for BG subtraction
        self._f0         = None   # baseline scalar for ΔF/F

        # rolling plot buffers
        self._x_buf  = deque(maxlen=PLOT_HISTORY)
        self._f_buf  = deque(maxlen=PLOT_HISTORY)
        self._df_buf = deque(maxlen=PLOT_HISTORY)

        # recording
        self._rec_writer = None
        self._rec_path   = None

        # display timer — pulls latest frame from worker at ~30 Hz
        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(33)
        self._disp_timer.timeout.connect(self._pull_frame)

        self.setWindowTitle("In vivo voltage imaging  (Single camera version)")
        self._build_ui()
        self._update_folder_label()
        if cam_info:
            self._lbl_serial.setText(str(cam_info.serial_number))
        if cam is not None:
            # Sync UI to actual camera state without triggering callbacks
            self._spn_exposure.blockSignals(True)
            self._spn_exposure.setValue(cam.get_exposure())
            self._spn_exposure.blockSignals(False)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._make_left_panel())
        splitter.addWidget(self._make_center_panel())
        splitter.addWidget(self._make_right_panel())
        splitter.setSizes([270, 820, 460])
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("Ready — click ● to start camera")
        self.resize(1550, 880)

    # ── Left panel ─────────────────────────────────────────────────────────────

    def _make_left_panel(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(275)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        vbox  = QVBoxLayout(inner)
        vbox.setSpacing(6)
        vbox.setContentsMargins(6, 6, 6, 6)

        title = QLabel("In vivo voltage imaging\n(Single camera version)")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Arial", 10, QFont.Bold))
        vbox.addWidget(title)

        # ── Camera info ──
        grp = QGroupBox("Camera 1")
        row = QHBoxLayout(grp)
        self._lbl_serial = QLabel("S/N: –")
        row.addWidget(self._lbl_serial)

        self._btn_green = QPushButton("● Offline")
        self._btn_green.setCheckable(True)
        self._btn_green.setFixedWidth(92)
        self._btn_green.setStyleSheet(
            "QPushButton{background:#c8c8c8;color:#444;border-radius:4px;padding:3px 6px}"
            "QPushButton:checked{background:#33bb33;color:white;font-weight:bold}"
        )
        self._btn_green.toggled.connect(self._toggle_acquisition)
        row.addWidget(self._btn_green)

        btn_quit = QPushButton("QUIT")
        btn_quit.setStyleSheet("background:#dd4444;color:white;font-weight:bold;"
                               "border-radius:4px;padding:3px 6px")
        btn_quit.clicked.connect(self.close)
        row.addWidget(btn_quit)
        vbox.addWidget(grp)

        # ── Acquisition settings ──
        grp = QGroupBox("Acquisition")
        lay = QFormLayout(grp)
        lay.setLabelAlignment(Qt.AlignRight)
        lay.setSpacing(4)

        # Binning — applies to camera; requires stop/restart if live
        self._cmb_binning = QComboBox()
        self._cmb_binning.addItems(["1×1  (full res)", "2×2", "4×4"])
        self._cmb_binning.setCurrentIndex(1)
        self._cmb_binning.currentIndexChanged.connect(self._on_binning_changed)
        lay.addRow("Resolution:", self._cmb_binning)

        # Exposure — applied live to camera
        from PyQt5.QtWidgets import QDoubleSpinBox
        self._spn_exposure = QDoubleSpinBox()
        self._spn_exposure.setRange(0.001, 10.0)
        self._spn_exposure.setSingleStep(0.001)
        self._spn_exposure.setDecimals(3)
        self._spn_exposure.setSuffix(" s")
        self._spn_exposure.setValue(0.010)
        self._spn_exposure.valueChanged.connect(self._on_exposure_changed)
        lay.addRow("Exposure:", self._spn_exposure)

        row = QHBoxLayout()
        self._spn_sec = QSpinBox()
        self._spn_sec.setRange(1, 3600); self._spn_sec.setValue(10)
        row.addWidget(QLabel("Rec sec:")); row.addWidget(self._spn_sec)
        self._spn_rate = QSpinBox()
        self._spn_rate.setRange(1, 200); self._spn_rate.setValue(30)
        row.addWidget(QLabel("Rate:")); row.addWidget(self._spn_rate)
        lay.addRow(row)

        self._lbl_mode = QLabel("–")
        lay.addRow("Mode:", self._lbl_mode)

        row = QHBoxLayout()
        self._lbl_frame = QLabel("0")
        self._lbl_total = QLabel("0")
        self._lbl_fps   = QLabel("–")
        row.addWidget(QLabel("Frame:")); row.addWidget(self._lbl_frame)
        row.addWidget(QLabel("Total:")); row.addWidget(self._lbl_total)
        lay.addRow(row)
        lay.addRow("FPS:", self._lbl_fps)

        self._cmb_source = QComboBox()
        self._cmb_source.addItems(["Internal", "External"])
        lay.addRow("Source:", self._cmb_source)
        vbox.addWidget(grp)

        # ── Record settings ──
        grp = QGroupBox("Record")
        lay = QFormLayout(grp)
        lay.setLabelAlignment(Qt.AlignRight)
        lay.setSpacing(4)

        row = QHBoxLayout()
        self._cmb_drive = QComboBox()
        self._cmb_drive.addItems(["C:", "D:", "E:", "F:"])
        self._cmb_drive.setCurrentText("D:")
        self._cmb_drive.currentTextChanged.connect(self._update_folder_label)
        row.addWidget(self._cmb_drive)
        self._edt_date = QLineEdit(datetime.now().strftime("%Y%m%d"))
        self._edt_date.setMaximumWidth(82)
        self._edt_date.textChanged.connect(self._update_folder_label)
        row.addWidget(QLabel("Date:")); row.addWidget(self._edt_date)
        lay.addRow("Drive:", row)

        self._edt_probe = QLineEdit("Ace")
        self._edt_probe.textChanged.connect(self._update_folder_label)
        lay.addRow("Probe:", self._edt_probe)

        row = QHBoxLayout()
        self._chk_mid = QCheckBox("MID"); row.addWidget(self._chk_mid)
        self._spn_fov = QSpinBox()
        self._spn_fov.setRange(1, 99); self._spn_fov.setValue(1)
        self._spn_fov.valueChanged.connect(self._update_folder_label)
        row.addWidget(QLabel("FOV:")); row.addWidget(self._spn_fov)
        self._spn_trial = QSpinBox()
        self._spn_trial.setRange(1, 999); self._spn_trial.setValue(1)
        self._spn_trial.valueChanged.connect(self._update_folder_label)
        row.addWidget(QLabel("Trial:")); row.addWidget(self._spn_trial)
        lay.addRow(row)

        row = QHBoxLayout()
        self._btn_record = QPushButton("● Record")
        self._btn_record.setCheckable(True)
        self._btn_record.setStyleSheet(
            "QPushButton{background:#c8c8c8;border-radius:4px;padding:3px 6px}"
            "QPushButton:checked{background:#33bb33;color:white;font-weight:bold}"
        )
        self._btn_record.toggled.connect(self._toggle_recording)
        row.addWidget(self._btn_record)
        btn_open = QPushButton("Open"); btn_open.clicked.connect(self._open_folder)
        row.addWidget(btn_open)
        btn_save = QPushButton("Save"); btn_save.clicked.connect(self._save_now)
        row.addWidget(btn_save)
        lay.addRow(row)

        self._lbl_folder = QLabel("–")
        self._lbl_folder.setWordWrap(True)
        self._lbl_folder.setStyleSheet("font-size:8pt;color:#333")
        lay.addRow("Folder:", self._lbl_folder)

        self._lst_trials = QListWidget()
        self._lst_trials.setMaximumHeight(90)
        lay.addRow(self._lst_trials)
        vbox.addWidget(grp)

        vbox.addStretch()
        scroll.setWidget(inner)
        return scroll

    # ── Center panel ────────────────────────────────────────────────────────────

    def _make_center_panel(self):
        outer = QWidget()
        vbox  = QVBoxLayout(outer)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(2)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100); self._progress.setValue(0)
        self._progress.setFixedHeight(14); self._progress.setTextVisible(False)
        vbox.addWidget(self._progress)

        # Row: [histogram | image view | side controls]
        img_row = QWidget()
        h_lay = QHBoxLayout(img_row)
        h_lay.setContentsMargins(0, 0, 0, 0)
        h_lay.setSpacing(4)

        # Image item lives in its own GraphicsView/ViewBox
        self._img_item = pg.ImageItem()
        self._gv = pg.GraphicsView()
        self._vb = pg.ViewBox(lockAspect=True, invertY=True)
        self._gv.setCentralItem(self._vb)
        self._vb.addItem(self._img_item)

        # Histogram widget (vertical) docked to the left of the image
        self._hist = pg.HistogramLUTWidget()
        self._hist.setImageItem(self._img_item)
        self._hist.setFixedWidth(86)
        h_lay.addWidget(self._hist)
        h_lay.addWidget(self._gv, stretch=1)

        # Side controls
        ctrl = QWidget(); ctrl.setFixedWidth(105)
        cv = QVBoxLayout(ctrl); cv.setSpacing(6)

        self._chk_auto = QCheckBox("Auto")
        self._chk_auto.setChecked(True)
        self._chk_auto.toggled.connect(self._on_auto_changed)
        cv.addWidget(self._chk_auto)

        cv.addWidget(QLabel("Zoom:"))
        self._spn_zoom = QSpinBox()
        self._spn_zoom.setRange(-8, 8); self._spn_zoom.setValue(-2)
        cv.addWidget(self._spn_zoom)

        self._chk_ts = QCheckBox("Timestamp")
        cv.addWidget(self._chk_ts)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine); cv.addWidget(sep)

        self._chk_bgsub = QCheckBox("BG sub")
        cv.addWidget(self._chk_bgsub)

        btn_snapbg = QPushButton("Snap BG")
        btn_snapbg.setToolTip("Capture current frame as background")
        btn_snapbg.clicked.connect(self._snap_bg)
        cv.addWidget(btn_snapbg)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine); cv.addWidget(sep2)

        btn_f0 = QPushButton("Set F₀")
        btn_f0.setToolTip("Set current ROI mean as ΔF/F baseline")
        btn_f0.clicked.connect(self._set_f0)
        cv.addWidget(btn_f0)

        btn_clr = QPushButton("Clear plot")
        btn_clr.clicked.connect(self._clear_plots)
        cv.addWidget(btn_clr)

        cv.addStretch()
        h_lay.addWidget(ctrl)
        vbox.addWidget(img_row, stretch=1)
        return outer

    # ── Right panel (signal plots) ──────────────────────────────────────────────

    def _make_right_panel(self):
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()

        def _plot(title, ylabel, yunits):
            pw = pg.PlotWidget(title=title)
            pw.setLabel("left", ylabel, units=yunits)
            pw.setLabel("bottom", "Frame count")
            pw.showGrid(x=True, y=True, alpha=0.3)
            return pw

        # ΔF/F tab
        self._pw_df = _plot("ΔF/F", "ΔF/F", "%")
        self._pw_df.setYRange(-100, 100)
        self._curve_df  = self._pw_df.plot(pen=pg.mkPen("b", width=1.5))
        self._vline     = pg.InfiniteLine(angle=90, movable=False,
                                          pen=pg.mkPen("k", width=1,
                                                        style=Qt.DashLine))
        self._pw_df.addItem(self._vline)
        tabs.addTab(self._pw_df, "Delta F")

        # Fit tab (ΔF/F + rolling mean overlay)
        self._pw_fit = _plot("ΔF/F + rolling mean", "ΔF/F", "%")
        self._curve_fit_raw  = self._pw_fit.plot(pen=pg.mkPen((120, 120, 255, 80), width=1))
        self._curve_fit_mean = self._pw_fit.plot(pen=pg.mkPen("r", width=2))
        tabs.addTab(self._pw_fit, "Fit")

        # F tab (raw ROI fluorescence)
        self._pw_f = _plot("Fluorescence (ROI mean)", "F", "counts")
        self._curve_f = self._pw_f.plot(pen=pg.mkPen("g", width=1.5))
        tabs.addTab(self._pw_f, "F")

        vbox.addWidget(tabs)
        return w

    # ── Acquisition ────────────────────────────────────────────────────────────

    def _toggle_acquisition(self, on: bool):
        if on:
            self._start_acq()
        else:
            self._stop_acq()

    def _start_acq(self):
        if self._acquiring:
            return
        self._frame_idx = 0

        if self._mock:
            self._worker = MockCameraWorker()
            self._lbl_mode.setText(
                f"{MockCameraWorker.W}×{MockCameraWorker.H}  "
                f"{MockCameraWorker.FPS:.0f} Hz  [mock]"
            )
        else:
            self._worker = CameraWorker(self._cam)
            if self._cam is not None:
                roi = self._cam.get_roi()
                w, h = roi[1] - roi[0], roi[3] - roi[2]
                exp = self._cam.get_exposure() * 1e3
                self._lbl_mode.setText(f"{w}×{h}  {exp:.1f} ms")

        self._worker.fps_update.connect(self._on_fps)
        self._worker.start()
        self._disp_timer.start()
        self._acquiring = True
        self._btn_green.setText("● Live")
        self._statusbar.showMessage("Acquiring…")

    def _stop_acq(self):
        if not self._acquiring:
            return
        self._disp_timer.stop()
        if self._recording:
            self._stop_recording()
        if self._worker:
            self._worker.stop()
            self._worker = None
        self._acquiring  = False
        self._btn_green.setChecked(False)
        self._btn_green.setText("● Offline")
        self._statusbar.showMessage("Stopped")

    # ── Per-frame processing ───────────────────────────────────────────────────

    def _pull_frame(self):
        """Called by the display timer; grabs latest frame from the worker."""
        if self._worker is None:
            return
        frame = self._worker.get_latest()
        if frame is None:
            return
        self._frame_idx = self._worker.total_frames
        self._lbl_frame.setText(str(self._frame_idx))
        self._on_frame(frame)

    def _on_frame(self, frame: np.ndarray):
        ds = self._downsample
        small = frame[::ds, ::ds].astype(np.float32)

        # Background subtraction on downsampled image
        if self._chk_bgsub.isChecked() and self._bg_frame is not None:
            small -= self._bg_frame

        # Display
        if self._chk_auto.isChecked():
            lo, hi = np.percentile(small, [1, 99])
            self._img_item.setLevels([lo, hi])
        self._img_item.setImage(small, autoLevels=False)

        if self._frame_idx == 1:
            self._vb.autoRange()

        # ΔF/F from whole-frame mean
        f_mean = float(small.mean())
        df = ((f_mean - self._f0) / self._f0 * 100.0
              if (self._f0 is not None and self._f0 > 0) else 0.0)
        self._x_buf.append(self._frame_idx)
        self._f_buf.append(f_mean)
        self._df_buf.append(df)
        self._update_plots()

        # Write full-res frame to disk if recording
        if self._recording:
            self._write_frame(frame)

    def _update_plots(self):
        x  = np.array(self._x_buf)
        f  = np.array(self._f_buf)
        df = np.array(self._df_buf)

        self._curve_df.setData(x, df)
        self._curve_f.setData(x, f)
        self._curve_fit_raw.setData(x, df)
        self._vline.setValue(self._frame_idx)

        if len(df) >= 5:
            mean_fit = uniform_filter1d(df, size=min(15, len(df)))
            self._curve_fit_mean.setData(x, mean_fit)

    def _on_fps(self, count: int, fps: float):
        self._lbl_fps.setText(f"{fps:.1f} fps")
        self._statusbar.showMessage(f"Acquiring  —  {fps:.1f} fps  —  frame {count}")

    def _on_exposure_changed(self, value: float):
        """Apply exposure change live; camera accepts this during acquisition."""
        if self._mock or self._cam is None:
            return
        try:
            self._cam.set_exposure(value)
            actual = self._cam.get_exposure()
            self._statusbar.showMessage(f"Exposure set to {actual * 1e3:.3f} ms")
        except Exception as e:
            self._statusbar.showMessage(f"Exposure error: {e}")

    def _on_binning_changed(self, index: int):
        """Change binning — stops acquisition, reconfigures, restarts."""
        binning_values = [1, 2, 4]
        b = binning_values[index]

        if self._mock or self._cam is None:
            return

        was_acquiring = self._acquiring
        if was_acquiring:
            self._stop_acq()

        try:
            self._cam.set_attribute_value("BINNING", b)
            roi = self._cam.get_roi()
            w, h = roi[1] - roi[0], roi[3] - roi[2]
            self._lbl_mode.setText(f"{w}×{h}  (bin {b}×{b})")
            # Background captured at old resolution is now invalid
            self._bg_frame = None
            self._statusbar.showMessage(f"Binning set to {b}×{b}  →  {w}×{h} px")
        except Exception as e:
            self._statusbar.showMessage(f"Binning error: {e}")

        if was_acquiring:
            self._btn_green.setChecked(True)  # triggers _start_acq via signal

    # ── Image controls ─────────────────────────────────────────────────────────

    def _on_auto_changed(self, on: bool):
        if not on:
            self._img_item.setLevels([0, 65535])

    def _snap_bg(self):
        if self._img_item.image is not None:
            self._bg_frame = self._img_item.image.copy()  # already downsampled float32
            self._statusbar.showMessage("Background captured")

    def _set_f0(self):
        if self._img_item.image is None:
            return
        self._f0 = float(self._img_item.image.mean())
        self._statusbar.showMessage(f"F₀ set to {self._f0:.1f} counts")

    def _clear_plots(self):
        self._x_buf.clear(); self._f_buf.clear(); self._df_buf.clear()
        for c in (self._curve_df, self._curve_f, self._curve_fit_raw, self._curve_fit_mean):
            c.setData([], [])

    # ── Recording ──────────────────────────────────────────────────────────────

    def _toggle_recording(self, on: bool):
        if on:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if not self._acquiring:
            self._btn_record.setChecked(False)
            self._statusbar.showMessage("Start the camera first")
            return
        folder = self._current_folder()
        folder.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fpath = folder / f"{folder.name}_{ts}.tif"
        try:
            import tifffile
            self._rec_writer = tifffile.TiffWriter(str(fpath), bigtiff=True)
            self._rec_count  = 0
            self._rec_target = self._spn_sec.value() * self._spn_rate.value()
            self._recording  = True
            self._rec_path   = fpath
            self._lbl_total.setText("0")
            self._statusbar.showMessage(
                f"Recording → {fpath.name}  (target {self._rec_target} frames)"
            )
        except Exception as e:
            self._btn_record.setChecked(False)
            self._statusbar.showMessage(f"Could not open TIFF writer: {e}")

    def _write_frame(self, frame: np.ndarray):
        if not self._rec_writer:
            return
        self._rec_writer.write(frame)
        self._rec_count += 1
        self._lbl_total.setText(str(self._rec_count))
        pct = int(100 * self._rec_count / max(1, self._rec_target))
        self._progress.setValue(min(pct, 100))
        if self._rec_count >= self._rec_target:
            self._stop_recording()
            self._btn_record.setChecked(False)

    def _stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        if self._rec_writer:
            self._rec_writer.close()
            self._rec_writer = None
        self._progress.setValue(0)
        label = (f"FOV{self._spn_fov.value()}_T{self._spn_trial.value()}"
                 f"  ({self._rec_count} fr)")
        self._lst_trials.addItem(label)
        self._spn_trial.setValue(self._spn_trial.value() + 1)
        self._statusbar.showMessage(
            f"Saved {self._rec_count} frames  →  "
            f"{self._rec_path.name if self._rec_path else '?'}"
        )

    def _save_now(self):
        if self._recording:
            self._stop_recording()
            self._btn_record.setChecked(False)

    def _open_folder(self):
        p = self._current_folder()
        p.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(p)])

    # ── Folder helpers ─────────────────────────────────────────────────────────

    def _current_folder(self) -> Path:
        drive = self._cmb_drive.currentText()
        probe = self._edt_probe.text().strip() or "probe"
        date  = self._edt_date.text().strip()
        stem  = f"FOV{self._spn_fov.value()}_T{self._spn_trial.value()}"
        return Path(f"{drive}\\{probe}\\{date}\\{stem}\\{stem}")

    def _update_folder_label(self):
        self._lbl_folder.setText(str(self._current_folder()))

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop_acq()
        if self._cam and not self._mock:
            try:
                self._cam.close()
            except Exception:
                pass
        event.accept()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="In vivo voltage imaging — single camera")
    ap.add_argument("--mock", action="store_true",
                    help="run with synthetic frames (no camera needed)")
    ap.add_argument("--exposure", type=float, default=0.01,
                    help="initial exposure in seconds (default 10 ms)")
    ap.add_argument("--downsample", type=int, default=4,
                    help="display every Nth pixel (default 4 → ~1108×592 for full sensor)")
    args = ap.parse_args()

    # Camera was already opened at module load (before Qt/scipy DLLs). Just
    # apply the exposure setting now that argparse has run.
    cam, cam_info, mock = _g_cam, _g_cam_info, _g_mock
    if cam is not None and not mock:
        try:
            cam.set_exposure(args.exposure)
        except Exception as e:
            print(f"Warning: could not set exposure: {e}")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = MainWindow(cam=cam, cam_info=cam_info, mock=mock, downsample=args.downsample)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    # Make the console unable to raise on this script's own output before it
    # prints anything (see acqApp/console.py -- a UnicodeEncodeError from a
    # diagnostic print is otherwise indistinguishable from a device failure).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    main()
