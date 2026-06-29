"""
Rotary wheel encoder — NI DAQ acquisition worker.

EncoderWorker   : QThread that polls ai2 (or any analog channel) and
                  exposes the latest (voltage, elapsed_s) via get_latest().
MockEncoderWorker: synthetic sine wave, no hardware needed.
"""

from __future__ import annotations
import threading
import time

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal


class EncoderWorker(QThread):
    fps_update = pyqtSignal(float)   # samples / second

    def __init__(self, chan: str = "Dev3/ai2", rate: float = 50.0):
        super().__init__()
        self._chan  = chan
        self._rate  = rate
        self._stop  = False
        self._lock  = threading.Lock()
        self._latest: tuple[float, float] | None = None  # (voltage, elapsed_s)
        self.total_samples = 0

    def get_latest(self) -> tuple[float, float] | None:
        with self._lock:
            s = self._latest
            self._latest = None
        return s

    def run(self) -> None:
        from nidaqmx import Task
        from nidaqmx.constants import TerminalConfiguration

        self._stop = False
        period = 1.0 / self._rate
        t0 = time.perf_counter()
        n  = 0

        with Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                self._chan,
                terminal_config=TerminalConfiguration.RSE,
                min_val=-10.0, max_val=10.0,
            )
            while not self._stop:
                voltage: float = task.read()  # type: ignore[assignment]
                elapsed = time.perf_counter() - t0
                n += 1
                with self._lock:
                    self._latest = (voltage, elapsed)
                    self.total_samples = n
                if n % int(self._rate) == 0:
                    self.fps_update.emit(n / elapsed)
                nxt = t0 + n * period
                slp = nxt - time.perf_counter()
                if slp > 0:
                    time.sleep(slp)

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


class MockEncoderWorker(QThread):
    """Synthetic encoder — 0.2 Hz sine on a ±2.5 V range."""
    fps_update = pyqtSignal(float)
    RATE = 50.0

    def __init__(self):
        super().__init__()
        self._stop        = False
        self._lock        = threading.Lock()
        self._latest: tuple[float, float] | None = None
        self.total_samples = 0

    def get_latest(self) -> tuple[float, float] | None:
        with self._lock:
            s = self._latest
            self._latest = None
        return s

    def run(self) -> None:
        self._stop = False
        period = 1.0 / self.RATE
        t0 = time.perf_counter()
        n  = 0
        while not self._stop:
            t = time.perf_counter() - t0
            voltage = 2.5 * np.sin(2 * np.pi * 0.2 * t)
            n += 1
            with self._lock:
                self._latest = (float(voltage), t)
                self.total_samples = n
            if n % int(self.RATE) == 0:
                self.fps_update.emit(n / t if t > 0 else 0.0)
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)

    def stop(self) -> None:
        self._stop = True
        self.wait(2000)
