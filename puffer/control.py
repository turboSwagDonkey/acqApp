"""
Air puffer — NI DAQ digital output controller + settings panel.

PufferController  : fires a TTL pulse on a digital output line.
MockPufferController: prints to stdout, no hardware needed.
SettingsPanel     : QWidget for channel and default duration.
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox, QFormLayout, QGroupBox,
    QLabel, QPushButton, QWidget, QComboBox,
)
from acqApp import style


@dataclass
class PufferSettings:
    channel:     str   = "Dev3/port0/line0"   # NI DAQ digital line
    duration_s:  float = 0.100                 # default puff duration


class PufferController(QObject):
    """Fires a TTL pulse on a NI DAQ digital output line."""
    puff_fired = pyqtSignal(float, float)   # (timestamp, duration_s)

    def __init__(self, settings: PufferSettings | None = None, parent=None):
        super().__init__(parent)
        self._s    = settings or PufferSettings()
        self._task = None
        self._open()

    def _open(self) -> None:
        try:
            import nidaqmx
            self._task = nidaqmx.Task()
            self._task.do_channels.add_do_chan(self._s.channel)
            self._task.start()
        except Exception as e:
            print(f"[puffer] DAQ init failed ({e}) — fire() will be a no-op")
            self._task = None

    def fire(self, duration_s: float | None = None) -> None:
        d = duration_s if duration_s is not None else self._s.duration_s
        t = time.perf_counter()
        self.puff_fired.emit(t, d)
        if self._task is None:
            return
        def _pulse():
            self._task.write(True)
            time.sleep(d)
            self._task.write(False)
        threading.Thread(target=_pulse, daemon=True).start()

    def close(self) -> None:
        if self._task:
            try:
                self._task.write(False)
                self._task.stop()
                self._task.close()
            except Exception:
                pass
            self._task = None


class MockPufferController(QObject):
    """Prints to stdout; no hardware."""
    puff_fired = pyqtSignal(float, float)

    def fire(self, duration_s: float = 0.1) -> None:
        t = time.perf_counter()
        print(f"[puffer MOCK] fired at t={t:.3f}  duration={duration_s:.3f} s")
        self.puff_fired.emit(t, duration_s)

    def close(self) -> None:
        pass


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits PufferSettings

    def __init__(self, settings: PufferSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or PufferSettings()
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("Puffer settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_chan = QComboBox()
        self._cmb_chan.setEditable(True)
        self._cmb_chan.addItems([
            "Dev3/port0/line0",
            "Dev3/port0/line1",
            "Dev3/port0/line2",
        ])
        self._cmb_chan.setCurrentText(self._s.channel)
        lay.addRow("DO channel:", self._cmb_chan)

        self._spn_dur = QDoubleSpinBox()
        self._spn_dur.setRange(0.010, 5.0)
        self._spn_dur.setSingleStep(0.010)
        self._spn_dur.setDecimals(3)
        self._spn_dur.setSuffix(" s")
        self._spn_dur.setValue(self._s.duration_s)
        lay.addRow("Duration:", self._spn_dur)

        btn_test = QPushButton("Test puff")
        btn_test.setStyleSheet(style.solid_btn("puffer"))
        btn_test.clicked.connect(self.test_requested)
        lay.addRow(btn_test)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

    # emitted when Test puff is clicked — owner calls controller.fire()
    test_requested = pyqtSignal()

    @property
    def settings(self) -> PufferSettings:
        return PufferSettings(
            channel=self._cmb_chan.currentText(),
            duration_s=self._spn_dur.value(),
        )
