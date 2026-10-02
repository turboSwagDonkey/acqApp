"""PMT/camera mirror — settings model. No Qt.

Two ThorImage switches move together (operator-confirmed), so
acqApp models ONE two-state toggle:

    CAMERA = galvo OUT + visualizer -> camera
    PMT    = galvo IN  + visualizer -> PMT

ThorImage holds COM54 exclusively while open, so nothing is read back; this
only persists the operator's last-asserted state.
"""
from __future__ import annotations
from dataclasses import dataclass

CAMERA = "camera"   # galvo out, visualizer -> camera
PMT = "pmt"         # galvo in,  visualizer -> PMT


@dataclass
class MirrorSettings:
    state: str = CAMERA   # CAMERA or PMT
