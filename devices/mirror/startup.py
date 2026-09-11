"""
Launch-time default check for chip 7 (PMT/camera light path): confirm the
GR and CAMERA channels report the CAMERA/epi state and correct them if not.

Raw target confirmed at the rig 2026-09-11: with ThorImage's light path set
to Camera, chip 7 reports GR=OUT, CAMERA=OUT; set to PMT/scanning, both
report IN. So MIRROR_OUT on both channels is the CAMERA/epi target -- no
guessing at the "New MCM6000 cards reverse camera lightpath positions"
inversion ThorImage's own source warns about.

Operator-authorized silent auto-correct (PLAN.md S2, 2026-09-11): this is a
deliberate, narrow exception to "ask before actuating" -- only for this
launch-time, ThorImage-closed check, never during a live session. Only ever
runs successfully when ThorImage does NOT hold COM54; when the port can't be
opened (ThorImage running, or the controller absent), this reports that
rather than raising, so a caller can warn without blocking startup.
"""
from __future__ import annotations
from dataclasses import dataclass

from acqApp.devices.stage.driver import (
    MCM6101, MCM6101Error, MIRROR_CHAN_GR, MIRROR_CHAN_CAMERA, MIRROR_OUT,
)

CHIP = 7
AXIS = CHIP - 1
CAMERA_STATE = MIRROR_OUT  # confirmed target for both GR and CAMERA channels


@dataclass
class MirrorCheckResult:
    ok: bool                     # True if the port opened and the check ran
    corrected: bool = False      # True if a SET was sent to fix a mismatch
    gr_state: int | None = None
    camera_state: int | None = None
    error: str | None = None     # set when the port couldn't be opened


def ensure_camera_default(port: str = "COM54", driver_cls=MCM6101) -> MirrorCheckResult:
    """Read chip 7's GR/CAMERA channels; correct either that isn't
    MIRROR_OUT (the confirmed CAMERA/epi default). `driver_cls` is
    injectable for testing without real hardware."""
    dev = driver_cls(port)
    try:
        dev.open()
    except Exception as exc:
        return MirrorCheckResult(ok=False, error=str(exc))

    try:
        gr = dev.get_mirror_state(AXIS, MIRROR_CHAN_GR)
        cam = dev.get_mirror_state(AXIS, MIRROR_CHAN_CAMERA)
        corrected = False
        if gr != CAMERA_STATE:
            dev.set_mirror_state(AXIS, MIRROR_CHAN_GR, CAMERA_STATE)
            corrected = True
        if cam != CAMERA_STATE:
            dev.set_mirror_state(AXIS, MIRROR_CHAN_CAMERA, CAMERA_STATE)
            corrected = True
        return MirrorCheckResult(ok=True, corrected=corrected, gr_state=gr, camera_state=cam)
    except MCM6101Error as exc:
        return MirrorCheckResult(ok=False, error=str(exc))
    finally:
        dev.close()
