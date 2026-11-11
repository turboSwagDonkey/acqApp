"""MainWindow: the session-wide shell. Its parts are mixins: services.py
(what adapters call), docks.py (layout), session.py (run, record)."""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from acqApp import adapters, config
from acqApp.saving import SavePanel
from acqApp.acq.sync import DEFAULT_TICK_MS, SyncController
from acqApp.acq.clock import SessionClock
from acqApp.acq.recorder import Recorder
from acqApp.sidebar import Fold
from acqApp.window.services import WindowServices
from acqApp.window.docks import DockLayout
from acqApp.window.session import folder_bytes, SessionControl


def close_camera(handle) -> None:
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


class MainWindow(WindowServices, DockLayout, SessionControl,
                 QMainWindow):
    """Session-wide shell. Implements `acq.devices.ModuleHost`, which
    documents the services adapters use."""

    def __init__(self, cam_info=None, mock=False, enabled: set[str] | None = None,
                 cam_handle=None):
        super().__init__()
        self._emulate = mock
        self._session_on = False
        self._enabled = (enabled if enabled is not None
                         else set(config.MODULES)) | config.ALWAYS_ON
        self._cam_info = cam_info
        self._cam_handle = cam_handle

        self._clock = SessionClock()
        self._sync  = SyncController(self._clock, tick_ms=DEFAULT_TICK_MS)
        self._sync.tick.connect(self._on_tick)
        self._sync.trigger_fired.connect(self._on_trigger)

        self._recorder: Recorder | None = None
        self._rec_path: Path | None = None
        self._rec_t0: float = 0.0     # session-clock time Record was pressed
        # File size is stat()ed at ~1 Hz: on a network share it can block.
        self._rec_size_t0: float = 0.0
        self._rec_size_txt: str = ""
        self._rec_warn: bool | None = None     # repaint only on change
        # Set by the routine before it opens/rolls a file; None = Save tab naming.
        self._routine_save_ctx: tuple[str, int, tuple | None] | None = None
        self._routine_arming_trigger = False
        self._save_panel: SavePanel | None = None
        self._folds: dict[str, Fold] = {}           # each card, its panel under it
        self._docks: dict[str, QDockWidget] = {}    # the own_dock modules'
        self._status_widgets: dict[str, QWidget] = {}   # beside Record

        self._modules = adapters.build_adapters(self, self._enabled)
        self._build_ui()
        # Controllers are configured from their panels, so after the UI.
        self._build_controllers()
        self._apply_title()

        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(33)   # ~30 Hz
        self._disp_timer.timeout.connect(self._display_tick)
        self._display_failed: set[str] = set()     # reported once per session

    # ── Sync callbacks ──────────────────────────────────────────────────────────

    def _on_tick(self, elapsed: float) -> None:
        self._lbl_time.setText(f"t = {elapsed:.1f} s")
        self._refresh_rec_readout(elapsed)
        if self._save_panel is not None:
            self._save_panel.set_active_fov(self.active_fov_name())

    def _refresh_rec_readout(self, elapsed: float) -> None:
        """Elapsed / size on disk / drops. Size is from the file, not what was
        enqueued: they differ exactly when the ring sheds."""
        if self._recorder is None or self._rec_path is None:
            self._lbl_rec.setText("")
            return
        mins, secs = divmod(int(elapsed - self._rec_t0), 60)
        txt = f"● REC  {mins:d}:{secs:02d}"
        now = time.monotonic()
        if now - self._rec_size_t0 >= 1.0:
            self._rec_size_t0 = now
            try:
                mb = folder_bytes(self._rec_path) / (1 << 20)
                self._rec_size_txt = (f"   {mb / 1024:.2f} GB" if mb >= 1024
                                      else f"   {mb:.0f} MB")
            except OSError:
                pass
        txt += self._rec_size_txt
        dropped = self._recorder.drop_count + self._recorder.late_count
        warn = bool(dropped)
        if dropped:
            txt += f"   ⚠ {dropped} samples shed"
        if warn != self._rec_warn:
            self._rec_warn = warn
            self._lbl_rec.setStyleSheet("color:#c62828; font-weight:bold;" if warn
                                        else "color:#2e7d32; font-weight:bold;")
        self._lbl_rec.setText(txt)

    def _on_trigger(self, name: str, duration: float) -> None:
        for m in self._modules:
            m.on_trigger(name, duration)

    def _display_tick(self) -> None:
        for m in self._modules:
            try:
                m.update_display()
            except Exception as e:  # noqa: BLE001 — a timer slot: an escape aborts the app
                if m.key not in self._display_failed:     # once, not at 30 Hz
                    self._display_failed.add(m.key)
                    self.status(f"{m.key}: display failed ({type(e).__name__}: {e})")
                    print(f"[main] {m.key}.update_display() raised: "
                          f"{type(e).__name__}: {e}")

    def _save_save_settings(self, *_args) -> None:
        if self._save_panel is not None:
            config.save_settings("saving", self._save_panel.as_dict())

    # ── Cleanup ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._save_layout()
        try:
            if self._session_on:
                self._stop_session()
        except Exception as e:      # noqa: BLE001 — an escape aborts the process
            print(f"[main] shutdown raised {type(e).__name__}: {e}")
        for m in self._modules:
            self._safe_close(m)
        # Always: a handle left open crashes the next launch.
        close_camera(self._cam_handle)
        self._cam_handle = None
        event.accept()
