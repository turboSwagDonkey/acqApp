"""
Closed loop — fire an output from what an instrument is measuring.

Phase 5. `sync.py`'s bus fires named events at a *time* ("puff at t = 5 s");
this is the other kind, one that depends on what the animal is doing.

  `SignalSource`      what a rule can watch, contributed by the adapters — so
                      this module depends on *a scalar signal*, never on the
                      wheel. Adding pupil radius is one method on that adapter.
  `LoopRule`          the decision, as a pure function of (value, time). No Qt,
                      no devices; pinned by `tests/test_closed_loop.py`.
  `ClosedLoopWorker`  a thread that samples the source and runs the rule.

**Why a thread.** The 30 Hz display tick paints the camera previews, so a rule
evaluated there inherits every stall they have (same argument as
`pupil_cam/track_worker.py`). It *polls* the source rather than consuming it:
`get_latest()` hands each sample out once and the display is already that
consumer, so the loop reads a non-consuming snapshot instead.

**The decision happens on this thread; the actuation does not.** The worker
emits `fired`; the adapter re-emits it on the ordinary trigger bus, so a
rule-driven puff runs the identical path to a scheduled one.

**Arming is deliberately absent from `LoopSettings`** so it cannot be
persisted. Same reasoning as the LED in audit #4: restoring "armed" means a
rule that fires the puffer at next launch, before anyone checked the threshold.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.acq.worker import PullWorker

COMPARISONS = ("above", "below")

# Outputs a rule can drive, key → label. Both are actuators: the key is a
# module key, so firing one is `sync.fire(key, duration)` and the module's own
# `on_trigger` does the rest.
TARGETS: dict[str, str] = {"puffer": "Puffer", "dmd": "DMD"}

# Rule evaluation rate. Not a panel setting: it is a latency floor, not an
# experimental parameter, and 200 Hz is already below the wheel's own 120 Hz
# sample rate — polling faster would only re-read the same sample.
POLL_HZ = 200.0


@dataclass(frozen=True)
class SignalSource:
    """A live scalar a rule can watch.

    `read()` returns `(value, acquired_at)`, or None while the signal isn't
    running yet — normal, not an error. `acquired_at` is a `perf_counter()`
    reading of when the sample was ACQUIRED, the domain `Recorder.put(at=)`
    wants, so a fire lands in the file at the sample that caused it rather than
    at the GUI hop after it.

    Called from the loop's thread, and must not consume: the display tick is
    already the consumer of every worker's `get_latest()`.
    """
    key:   str                                       # stable id: config + file
    label: str                                       # what the combo shows
    units: str
    read:  Callable[[], tuple[float, float] | None]


@dataclass
class LoopSettings:
    """The rule, as persisted — note what is *not* here: `armed`.

    All of this reaches `acqapp_local.json` and the session file. Arming lives
    on the panel, so no code path can restore a rig into an armed state.
    """
    source:       str   = ""          # SignalSource.key; "" = first on offer
    comparison:   str   = "above"     # one of COMPARISONS
    threshold:    float = 50.0        # in the source's units
    hold_s:       float = 0.25        # condition must hold this long to count
    refractory_s: float = 5.0         # minimum gap between two fires
    retrigger:    bool  = True        # False = the condition must clear first
    target:       str   = "puffer"    # a TARGETS key
    duration_s:   float = 0.100       # passed to the target
    max_fires:    int   = 0           # 0 = no limit


class LoopRule:
    """Should this fire, given the newest value? Pure, Qt-free, testable.

    One `update()` per sample, True on exactly the samples that should actuate.
    Each gate exists for a way a bare threshold misbehaves on a real signal:

      `hold_s`       noise crosses a threshold many times a second; holding
                     turns a crossing into an event
      `refractory_s` without it, a condition that stays true fires on every
                     sample — 200 puffs a second
      `retrigger`    True: re-fire each refractory while the condition holds.
                     False: one fire per bout, the signal must fall back first
      `max_fires`    session ceiling, so a wrong rule is wrong a bounded number
                     of times

    `update(None, t)` is "no signal yet" and never fires — which matters for
    `below`: a source that isn't running must not read as zero and satisfy it.
    """

    def __init__(self, settings: LoopSettings | None = None) -> None:
        self._s = settings or LoopSettings()
        self.n_fires = 0
        self.reset()

    def reset(self) -> None:
        """Forget everything, including the fire count. Per session."""
        self._since: float | None = None      # when the condition became true
        self._last_fire: float | None = None
        self._cleared = True                  # false since the last fire?
        self.n_fires = 0

    def configure(self, settings: LoopSettings) -> None:
        """Adopt new settings mid-session, keeping the fire history: nudging a
        threshold must not hand back a fresh `max_fires` budget, nor let the
        next sample fire inside the refractory window."""
        self._s = settings
        self._since = None

    def idle(self) -> None:
        """Called while disarmed. Forgets the in-progress hold, so arming
        starts the hold timer at the moment of arming rather than from whenever
        the animal happened to start running. The count and refractory survive.
        """
        self._since = None
        self._cleared = True

    @property
    def settings(self) -> LoopSettings:
        return self._s

    def satisfied(self, value: float | None) -> bool:
        """Is the condition true right now? (Ignores every gate — this is what
        the panel's readout shows, so a threshold can be set against a live
        animal while disarmed.)"""
        if value is None:
            return False
        return (value > self._s.threshold if self._s.comparison == "above"
                else value < self._s.threshold)

    def update(self, value: float | None, t: float) -> bool:
        s = self._s
        if not self.satisfied(value):
            self._since = None
            self._cleared = True
            return False
        if self._since is None:
            self._since = t
        if t - self._since < s.hold_s:
            return False
        if not s.retrigger and not self._cleared:
            return False
        if self._last_fire is not None and t - self._last_fire < s.refractory_s:
            return False
        if s.max_fires and self.n_fires >= s.max_fires:
            return False
        self._last_fire = t
        self._cleared = False
        self.n_fires += 1
        return True


class ClosedLoopWorker(PullWorker):
    """Samples one `SignalSource` and runs a `LoopRule` over it.

    `fired` is emitted from this thread; Qt queues it, so the actuation happens
    on the GUI thread with every other one.

    Disarmed, the rule still *evaluates* and the panel still shows whether the
    condition is met — it just does not fire. That is what makes a threshold
    settable against a live animal without actuating anything.
    """

    fired = pyqtSignal(str, float, float)      # (target, duration_s, value)

    _STOP_WAIT_MS = 2000

    def __init__(self, source: SignalSource,
                 settings: LoopSettings | None = None,
                 poll_hz: float = POLL_HZ) -> None:
        super().__init__()
        self._source = source
        self._rule = LoopRule(settings)
        self._period = 1.0 / max(1.0, poll_hz)
        self._armed = False
        self._recorded = 0
        self._cfg_lock = threading.Lock()
        self._pending: LoopSettings | None = None

    # ── GUI side ─────────────────────────────────────────────────────────────
    def set_armed(self, on: bool) -> None:
        self._armed = bool(on)

    @property
    def armed(self) -> bool:
        return self._armed

    def configure(self, settings: LoopSettings) -> None:
        """Queue a settings change, applied before the next evaluation. Queued,
        not written straight in, for the same reason as `track_worker`: the
        panel edits on the GUI thread while `update()` runs on this one."""
        with self._cfg_lock:
            self._pending = settings

    @property
    def n_fires(self) -> int:
        """Fires this SESSION — the loop runs under Live view too, so this can
        exceed what is in the file."""
        return self._rule.n_fires

    @property
    def recorded_fires(self) -> int:
        """Fires that reached the sink, i.e. that are in `/closed_loop`.

        Separate from `n_fires` because they genuinely differ: the rule runs all
        session but the sink is attached only while recording, so an armed rule
        can fire before Record. Filing `n_fires` as `loop_fires` would leave an
        attribute disagreeing with the length of the stream beside it.
        """
        return self._recorded

    @property
    def source_key(self) -> str:
        return self._source.key

    # ── thread ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop:
            now = time.perf_counter()

            with self._cfg_lock:
                pending, self._pending = self._pending, None
            if pending is not None:
                self._rule.configure(pending)

            sample = self._source.read()
            if sample is None:
                value, at = None, now
            else:
                value, at = float(sample[0]), float(sample[1])

            if self._armed:
                hit = self._rule.update(value, at)
            else:
                self._rule.idle()
                hit = False

            # Readout for the display tick — the newest evaluation, whether or
            # not it fired. Not routed through _publish(): the sink must carry
            # fires only, not a 200 Hz copy of a stream the wheel already
            # records.
            self._set_latest((value, self._rule.satisfied(value),
                              self._rule.n_fires, self._armed))

            if hit:
                s = self._rule.settings
                sink = self._sink           # snapshot: set_sink(None) is safe
                if sink is not None:
                    sink((value, at))
                    self._recorded += 1
                self.fired.emit(s.target, s.duration_s, float(value or 0.0))

            slp = self._period - (time.perf_counter() - now)
            if slp > 0:
                time.sleep(slp)


class SettingsPanel(QWidget):
    """The rule, plus the arming switch.

    `settings_changed` carries a `LoopSettings` (persisted); `armed_changed`
    carries the arming state (never persisted — see the module docstring).
    """

    settings_changed = pyqtSignal(object)      # emits LoopSettings
    armed_changed    = pyqtSignal(bool)

    def __init__(self, settings: LoopSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or LoopSettings()
        self._sources: list[SignalSource] = []
        self._build()

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── the condition ────────────────────────────────────────────────────
        grp = QGroupBox("Trigger rule")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_source = QComboBox()
        self._cmb_source.setToolTip(
            "Live signals offered by the modules loaded this session.")
        lay.addRow("Watch:", self._cmb_source)

        self._cmb_cmp = QComboBox()
        self._cmb_cmp.addItems(list(COMPARISONS))
        self._cmb_cmp.setCurrentText(self._s.comparison)
        lay.addRow("Fire when it goes:", self._cmb_cmp)

        self._spn_thr = QDoubleSpinBox()
        self._spn_thr.setRange(-1e6, 1e6)
        self._spn_thr.setDecimals(2)
        self._spn_thr.setValue(self._s.threshold)
        lay.addRow("Threshold:", self._spn_thr)

        self._spn_hold = QDoubleSpinBox()
        self._spn_hold.setRange(0.0, 60.0)
        self._spn_hold.setDecimals(2)
        self._spn_hold.setSingleStep(0.05)
        self._spn_hold.setSuffix(" s")
        self._spn_hold.setValue(self._s.hold_s)
        self._spn_hold.setToolTip(
            "The condition must hold this long before it counts as an event. "
            "0 fires on any single sample that crosses, including noise.")
        lay.addRow("…and holds for:", self._spn_hold)

        self._spn_refr = QDoubleSpinBox()
        self._spn_refr.setRange(0.0, 3600.0)
        self._spn_refr.setDecimals(2)
        self._spn_refr.setSuffix(" s")
        self._spn_refr.setValue(self._s.refractory_s)
        self._spn_refr.setToolTip(
            "Minimum gap between two fires. At 0 a condition that stays true "
            "fires on every evaluation — 200 a second.")
        lay.addRow("Minimum gap:", self._spn_refr)

        self._chk_retrig = QCheckBox("Repeat while the condition holds")
        self._chk_retrig.setChecked(self._s.retrigger)
        self._chk_retrig.setToolTip(
            "Off: the signal must fall back past the threshold before the rule "
            "can fire again — one event per bout.")
        lay.addRow(self._chk_retrig)

        self._spn_max = QSpinBox()
        self._spn_max.setRange(0, 9999)
        self._spn_max.setSpecialValueText("∞  no limit")
        self._spn_max.setValue(self._s.max_fires)
        lay.addRow("Stop after:", self._spn_max)
        root.addWidget(grp)

        # ── the output ───────────────────────────────────────────────────────
        ogrp = QGroupBox("Output")
        olay = QFormLayout(ogrp)
        olay.setSpacing(4)

        self._cmb_target = QComboBox()
        for key, label in TARGETS.items():
            self._cmb_target.addItem(label, key)
        idx = self._cmb_target.findData(self._s.target)
        self._cmb_target.setCurrentIndex(max(0, idx))
        olay.addRow("Fires the:", self._cmb_target)

        self._spn_dur = QDoubleSpinBox()
        self._spn_dur.setRange(0.0, 10.0)
        self._spn_dur.setDecimals(3)
        self._spn_dur.setSingleStep(0.010)
        self._spn_dur.setSuffix(" s")
        self._spn_dur.setValue(self._s.duration_s)
        self._spn_dur.setToolTip(
            "Puffer: how long the valve stays open. DMD: how long the pattern "
            "is held before it is stopped (0 = until Stop is pressed).")
        olay.addRow("For:", self._spn_dur)
        root.addWidget(ogrp)

        # ── arming ───────────────────────────────────────────────────────────
        agrp = QGroupBox("Arm")
        alay = QVBoxLayout(agrp)
        alay.setSpacing(4)

        self._btn_arm = QPushButton()
        self._btn_arm.setCheckable(True)
        self._btn_arm.setStyleSheet(style.toggle_btn("closed_loop"))
        self._btn_arm.toggled.connect(self._on_arm_toggled)
        alay.addWidget(self._btn_arm)

        note = QLabel("Arming is never remembered between launches, and the "
                      "rule only runs while a session does.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa0a6;")
        alay.addWidget(note)

        self._lbl_readout = QLabel("—")
        f = self._lbl_readout.font()
        f.setPointSize(f.pointSize() + 1)
        f.setBold(True)
        self._lbl_readout.setFont(f)
        self._lbl_readout.setWordWrap(True)
        alay.addWidget(self._lbl_readout)
        root.addWidget(agrp)
        root.addStretch()

        self._on_arm_toggled(False)

        self._cmb_source.currentIndexChanged.connect(self._on_source_picked)
        for w in (self._spn_thr, self._spn_hold, self._spn_refr, self._spn_dur):
            w.valueChanged.connect(self._emit)
        self._spn_max.valueChanged.connect(self._emit)
        self._chk_retrig.toggled.connect(self._emit)
        self._cmb_cmp.currentTextChanged.connect(self._emit)
        self._cmb_target.currentIndexChanged.connect(self._emit)

    # ── sources ──────────────────────────────────────────────────────────────
    def set_sources(self, sources: list[SignalSource]) -> None:
        """Fill the source list from what the loaded modules offer.

        Keeps the selected key if it is still on offer. The combo is rebuilt
        whenever a session starts (the units follow the wheel's scaling), and
        falling back to index 0 there would silently repoint a configured rule
        at a different signal.
        """
        want = self.selected_source() or self._s.source
        self._sources = list(sources)
        blocked = self._cmb_source.blockSignals(True)
        self._cmb_source.clear()
        for s in sources:
            self._cmb_source.addItem(f"{s.label}  [{s.units}]", s.key)
        idx = self._cmb_source.findData(want)
        self._cmb_source.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_source.blockSignals(blocked)
        self._apply_units()

    def selected_source(self) -> str:
        return self._cmb_source.currentData() or ""

    def set_targets(self, keys) -> None:
        """Restrict the output list to the modules actually loaded.

        A rule pointed at an unloaded module fires onto the trigger bus and
        nothing listens — it would look armed and working and do nothing.
        """
        want = self._cmb_target.currentData() or self._s.target
        allowed = [k for k in TARGETS if k in set(keys)]
        blocked = self._cmb_target.blockSignals(True)
        self._cmb_target.clear()
        for key in allowed:
            self._cmb_target.addItem(TARGETS[key], key)
        idx = self._cmb_target.findData(want)
        self._cmb_target.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_target.blockSignals(blocked)

    def _units(self) -> str:
        for s in self._sources:
            if s.key == self.selected_source():
                return s.units
        return ""

    def _apply_units(self) -> None:
        u = self._units()
        self._spn_thr.setSuffix(f" {u}" if u else "")

    def _on_source_picked(self, *_a) -> None:
        self._apply_units()
        self._emit()

    # ── arming ───────────────────────────────────────────────────────────────
    @property
    def armed(self) -> bool:
        return self._btn_arm.isChecked()

    def _on_arm_toggled(self, on: bool) -> None:
        self._btn_arm.setText("ARMED — the rule will fire" if on
                              else "Disarmed — the rule will not fire")
        self.armed_changed.emit(on)

    # ── readout ──────────────────────────────────────────────────────────────
    def set_readout(self, value: float | None, met: bool, n_fires: int,
                    armed: bool) -> None:
        u = self._units()
        v = "—" if value is None else f"{value:+.1f}{(' ' + u) if u else ''}"
        self._lbl_readout.setText(
            f"{v}   ·   condition {'MET' if met else 'not met'}   ·   "
            f"{n_fires} fired{'' if armed else '   (disarmed)'}")
        self._lbl_readout.setStyleSheet(
            f"color:{style.HEX['closed_loop']};" if (met and armed) else "")

    def clear_readout(self) -> None:
        self._lbl_readout.setText("—")
        self._lbl_readout.setStyleSheet("")

    # ── settings ─────────────────────────────────────────────────────────────
    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> LoopSettings:
        return LoopSettings(
            source=self.selected_source(),
            comparison=self._cmb_cmp.currentText(),
            threshold=self._spn_thr.value(),
            hold_s=self._spn_hold.value(),
            refractory_s=self._spn_refr.value(),
            retrigger=self._chk_retrig.isChecked(),
            target=self._cmb_target.currentData() or self._s.target,
            duration_s=self._spn_dur.value(),
            max_fires=self._spn_max.value(),
        )
