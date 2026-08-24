"""
The DMD projector's adapter: an output that also answers to the trigger bus.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import ProjectorController
from acqApp.devices.dmd import alp
from acqApp.devices.dmd.control import DmdController, DmdSettings, MockDmdController
from acqApp.devices.dmd.panel import SettingsPanel as DmdPanel
from acqApp.adapters.base import ModuleAdapter


class DmdModule(ModuleAdapter):
    key = "dmd"
    tab_label = "DMD"

    controller: ProjectorController | None   # narrows ModuleAdapter.controller

    def __init__(self, win) -> None:
        super().__init__(win)
        # Whether the ALP really opened. Not re-derived from the class, because
        # a real DmdController that failed and fell back is the case that
        # matters, and only build_controller knows.
        self._real = False

    def build_panel(self) -> QWidget:
        self.panel = DmdPanel(self._settings())
        # Route through this adapter, not straight to the controller: the
        # controller is rebuilt whenever Emulate is toggled, and the panel binds
        # its signals only once.
        self.panel.load_requested.connect(self.load)
        self.panel.display_requested.connect(self.display)
        self.panel.stop_requested.connect(self.stop_display)
        self.panel.settings_changed.connect(self._save)
        self.panel.rois_edit_requested.connect(self.edit_rois)
        self.panel.calibrate_requested.connect(self.calibrate)
        return self.panel

    # ── the camera↔DMD registration ──
    def calibrate(self) -> None:
        """Open the sweep dialog: project patterns, image them, fit a transform.

        The two hardware operations `run_calibration` wants are exactly the two
        this adapter can reach — `project_frame` on its own controller, and the
        **voltage** camera's newest frame through the host. Nothing narrower
        would do: the DMD images through that camera, so the registration is in
        ORCA pixels.

        This is the app's only actuating path that is not a Display. The dialog
        states what it will emit and does nothing until a button is pressed
        (§2).

        It needs no setup from the operator: `set_live` starts the camera and
        puts it back, and `project_frame` drives the DMD directly, so neither
        Live view nor Display has to be pressed first. Requiring them was
        friction with no safety value — the actuation decision is the dialog's
        button, not whether a preview happened to be running.
        """
        from PyQt6.QtWidgets import QMessageBox

        from acqApp.devices.dmd.sweep import CalibrationDialog

        if self.controller is None:
            return
        if "voltage_cam" not in self.win.module_keys():
            QMessageBox.information(
                self.panel, "No voltage camera",
                "The sweep images each pattern with the voltage camera, and "
                "that module is not loaded.\n\n"
                "Restart and tick Voltage camera in the startup picker.")
            return
        dlg = CalibrationDialog(
            self.controller, lambda: self.win.latest_frame("voltage_cam"),
            parent=self.panel, real=self._real,
            on_saved=self._adopt_calibration,
            set_live=self.win.set_live)
        dlg.exec()

    def _adopt_calibration(self, path: str) -> None:
        """Point the panel at the calibration the sweep just wrote.

        Measuring one and then leaving the ROI editor on the old one is the
        failure this exists to prevent — the editor would draw a field outline
        that no longer describes the projector.
        """
        self.panel.set_calib_path(path)
        self.win.status(f"DMD calibration saved and loaded: {Path(path).name}")


    # ── photostimulation ROIs ──
    def edit_rois(self) -> None:
        """Open `RoiEditor` on the voltage camera's newest frame.

        Here rather than in the panel because only the adapter can reach
        another module (`ModuleHost.latest_frame`), and the DMD images through
        the **voltage** camera — that is the optical path it projects into.

        Nothing in this path commands a camera or the projector: it draws on
        the frame that already exists. Putting the DMD all-on first is the
        operator's step, and it is the one that emits light.
        """
        from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QMessageBox,
                                     QVBoxLayout)

        from acqApp.devices.dmd.roi import RoiSet
        from acqApp.devices.dmd.roi_panel import RoiEditor

        frame = self.win.latest_frame("voltage_cam")
        if frame is None:
            QMessageBox.information(
                self.panel, "No camera frame",
                "The ROI editor draws on a voltage-camera frame, and none has "
                "arrived yet.\n\nLoad the voltage camera and press Free run "
                "(or Record), then try again.\n\nTo see the projected field in "
                "the snapshot, put the DMD in all-on and press Display first.")
            return

        calib, why = self._calibration()
        dlg = QDialog(self.panel)
        dlg.setWindowTitle("Photostimulation ROIs")
        dlg.resize(1000, 760)
        # Same accent the settings tab wears (`dialogs.add_panel`), so the DMD's
        # own windows are not the only untinted surfaces in the app.
        from acqApp import style
        dlg.setStyleSheet(style.accent_panel("dmd"))
        lay = QVBoxLayout(dlg)
        ed = RoiEditor(calib)
        ed.set_image(frame)
        if self.panel.rois:
            ed.load(RoiSet.from_list(list(self.panel.rois)))
        lay.addWidget(ed, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if why:
            self.win.status(why)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            rois = ed.roi_set.to_list()
            self.panel.set_rois(tuple(rois))
            self.win.status(f"{len(rois)} photostimulation ROI(s) saved")

    def _calibration(self):
        """The saved camera↔DMD registration -> (calib | None, complaint).

        A missing or unreadable one is not fatal: ROIs can still be drawn and
        saved, and the editor says so itself. What must not happen is a
        *silently* absent calibration, because the field outline is then simply
        not drawn and everything looks fine.
        """
        path = self.panel.calib_path if self.panel is not None else ""
        if not path:
            return None, ""
        try:
            from acqApp.devices.dmd.calibration import DmdCalibration
            return DmdCalibration.load(path), ""
        except Exception as e:      # noqa: BLE001 — missing, corrupt, or stale
            return None, (f"DMD calibration {Path(path).name} could not be "
                          f"read ({type(e).__name__}) — ROIs can be drawn but "
                          f"not projected")

    def _settings(self) -> DmdSettings:
        """Saved settings, defaulting to the standalone DMD app's alignment.

        Scale and rotation register the pattern to the optics, and that
        alignment lives in `dmdGUI_project`. A fresh install at 100 % / 0° would
        project in the wrong place while looking correctly configured — the same
        arrangement the stage has with `stage_control/config.json`.
        """
        s = config.load_dataclass(DmdSettings, self.key)
        saved = config.load_settings(self.key)
        shared = alp.sibling_config()
        if "scale_pct" not in saved and "defaultScale" in shared:
            s.scale_pct = float(shared["defaultScale"])
        if "rotation_deg" not in saved and "defaultRot" in shared:
            s.rotation_deg = float(shared["defaultRot"])
        return s

    def _save(self, s) -> None:
        d = asdict(s)
        d["pattern_path"] = str(s.pattern_path) if s.pattern_path else None
        d["rois"] = list(s.rois or ())      # JSON has no tuples
        config.save_settings(self.key, d)

    def build_controller(self, emulate: bool) -> None:
        s = self.panel.settings if self.panel is not None else DmdSettings()
        real = False
        if emulate:
            self.controller = MockDmdController(s)
        else:
            try:
                self.controller = DmdController(s)
                real = True
            except Exception as e:      # noqa: BLE001 — any ALP/driver failure
                # Most often the ALP is still held by the standalone
                # dmdGUI_project app: only one process can own it.
                print(f"[main] DMD unavailable ({type(e).__name__}: {e}) — "
                      f"using mock. If the standalone DMD app is open, close "
                      f"it and toggle Emulate off again.")
                self.controller = MockDmdController(s)
        self._real = real
        if self.panel is not None:
            self.panel.set_device(self.controller.device_name,
                                  self.controller.resolution, real)
        # A pattern the panel is showing must be uploaded to this controller
        # too, or Display projects the fresh controller's default while the
        # panel names a file.
        path = s.pattern_path
        if path is not None and Path(path).is_file():
            self.controller.load_pattern(Path(path))

    def load(self, path) -> None:
        if self.controller is not None:
            self.controller.load_pattern(path)

    def display(self) -> None:
        """Apply the panel's current settings, then start displaying."""
        if self.controller is not None:
            self.controller.apply_settings(self.panel.settings)
            self.controller.display()

    def stop_display(self) -> None:
        if self.controller is not None:
            self.controller.stop()

    def on_trigger(self, name: str, duration: float) -> None:
        """Project on a trigger — the closed loop's other output.

        The DMD has no pulse of its own: the ALP free-runs its sequence once
        started and holds until Stop. So a *timed* stimulus is display-now plus
        a single-shot stop, and `duration <= 0` leaves the pattern up.
        """
        if name != self.key:
            return
        self.display()
        if duration > 0:
            QTimer.singleShot(int(duration * 1000), self.stop_display)

    def attach_sink(self, rec) -> None:
        if self.controller is not None:
            # DMD frames are logged straight from its thread (no GUI-thread hop),
            # stamped on the shared clock like every other stream.
            self.controller.set_sink(lambda idx: rec.put("dmd", float(idx)))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        c = self.controller
        # Both twins declare device_name/resolution/on_pixels
        # (`ProjectorController`), so these are read, not guessed: the old
        # `getattr(c, "device_name", "none")` filed a session that really
        # projected as one that never did (§5b A1).
        w, h = c.resolution if c is not None else (0, 0)
        return {
            "dmd_on_time_ms":  s.on_time_ms,
            "dmd_static_hold": s.static_hold,
            "dmd_trigger":     s.trigger_mode,
            "dmd_repeats":     s.n_repeats,
            # What was projected, and where: without the geometry a recorded
            # stimulus cannot be located in the FOV afterwards, and without the
            # name a real session is indistinguishable from a mock one.
            "dmd_device":      c.device_name if c is not None else "none",
            "dmd_width":       w,
            "dmd_height":      h,
            "dmd_pattern":     s.pattern_path.name if s.pattern_path else "",
            "dmd_scale_pct":   s.scale_pct,
            "dmd_rotation_deg": s.rotation_deg,
            "dmd_offset_x":    s.offset_x,
            "dmd_offset_y":    s.offset_y,
            "dmd_invert":      s.invert,
            # An all-on frame ignores the pattern and the geometry above, so
            # without this the recorded scale/rotation would describe a
            # placement that was never used.
            "dmd_all_on":      s.all_on,
            "dmd_fit":         s.fit,
            # 0 mirrors on is a dark panel — a Display that "worked" and
            # projected nothing looks identical in every other field here.
            "dmd_on_pixels":   c.on_pixels if c is not None else 0,
            # Photostimulation targets. Recorded as a count plus the JSON,
            # because "where was the light aimed" cannot be recovered later
            # from anything else in the file.
            "dmd_n_rois":      len(s.rois or ()),
            "dmd_rois":        json.dumps(list(s.rois or ())),
            "dmd_calibration": Path(s.calib_path).name if s.calib_path else "",
        }
