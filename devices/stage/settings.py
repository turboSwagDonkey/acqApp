"""XY stage — the calibration model and its file format. No Qt.

The file is SHARED with the standalone `stage_control` app; both read and
write it. Two halves:
  * frame-independent — `counts_per_um`, `span_counts`.
  * frame-specific — `slope`, `offset`, `true_center`, `travel_*`, `soft_*`.
    An MCM6101 hard-limit hit re-references the command origin and
    invalidates these; `establish_frame` remakes them.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

# The standalone app's file wins; ours is the fallback.
_LOCAL_CONFIG  = Path(__file__).with_name("stage_config.json")
_SHARED_CONFIG = Path(__file__).resolve().parents[3] / "stage_control" / "config.json"

FULL_TRAVEL_UM = 25400.0        # 1 inch per axis
# Home (0,0) is the imaging window's centre. While "Limit to window" is on,
# acqApp keeps X/Y within ±WINDOW_RANGE_UM of it (never written to the shared
# file) and the map shows that range, with the window drawn dashed.
WINDOW_RANGE_UM = 350.0
WINDOW_DIAMETER_UM = 300.0

# Legend colours, shared with map_widget.py.
_C_CUR, _C_ORIGIN, _C_SOFT = "#1f77b4", "#2ca02c", "#b58900"
_C_FOV = "#9467bd"
_BAD = "#c0392b"


def config_path() -> Path:
    return _SHARED_CONFIG if _SHARED_CONFIG.is_file() else _LOCAL_CONFIG


@dataclass
class StageAxis:
    index:          int
    name:           str
    counts_per_um:  float
    invert:         bool = False
    ref_counts:     float = 0.0        # encoder counts at 0 µm
    origin_set:     bool = False
    slope:          float | None = None  # command -> encoder, for absolute moves
    offset:         float | None = None
    soft_min:       int | None = None    # encoder counts
    soft_max:       int | None = None
    travel_min:     int | None = None
    travel_max:     int | None = None
    span_counts:    int | None = None
    step_um:        float = 50.0
    # Runtime-only: set when a hard-limit bit is seen on this axis (the map
    # is then wrong), cleared only by establish_frame's remeasured slope.
    frame_stale:    bool = False
    # Runtime-only, acqApp's: motion confined to ±this µm of 0,0 (None = off).
    window_um:      float | None = None

    @property
    def sign(self) -> int:
        return -1 if self.invert else 1

    @property
    def has_frame(self) -> bool:
        """Absolute go-to can be trusted."""
        return (self.slope is not None and self.offset is not None
                and self.origin_set and not self.frame_stale)

    def default_span(self) -> int:
        if self.span_counts:
            return int(self.span_counts)
        return int(round(FULL_TRAVEL_UM * self.counts_per_um))

    # ── frame edits (return the JSON keys to persist) ───────────────────────
    def center_updates(self, counts: int, margin_um: float = 50.0) -> dict:
        """`counts` as origin, travel/soft limits at ±half-travel around it."""
        half_um = FULL_TRAVEL_UM / 2.0
        half = int(round(half_um * self.counts_per_um))
        soft = int(round((half_um - margin_um) * self.counts_per_um))
        c = int(counts)
        return {"true_center": c,
                "travel_min": c - half, "travel_max": c + half,
                "soft_min":   c - soft, "soft_max":   c + soft}

    def apply_updates(self, upd: dict) -> None:
        if "true_center" in upd:
            # Explicit None clears the origin, so a stale one can't survive.
            if upd["true_center"] is None:
                self.ref_counts, self.origin_set = 0.0, False
            else:
                self.ref_counts = float(upd["true_center"])
                self.origin_set = True
        for k in ("slope", "offset", "soft_min", "soft_max", "travel_min", "travel_max"):
            if k in upd:
                setattr(self, k, upd[k])
        # Only establish_frame sends `slope`, freshly measured in raw units.
        if "slope" in upd:
            self.frame_stale = False

    def to_um(self, counts: float) -> float:
        if not self.counts_per_um:
            return 0.0
        return self.sign * (counts - self.ref_counts) / self.counts_per_um

    def um_to_counts(self, um: float) -> int:
        return int(round(self.ref_counts + self.sign * um * self.counts_per_um))

    def window_limits_um(self) -> tuple[float, float] | None:
        """±window_um around 0,0, or None when off or there is no 0,0."""
        if self.window_um is None or not self.origin_set:
            return None
        return (-self.window_um, self.window_um)

    def clamp_counts(self, counts: int) -> int:
        """Clamp to soft limits (and the window); unbounded on a
        never-calibrated axis (jog must work to set the first zero). An origin
        with no soft limits is a hand-edit contradiction and refuses."""
        if self.soft_min is None or self.soft_max is None:
            if self.origin_set:
                raise ValueError(
                    f"{self.name}: claims a calibrated origin but has no "
                    f"soft limits — refusing to clamp {counts} counts "
                    f"unbounded. Re-establish the frame.")
            return counts
        c = max(self.soft_min, min(counts, self.soft_max))
        w = self.window_limits_um()
        if w is not None:
            lo, hi = sorted(self.um_to_counts(u) for u in w)
            c = max(lo, min(c, hi))
        return c

    def soft_limits_um(self) -> tuple[float, float]:
        """Where motion may go, in µm: the soft limits, narrowed to the
        window while it is on."""
        if self.soft_min is not None and self.soft_max is not None:
            a, b = self.to_um(self.soft_min), self.to_um(self.soft_max)
            lo, hi = min(a, b), max(a, b)
        else:
            lo, hi = (-6350.0, 6350.0)   # ±¼ inch fallback
        w = self.window_limits_um()
        if w is not None and max(lo, w[0]) < min(hi, w[1]):
            lo, hi = max(lo, w[0]), min(hi, w[1])
        return (lo, hi)

    def view_limits_um(self) -> tuple[float, float]:
        """What the map shows: the window's range while on, else the travel."""
        return self.window_limits_um() or self.travel_limits_um()

    def travel_limits_um(self) -> tuple[float, float]:
        if self.travel_min is not None and self.travel_max is not None:
            a, b = self.to_um(self.travel_min), self.to_um(self.travel_max)
            return (min(a, b), max(a, b))
        if self.soft_min is not None and self.soft_max is not None:
            a, b = self.to_um(self.soft_min), self.to_um(self.soft_max)
            return (min(a, b), max(a, b))
        return (-6350.0, 6350.0)


