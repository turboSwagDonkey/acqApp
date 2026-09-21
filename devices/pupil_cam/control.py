"""
Eye-tracking LED controller — NI DAQ analog output.

Drives the LEDD1B driver's MOD input directly: 0-5V = 0-100% intensity is
the driver's own convention (Thorlabs datasheet), unverified against this
specific unit — check the first real on() lands where the front-panel
readout expects before trusting the scale.

NOTE: ao0 on this rig (operator-confirmed, 2026-09-10). Previously wired to
port0/line0 as a plain digital on/off (a DO line can only put out two
voltage levels, so MOD only ever saw 0V or logic-high — full or nothing,
no dial); rewired to an analog output for real intensity control. The
puffer (line7) and primary LED (line2, devices/voltage_cam/led.py — its
DC20 driver has no analog setpoint input, so that one stays digital-only)
are unaffected.

LedController     : drives Dev3/ao0 (or any AO channel) via nidaqmx.
MockLedController : no hardware, tracks state only.
"""

from __future__ import annotations


class LedController:
    MAX_VOLTS = 5.0   # driver's full-scale MOD input, 0..1 intensity below

    def __init__(self, chan: str = "Dev3/ao0"):
        from nidaqmx import Task
        self._task = Task()
        self._task.ao_channels.add_ao_voltage_chan(
            chan, min_val=0.0, max_val=self.MAX_VOLTS)
        self._state = False
        self._intensity = 1.0   # 0..1; on() writes this * MAX_VOLTS

    @property
    def is_on(self) -> bool:
        return self._state

    @property
    def intensity(self) -> float:
        return self._intensity

    def set_intensity(self, fraction: float) -> None:
        """0..1 of MAX_VOLTS. Applies immediately if already on; otherwise
        just the level the next on() will use — same "no light until an
        explicit on()" rule as everything else here."""
        self._intensity = max(0.0, min(1.0, float(fraction)))
        if self._state:
            self._task.write(self._intensity * self.MAX_VOLTS)

    def on(self) -> None:
        self._state = True
        self._task.write(self._intensity * self.MAX_VOLTS)

    def off(self) -> None:
        self._state = False
        self._task.write(0.0)

    def set(self, value: bool) -> None:
        self._state = value
        self._task.write(self._intensity * self.MAX_VOLTS if value else 0.0)

    def close(self) -> None:
        try:
            self._task.write(0.0)   # leave LED off on exit
        except Exception:
            pass
        self._task.close()


class MockLedController:
    MAX_VOLTS = 5.0   # matches LedController's API surface (test_device_contracts)

    def __init__(self, chan: str = "Dev3/ao0"):
        self._state = False
        self._intensity = 1.0

    @property
    def is_on(self) -> bool:
        return self._state

    @property
    def intensity(self) -> float:
        return self._intensity

    def set_intensity(self, fraction: float) -> None:
        self._intensity = max(0.0, min(1.0, float(fraction)))

    def on(self)  -> None: self._state = True
    def off(self) -> None: self._state = False
    def set(self, value: bool) -> None: self._state = value
    def close(self) -> None: self._state = False
