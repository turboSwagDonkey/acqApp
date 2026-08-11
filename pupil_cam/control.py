"""
Eye-tracking LED controller — NI DAQ digital output.

LedController     : drives Dev3/port0/line0 (or any DO line) via nidaqmx.
MockLedController : no hardware, tracks state only.
"""

from __future__ import annotations


class LedController:
    # NOTE: line1, distinct from the puffer's line0 — two DAQ tasks cannot
    # own the same physical digital line.
    def __init__(self, chan: str = "Dev3/port0/line1"):
        from nidaqmx import Task
        self._task = Task()
        self._task.do_channels.add_do_chan(chan)
        self._state = False
        # don't call task.start() — let write(auto_start=True) handle it

    @property
    def is_on(self) -> bool:
        return self._state

    def on(self) -> None:
        self._state = True
        self._task.write(True)

    def off(self) -> None:
        self._state = False
        self._task.write(False)

    def set(self, value: bool) -> None:
        self._state = value
        self._task.write(value)

    def close(self) -> None:
        try:
            self._task.write(False)   # leave LED off on exit
        except Exception:
            pass
        self._task.close()


class MockLedController:
    def __init__(self, chan: str = "Dev3/port0/line1"):
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    def on(self)  -> None: self._state = True
    def off(self) -> None: self._state = False
    def set(self, value: bool) -> None: self._state = value
    def close(self) -> None: self._state = False
