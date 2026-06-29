"""
In vivo acquisition suite — top-level entry point.

Wires together voltage_cam, pupil_cam, wheel, puffer, and the sync
controller.  Each subsystem lives in its own subpackage; this file only
does orchestration.

Run:
  .venv\Scripts\python.exe main.py
  .venv\Scripts\python.exe main.py --mock
"""

from __future__ import annotations
import argparse
import sys

# ── Hardware pre-init (must precede any Qt / scipy import) ────────────────────
_cam = _cam_info = None
_mock = "--mock" in sys.argv
if not _mock:
    try:
        from pylablib.devices import DCAM as _DCAM
        if _DCAM.get_cameras_number() > 0:
            _cam      = _DCAM.DCAMCamera(idx=0)
            _cam_info = _cam.get_device_info()
            print(f"Voltage cam: {_cam_info}")
        else:
            print("No DCAM camera — mock mode")
            _mock = True
    except Exception as _e:
        print(f"Camera init failed ({_e}) — mock mode")
        _mock = True
# ─────────────────────────────────────────────────────────────────────────────

from PyQt5.QtCore  import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QTabWidget,
    QVBoxLayout, QWidget, QStatusBar,
)
import pyqtgraph as pg

from acqApp import style
from acqApp.sync import SyncController

from acqApp.voltage_cam.acquisition import CameraWorker, MockCameraWorker
from acqApp.voltage_cam.settings    import CameraSettings, SettingsPanel as CamSettingsPanel
from acqApp.voltage_cam.recording   import RecordingManager

from acqApp.wheel.acquisition import EncoderWorker, MockEncoderWorker
from acqApp.wheel.settings    import EncoderSettings, SettingsPanel as WheelSettingsPanel
from acqApp.wheel.recording   import CSVWriter

from acqApp.pupil_cam.acquisition import MockPupilCameraWorker
from acqApp.puffer.control        import MockPufferController, SettingsPanel as PufferPanel

pg.setConfigOptions(imageAxisOrder="row-major")


