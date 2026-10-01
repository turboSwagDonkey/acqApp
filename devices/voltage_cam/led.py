"""Primary illumination LED on an NI DAQ digital line (nidaqmx), and a mock.

Same shape as pupil_cam's LedController. This rig: pupil LED line0, primary
LED line2, puffer line7 — no two tasks may share a line.
"""

from __future__ import annotations


class LedController:
    def __init__(self, chan: str = "Dev3/port0/line2"):
        from nidaqmx import Task
        self._task = Task()
        self._task.do_channels.add_do_chan(chan)   # write() auto-starts it
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    def on(self) -> None:
        self.set(True)

    def off(self) -> None:
        self.set(False)

    def set(self, value: bool) -> None:
        self._state = value
        self._task.write(value)

    def close(self) -> None:
        try:
            self._task.write(False)
        except Exception:
            pass
        self._task.close()


class MockLedController:
    def __init__(self, chan: str = "Dev3/port0/line2"):
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    def on(self)  -> None: self._state = True
    def off(self) -> None: self._state = False
    def set(self, value: bool) -> None: self._state = value
    def close(self) -> None: self._state = False
