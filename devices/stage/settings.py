"""XY stage — the calibration model and its file format.

SHARED with the standalone `stage_control` app: a coordinate frame can only be
created in one place, so both read *and write* this file rather than drift.

Two halves, expiring differently:
  * frame-INDEPENDENT — `counts_per_um`, `span_counts`. Stable forever.
  * frame-SPECIFIC    — `slope`, `offset`, `true_center`, `travel_*`, `soft_*`.
    Valid only until the next HARD LIMIT hit, which re-references the
    controller's command origin. `establish_frame` remakes them.

No Qt (widgets are in `panel.py`), so this is testable without a QApplication.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

# The standalone app is the single source of truth; acqApp's own copy is the
# fallback for when that sibling folder isn't present.
_LOCAL_CONFIG  = Path(__file__).with_name("stage_config.json")
_SHARED_CONFIG = Path(__file__).resolve().parents[3] / "stage_control" / "config.json"

FULL_TRAVEL_UM = 25400.0        # 1 inch of travel per axis

# Legend swatches — kept in step with map_widget's pens.
_C_CUR, _C_ORIGIN, _C_HOME, _C_SOFT = "#1f77b4", "#2ca02c", "#ff7f0e", "#b58900"
_BAD = "#c0392b"


def config_path() -> Path:
    """The calibration file in use — the shared stage_control one if it exists."""
    return _SHARED_CONFIG if _SHARED_CONFIG.is_file() else _LOCAL_CONFIG


@dataclass
class StageAxis:
    index:          int
    name:           str
    counts_per_um:  float
    invert:         bool = False
    ref_counts:     float = 0.0        # encoder counts at 0 µm (true center)
    origin_set:     bool = False       # was true_center actually calibrated?
    slope:          float | None = None  # command→encoder map (for abs moves)
    offset:         float | None = None
    soft_min:       int | None = None    # soft limits, encoder counts
    soft_max:       int | None = None
    travel_min:     int | None = None    # hard travel ends, encoder counts
    travel_max:     int | None = None
    span_counts:    int | None = None    # full travel, frame-independent
    step_um:        float = 50.0
    # A bookmark, NOT calibration: never loaded from or written to the config,
    # so a convenience marker can't be mistaken for the true zero next session.
    home_counts:    int | None = None

    @property
    def sign(self) -> int:
        return -1 if self.invert else 1

    @property
    def has_frame(self) -> bool:
        """True when absolute go-to can be trusted (command→encoder map known)."""
        return self.slope is not None and self.offset is not None and self.origin_set

    def default_span(self) -> int:
        """Full travel in counts — the configured value, else 1 inch worth."""
        if self.span_counts:
            return int(self.span_counts)
        return int(round(FULL_TRAVEL_UM * self.counts_per_um))

    # ── frame edits (return the JSON keys to persist) ───────────────────────
    def center_updates(self, counts: int, margin_um: float = 50.0) -> dict:
        """Config keys making `counts` the origin, travel/soft limits at
        ±half-travel around it (the ±0.5" no-wrap zone)."""
        half_um = FULL_TRAVEL_UM / 2.0
        half = int(round(half_um * self.counts_per_um))
        soft = int(round((half_um - margin_um) * self.counts_per_um))
        c = int(counts)
        return {"true_center": c,
                "travel_min": c - half, "travel_max": c + half,
                "soft_min":   c - soft, "soft_max":   c + soft}

    def apply_updates(self, upd: dict) -> None:
        """Fold persisted frame keys back into this live axis."""
        if "true_center" in upd:
            # Explicit None = "no longer meaningful", so a stale origin can't
            # survive a re-frame.
            if upd["true_center"] is None:
                self.ref_counts, self.origin_set = 0.0, False
            else:
                self.ref_counts = float(upd["true_center"])
                self.origin_set = True
        if "slope" in upd:
            self.slope = upd["slope"]
        if "offset" in upd:
            self.offset = upd["offset"]
        for k in ("soft_min", "soft_max", "travel_min", "travel_max"):
            if k in upd:
                setattr(self, k, upd[k])

    def to_um(self, counts: float) -> float:
        if not self.counts_per_um:
            return 0.0
        return self.sign * (counts - self.ref_counts) / self.counts_per_um

    def um_to_counts(self, um: float) -> int:
        return int(round(self.ref_counts + self.sign * um * self.counts_per_um))

    def clamp_counts(self, counts: int) -> int:
        if self.soft_min is not None:
            counts = max(counts, self.soft_min)
        if self.soft_max is not None:
            counts = min(counts, self.soft_max)
        return counts

    def soft_limits_um(self) -> tuple[float, float]:
        """Soft limits expressed in µm (sorted), or a safe default span."""
        if self.soft_min is not None and self.soft_max is not None:
            a, b = self.to_um(self.soft_min), self.to_um(self.soft_max)
            return (min(a, b), max(a, b))
        return (-6350.0, 6350.0)   # ±¼ inch fallback

    def travel_limits_um(self) -> tuple[float, float]:
        """Hard travel ends in µm (sorted); falls back to the soft limits."""
        if self.travel_min is not None and self.travel_max is not None:
            a, b = self.to_um(self.travel_min), self.to_um(self.travel_max)
            return (min(a, b), max(a, b))
        return self.soft_limits_um()

    def home_um(self) -> float | None:
        """The session home in µm, or None if none has been set."""
        return None if self.home_counts is None else self.to_um(self.home_counts)


@dataclass
class StageSettings:
    port:    str = "COM54"
    poll_hz: float = 4.0
    confirm_move_um: float = 3000.0    # ask before moves larger than this
    margin_um: float = 50.0            # soft-limit inset from the travel ends
    invert_y: bool = True              # draw the map with +Y screen-up
    x: StageAxis = None      # type: ignore[assignment]
    y: StageAxis = None      # type: ignore[assignment]

    def __post_init__(self):
        if self.x is None:
            self.x = StageAxis(0, "X", 61.9864)
        if self.y is None:
            self.y = StageAxis(1, "Y", 61.8735, invert=True)

    @property
    def has_frame(self) -> bool:
        return self.x.has_frame and self.y.has_frame


def load_settings() -> StageSettings:
    """Build StageSettings from the shared calibration, falling back to defaults."""
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return StageSettings()

    axes = {a["index"]: a for a in cfg.get("axes", [])}
    pad = cfg.get("xy_pad", {})
    xi = pad.get("x_axis", 0)
    yi = pad.get("y_axis", 1)

    def _axis(i: int, default_name: str) -> StageAxis:
        a = axes.get(i, {})
        return StageAxis(
            index         = i,
            name          = a.get("name", default_name),
            counts_per_um = float(a.get("counts_per_um", 1.0)) or 1.0,
            invert        = bool(a.get("invert", False)),
            ref_counts    = float(a.get("true_center") or 0.0),
            origin_set    = a.get("true_center") is not None,
            slope         = a.get("slope"),
            offset        = a.get("offset"),
            soft_min      = a.get("soft_min"),
            soft_max      = a.get("soft_max"),
            travel_min    = a.get("travel_min"),
            travel_max    = a.get("travel_max"),
            span_counts   = a.get("span_counts"),
            step_um       = float(a.get("step_um", 50.0)),
        )

    x = _axis(xi, "X")
    confirm_counts = cfg.get("max_unconfirmed_move_counts", 200000)
    return StageSettings(
        port            = cfg.get("port", "COM54"),
        poll_hz         = 1000.0 / cfg.get("poll_interval_ms", 250),
        confirm_move_um = confirm_counts / (x.counts_per_um or 1.0),
        margin_um       = float(cfg.get("margin_um", 50)),
        invert_y        = bool(pad.get("invert_y", True)),
        x               = x,
        y               = _axis(yi, "Y"),
    )


def save_axis_updates(updates: dict[int, dict]) -> Path:
    """Persist per-axis calibration keys ({axis_index: {key: value}}) into the
    shared config, leaving every other key untouched. Temp file + replace so a
    crash mid-write can't destroy it; the old contents stay as `<name>.bak`.

    A missing or unreadable config must NOT raise: callers apply the updates to
    the live axes first, and `establish_frame()` has already spent minutes
    driving both hard limits — raising here would leave memory and disk
    disagreeing about where 0,0 is.
    """
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raw = ""                                # first run, or sibling app absent
    if raw:
        try:
            path.with_suffix(path.suffix + ".bak").write_text(raw, encoding="utf-8")
        except OSError:
            pass                                # a read-only dir must not block the save
    try:
        cfg = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        cfg = {}                                # corrupt; recoverable from the .bak
    if not isinstance(cfg, dict):
        cfg = {}
    by_index = {a.get("index"): a for a in cfg.get("axes", [])}
    for idx, upd in updates.items():
        entry = by_index.get(idx)
        if entry is None:                       # axis missing from the file
            entry = {"index": idx}
            cfg.setdefault("axes", []).append(entry)
        entry.update(upd)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