class MainWindow(QMainWindow):

    def __init__(self, cam=None, cam_info=None, mock=False):
        super().__init__()
        self._cam  = cam
        self._mock = mock

        # ── Subsystem workers (created on demand in start_all) ──────────────
        self._cam_worker:   CameraWorker | MockCameraWorker | None = None
        self._wheel_worker: EncoderWorker | MockEncoderWorker | None = None
        self._pupil_worker: MockPupilCameraWorker | None = None
        self._puffer       = MockPufferController()

        # ── Data managers ───────────────────────────────────────────────────
        self._cam_rec   = RecordingManager()
        self._wheel_rec = CSVWriter()

        # ── Sync controller ─────────────────────────────────────────────────
        self._sync = SyncController(tick_ms=100)
        self._sync.tick.connect(self._on_tick)
        self._sync.trigger_fired.connect(self._on_trigger)

        self._build_ui()
        if cam_info:
            self.setWindowTitle(
                f"Acquisition suite  —  {cam_info.serial_number}"
            )
        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(33)   # ~30 Hz display
        self._disp_timer.timeout.connect(self._pull_frames)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setWindowTitle("Acquisition suite")
        self.resize(1600, 900)
        self.setStyleSheet(style.APP_QSS)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Horizontal)

        # Left: settings tabs
        settings_tabs = QTabWidget()
        settings_tabs.setFixedWidth(280)
        self._cam_settings_panel = CamSettingsPanel()
        self._cam_settings_panel.exposure_changed.connect(self._on_exposure)
        self._cam_settings_panel.binning_changed.connect(self._on_binning)
        settings_tabs.addTab(self._cam_settings_panel, "Voltage cam")

        self._wheel_settings_panel = WheelSettingsPanel()
        settings_tabs.addTab(self._wheel_settings_panel, "Wheel")

        self._puffer_panel = PufferPanel()
        self._puffer_panel.test_requested.connect(lambda: self._puffer.fire())
        settings_tabs.addTab(self._puffer_panel, "Puffer")

        splitter.addWidget(settings_tabs)

        # Centre: voltage cam image
        self._img_item  = pg.ImageItem()
        self._cam_hist  = pg.HistogramLUTWidget()
        self._cam_hist.setImageItem(self._img_item)
        self._cam_hist.setFixedWidth(86)
        self._cam_gv   = pg.GraphicsView()
        self._cam_vb   = pg.ViewBox(lockAspect=True, invertY=True)
        self._cam_gv.setCentralItem(self._cam_vb)
        self._cam_vb.addItem(self._img_item)

        cam_w = QWidget()
        cam_h = __import__("PyQt5.QtWidgets", fromlist=["QHBoxLayout"]).QHBoxLayout(cam_w)
        cam_h.setContentsMargins(0, 0, 0, 0)
        cam_h.addWidget(self._cam_hist)
        cam_h.addWidget(self._cam_gv)
        splitter.addWidget(cam_w)

        # Right: signal plots
        plot_tabs = QTabWidget()
        self._pw_df = pg.PlotWidget(title="ΔF/F")
        self._pw_df.setLabel("left", "ΔF/F", units="%")
        self._pw_df.setLabel("bottom", "Frame")
        self._pw_df.showGrid(x=True, y=True, alpha=0.3)
        self._curve_df = self._pw_df.plot(pen=pg.mkPen(style.HEX["voltage_cam"], width=1.5))
        plot_tabs.addTab(self._pw_df, "ΔF/F")

        self._pw_wheel = pg.PlotWidget(title="Wheel velocity")
        self._pw_wheel.setLabel("left", "Voltage", units="V")
        self._pw_wheel.setLabel("bottom", "Sample")
        self._pw_wheel.showGrid(x=True, y=True, alpha=0.3)
        self._curve_wheel = self._pw_wheel.plot(pen=pg.mkPen(style.HEX["wheel"], width=1.5))
        plot_tabs.addTab(self._pw_wheel, "Wheel")

        self._pw_pupil = pg.PlotWidget(title="Pupil radius")
        self._pw_pupil.setLabel("left", "Radius", units="px")
        self._pw_pupil.setLabel("bottom", "Frame")
        self._pw_pupil.showGrid(x=True, y=True, alpha=0.3)
        self._curve_pupil = self._pw_pupil.plot(pen=pg.mkPen(style.HEX["pupil_cam"], width=1.5))
        plot_tabs.addTab(self._pw_pupil, "Pupil")

        splitter.addWidget(plot_tabs)
        splitter.setSizes([280, 900, 420])
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.showMessage("Ready")

    # ── Sync callbacks ──────────────────────────────────────────────────────

    def _on_tick(self, elapsed: float) -> None:
        self.statusBar().showMessage(f"t = {elapsed:.1f} s")

    def _on_trigger(self, name: str, duration: float) -> None:
        if name == "puffer":
            self._puffer.fire(duration)

    # ── Frame pull ──────────────────────────────────────────────────────────

    def _pull_frames(self) -> None:
        if self._cam_worker:
            f = self._cam_worker.get_latest()
            if f is not None:
                ds   = 4
                disp = f[::ds, ::ds].astype("float32")
                lo, hi = disp.min(), disp.max()
                self._img_item.setLevels([lo, hi])
                self._img_item.setImage(disp, autoLevels=False)

        if self._wheel_worker:
            s = self._wheel_worker.get_latest()
            if s is not None:
                v, t = s
                # TODO: accumulate into a deque and plot rolling window

    # ── Camera settings handlers ────────────────────────────────────────────

    def _on_exposure(self, val: float) -> None:
        if self._cam and not self._mock:
            try:
                self._cam.set_exposure(val)
            except Exception:
                pass

    def _on_binning(self, val: int) -> None:
        # Requires stop/restart — handled by start_all / stop_all cycle
        pass

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._sync.stop_all()
        self._disp_timer.stop()
        if self._cam and not self._mock:
            try:
                self._cam.close()
            except Exception:
                pass
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock",     action="store_true")
    ap.add_argument("--exposure", type=float, default=0.010)
    args = ap.parse_args()

    if _cam and not _mock:
        try:
            _cam.set_exposure(args.exposure)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(style.APP_QSS)

    win = MainWindow(cam=_cam, cam_info=_cam_info, mock=_mock or args.mock)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
