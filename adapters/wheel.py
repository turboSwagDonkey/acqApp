"""
The running wheel's adapter — and the first module to offer a closed-loop
signal source (`signal_sources`).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.closed_loop import SignalSource
from acqApp.devices import ClockedWorker
from acqApp.adapters.base import PLOT_HISTORY, ModuleAdapter, _plot
from acqApp.wheel.acquisition import EncoderWorker, MockEncoderWorker
from acqApp.wheel.settings import (EncoderSettings,
                                   SettingsPanel as WheelSettingsPanel)


class WheelModule(ModuleAdapter):
    key = "wheel"
    tab_label = "Wheel"
    plot_label = "Wheel"

    worker: ClockedWorker | None         # narrows ModuleAdapter.worker

    def __init__(self, win) -> None:
        super().__init__(win)
        self._plot_w = None
        self._curve = None
        self._y: list[float] = []
        self._units: str | None = None       # current Y-axis unit label

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = WheelSettingsPanel(
            config.load_dataclass(EncoderSettings, self.key))
        self.panel.settings_changed.connect(self._on_settings)
        return self.panel

    def build_plot(self) -> QWidget:
        self._plot_w, self._curve = _plot(
            "Wheel distance", "Distance", "m", "Sample", self.key)
        return self._plot_w

    def _on_settings(self, st) -> None:
        """Push live V/rev and wheel-diameter changes to a running worker.

        These two are the scaling constants for every wheel number in the
        session file, and both are still unmeasured on this rig — so they are
        also the values most likely to be silently wrong if they reset.
        """
        config.save_settings(self.key, asdict(st))
        if self.worker is not None:
            self.worker.set_scaling(st.volts_per_rev, st.wheel_dia_mm)

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        s = self.panel.settings
        if emulate:
            self._adopt(MockEncoderWorker(s.volts_per_rev, s.wheel_dia_mm))
        else:
            self._adopt(EncoderWorker(s.channel, s.rate,
                                      s.volts_per_rev, s.wheel_dia_mm))
        self._y.clear()

    # ── display ──
    def update_display(self) -> None:
        sample = self.worker.get_latest() if self.worker is not None else None
        if sample is None:
            return
        v, speed, dist, _t = sample      # the worker already derived speed+distance
        self._y.append(self._show(v, speed, dist))
        del self._y[:-PLOT_HISTORY]
        self._curve.setData(self._y)

    def _show(self, v: float, speed: float, dist: float) -> float:
        """Pick units/labels for the current scaling, update the live readout,
        and return the value to plot. With no V/rev set there is nothing to
        derive, so it plots the raw voltage instead."""
        s = self.panel.settings
        if not s.volts_per_rev:
            self._axis("Voltage", "V")
            self._title(None, "")
            self.panel.set_readout(f"{v:.4f} V   (set V/rev to get speed)")
            return v
        if s.wheel_dia_mm:
            self._axis("Distance", "m")
            self._title(speed, "mm/s")
            self.panel.set_readout(
                f"speed {speed:+.1f} mm/s      net {dist / 1000:+.2f} m")
            return dist / 1000.0                 # plot net distance in metres
        self._axis("Distance", "rev")
        self._title(speed, "rev/s")
        self.panel.set_readout(f"speed {speed:+.2f} rev/s      net {dist:+.1f} rev")
        return dist                              # plot net distance in revolutions

    def _axis(self, name: str, units: str) -> None:
        """Relabel the Y axis only when the unit actually changes."""
        if self._units == units:
            return
        self._units = units
        self._plot_w.setLabel("left", name, units=units)

    def _title(self, speed: float | None, units: str) -> None:
        """Show the live speed as a number in the distance plot's title."""
        if speed is None:
            self._plot_w.setTitle("Wheel distance")
        else:
            prec = 1 if units == "mm/s" else 2
            self._plot_w.setTitle(
                f"Wheel distance   —   speed {speed:+.{prec}f} {units}")

    # ── closed loop ──
    def signal_sources(self) -> list[SignalSource]:
        """Both wheel speeds, because they are not interchangeable.

        `wheel_speed` is the recorded one — a least-squares slope centred a
        second in the past (`_EncoderBase._report`), so a rule on it acts a
        second after the animal starts running, and agrees exactly with the
        trace in the file. `wheel_speed_live` is the EMA velocity behind it:
        noisier, but current. Which one a paradigm wants is a scientific
        choice, so both are offered and the session file records which was
        used.

        The reads are non-consuming (`snapshot()`), so watching the wheel never
        takes a sample away from the plot.
        """
        u = self._speed_units()
        return [
            SignalSource("wheel_speed_live", "Wheel speed (live)", u,
                         self._read_live),
            SignalSource("wheel_speed", "Wheel speed (recorded, ~1 s lag)", u,
                         self._read_reported),
        ]

    def _speed_units(self) -> str:
        s = self.panel.settings if self.panel is not None else EncoderSettings()
        return "mm/s" if (s.volts_per_rev and s.wheel_dia_mm) else "rev/s"

    def _read_live(self) -> tuple[float, float] | None:
        snap = self.worker.snapshot() if self.worker is not None else None
        return None if snap is None else (snap[2], snap[3])

    def _read_reported(self) -> tuple[float, float] | None:
        snap = self.worker.snapshot() if self.worker is not None else None
        return None if snap is None else (snap[1], snap[3])

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(sample: tuple[float, float, float, float | None]) -> None:
            """Raw voltage + derived speed + distance: three scalar streams
            sharing the one timebase.

            `at` is when the DAQ sampled the voltage, not when the block
            carrying it reached us — hardware-timed reads arrive in batches, so
            stamping on arrival would quantise the wheel's timebase to the read
            cadence exactly as it did for the camera (#1).
            """
            v, speed, dist, at = sample
            rec.put("wheel_voltage", v, at=at)
            rec.put("wheel_speed", speed, at=at)
            rec.put("wheel_distance", dist, at=at)

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        linear = bool(s.volts_per_rev and s.wheel_dia_mm)
        return {
            "wheel_channel":        s.channel,
            "wheel_rate_hz":        s.rate,
            "wheel_volts_per_rev":  s.volts_per_rev or 0.0,
            "wheel_dia_mm":         s.wheel_dia_mm or 0.0,
            "wheel_speed_units":    "mm/s" if linear else "rev/s",
            "wheel_distance_units": "mm"   if linear else "rev",
            "wheel_distance_mode":  "net_forward",   # signed; back-spin subtracts
            # Derived speed/distance are computed with look-ahead and so lag the
            # (live) voltage stream by this many seconds. _SIGN orients forward.
            "wheel_speed_lag_s":    EncoderWorker._LAG_S,
            "wheel_sign":           EncoderWorker._SIGN,
        }

    def final_metadata(self) -> dict[str, Any]:
        # Which timebase the samples actually carry is only known once the
        # worker has configured the board — and it decides whether the recorded
        # speed is a hardware measurement or a scheduler artefact. Read off the
        # worker rather than defaulted: "software" and "unknown" mean different
        # things, and a rate of 0.0 would be indistinguishable from a measured
        # stall.
        if self.worker is None:
            return {"wheel_timestamp_source": "unknown",
                    "wheel_rate_actual_hz":   0.0}
        return {
            "wheel_timestamp_source": self.worker.timestamp_source,
            "wheel_rate_actual_hz":   self.worker.actual_rate,
        }