@dataclass
class StageSettings:
    port:    str = "COM54"
    controller: str = "auto"           # "auto" probes; see backend.py
    poll_hz: float = 4.0
    confirm_move_um: float = 3000.0    # ask before larger moves
    # Tighter for Z: it moves directly under the objective.
    confirm_move_z_um: float = 500.0
    margin_um: float = 50.0            # soft-limit inset from the travel ends
    invert_y: bool = True              # +Y screen-up on the map
    # Rotates only the jog buttons' direction; local to acqApp, not calibration.
    frame_rotation_deg: float = 0.0
    # Measured stage -> camera axes (`cam_axes.CamAxes`); overrides the
    # rotation above when set. Local to acqApp, like it.
    cam_axes: tuple[float, float, float, float] | None = None
    # Speed cap for dragging the position on the travel map; local to acqApp.
    drag_speed_um_s: float = 200.0
    # Confine X/Y to ±WINDOW_RANGE_UM of 0,0 (the axes' `window_um`); local.
    window_limit: bool = True
    x: StageAxis = None      # type: ignore[assignment]
    y: StageAxis = None      # type: ignore[assignment]
    z: StageAxis | None = None         # None on a rig without focus

    def __post_init__(self):
        if self.x is None:
            self.x = StageAxis(0, "X", 61.9864)
        if self.y is None:
            self.y = StageAxis(1, "Y", 61.8735, invert=True)

    @property
    def has_frame(self) -> bool:
        """X/Y only: Z calibrates separately, and an uncalibrated Z must not
        disable XY go-to."""
        return self.x.has_frame and self.y.has_frame

    @property
    def has_z(self) -> bool:
        return self.z is not None


def load_settings() -> StageSettings:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return StageSettings()
    if not isinstance(cfg, dict):           # shared file, edited by hand too
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
    # Z needs both `xy_pad.z_axis` and `"active": true` on that axis.
    # `active` isn't honoured for X/Y: every code path assumes they exist.
    zi = pad.get("z_axis")
    z = (_axis(zi, "Z") if zi is not None
         and bool(axes.get(zi, {}).get("active", False)) else None)
    confirm_counts = cfg.get("max_unconfirmed_move_counts", 200000)
    poll_ms = cfg.get("poll_interval_ms", 250)
    if not isinstance(poll_ms, (int, float)) or not 0 < poll_ms < 1e6:
        poll_ms = 250                       # 0 divided; nonsense polls forever
    return StageSettings(
        port              = cfg.get("port", "COM54"),
        controller        = cfg.get("controller", "auto"),
        poll_hz           = 1000.0 / poll_ms,
        confirm_move_um   = confirm_counts / (x.counts_per_um or 1.0),
        confirm_move_z_um = float(cfg.get("max_unconfirmed_move_z_um", 500.0)),
        margin_um         = float(cfg.get("margin_um", 50)),
        invert_y          = bool(pad.get("invert_y", True)),
        x                 = x,
        y                 = _axis(yi, "Y"),
        z                 = z,
    )


def save_axis_updates(updates: dict[int, dict]) -> Path:
    """Merge per-axis keys into the shared config ({index: {key: value}}),
    atomically, keeping the old contents as `.bak`. Never raises on a missing
    or corrupt file: the live axes are already updated, and establish_frame
    may have spent minutes driving the limits."""
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    if raw:
        try:
            path.with_suffix(path.suffix + ".bak").write_text(raw, encoding="utf-8")
        except OSError:
            pass
    try:
        cfg = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        cfg = {}                                # recoverable from the .bak
    if not isinstance(cfg, dict):
        cfg = {}
    by_index = {a.get("index"): a for a in cfg.get("axes", [])}
    for idx, upd in updates.items():
        entry = by_index.get(idx)
        if entry is None:
            entry = {"index": idx}
            cfg.setdefault("axes", []).append(entry)
        entry.update(upd)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
