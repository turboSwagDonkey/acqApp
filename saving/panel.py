"""The Save tab — destination, template and the capacity estimate."""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
)

from acqApp.saving.config import (_DEFAULT_SUBDIR, TOKENS, SaveConfig, _gb,
                                  benchmark_drive, default_folder, free_bytes,
                                  list_drives)


class SavePanel(QWidget):
    """Settings tab: destination drive/folder, naming, and capacity readout."""

    settings_changed = pyqtSignal()

    # A short burst reads fast on any SSD — its own SLC write cache absorbs
    # it — which is exactly the trap that hid a SATA drive's real ceiling
    # behind the writer/GIL for a whole session (PLAN.md sec 6 item 1). 1 GiB
    # is enough to run past that cache on the drives this rig actually has.
    _SCAN_SIZE_MB = 1024
    # The writer's own overhead over a raw write, measured in
    # docs/CAMERA_TRANSFER.md (direct-chunk 2696 -> 2464 MB/s through the
    # whole path, ~9%) — derate the raw measurement before calling it safe.
    _SCAN_DERATE = 0.85

    def __init__(self, config: SaveConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or SaveConfig()
        if not self._cfg.folder.strip():
            self._cfg.folder = str(default_folder())
        self._rate_mbps: float = 0.0        # set by the owner from the cam config
        self._writer_mbps: float = 0.0      # …and what the writer sustains
        self._build()
        self._refresh()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        grp = QGroupBox("Save configuration")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        # Drive shortcut: picking one rewrites the folder to that drive.
        self._cmb_drive = QComboBox()
        self._reload_drives()
        self._cmb_drive.activated.connect(self._on_drive_picked)
        self._btn_scan = QPushButton("Scan drives")
        self._btn_scan.setToolTip(
            "Write ~1 GiB to each drive to measure its real sustained write "
            "speed, and flag any that would drop frames at the current "
            "acquisition rate.")
        self._btn_scan.clicked.connect(self._on_scan_drives)
        drive_row = QHBoxLayout()
        drive_row.setContentsMargins(0, 0, 0, 0)
        drive_row.addWidget(self._cmb_drive, 1)
        drive_row.addWidget(self._btn_scan)
        drive_row_w = QWidget()
        drive_row_w.setLayout(drive_row)
        lay.addRow("Drive:", drive_row_w)

        self._lbl_scan = QLabel()
        self._lbl_scan.setWordWrap(True)
        self._lbl_scan.setStyleSheet("color:#8a8a8a;")
        lay.addRow("Drive scan:", self._lbl_scan)

        self._ed_folder = QLineEdit(self._cfg.folder)
        self._ed_folder.editingFinished.connect(self._on_edited)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._on_browse)
        btn_open = QPushButton("Open")
        btn_open.setToolTip("Open this folder in Explorer")
        btn_open.clicked.connect(self._on_open)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._ed_folder, 1)
        row.addWidget(btn_browse)
        row.addWidget(btn_open)
        row_w = QWidget()
        row_w.setLayout(row)
        lay.addRow("Folder:", row_w)

        self._ed_subject = QLineEdit(self._cfg.subject)
        self._ed_subject.setPlaceholderText("animal / subject ID")
        self._ed_subject.editingFinished.connect(self._on_edited)
        lay.addRow("Subject:", self._ed_subject)

        self._ed_session = QLineEdit(self._cfg.session)
        self._ed_session.setPlaceholderText("optional run label")
        self._ed_session.editingFinished.connect(self._on_edited)
        lay.addRow("Session:", self._ed_session)

        self._ed_template = QLineEdit(self._cfg.template)
        self._ed_template.setToolTip("Tokens: " + "  ".join(TOKENS))
        self._ed_template.editingFinished.connect(self._on_edited)
        lay.addRow("Filename:", self._ed_template)

        self._chk_subfolder = QCheckBox("Give each recording its own subfolder")
        self._chk_subfolder.setChecked(self._cfg.subfolder)
        self._chk_subfolder.toggled.connect(self._on_edited)
        lay.addRow("", self._chk_subfolder)

        self._lbl_preview = QLabel()
        self._lbl_preview.setWordWrap(True)
        self._lbl_preview.setStyleSheet("color:#8a8a8a;")
        lay.addRow("Next file:", self._lbl_preview)

        # The point of the whole panel: how long can this actually record?
        self._lbl_space = QLabel()
        self._lbl_space.setWordWrap(True)
        lay.addRow("Capacity:", self._lbl_space)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

    def _reload_drives(self) -> None:
        self._cmb_drive.blockSignals(True)
        self._cmb_drive.clear()
        for root, free, total in list_drives():
            self._cmb_drive.addItem(
                f"{root}   {_gb(free)} free of {_gb(total)}", root)
        self._cmb_drive.blockSignals(False)
        self._sync_drive_combo()

    def _sync_drive_combo(self) -> None:
        """Point the combo at whichever drive the current folder lives on."""
        try:
            anchor = os.path.splitdrive(str(Path(self._cfg.folder)))[0].upper()
        except (ValueError, OSError):
            return
        for i in range(self._cmb_drive.count()):
            data = self._cmb_drive.itemData(i) or ""
            if os.path.splitdrive(data)[0].upper() == anchor:
                self._cmb_drive.blockSignals(True)
                self._cmb_drive.setCurrentIndex(i)
                self._cmb_drive.blockSignals(False)
                return

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _on_drive_picked(self, idx: int) -> None:
        root = self._cmb_drive.itemData(idx)
        if root:
            self._ed_folder.setText(str(Path(root) / _DEFAULT_SUBDIR))
            self._on_edited()

    def _on_browse(self) -> None:
        start = self._cfg.folder or str(default_folder())
        chosen = QFileDialog.getExistingDirectory(self, "Session folder", start)
        if chosen:
            self._ed_folder.setText(chosen)
            self._on_edited()

    def _on_open(self) -> None:
        folder = self._cfg.resolved_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))                    # noqa: S606 (Windows)
        except (OSError, AttributeError) as e:
            self._lbl_space.setText(f"Could not open {folder}: {e}")

    def _on_scan_drives(self) -> None:
        """Benchmark every fixed drive and flag any too slow for the current
        acquisition rate. Synchronous (like dialogs.py's device probe loop) —
        each drive is only ~1 GiB, a couple of seconds even on SATA, and
        `processEvents()` between drives keeps the window from looking frozen.
        """
        self._btn_scan.setEnabled(False)
        html = []
        try:
            for root, free, total in list_drives():
                self._lbl_scan.setText(f"scanning {root}…")
                self._lbl_scan.setStyleSheet("color:#8a8a8a;")
                QApplication.processEvents()
                need = (self._SCAN_SIZE_MB << 20) * 4     # leave the drive most of its room
                if free < need:
                    html.append(f"{root}&nbsp;&nbsp;skipped — only {_gb(free)} free "
                                f"(need some room to test meaningfully)")
                    continue
                mbps = benchmark_drive(root, self._SCAN_SIZE_MB << 20)
                if mbps is None:
                    html.append(f"{root}&nbsp;&nbsp;write test failed (permissions?)")
                    continue
                line = f"{root}&nbsp;&nbsp;{mbps:.0f} MB/s"
                if self._rate_mbps > 0:
                    safe = mbps * self._SCAN_DERATE
                    if safe < self._rate_mbps:
                        pct = 100 * (1 - safe / self._rate_mbps)
                        line = (f'<span style="color:#c62828; font-weight:bold;">'
                                f'{line} ⚠ would drop ~{pct:.0f}% of frames '
                                f'at {self._rate_mbps:.0f} MB/s</span>')
                    else:
                        line += ' <span style="color:#2e7d32;">— OK at the current rate</span>'
                html.append(line)
            if self._rate_mbps <= 0:
                html.append("(no acquisition rate known yet — showing raw "
                            "write speed only)")
            self._lbl_scan.setStyleSheet("")
            self._lbl_scan.setText("<br>".join(html))
        finally:
            self._btn_scan.setEnabled(True)

    def _on_edited(self, *_a) -> None:
        self._cfg.folder    = self._ed_folder.text().strip()
        self._cfg.subject   = self._ed_subject.text().strip()
        self._cfg.session   = self._ed_session.text().strip()
        self._cfg.template  = self._ed_template.text().strip() or "{subject}_{date}_{time}"
        self._cfg.subfolder = self._chk_subfolder.isChecked()
        self._sync_drive_combo()
        self._refresh()
        self.settings_changed.emit()

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def settings(self) -> SaveConfig:
        return self._cfg

    def as_dict(self) -> dict:
        return asdict(self._cfg)

    def resolve(self, when: datetime | None = None, *,
                unique: bool = False) -> Path:
        return self._cfg.resolve(when, unique=unique)

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        """Data rate of the current acquisition config, for the capacity estimate.

        `writer_mbps` is what the write path can actually sustain. Passed in
        rather than imported: it is a camera-side measurement, and `saving/`
        does not depend on `devices/` (see docs/STRUCTURE.md). 0 means unknown,
        and the estimate then assumes everything offered is written.
        """
        self._rate_mbps = max(0.0, float(mbps))
        self._writer_mbps = max(0.0, float(writer_mbps))
        self._refresh()

    def writable_error(self) -> str | None:
        """Human-readable reason the target is unusable, or None if it is fine."""
        folder = self._cfg.resolved_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"cannot create {folder}: {e}"
        if not os.access(str(folder), os.W_OK):
            return f"{folder} is not writable"
        return None

    # ── Readouts ─────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        # Preview the path a recording started now would actually get, so a
        # template that collides shows its `_001` here rather than surprising
        # the operator in the status line after the fact.
        plain = self.resolve()
        unique = self.resolve(unique=True)
        self._lbl_preview.setText(str(unique))
        self._lbl_preview.setToolTip(
            f"{plain.name} exists — the next recording is auto-numbered."
            if unique != plain else "")

        free = free_bytes(self._cfg.folder or str(default_folder()))
        if free is None:
            self._lbl_space.setText("Target folder does not exist yet.")
            self._lbl_space.setStyleSheet("color:#c47f00;")
            return

        txt = f"{_gb(free)} free"
        warn = free < (10 << 30)          # under 10 GB is not a usable target
        if self._rate_mbps > 0:
            # The disk fills at what is WRITTEN, not what the camera offers, and
            # those differ: full frame at bin 1 acquires ~2200 MB/s against a
            # writer that sustains ~1000. Estimating from the offered rate both
            # halved the time and — worse — showed a configuration that sheds
            # half its frames in the same green as a healthy one.
            cap = self._writer_mbps or self._rate_mbps
            written = min(self._rate_mbps, cap)
            secs = free / (written * (1 << 20))
            txt += f" — about {secs / 60:.1f} min at {written:.0f} MB/s"
            if self._rate_mbps > cap:
                txt += (f"; the camera offers {self._rate_mbps:.0f} MB/s, so "
                        f"~{100 * (1 - written / self._rate_mbps):.0f}% of "
                        f"frames cannot be written — see the Voltage cam tab")
                warn = True
            warn = warn or secs < 120     # under 2 minutes of headroom
        self._lbl_space.setText(txt)
        self._lbl_space.setStyleSheet(
            "color:#c47f00; font-weight:bold;" if warn else "color:#2e7d32;")
