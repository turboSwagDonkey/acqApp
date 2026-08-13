"""
The closed loop's adapter. The rule itself is instrument-agnostic and lives in
`acqApp/closed_loop.py`; this only wires it to the loaded modules' signal
sources and to the shared trigger bus.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.closed_loop import (ClosedLoopWorker, LoopSettings, SignalSource,
                                SettingsPanel as LoopPanel)
from acqApp.modules.base import ModuleAdapter


class ClosedLoopModule(ModuleAdapter):
    """Phase 5: fire an output from what another instrument is measuring.

    Not an instrument — it owns no device. It is a `ModuleAdapter` because it
    needs exactly what one provides: a settings tab, a per-session worker, a
    recording sink and metadata. It is **last** in `config.MODULES` so that
    every source-providing adapter exists before its panel asks what is on
    offer.

    It reaches its neighbours only through the two service methods on the
    window (`signal_sources`, `module_keys`) and the shared trigger bus — never
    into another adapter. That is what keeps the wheel ignorant of the loop and
    the loop ignorant of the puffer.
    """
    key = "closed_loop"
    tab_label = "Closed loop"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._sources: dict[str, SignalSource] = {}
        self._reported_fires = 0        # last count announced on the status bar

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = LoopPanel(config.load_dataclass(LoopSettings, self.key))
        self._refresh_offers()
        self.panel.settings_changed.connect(self._on_settings)
        self.panel.armed_changed.connect(self._on_armed)
        return self.panel

    def _refresh_offers(self) -> None:
        """Re-read what the loaded modules offer. Done at build and again per
        session, because the wheel's units follow its V/rev and diameter."""
        self._sources = {s.key: s for s in self.win.signal_sources()}
        self.panel.set_sources(list(self._sources.values()))
        self.panel.set_targets(self.win.module_keys())

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        if self.worker is not None:
            self.worker.configure(s)

    def _on_armed(self, on: bool) -> None:
        if self.worker is not None:
            self.worker.set_armed(on)
        if not on:
            self.win.status("closed loop disarmed")
        elif self.worker is None:
            self.win.status("closed loop ARMED — it runs when a session starts")
        else:
            s = self.panel.settings
            self.win.status(f"closed loop ARMED — {s.source} {s.comparison} "
                            f"{s.threshold:g} fires the {s.target}")

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        self._refresh_offers()
        s = self.panel.settings
        src = self._sources.get(s.source)
        if src is None:
            # The module that produces this signal isn't loaded this session.
            # Say so: an armed rule that can never fire looks identical to one
            # whose condition simply hasn't been met.
            self.win.status("closed loop: no signal source loaded — rule idle")
            return
        self._reported_fires = 0
        worker = self._adopt(ClosedLoopWorker(src, s))
        worker.set_armed(self.panel.armed)
        worker.fired.connect(self._on_fired)

    def stop(self) -> None:
        super().stop()
        self.panel.clear_readout()

    def _on_fired(self, target: str, duration: float, value: float) -> None:
        """The rule fired. Runs on the GUI thread — but keep it to one emit.

        `fired` comes from the loop's own thread, and a `ModuleAdapter` is not a
        QObject, so the obvious conclusion is that Qt calls this directly on the
        emitting thread. Measured on PyQt6, that is **wrong**: a slot which is
        not a QObject's bound method runs on the thread where `connect()` was
        called — `build_session`, on the GUI thread. So it arrives queued.

        That guarantee lives in the caller, not here: move the `connect()` onto
        a worker thread and this silently becomes a cross-thread GUI call. Hence
        the status line stays in `update_display()`, where the thread is
        obvious. `sync.fire()` then takes a rule-driven puff down the identical
        path as a scheduled one, into the session file included.
        """
        self.win.sync.fire(target, duration)

    # ── display ──
    def update_display(self) -> None:
        latest = self.worker.get_latest() if self.worker is not None else None
        if latest is None:
            return
        self.panel.set_readout(*latest)
        # Reporting the fire here rather than in _on_fired keeps the GUI call
        # somewhere its thread is guaranteed locally — see the note there.
        n = latest[2]
        if n != self._reported_fires:
            self._reported_fires = n
            s = self.panel.settings
            self.win.status(f"closed loop → {s.target}  "
                            f"({latest[0]:+.3g}, {n} this session)")

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(event: tuple[float | None, float]) -> None:
            """One entry per fire: the value that caused it, stamped at the
            instant of the sample it was measured from — so the decision lines
            up in the file with its own cause rather than with the GUI hop
            after it."""
            value, at = event
            rec.put("closed_loop", float(value or 0.0), at=at)

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        src = self._sources.get(s.source)
        return {
            # A rule that was never armed and one that was armed but never met
            # its condition both leave /closed_loop empty. This is what tells
            # them apart afterwards.
            "loop_armed":        self.panel.armed,
            "loop_source":       s.source,
            "loop_source_units": src.units if src is not None else "",
            "loop_comparison":   s.comparison,
            "loop_threshold":    s.threshold,
            "loop_hold_s":       s.hold_s,
            "loop_refractory_s": s.refractory_s,
            "loop_retrigger":    s.retrigger,
            "loop_target":       s.target,
            "loop_duration_s":   s.duration_s,
            "loop_max_fires":    s.max_fires,
        }

    def final_metadata(self) -> dict[str, Any]:
        # 0 is the honest answer for "no worker": build_session refuses to make
        # one when the rule's signal source isn't loaded, and a rule that never
        # ran fired nothing. `loop_armed` above is what distinguishes that from
        # a rule that ran and never met its condition.
        if self.worker is None:
            return {"loop_fires": 0, "loop_fires_session": 0}
        return {
            # Fires that are IN this file — always equal to len(/closed_loop),
            # so an attribute and the stream beside it cannot disagree.
            "loop_fires":         self.worker.recorded_fires,
            # Fires over the whole session. Larger when the rule was armed
            # during Live view and fired before Record was pressed — those
            # actuated the hardware but are in no file, which is worth knowing
            # rather than silently dropping.
            "loop_fires_session": self.worker.n_fires,
        }
