"""
Launch-time check that chip 7 (PMT/camera light path) is on CAMERA/epi,
correcting either channel that isn't.

Measured 2026-09-11: ThorImage's Camera path reads GR=OUT, CAMERA=OUT; PMT
reads both IN. So MIRROR_OUT on both is the target, whatever ThorImage's
"New MCM6000 cards reverse camera lightpath positions" flip implies.

Operator-authorized silent auto-correct (PLAN.md S2, 2026-09-11): a narrow
exception to "ask before actuating", for this launch-time check only, never
mid-session. If the port won't open (ThorImage holds COM54, or no
controller), it reports instead of raising so startup isn't blocked.
"""
from __future__ import annotations
from dataclasses import dataclass

from acqApp.devices.stage.driver import (
    MCM6101, MCM6101Error, MIRROR_CHAN_GR, MIRROR_CHAN_CAMERA, MIRROR_OUT,
)

CHIP = 7
AXIS = CHIP - 1
CAMERA_STATE = MIRROR_OUT  # for both GR and CAMERA


@dataclass
class MirrorCheckResult:
    ok: bool                     # True if the port opened and the check ran
    corrected: bool = False      # True if a SET was sent to fix a mismatch
    gr_state: int | None = None
    camera_state: int | None = None
    error: str | None = None     # set when the port couldn't be opened


def ensure_camera_default(port: str = "COM54", driver_cls=MCM6101) -> MirrorCheckResult:
    """Read chip 7's GR/CAMERA channels; set either that isn't CAMERA_STATE.
    `driver_cls` is injectable for tests."""
    dev = driver_cls(port)
    try:
        dev.open()
    except Exception as exc:
        return MirrorCheckResult(ok=False, error=str(exc))

    try:
        gr = dev.get_mirror_state(AXIS, MIRROR_CHAN_GR)
        cam = dev.get_mirror_state(AXIS, MIRROR_CHAN_CAMERA)
        corrected = False
        for chan, state in ((MIRROR_CHAN_GR, gr), (MIRROR_CHAN_CAMERA, cam)):
            if state != CAMERA_STATE:
                dev.set_mirror_state(AXIS, chan, CAMERA_STATE)
                corrected = True
        return MirrorCheckResult(ok=True, corrected=corrected, gr_state=gr, camera_state=cam)
    except MCM6101Error as exc:
        return MirrorCheckResult(ok=False, error=str(exc))
    finally:
        dev.close()
