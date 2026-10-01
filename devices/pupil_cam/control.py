"""Eye-tracking LED — NI DAQ analog output into the LEDD1B's MOD input.

0-5 V = 0-100% is Thorlabs' datasheet convention, unverified on this unit:
check the first real on() against the front-panel readout.

Dev3/ao0 on this rig (operator-confirmed 2026-09-10). A DO line only gives
MOD 0 V or logic-high, hence analog.
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
        self._intensity = 1.0

    @property
    def is_on(self) -> bool:
        return self._state

    @property
    def intensity(self) -> float:
        return self._intensity

    def set_intensity(self, fraction: float) -> None:
        """0..1 of MAX_VOLTS. Live if on; never turns the LED on."""
        self._intensity = max(0.0, min(1.0, float(fraction)))
        if self._state:
            self._task.write(self._intensity * self.MAX_VOLTS)

    def on(self) -> None:
        self.set(True)

    def off(self) -> None:
        self.set(False)

    def set(self, value: bool) -> None:
        self._state = value
        self._task.write(self._intensity * self.MAX_VOLTS if value else 0.0)

    def close(self) -> None:
        try:
            self._task.write(0.0)
        except Exception:
            pass
        self._task.close()


class MockLedController:
    MAX_VOLTS = 5.0   # test_device_contracts wants LedController's surface

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
