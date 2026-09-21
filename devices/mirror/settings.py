"""PMT/camera mirror — settings model. No Qt.

There's no device to configure: two switches in ThorImage move together
(operator-confirmed, PLAN.md §6) — the galvo (in/out) and the visualizer
path (camera/PMT) — and acqApp represents that as ONE two-state toggle:

    CAMERA = galvo OUT + visualizer -> camera
    PMT    = galvo IN  + visualizer -> PMT

Both are driven by ThorImage over a serial connection acqApp can't share
with it (ThorImage holds COM54 exclusively while open — see PLAN.md §0/§6),
so there's nothing here to read back, only to persist: which state the
operator last told acqApp they set BOTH switches to, so the panel doesn't
reset to a default that may be wrong.
"""
from __future__ import annotations
from dataclasses import dataclass

CAMERA = "camera"   # galvo out, visualizer -> camera
PMT = "pmt"         # galvo in,  visualizer -> PMT


@dataclass
class MirrorSettings:
    state: str = CAMERA   # CAMERA or PMT — the operator's last-asserted state
