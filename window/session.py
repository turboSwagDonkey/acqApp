"""Session start/stop, loading instruments in place, and recording."""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QSignalBlocker
from PyQt6.QtWidgets import QDialog

from acqApp import adapters, config
from acqApp.dialogs import ModuleSelectDialog
from acqApp.saving import write_routine_fov_sidecar
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import SessionWriter
from acqApp.window.common import MODULES_TIP, module_rank


RING_FRAMES  = 512          # ring item cap (scalar streams)
# Payload cap. 512 MB shed 14-54 frames per 30 s run at 106 Hz on a writer
# stall; 2 GB shed none; 4 GB buys nothing more.
RING_BYTES   = 2048 << 20


def folder_bytes(folder: Path) -> int:
    """Bytes in the session folder's files: stat() on the folder itself is its
    directory entry (0 on NTFS), not what it holds."""
    with os.scandir(folder) as it:
        return sum(e.stat().st_size for e in it if e.is_file())


def _sample_nbytes(item) -> int:
    """Payload bytes of a ring item (stream, ts, data); 0 for scalars."""
    return getattr(item[2], "nbytes", 0)


class SessionControl:
    """MainWindow's run, module and recording control."""

    # ── Device connection monitor ────────────────────────────────────────────────

    def _probe_kwargs(self) -> dict:
        return {k: v for m in self._modules for k, v in m.probe_kwargs().items()}

    # ── Session start / stop ──────────────────────────────────────────────────

    def _on_run_toggled(self, on: bool) -> None:
        if on:
            self._start_session()
        else:
            self._stop_session()

    def _start_session(self) -> None:
        # A plain Live/Record press resets the camera to Internal, undoing a
        # previous routine's External edge. Set on the panel directly:
        # set_camera_trigger would re-enter here through set_live.
        if not self._routine_arming_trigger:
            self._call("voltage_cam", "set_trigger_mode_manual")

        # Build first, start after: the clock must be at t=0 before any sample.
        where = "clock"
        try:
            for m in self._modules:
                where = m.key
                m.build_session(self._emulate)
            where = "clock"
            self._sync.start_all()
            for m in self._modules:
                where = m.key
                m.start()
        except Exception as e:      # noqa: BLE001 — a slot: an escape aborts the app
            self._abort_start(where, e)
            return

        self._display_failed.clear()
        self._session_on = True
        self._disp_timer.start()
        self._btn_run.setText("Stop")
        self._btn_emulate.setEnabled(False)

    def _abort_start(self, where: str, e: Exception) -> None:
        """Undo a half-started session: every module stopped, the clock off,
        the button back up (signals blocked, so the error stays on screen)."""
        print(f"[main] Live view failed at {where}: {type(e).__name__}: {e}")
        for m in self._modules:
            self._safe_stop(m)
        self._sync.stop_all()
        with QSignalBlocker(self._btn_run):
            self._btn_run.setChecked(False)
        self.status(f"Live view failed — {where}: {type(e).__name__}: {e}")

    def _guarded(self, m, method: str) -> None:
        """Guarded: one raise would strand every later module running or open
        (and, via closeEvent, skip the DCAM close)."""
        try:
            getattr(m, method)()
        except Exception as e:
            self.status(f"{m.key}: {method} failed ({type(e).__name__}: {e})")
            print(f"[main] {m.key}.{method}() raised: {type(e).__name__}: {e}")

    def _safe_stop(self, m) -> None:
        self._guarded(m, "stop")

    def _safe_close(self, m) -> None:
        self._guarded(m, "close_controller")

    def _stop_session(self) -> None:
        if self._btn_rec.isChecked():
            self._btn_rec.setChecked(False)

        self._disp_timer.stop()
        for m in self._modules:
            self._safe_stop(m)
        self._sync.stop_all()

        self._session_on = False
        self._btn_run.setText("Live view")
        self._btn_emulate.setEnabled(True)
        self._lbl_time.setText("")
        self.status("Stopped")

    def _on_emulate_toggled(self, on: bool) -> None:
        self._emulate = on
        self._build_controllers()
        self._apply_title()
        self.status("Emulate ON — simulated signals" if on
                    else "Emulate OFF — real hardware")

    # ── Loading and unloading instruments in place ───────────────────────────

    def set_modules(self, keys) -> tuple[list[str], list[str]]:
        """Refused while recording (the file names its modules once). A module
        loaded mid-session starts against the running clock, which is safe:
        workers stamp in perf_counter and only the Recorder converts."""
        if self._recorder is not None:
            raise RuntimeError("stop the recording first")
        for m in self._modules:
            why = m.busy_reason()
            if why:
                raise RuntimeError(why)

        keys = set(keys) | config.ALWAYS_ON
        want = [k for k in config.MODULES if k in keys and k in adapters.ADAPTERS]
        have = [m.key for m in self._modules]
        removed = [k for k in have if k not in want]
        added = [k for k in want if k not in have]
        if not removed and not added:
            return [], []

        for key in removed:
            self._unload_module(key)
        for key in added:
            self._load_module(key)

        self._modules.sort(key=lambda m: module_rank(m.key))

        self._enabled = {m.key for m in self._modules}
        config.save_enabled_modules(list(self._enabled))
        for m in self._modules:
            m.on_modules_changed()
        return added, removed

    def _load_module(self, key: str) -> None:
        m = adapters.ADAPTERS[key](self)
        self._modules.append(m)             # the caller sorts

        m.build_panel()
        self._place_module(m)
        self._dock_beside(key)
        m.build_controller(self._emulate)
        self._add_status_widget(m)
        self._size_status_boxes()

        if self._session_on:
            try:
                m.build_session(self._emulate)
                m.start()
            except Exception as e:  # noqa: BLE001 — loaded, but not running
                self._safe_stop(m)
                self.status(f"{key}: start failed ({type(e).__name__}: {e})")
                print(f"[main] {key} start raised: {type(e).__name__}: {e}")

    def _unload_module(self, key: str) -> None:
        """Stop one adapter and take back everything it put on the window."""
        m = self._module(key)
        if m is None:
            return
        self._safe_stop(m)
        self._safe_close(m)

        # setParent(None) before deleteLater: removal only leaves the layout,
        # and a still-parented dock would come back on restoreState().
        # The panel and the card's view go with their fold.
        fold = self._folds.pop(key, None)
        dock = self._docks.pop(key, None)
        if dock is not None:
            self.removeDockWidget(dock)
            dock.setParent(None)
            dock.deleteLater()
        elif fold is not None:
            self._sections.removeWidget(fold)
            fold.setParent(None)
            fold.deleteLater()
        self._remove_status_widget(key)

        self._modules.remove(m)

    def _modules_dialog(self) -> ModuleSelectDialog:
        """The module picker, each row also reporting its hardware."""
        running = " — applied to the running session" if self._session_on else ""
        return ModuleSelectDialog(
            sorted(self._enabled), parent=self, title="Modules and devices",
            prompt=f"Instruments to load{running}:",
            probe_kwargs=self._probe_kwargs)

    def _open_modules_dialog(self) -> None:
        if self._recorder is not None:
            self.status("Stop the recording before changing which modules "
                        "are loaded")
            return
        dlg = self._modules_dialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            added, removed = self.set_modules(dlg.selected())
        except RuntimeError as e:
            self.status(f"Cannot change modules — {e}")
            return
        if not added and not removed:
            self.status("Modules unchanged")
            return
        bits = []
        if added:
            bits.append("loaded " + ", ".join(added))
        if removed:
            bits.append("unloaded " + ", ".join(removed))
        self.status("; ".join(bits)
                    + (" — running" if self._session_on else ""))

    def _build_controllers(self) -> None:
        """(Re)create the output controllers for the current emulate mode."""
        for m in self._modules:
            self._safe_close(m)
        for m in self._modules:
            m.build_controller(self._emulate)

    def _apply_title(self) -> None:
        title = "Acquisition suite"
        if self._cam_info is not None:
            title += f"  —  {getattr(self._cam_info, 'serial_number', self._cam_info)}"
        if self._emulate:
            title += "  [emulated]"
        self.setWindowTitle(title)

    # ── Recording ──────────────────────────────────────────────────────────────

    def _on_record_toggled(self, on: bool) -> None:
        if on:
            if not self._session_on:
                self._btn_run.setChecked(True)
            if not self._session_on:              # start failed
                self._btn_rec.setChecked(False)
                return
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self._save_panel is None:
            return
        err = self._save_panel.writable_error()
        if err is not None:
            self.status(f"Cannot record — {err}")
            self._btn_rec.setChecked(False)
            return

        now = datetime.now()
        sp = self._save_panel
        sc = sp.settings
        ctx = self._routine_save_ctx
        # unique=True: take the next free name rather than refuse. A routine's
        # (FOV, trial) uses its folder scheme, not mouse_date_time.
        if ctx is not None:
            path = sp.resolve_routine_dir(ctx[0], ctx[1], now, unique=True)
        else:
            path = sp.resolve_dir(now, unique=True)
        metadata = {
            "created":  now.strftime("%Y%m%d_%H%M%S"),
            "emulated": self._emulate,
            "modules":  ",".join(sorted(self._enabled)),
            "mouse_id": sc.mouse_id,
        }
        for m in self._modules:
            metadata.update(m.metadata())

        # A folder per recording; a module may ask for its own image format
        # (the pupil camera's .avi), else TIFF.
        writer = SessionWriter({m.key: m.image_format for m in self._modules
                                if m.image_format})
        rec = Recorder(
            self._clock, writer,
            RingBuffer(RING_FRAMES, maxbytes=RING_BYTES, sizeof=_sample_nbytes))
        try:
            rec.start(path, metadata)
        except OSError as e:
            self.status(f"Cannot record → {path}: {e}")
            self._btn_rec.setChecked(False)
            return
        if ctx is not None and ctx[2] is not None:
            # Raw coordinates: "FOVcustom" in the name alone loses them.
            write_routine_fov_sidecar(path, *ctx[2])
        self._recorder = rec
        self._rec_path = path
        sp.set_recording_active(True)
        self._rec_t0 = self._sync.elapsed()
        self._rec_size_t0 = 0.0
        self._rec_size_txt = ""
        self._rec_warn = None

        for m in self._modules:
            m.attach_sink(self._recorder)

        self._btn_rec.setText("■ Stop rec")
        self._modules_action.setEnabled(False)
        self._modules_action.setToolTip(
            "Not while recording — the file names its modules once, at the start")
        self.status(f"Recording → {path}")

    def _stop_recording(self) -> None:
        self._modules_action.setEnabled(True)
        self._modules_action.setToolTip(MODULES_TIP)
        for m in self._modules:
            m.detach_sink()
        rec = self._recorder
        self._recorder = None
        if self._save_panel is not None:
            self._save_panel.set_recording_active(False)
        if rec is not None:
            # A callback: counts are final only between drain and close.
            def final() -> dict[str, Any]:
                d: dict[str, Any] = {
                    "recorder_dropped_samples":   rec.drop_count,
                    "recorder_late_samples":      rec.late_count,
                    "recorder_unstamped_samples": rec.unstamped_count,
                }
                for m in self._modules:
                    d.update(m.final_metadata())
                return d

            remaining = rec.stop(final_metadata=final)
            lost = rec.drop_count + rec.late_count + remaining
            msg = f"Recording stopped (dropped {rec.drop_count} samples while running"
            for n, what in ((rec.late_count, "late"), (remaining, "un-drained")):
                if n:
                    msg += f", {n} {what}"
            msg += ")"
            if lost:
                msg += "  — see the file's recorder_* attributes"
            self.status(msg)
        self._btn_rec.setText("● Record")
        self._lbl_rec.setText("")

    def roll_recording(self) -> bool:
        """Close and immediately reopen, for a routine splitting files. No
        event-loop turn between, so the button never shows "stopped"."""
        self._stop_recording()
        self._start_recording()
        return self._recorder is not None

    def set_routine_save_context(self, fov: str | None, trial: int | None,
                                 coords: tuple[float | None, float | None,
                                              float | None] | None = None
                                 ) -> None:
        self._routine_save_ctx = (None if fov is None or trial is None
                                  else (fov, trial, coords))

    def routine_arming_trigger(self, on: bool) -> None:
        self._routine_arming_trigger = bool(on)

    def recording_path(self) -> Path | None:
        return self._rec_path if self._recorder is not None else None

    def routine_folder(self) -> Path:
        return self._save_panel.settings.routine_base(datetime.now())
