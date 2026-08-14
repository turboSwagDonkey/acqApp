"""Wheel encoder — the settings model. No Qt (the widgets are in `panel.py`)."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class EncoderSettings:
    channel:       str   = "Dev3/ai2"
    rate:          float = 120.0   # Hz — 120 keeps real spins < 0.5 rev/sample (no aliasing)
    # The ai2 voltage encodes wheel POSITION (angle). These scale it to motion:
    #   revolutions = voltage / volts_per_rev  →  speed = d(rev)/dt (low-pass filtered)
    #   distance = net (signed) revolutions;  linear: mm = rev · π · wheel_dia_mm
    volts_per_rev: float = 4.912   # measured V/rev from the rig capture; None → raw voltage
    wheel_dia_mm:  float = 150.0   # None → report speed/distance in rev, not mm
