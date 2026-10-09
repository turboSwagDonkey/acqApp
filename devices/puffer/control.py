"""Air puffer: a TTL pulse on an NI DAQ digital line (PufferController), its
mock, and the settings panel (channel, duration, test fire, schedule)."""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from acqApp import config, style
from acqApp.console import short_error
from acqApp.widgets import ROW_GAP, button_row, compact, spin


@dataclass
class PufferSettings:
    channel:     str   = "Dev3/port0/line7"    # rig-dev3, operator-confirmed
    duration_s:  float = 0.100


def free_do_lines(current: str) -> list[str]:
    """This rig's port0 lines, less those another device claims in rigs.json
    (two tasks can't own one physical line), `current` first."""
    taken = {config.rig_channel(k) for k in config.rig_profile()
             .get("channels", {}) if k != "puffer"}
    lines = [f"{config.rig_device()}/port0/line{i}" for i in range(8)]
    head = [current] if current else []
    return head + [c for c in lines if c not in taken and c != current]


class PufferController(QObject):
    """Fires a TTL pulse on an NI DAQ digital output line, using the
    panel's current settings (duration and line)."""
    puff_fired = pyqtSignal(float, float)   # (timestamp, duration_s)

    def __init__(self, settings: PufferSettings | None = None, parent=None):
        super().__init__(parent)
        self._s    = settings or PufferSettings()
        self._task = None
        # The pulse thread can outlive its task (line re-pointed, or app
        # closed mid-pulse), so _task is guarded and the pulse re-checks it.
        self._task_lock = threading.Lock()
        self._sink: Callable[[float], None] | None = None
        self._open()

    def set_sink(self, sink: Callable[[float], None] | None) -> None:
        """Attach (or clear) a sink; it receives each puff's duration_s."""
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
        # A blank channel is config.load_dataclass's "no puffer on this rig".
        if not self._s.channel:
            print("[puffer] not fitted on this rig — fire() will be a no-op")
            with self._task_lock:
                self._task = None
            return
        task = None
        try:
            import nidaqmx
            task = nidaqmx.Task()
            task.do_channels.add_do_chan(self._s.channel)
            task.start()
        except Exception as e:
            if task is not None:
                task.close()        # else nidaqmx warns it was never closed
            print(f"[puffer] not available on {self._s.channel} — "
                  f"{short_error(e)}; it won't fire")
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
        with self._task_lock:
            task = self._task
        if task is None:
            # Nothing fires, so emit/log nothing.
            where = (f" on {self._s.channel}" if self._s.channel
                     else " — not fitted on this rig")
            print(f"[puffer] fire() ignored — no open DAQ task{where}")
            return
        d = duration_s if duration_s is not None else self._s.duration_s
        t = time.perf_counter()
        self.puff_fired.emit(t, d)
        sink = self._sink
        if sink is not None:
            sink(d)   # logged against the shared session clock by the Recorder

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
    """Prints to stdout; no hardware. Same settings contract as the real
    one."""
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

        # ── Connection + default duration ──────────────────────────────────
        grp = QGroupBox("Puffer settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_chan = compact(QComboBox())
        self._cmb_chan.setEditable(True)
        self._cmb_chan.addItems(free_do_lines(self._s.channel))
        self._cmb_chan.setCurrentText(self._s.channel)
        lay.addRow("DO channel:", self._cmb_chan)

        self._spn_dur = spin(0.010, 5.0, self._s.duration_s,
                             decimals=3, step=0.010, suffix=" s")
        lay.addRow("Duration:", self._spn_dur)

        # Without these the controller keeps the settings it started with.
        self._typing = False
        self._cmb_chan.lineEdit().textEdited.connect(self._on_channel_typed)
        self._cmb_chan.currentTextChanged.connect(self._on_channel_changed)
        self._cmb_chan.lineEdit().editingFinished.connect(self._on_channel_done)
        self._spn_dur.valueChanged.connect(self._emit)

        btn_test = QPushButton("Test puff")
        btn_test.setStyleSheet(style.solid_btn("puffer"))
        btn_test.clicked.connect(self._on_test_clicked)
        lay.addRow(button_row(btn_test))
        root.addWidget(grp)

        # ── Scheduled puffs (fire at t = N s after session Start) ──────────
        sgrp = QGroupBox("Scheduled puffs")
        sl = QVBoxLayout(sgrp)
        sl.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(ROW_GAP)
        row.addWidget(QLabel("Puff at t ="))
        self._spn_at = spin(0.0, 86_400.0, 5.0, decimals=1, suffix=" s")
        row.addWidget(self._spn_at)
        btn_add = QPushButton("Schedule")
        btn_add.clicked.connect(self._schedule)
        row.addWidget(btn_add)
        row.addStretch()
        sl.addLayout(row)

        self._lst_sched = QListWidget()
        self._lst_sched.setMaximumHeight(90)
        sl.addWidget(self._lst_sched)

        btn_clear = QPushButton("Clear all")
        btn_clear.clicked.connect(self._clear_schedule)
        sl.addLayout(button_row(right=(btn_clear,)))
        root.addWidget(sgrp)
        root.addStretch()

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    # Each applied channel closes and reopens the DAQ task, and every
    # keystroke changes the combo's text: while typing, wait for Enter or
    # focus-out (`textEdited` comes before `currentTextChanged`).
    def _on_channel_typed(self, _text: str) -> None:
        self._typing = True

    def _on_channel_changed(self, _text: str) -> None:
        if not self._typing:
            self._emit()

    def _on_channel_done(self) -> None:
        self._typing = False
        self._emit()

    def _on_test_clicked(self) -> None:
        """Confirm before a manual puff. Scheduled puffs are their own
        deliberate act and aren't gated here."""
        dur = self._spn_dur.value()
        if QMessageBox.question(
            self, "Test puff",
            f"Fire a {dur:.3f} s puff now on {self._cmb_chan.currentText()}?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.test_requested.emit()

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
