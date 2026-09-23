"""Closed loop — the rule's Qt panel and the arming switch."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.widgets import spin
from acqApp.closed_loop.settings import (COMPARISONS, TARGETS, LoopSettings,
                                         SignalSource)


class SettingsPanel(QWidget):
    """The rule, plus the arming switch. `settings_changed` carries a
    `LoopSettings` (persisted); `armed_changed` the arming state (never)."""

    settings_changed = pyqtSignal(object)      # emits LoopSettings
    armed_changed    = pyqtSignal(bool)

    def __init__(self, settings: LoopSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or LoopSettings()
        self._sources: list[SignalSource] = []
        self._lit: bool | None = None      # last readout styling applied
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
            "Live signals offered by modules loaded this session.")
        lay.addRow("Watch:", self._cmb_source)

        self._cmb_cmp = QComboBox()
        self._cmb_cmp.addItems(list(COMPARISONS))
        self._cmb_cmp.setCurrentText(self._s.comparison)
        lay.addRow("Fire when it goes:", self._cmb_cmp)

        self._spn_thr = spin(-1e6, 1e6, self._s.threshold, decimals=2)
        lay.addRow("Threshold:", self._spn_thr)

        self._spn_hold = spin(
            0.0, 60.0, self._s.hold_s, decimals=2, step=0.05, suffix=" s",
            tooltip="Condition must hold this long before it counts as an "
                    "event. 0 fires on any single sample that crosses, "
                    "including noise.")
        lay.addRow("…and holds for:", self._spn_hold)

        self._spn_refr = spin(
            0.0, 3600.0, self._s.refractory_s, decimals=2, suffix=" s",
            tooltip="Minimum gap between two fires. At 0 a condition that "
                    "stays true fires on every evaluation — 200 a second.")
        lay.addRow("Minimum gap:", self._spn_refr)

        self._chk_retrig = QCheckBox("Repeat while the condition holds")
        self._chk_retrig.setChecked(self._s.retrigger)
        self._chk_retrig.setToolTip(
            "Off: signal must fall back past threshold before rule "
            "can fire again — one event per bout.")
        lay.addRow(self._chk_retrig)

        self._spn_max = spin(0, 9999, self._s.max_fires)
        self._spn_max.setSpecialValueText("∞  no limit")
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

        self._spn_dur = spin(
            0.0, 10.0, self._s.duration_s, decimals=3, step=0.010, suffix=" s",
            tooltip="Puffer: how long valve stays open. DMD: how long pattern "
                    "is held before it's stopped (0 = until Stop is pressed).")
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

        note = QLabel("Arming is never remembered between launches, and "
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
        """Fill source list from what loaded modules offer.

        Keeps selected key if it's still on offer. Combo is rebuilt
        whenever a session starts (units follow wheel's scaling), and
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
        """Restrict output list to modules actually loaded.

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
        self._btn_arm.setText("ARMED — rule will fire" if on
                              else "Disarmed — rule won't fire")
        self.armed_changed.emit(on)

    # ── readout ──────────────────────────────────────────────────────────────
    def set_readout(self, value: float | None, met: bool, n_fires: int,
                    armed: bool) -> None:
        u = self._units()
        v = "—" if value is None else f"{value:+.1f}{(' ' + u) if u else ''}"
        self._lbl_readout.setText(
            f"{v}   ·   condition {'MET' if met else 'not met'}   ·   "
            f"{n_fires} fired{'' if armed else '   (disarmed)'}")
        # Only on a change: this runs every display tick, and setStyleSheet
        # repolishes the widget against the window's whole cascade to re-apply
        # an identical string (269 calls in a 6 s session, 2026-08-26 sweep).
        lit = met and armed
        if lit is not self._lit:
            self._lbl_readout.setStyleSheet(
                f"color:{style.HEX['closed_loop']};" if lit else "")
            self._lit = lit

    def clear_readout(self) -> None:
        self._lbl_readout.setText("—")
        self._lbl_readout.setStyleSheet("")
        self._lit = None                   # written out of band; force a repaint

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
