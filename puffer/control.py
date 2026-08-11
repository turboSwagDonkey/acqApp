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
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget,
)
from acqApp import style


@dataclass
class PufferSettings:
    # NI DAQ digital line. NOTE: keep distinct from the pupil-cam LED line
    # (Dev3/port0/line1) — two tasks may not own the same physical line.
    channel:     str   = "Dev3/port0/line0"
    duration_s:  float = 0.100                 # default puff duration


class PufferController(QObject):
    """Fires a TTL pulse on a NI DAQ digital output line.

    Holds the settings the panel is showing, so "Test puff" uses the duration
    on screen and re-pointing the DO channel actually moves the line.
    """
    puff_fired = pyqtSignal(float, float)   # (timestamp, duration_s)

    def __init__(self, settings: PufferSettings | None = None, parent=None):
        super().__init__(parent)
        self._s    = settings or PufferSettings()
        self._task = None
        # The pulse runs on its own thread and the channel can be re-pointed (or
        # the app closed) while it is mid-pulse, so every touch of _task is
        # guarded and the pulse re-checks that its task is still the live one.
        self._task_lock = threading.Lock()
        self._sink: Callable[[float], None] | None = None
        self._open()

    def set_sink(self, sink: Callable[[float], None] | None) -> None:
        """Attach (or clear) an event sink; receives the puff duration_s."""
        self._sink = sink

    def apply_settings(self, settings: PufferSettings) -> None:
        """Adopt the panel's settings, re-opening the task if the line changed."""
        changed = settings.channel != self._s.channel
        self._s = settings
        if changed:
            self._close_task()
            self._open()

    @property
    def settings(self) -> PufferSettings:
        return self._s

    def _open(self) -> None:
        try:
            import nidaqmx
            task = nidaqmx.Task()
            task.do_channels.add_do_chan(self._s.channel)
            task.start()
        except Exception as e:
            print(f"[puffer] DAQ init failed on {self._s.channel} ({e}) — "
                  f"fire() will be a no-op")
            task = None
        with self._task_lock:
            self._task = task

    def _close_task(self) -> None:
        with self._task_lock:
            task, self._task = self._task, None
        if task is None:
            return
        try:
            task.write(False)       # never leave the valve latched open
            task.stop()
            task.close()
        except Exception:
            pass

    def fire(self, duration_s: float | None = None) -> None:
        """Open the valve for `duration_s`, or the configured default."""
        d = duration_s if duration_s is not None else self._s.duration_s
        t = time.perf_counter()
        self.puff_fired.emit(t, d)
        sink = self._sink
        if sink is not None:
            sink(d)   # logged against the shared session clock by the Recorder
        with self._task_lock:
            task = self._task
        if task is None:
            return

        def _pulse():
            try:
                with self._task_lock:
                    if self._task is not task:
                        return              # re-pointed or closed before we ran
                    task.write(True)
                time.sleep(d)
                with self._task_lock:
                    if self._task is task:
                        task.write(False)
            except Exception as e:
                print(f"[puffer] pulse failed ({e})")

        threading.Thread(target=_pulse, daemon=True).start()

    def close(self) -> None:
        self._close_task()


class MockPufferController(QObject):
    """Prints to stdout; no hardware. Same settings contract as the real one, so
    the panel's channel/duration behave identically in Emulate mode."""
    puff_fired = pyqtSignal(float, float)

    def __init__(self, settings: PufferSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or PufferSettings()
        self._sink: Callable[[float], None] | None = None

    def set_sink(self, sink: Callable[[float], None] | None) -> None:
        self._sink = sink

    def apply_settings(self, settings: PufferSettings) -> None:
        self._s = settings

    @property
    def settings(self) -> PufferSettings:
        return self._s

    def fire(self, duration_s: float | None = None) -> None:
        d = duration_s if duration_s is not None else self._s.duration_s
        t = time.perf_counter()
        print(f"[puffer MOCK] fired at t={t:.3f}  duration={d:.3f} s "
              f"on {self._s.channel}")
        self.puff_fired.emit(t, d)
        sink = self._sink
        if sink is not None:
            sink(d)

    def close(self) -> None:
        pass


class SettingsPanel(QWidget):
    settings_changed         = pyqtSignal(object)         # emits PufferSettings
    test_requested           = pyqtSignal()               # Test puff → fire now
    schedule_requested       = pyqtSignal(float, float)   # (at_t_s, duration_s)
    clear_schedule_requested = pyqtSignal()

    def __init__(self, settings: PufferSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or PufferSettings()
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Connection + default duration ───────────────────────────────────────
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

        # Push edits straight to the controller: without this the panel is
        # decorative — the puff keeps using whatever the controller started with.
        self._cmb_chan.currentTextChanged.connect(self._emit)
        self._spn_dur.valueChanged.connect(self._emit)

        btn_test = QPushButton("Test puff")
        btn_test.setStyleSheet(style.solid_btn("puffer"))
        btn_test.clicked.connect(self.test_requested)
        lay.addRow(btn_test)
        root.addWidget(grp)

        # ── Scheduled puffs (fire at t = N s after session Start) ───────────────
        sgrp = QGroupBox("Scheduled puffs")
        sl = QVBoxLayout(sgrp)
        sl.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel("Puff at t ="))
        self._spn_at = QDoubleSpinBox()
        self._spn_at.setRange(0.0, 86_400.0)
        self._spn_at.setDecimals(1)
        self._spn_at.setSuffix(" s")
        self._spn_at.setValue(5.0)
        row.addWidget(self._spn_at)
        btn_add = QPushButton("Schedule")
        btn_add.clicked.connect(self._schedule)
        row.addWidget(btn_add)
        sl.addLayout(row)

        self._lst_sched = QListWidget()
        self._lst_sched.setMaximumHeight(90)
        sl.addWidget(self._lst_sched)

        btn_clear = QPushButton("Clear all")
        btn_clear.clicked.connect(self._clear_schedule)
        sl.addWidget(btn_clear)
        root.addWidget(sgrp)
        root.addStretch()

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    def _schedule(self) -> None:
        at, dur = self._spn_at.value(), self._spn_dur.value()
        self._lst_sched.addItem(f"t = {at:.1f} s   dur = {dur:.3f} s")
        self.schedule_requested.emit(at, dur)

    def _clear_schedule(self) -> None:
        self._lst_sched.clear()
        self.clear_schedule_requested.emit()

    @property
    def settings(self) -> PufferSettings:
        return PufferSettings(
            channel=self._cmb_chan.currentText(),
            duration_s=self._spn_dur.value(),
        )
