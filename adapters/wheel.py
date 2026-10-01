"""The running wheel's adapter; also offers its speed as a SignalSource."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import ClockedWorker, SignalSource
from acqApp.adapters.base import PLOT_HISTORY, ModuleAdapter, _plot
from acqApp.devices.wheel.acquisition import EncoderWorker, MockEncoderWorker
from acqApp.devices.wheel.panel import SettingsPanel as WheelSettingsPanel
from acqApp.devices.wheel.settings import EncoderSettings


class WheelModule(ModuleAdapter):
    key = "wheel"
    tab_label = "Wheel"
    plot_label = "Wheel"

    worker: ClockedWorker | None

    def __init__(self, win) -> None:
        super().__init__(win)
        self._plot_w = None
        self._curve = None
        self._y: list[float] = []
        # Last-set labels: each set is a relayout, so only on change.
        self._units: str | None = None
        self._title_text: str | None = None
        self._readout_text: str | None = None
        # Cached: `panel.settings` rebuilds from widgets per call.
        self._cfg = EncoderSettings()

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = WheelSettingsPanel(
            config.load_dataclass(EncoderSettings, self.key))
        self.panel.settings_changed.connect(self._on_settings)
        self._cfg = self.panel.settings
        return self.panel

    def build_plot(self) -> QWidget:
        self._plot_w, self._curve = _plot(
            "Wheel distance", "Distance", "m", "Sample", self.key)
        return self._plot_w

    def _on_settings(self, st) -> None:
        """V/rev and diameter apply live; they scale every wheel number filed."""
        config.save_settings(self.key, asdict(st))
        self._cfg = st
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
        v, speed, dist, _t = sample
        self._y.append(self._show(v, speed, dist))
        del self._y[:-PLOT_HISTORY]
        self._curve.setData(self._y)

    def _show(self, v: float, speed: float, dist: float) -> float:
        """Label for the current scaling and return what to plot (raw volts
        without V/rev)."""
        cfg = self._cfg
        if not cfg.volts_per_rev:
            self._axis("Voltage", "V")
            self._title(None, "")
            self._readout(f"{v:.4f} V   (set V/rev to get speed)")
            return v
        if cfg.wheel_dia_mm:
            self._axis("Distance", "m")
            self._title(speed, "mm/s")
            self._readout(
                f"speed {speed:+.1f} mm/s      net {dist / 1000:+.2f} m")
            return dist / 1000.0
        self._axis("Distance", "rev")
        self._title(speed, "rev/s")
        self._readout(f"speed {speed:+.2f} rev/s      net {dist:+.1f} rev")
        return dist

    def _readout(self, text: str) -> None:
        if text != self._readout_text:
            self._readout_text = text
            self.panel.set_readout(text)

    def _axis(self, name: str, units: str) -> None:
        if self._units == units:
            return
        self._units = units
        self._plot_w.setLabel("left", name, units=units)

    def _title(self, speed: float | None, units: str) -> None:
        if speed is None:
            text = "Wheel distance"
        else:
            prec = 1 if units == "mm/s" else 2
            text = f"Wheel distance   —   speed {speed:+.{prec}f} {units}"
        if text != self._title_text:
            self._title_text = text
            self._plot_w.setTitle(text)

    # ── live signal (visuomotor) ──
    def signal_sources(self) -> list[SignalSource]:
        """Both speeds: the recorded one matches the file but is ~1 s late;
        the live EMA is noisier but current. Non-consuming reads."""
        u = self._speed_units()
        return [
            SignalSource("wheel_speed_live", "Wheel speed (live)", u,
                         self._read_live),
            SignalSource("wheel_speed", "Wheel speed (recorded, ~1 s lag)", u,
                         self._read_reported),
        ]

    def _speed_units(self) -> str:
        s = self._cfg
        return "mm/s" if (s.volts_per_rev and s.wheel_dia_mm) else "rev/s"

    def _snapshot(self):
        return self.worker.snapshot() if self.worker is not None else None

    def _read_live(self) -> tuple[float, float] | None:
        snap = self._snapshot()
        return None if snap is None else (snap[2], snap[3])

    def _read_reported(self) -> tuple[float, float] | None:
        snap = self._snapshot()
        return None if snap is None else (snap[1], snap[3])

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(sample: tuple[float, float, float, float | None]) -> None:
            # `at` is the DAQ sample time; blocks arrive batched.
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
            "wheel_distance_mode":  "net_forward",   # back-spin subtracts
            "wheel_speed_lag_s":    EncoderWorker._LAG_S,
            "wheel_sign":           EncoderWorker._SIGN,
        }

    def final_metadata(self) -> dict[str, Any]:
        # No worker: "unknown" keeps the 0.0 Hz from reading as a stall.
        if self.worker is None:
            return {"wheel_timestamp_source": "unknown",
                    "wheel_rate_actual_hz":   0.0}
        return {
            "wheel_timestamp_source": self.worker.timestamp_source,
            "wheel_rate_actual_hz":   self.worker.actual_rate,
        }
