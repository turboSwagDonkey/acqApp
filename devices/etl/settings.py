"""Electrically tunable lens (path 2) — the settings model. No Qt (the
widgets are in `panel.py`).

The ETL offsets path 2's focal plane from path 1's. Calibrated as a line:
offset_um = (volts - cofocal_volts) * um_per_volt, along stage Z (so a
positive offset is path 2 focusing where +Z would take path 1). A real lens
bends that line; a calibration holds near where it was measured.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EtlSettings:
    volts:     float = 0.0      # the setpoint held on the AO line
    # The driver's analog-input range. No widget: edited in acqapp_local.json,
    # since a wrong range can drive the lens past its rating.
    min_volts: float = 0.0
    max_volts: float = 5.0
    # Measured: both paths in focus on one plane at cofocal_volts, and the
    # stage Z that refocuses path 1 per volt. 0 = not calibrated (volts only).
    cofocal_volts: float = 0.0
    um_per_volt:   float = 0.0

    @property
    def limits(self) -> tuple[float, float]:
        lo, hi = float(self.min_volts), float(self.max_volts)
        return (lo, hi) if lo <= hi else (hi, lo)

    def clamped(self) -> float:
        lo, hi = self.limits
        return min(hi, max(lo, float(self.volts)))

    @property
    def calibrated(self) -> bool:
        return bool(self.um_per_volt)

    def offset_um(self) -> float | None:
        """The held (clamped) setpoint as µm, or None uncalibrated."""
        if not self.calibrated:
            return None
        return (self.clamped() - self.cofocal_volts) * self.um_per_volt

    def volts_for(self, offset_um: float) -> float:
        """The setpoint for `offset_um` (unclamped; the write clamps)."""
        if not self.calibrated:
            raise ValueError("ETL not calibrated: no µm per volt")
        return self.cofocal_volts + offset_um / self.um_per_volt
