"""Electrically tunable lens (path 2): one NI-DAQ analog output into the
lens driver's analog input, held at a setpoint.

The volts-to-focus relation is the driver's and the lens's; nothing here
assumes one. Every write goes to the recording sink, so the file holds the
setpoint on the session clock.
"""

from __future__ import annotations

from acqApp.acq.devices import Sink
from acqApp.devices.etl.settings import EtlSettings


class EtlController:
    """Raises from `__init__` if the line can't be opened, so the adapter
    swaps in the mock (an inert one would pass for a live lens)."""

    def __init__(self, chan: str, settings: EtlSettings | None = None):
        from nidaqmx import Task
        self._s = settings or EtlSettings()
        self._chan = chan
        self._sink: Sink | None = None
        self._volts: float | None = None
        lo, hi = self._s.limits
        self._task = Task()
        try:
            # The task's own range: the DAQ refuses a write outside it.
            self._task.ao_channels.add_ao_voltage_chan(chan, min_val=lo, max_val=hi)
        except Exception:
            self._task.close()      # else nidaqmx warns it was never closed
            raise

    @property
    def channel(self) -> str:
        return self._chan

    @property
    def volts(self) -> float | None:
        """Last value written; None before the first."""
        return self._volts

    def set_sink(self, sink: Sink | None) -> None:
        self._sink = sink

    def apply_settings(self, settings: EtlSettings) -> None:
        # Limits stay the task's from __init__; only the setpoint moves.
        self._s.volts = settings.volts
        v = self._s.clamped()
        self._task.write(v)         # first: a failed write must not be recorded
        self._volts = v
        if self._sink is not None:
            self._sink(v)

    def close(self) -> None:
        try:
            self._task.write(self._s.limits[0])     # rest at the range's bottom
        except Exception:
            pass
        self._task.close()


class MockEtlController:
    """The same surface, no DAQ."""

    def __init__(self, chan: str = "", settings: EtlSettings | None = None):
        self._s = settings or EtlSettings()
        self._chan = chan
        self._sink: Sink | None = None
        self._volts: float | None = None

    @property
    def channel(self) -> str:
        return self._chan

    @property
    def volts(self) -> float | None:
        return self._volts

    def set_sink(self, sink: Sink | None) -> None:
        self._sink = sink

    def apply_settings(self, settings: EtlSettings) -> None:
        self._s.volts = settings.volts
        self._volts = self._s.clamped()
        if self._sink is not None:
            self._sink(self._volts)

    def close(self) -> None:
        self._volts = None
