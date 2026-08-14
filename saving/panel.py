"""The Save tab — destination, template and the capacity estimate."""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QWidget,
)

from acqApp.saving.config import (_DEFAULT_SUBDIR, TOKENS, SaveConfig, _gb,
                                  default_folder, free_bytes, list_drives,
                                  sanitize)


class SavePanel(QWidget):
    """Settings tab: destination drive/folder, naming, and capacity readout."""

    settings_changed = pyqtSignal()

    def __init__(self, config: SaveConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or SaveConfig()
        if not self._cfg.folder.strip():
            self._cfg.folder = str(default_folder())
        self._rate_mbps: float = 0.0        # set by the owner from the cam config
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
        lay.addRow("Drive:", self._cmb_drive)

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

    def set_expected_rate(self, mbps: float) -> None:
        """Data rate of the current acquisition config, for the capacity estimate."""
        self._rate_mbps = max(0.0, float(mbps))
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
            secs = free / (self._rate_mbps * (1 << 20))
            txt += (f" — about {secs / 60:.1f} min at "
                    f"{self._rate_mbps:.0f} MB/s")
            warn = warn or secs < 120     # under 2 minutes of headroom
        self._lbl_space.setText(txt)
        self._lbl_space.setStyleSheet(
            "color:#c47f00; font-weight:bold;" if warn else "color:#2e7d32;")
