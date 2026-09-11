"""
Mirror: the launch-time CAMERA/epi default check (`devices/mirror/startup.py`).

Silent auto-correct is a deliberate, narrow exception to "ask before
actuating" (PLAN.md S2, 2026-09-11) — worth defending with a control that
proves it corrects only what's actually wrong, touches nothing else, and
never raises past the caller when the port can't be opened (ThorImage
holding COM54 is the normal case this exists for).

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_mirror_startup.py
"""
from __future__ import annotations
import sys

from _harness import Report

from acqApp.devices.mirror.startup import ensure_camera_default, AXIS
from acqApp.devices.stage.driver import (
    MIRROR_CHAN_GR, MIRROR_CHAN_CAMERA, MIRROR_OUT, MIRROR_IN,
)


class FakeDriver:
    """Stands in for MCM6101: no serial port, records every call."""

    def __init__(self, port: str, states: dict[int, int] | None = None,
                 open_fails: bool = False):
        self.port = port
        self._states = dict(states or {})
        self._open_fails = open_fails
        self.opened = False
        self.closed = False
        self.set_calls: list[tuple[int, int, int]] = []

    def open(self):
        if self._open_fails:
            raise PermissionError("Access is denied.")  # matches the real ThorImage-holds-port error
        self.opened = True

    def close(self):
        self.closed = True

    def get_mirror_state(self, axis: int, channel: int) -> int:
        return self._states.get(channel, MIRROR_OUT)

    def set_mirror_state(self, axis: int, channel: int, state: int):
        self._states[channel] = state
        self.set_calls.append((axis, channel, state))


def check_already_correct(r: Report) -> None:
    """Both channels already OUT: no SET sent, reported as not corrected."""
    fake = FakeDriver("COM54", states={MIRROR_CHAN_GR: MIRROR_OUT, MIRROR_CHAN_CAMERA: MIRROR_OUT})
    result = ensure_camera_default(driver_cls=lambda port: fake)
    r.check(result.ok, "check runs when the port opens")
    r.check(not result.corrected, "already-correct state is not reported as corrected")
    r.check(fake.set_calls == [], "no SET sent when nothing was wrong")
    r.check(fake.closed, "port closed after a successful check")


def check_corrects_mismatch(r: Report) -> None:
    """GR left on PMT (IN): only GR is corrected, CAMERA (already OUT) is left alone."""
    fake = FakeDriver("COM54", states={MIRROR_CHAN_GR: MIRROR_IN, MIRROR_CHAN_CAMERA: MIRROR_OUT})
    result = ensure_camera_default(driver_cls=lambda port: fake)
    r.check(result.ok and result.corrected, "mismatch is detected and corrected")
    r.check(fake.set_calls == [(AXIS, MIRROR_CHAN_GR, MIRROR_OUT)],
            "SET sent for the wrong channel only, not the one already correct")


def check_corrects_both(r: Report) -> None:
    """Both left on PMT (IN): both channels corrected."""
    fake = FakeDriver("COM54", states={MIRROR_CHAN_GR: MIRROR_IN, MIRROR_CHAN_CAMERA: MIRROR_IN})
    result = ensure_camera_default(driver_cls=lambda port: fake)
    r.check(result.ok and result.corrected, "both-wrong case is detected and corrected")
    r.check(set(fake.set_calls) == {(AXIS, MIRROR_CHAN_GR, MIRROR_OUT),
                                     (AXIS, MIRROR_CHAN_CAMERA, MIRROR_OUT)},
            "SET sent for both channels")


def check_port_unavailable(r: Report) -> None:
    """ThorImage holding COM54 (or the controller absent): reported, not raised."""
    fake = FakeDriver("COM54", open_fails=True)
    try:
        result = ensure_camera_default(driver_cls=lambda port: fake)
    except Exception as e:                                # noqa: BLE001
        r.check(False, f"a closed port raised {type(e).__name__} instead of being reported")
        return
    r.check(not result.ok and result.error, "unopenable port reported as not ok, with a reason")
    r.check(not result.corrected, "no correction attempted when the port never opened")
    r.check(fake.set_calls == [], "no SET sent when the port never opened")


def main() -> int:
    r = Report("mirror-startup")
    check_already_correct(r)
    check_corrects_mismatch(r)
    check_corrects_both(r)
    check_port_unavailable(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
