"""The DMD projector's adapter: an output that also answers to the trigger bus."""
from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from acqApp import config, style
from acqApp.acq.devices import ProjectorController
from acqApp.devices.dmd import alp
from acqApp.devices.dmd.control import (MODE_PATTERN, MODE_ROI, DmdController,
                                        DmdSettings, MockDmdController)
from acqApp.devices.dmd.panel import SettingsPanel as DmdPanel
from acqApp.adapters.base import ModuleAdapter
from acqApp.sidebar import MiniPattern

# The standalone dmdGUI_project app's alignment keys -> DmdSettings attribute.
_SHARED_ALIGNMENT = (("defaultScale", "scale_pct"), ("defaultRot", "rotation_deg"))
_LIGHT_OFF, _LIGHT_ON = "DMD OFF", "● DMD ON"       # the card's light switch


def _shared_alignment() -> dict[str, float]:
    """DmdSettings attribute -> the standalone app's value. Another program's
    file: a value that isn't a finite number is skipped, not raised."""
    shared = alp.sibling_config()
    out: dict[str, float] = {}
    for shared_key, attr in _SHARED_ALIGNMENT:
        try:
            val = float(shared[shared_key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(val):
            out[attr] = val
    return out


class DmdModule(ModuleAdapter):
    key = "dmd"
    tab_label = "DMD"

    controller: ProjectorController | None

    # Live update coalesces a drag's burst of changes into one re-upload.
    _LIVE_DEBOUNCE_MS = 150

    def __init__(self, win) -> None:
        super().__init__(win)
        self._real = False              # the ALP really opened (not a fallback)
        self._live_timer = QTimer()
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(self._LIVE_DEBOUNCE_MS)
        self._live_timer.timeout.connect(lambda: self._guard("display", self.display))
        # One timer, restarted per trigger: an earlier trigger's stop must not
        # cut a later stimulus short.
        self._stop_timer = QTimer()
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(lambda: self._guard("stop", self.stop_display))
        self._light_btn: QPushButton | None = None

    def _guard(self, what: str, fn, *args, dark: bool = True) -> None:
        """Run a slot's work. A raise would abort the app (no excepthook) with
        the DMD perhaps lit, so it is reported, and with `dark` the light is
        forced off: after a failed display or stop its state is unknown."""
        try:
            fn(*args)
        except Exception as e:                      # noqa: BLE001
            msg = f"{what} failed ({type(e).__name__}: {e})"
            print(f"[DMD] {msg}")
            self.win.status(f"DMD {msg}" + (" — light forced off" if dark else ""))
            if dark:
                self._go_dark()

    def _go_dark(self) -> None:
        c = self.controller
        if c is not None:
            try:
                c.stop()
            except Exception as e:                  # noqa: BLE001
                print(f"[DMD] could not stop after a failure: {e}")
        self._stop_timer.stop()
        self._live_timer.stop()
        self._show_light(bool(c is not None and c.projecting))

    # ── the sidebar card: the uploaded frame and the light switch ──
    def mini_view(self) -> QWidget:
        """Built fresh per card, so the switch is too; the newest is the one
        `_show_light` keeps in step."""
        b = QPushButton()
        b.setCheckable(True)
        c = style.HEX["dmd"]
        b.setStyleSheet(
            style.toggle_btn("dmd")
            + f"QPushButton{{font-weight:bold;padding:4px 8px}}"
              f"QPushButton:checked{{background:{c};color:white;"
              f"border:1px solid {c}}}")
        b.setToolTip(
            "THIS PROJECTS LIGHT.\nTurns the DMD on or off using the "
            "display selected on the DMD tab (Image, ROIs or All on). "
            "With nothing selected it projects all-on.")
        b.toggled.connect(lambda on: self._guard("switch", self._on_light_button, on))
        self._light_btn = b
        self._show_light(self.controller is not None
                         and self.controller.projecting)

        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(MiniPattern(self.key, self._mini_state))
        lay.addWidget(b)
        return box

    def _show_light(self, lit: bool) -> None:
        """Make the button say what the projector is doing; never re-triggers it."""
        b = self._light_btn
        if b is None:
            return
        b.blockSignals(True)
        b.setChecked(lit)
        b.blockSignals(False)
        b.setText(_LIGHT_ON if lit else _LIGHT_OFF)
        b.setEnabled(self.controller is not None)

    def _display_problem(self) -> str:
        """Why the SELECTED display can't be shown ('' = it can). It is never
        swapped for all-on: a stale file or a missing calibration is a fault to
        report, not a licence to light the whole field."""
        s = self.panel.settings
        if s.display_mode == MODE_PATTERN and s.pattern_path is not None \
                and not Path(s.pattern_path).is_file():
            return f"DMD not lit: pattern file {Path(s.pattern_path).name} is missing"
        if s.display_mode == MODE_ROI and s.rois and not s.calib_path:
            return "DMD not lit: ROIs need a calibration (run Calibrate… first)"
        return ""

    def _nothing_selected(self) -> bool:
        s = self.panel.settings
        return ((s.display_mode == MODE_PATTERN and s.pattern_path is None)
                or (s.display_mode == MODE_ROI and not s.rois))

    def _on_light_button(self, on: bool) -> None:
        if not on:
            self.stop_display()
            self.win.status("DMD off")
            return
        why = self._display_problem()
        if why:
            self._show_light(False)
            self.win.status(why)
            return
        fallback = self._nothing_selected()
        if fallback:
            self.panel.set_all_on()     # the tab shows what is lit
        self.display()
        name = {"pattern": "the pattern", "roi": "the ROIs",
                "all_on": "all-on"}[self.panel.mode]
        self.win.status(f"DMD on: {name}"
                        + (" (nothing was selected)" if fallback else ""))

    def _mini_state(self):
        """The uploaded frame, the selection's name, and whether it is lit."""
        c = self.controller
        name = self.panel.display_name if self.panel is not None else ""
        # The mock uploads a placeholder for "nothing selected".
        frame = c.frame if (c is not None and name) else None
        return frame, name, bool(c is not None and c.projecting)

    def build_panel(self) -> QWidget:
        self.panel = DmdPanel(self._settings())
        # Via the adapter: the controller is rebuilt on every Emulate toggle.
        g = self._guard
        self.panel.load_requested.connect(lambda p: g("load", self.load, p))
        self.panel.display_requested.connect(lambda: g("display", self.display))
        self.panel.stop_requested.connect(lambda: g("stop", self.stop_display))
        self.panel.settings_changed.connect(
            lambda s: g("settings save", self._save, s, dark=False))
        self.panel.rois_edit_requested.connect(lambda: g("ROI editor", self.edit_rois))
        self.panel.calibrate_requested.connect(lambda: g("calibration", self.calibrate))
        self.panel.live_toggled.connect(self._on_live_toggled)
        return self.panel

    def _on_live_toggled(self, on: bool) -> None:
        # Or a change just before unchecking still projects 150 ms later.
        if not on:
            self._live_timer.stop()

    # ── the camera↔DMD registration ──
    def calibrate(self) -> None:
        """The sweep dialog: project stripes, image them, fit a transform."""
        from PyQt6.QtWidgets import QMessageBox

        from acqApp.devices.dmd.sweep import CalibrationDialog
        from acqApp.devices.voltage_cam.presets import DEFAULT_PRESET

        if self.controller is None:
            return
        if "voltage_cam" not in self.win.module_keys():
            QMessageBox.information(
                self.panel, "No voltage camera",
                "The sweep images each pattern with the voltage camera, and "
                "that module isn't loaded.\n\n"
                "Restart and tick Voltage camera in the startup picker.")
            return

        # Everything downstream speaks unbinned full-sensor px, so measure at
        # full frame 1x1: a crop shifts the origin, binning scales the pixels.
        prev_preset = self.win.camera_preset("voltage_cam")
        prev_binning = self.win.camera_binning("voltage_cam")
        cropped = prev_preset is not None and prev_preset != DEFAULT_PRESET
        binned = prev_binning is not None and prev_binning != 1
        needs_switch = cropped or binned
        was_live = None
        if needs_switch:
            what = " and ".join(
                (["cropped to a smaller capture area"] if cropped else [])
                + ([f"binned {prev_binning}x{prev_binning}"] if binned else []))
            if self.win.is_recording():
                QMessageBox.warning(
                    self.panel, "Recording in progress",
                    f"The voltage camera is {what}, and calibration needs "
                    "full frame at 1x1 — but a recording is running, and "
                    "changing either needs a restart.\n\nStop recording "
                    "first, then calibrate.")
                return
            QMessageBox.information(
                self.panel, "Switching to full frame",
                f"The voltage camera is {what}. Calibration only means what "
                "it says in the pixels it was measured in, so this switches "
                "to full frame at 1x1 for the run and puts the camera back "
                "the way it was afterwards.")
            # Structural settings: stop live so the dialog restarts it with them.
            was_live = self.win.set_live(False)
            if cropped:
                self.win.set_camera_preset("voltage_cam", DEFAULT_PRESET)
            if binned:
                self.win.set_camera_binning("voltage_cam", 1)

        try:
            dlg = CalibrationDialog(
                self.controller, lambda: self.win.latest_frame("voltage_cam"),
                parent=self.panel, real=self._real,
                on_saved=self._adopt_calibration,
                set_live=self.win.set_live)
            dlg.exec()
        finally:
            # The dialog drives the controller directly; the card must follow.
            self._show_light(self.controller.projecting)
            if needs_switch:
                self.win.set_live(False)
                if cropped:
                    self.win.set_camera_preset("voltage_cam", prev_preset)
                if binned:
                    self.win.set_camera_binning("voltage_cam", prev_binning)
                self.win.set_live(was_live)

    def _adopt_calibration(self, path: str) -> None:
        self.panel.set_calib_path(path)
        self.win.status(f"DMD calibration saved and loaded: {Path(path).name}")

    # ── photostimulation ROIs ──
    def _frame_geometry(self, frame) -> tuple[tuple[float, float], float]:
        """-> (offset, scale) mapping `frame`'s px onto full-sensor px, from
        the preset it was CAPTURED under (the combo may already name the next
        one): its (hpos, vpos) and its binning."""
        from acqApp.devices.voltage_cam.presets import PRESETS

        preset = PRESETS.get(self.win.latest_frame_preset("voltage_cam"))
        if preset is None:
            return (0.0, 0.0), 1.0
        return (preset.hpos, preset.vpos), preset.hsize / frame.shape[1]

    def _live_frame(self):
        """The camera's newest frame as a `roi_live.LiveFrame`, or None."""
        f = self.win.latest_frame("voltage_cam")
        return None if f is None else (f, *self._frame_geometry(f))

    def _editor(self, frame):
        """A `RoiEditor` showing `frame`, mapped onto the calibration (which
        is in full-sensor px)."""
        from acqApp.devices.dmd.roi_panel import RoiEditor
        from acqApp.devices.voltage_cam.presets import SENSOR_H, SENSOR_W

        calib, why = self._calibration()
        if why:
            self.win.status(why)
        offset, scale = self._frame_geometry(frame)
        ed = RoiEditor(calib, offset=offset, sensor=(SENSOR_W, SENSOR_H),
                       scale=scale)
        ed.set_image(frame)
        return ed

    def edit_rois(self) -> None:
        """`RoiEditor` on the camera's newest frame (taken full-field), live
        view under it. Starts Live view, never light: all-on first is the
        operator's step."""
        from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QMessageBox,
                                     QVBoxLayout)

        from acqApp import style
        from acqApp.devices.dmd.roi import RoiSet
        from acqApp.devices.dmd.roi_live import attach_live_view

        frame = self.win.latest_frame("voltage_cam")
        if frame is None:
            QMessageBox.information(
                self.panel, "No camera frame",
                "The ROI editor draws on a voltage-camera frame, and none has "
                "arrived yet.\n\nLoad the voltage camera and press Live view "
                "(or Record), then try again.\n\nTo see the projected field in "
                "the snapshot, put the DMD in all-on and press Display first.")
            return

        dlg = QDialog(self.panel)
        dlg.setWindowTitle("Photostimulation ROIs")
        dlg.resize(1000, 940)
        dlg.setStyleSheet(style.accent_panel("dmd"))
        lay = QVBoxLayout(dlg)
        ed = self._editor(frame)
        if self.panel.rois:
            ed.load(RoiSet.from_list(list(self.panel.rois)))
        live = attach_live_view(ed, self._live_frame)
        lay.addWidget(ed, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        was_live = self.win.set_live(True)
        live.start()
        try:
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
        finally:
            live.stop()
            self.win.set_live(was_live)
        if accepted:
            rois = ed.roi_set.to_list()
            self.panel.set_rois(tuple(rois))
            self.win.status(f"{len(rois)} photostimulation ROI(s) saved")

    def draw_paired_rois(self, name: str):
        """`PairRoiDialog` on the camera's newest frame (the FOV just saved)
        -> the saved ROI set's path, or None. Light only from its Illuminate
        button, and off again when it closes."""
        from acqApp.devices.dmd import roi_store
        from acqApp.devices.dmd.pair_dialog import PairRoiDialog

        frame = self.win.latest_frame("voltage_cam")
        if frame is None or self.panel is None:
            return None

        def illuminate(rois) -> None:
            if rois is None:
                self.stop_display()
            else:
                self.panel.set_roi_pattern(name, rois.to_list())
                self.display()

        dlg = PairRoiDialog(
            self._editor(frame), name, live_source=self._live_frame,
            illuminate=illuminate, set_live=self.win.set_live,
            parent=self.panel)
        dlg.exec()
        if dlg.path is not None:        # the panel shows what was saved
            self.panel.set_roi_pattern(name, roi_store.load(dlg.path).to_list())
        return dlg.path

    def _calibration(self):
        """-> (calib | None, complaint). Missing is allowed but never silent."""
        path = self.panel.calib_path if self.panel is not None else ""
        if not path:
            return None, ""
        try:
            from acqApp.devices.dmd.calibration import DmdCalibration
            from acqApp.devices.dmd.control import orient_calibration
            return orient_calibration(DmdCalibration.load(path),
                                      self.panel.settings), ""
        except Exception as e:      # noqa: BLE001 — missing, corrupt, or stale
            return None, (f"DMD calibration {Path(path).name} could not be "
                          f"read ({type(e).__name__}) — ROIs can be drawn but "
                          f"not projected")

    def _settings(self) -> DmdSettings:
        """Saved settings; scale/rotation re-adopted from the standalone app
        if it re-aligned since our last save (`_shared_*` = what we saw)."""
        s = config.load_dataclass(DmdSettings, self.key)
        saved = config.load_settings(self.key)
        for attr, val in _shared_alignment().items():
            if attr not in saved or saved.get(f"_shared_{attr}") != val:
                setattr(s, attr, val)
        return s

    def _save(self, s) -> None:
        d = asdict(s)
        d["pattern_path"] = str(s.pattern_path) if s.pattern_path else None
        d["rois"] = list(s.rois or ())
        for attr, val in _shared_alignment().items():
            d[f"_shared_{attr}"] = val
        config.save_settings(self.key, d)
        if self.panel is not None and self.panel.live:
            self._live_timer.start()

    def build_controller(self, emulate: bool) -> None:
        s = self.panel.settings if self.panel is not None else DmdSettings()
        real = False
        if not emulate:
            try:
                self.controller = DmdController(s)
                real = True
            except Exception as e:      # noqa: BLE001 — any ALP/driver failure
                # Usually the standalone app still holds the ALP.
                print(f"[main] DMD unavailable ({type(e).__name__}: {e}) — "
                      f"using mock. If the standalone DMD app is open, close "
                      f"it and toggle Emulate off again.")
        if not real:
            self.controller = MockDmdController(s)
        self._real = real
        if self.panel is not None:
            self.panel.set_device(self.controller.device_name,
                                  self.controller.resolution, real)
        # Or Display projects the new controller's default, not the named file.
        path = s.pattern_path
        if path is not None and Path(path).is_file():
            self.controller.load_pattern(Path(path))
        self._show_light(False)         # a new controller starts dark

    def load(self, path) -> None:
        if self.controller is not None:
            self.controller.load_pattern(path)

    def display(self) -> None:
        if self.controller is not None:
            self.controller.apply_settings(self.panel.settings)
            self.controller.display()
            self._show_light(True)

    def stop_display(self) -> None:
        if self.controller is not None:
            self.controller.stop()
            self._show_light(False)

    def on_trigger(self, name: str, duration: float) -> None:
        """The ALP holds until Stop, so a timed stimulus is display plus a
        single-shot stop; `duration <= 0` leaves it up."""
        if name != self.key:
            return
        self._guard("trigger", self._triggered, duration)

    def _triggered(self, duration: float) -> None:
        self.display()
        if duration > 0:
            self._stop_timer.start(int(duration * 1000))
        else:
            self._stop_timer.stop()

    # ── what a routine may drive ──
    def pattern_target(self):
        return self if self.controller is not None else None

    def set_pattern(self, path: str) -> None:
        """Load an image or a `.roi.json`. Uploads, projects nothing."""
        from acqApp.devices.dmd import roi_store

        p = Path(path)
        if roi_store.is_roi_file(p):
            name, rois = roi_store.load_named(p)
            self.panel.set_roi_pattern(name, rois.to_list())
            if self.controller is not None:
                self.controller.apply_settings(self.panel.settings)
        else:
            self.panel.set_pattern_path(p)
            self.load(p)

    def set_all_on(self) -> None:
        """Config only (projects on Display, or at once with Live update)."""
        if self.panel is not None:
            self.panel.set_all_on()

    def set_sub_sampling(self, n: int) -> None:
        """Config only; n = 1 is off."""
        if self.panel is not None:
            self.panel.set_sub_sampling(n)

    def set_light(self, on: bool) -> None:
        """THE call that emits light."""
        self.display() if on else self.stop_display()

    def attach_sink(self, rec) -> None:
        if self.controller is not None:
            self.controller.set_sink(lambda idx: rec.put("dmd", float(idx)))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        c = self.controller
        w, h = c.resolution if c is not None else (0, 0)
        return {
            "dmd_on_time_ms":  s.on_time_ms,
            "dmd_static_hold": s.static_hold,
            "dmd_trigger":     s.trigger_mode,
            "dmd_repeats":     s.n_repeats,
            "dmd_device":      c.device_name if c is not None else "none",
            "dmd_width":       w,
            "dmd_height":      h,
            "dmd_pattern":     s.pattern_path.name if s.pattern_path else "",
            "dmd_scale_pct":   s.scale_pct,
            "dmd_rotation_deg": s.rotation_deg,
            "dmd_offset_x":    s.offset_x,
            "dmd_offset_y":    s.offset_y,
            "dmd_invert":      s.invert,
            # All-on ignores the pattern and geometry above.
            "dmd_all_on":      s.all_on,
            "dmd_fit":         s.fit,
            "dmd_sub_sampling": s.sub_sampling,
            # 0 = a Display that "worked" and projected nothing.
            "dmd_on_pixels":   c.on_pixels if c is not None else 0,
            "dmd_n_rois":      len(s.rois or ()),
            "dmd_rois":        json.dumps(list(s.rois or ())),
            "dmd_calibration": Path(s.calib_path).name if s.calib_path else "",
        }
