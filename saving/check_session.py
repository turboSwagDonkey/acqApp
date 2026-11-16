"""Check recordings after a session: closed cleanly, every stream there, no
drops, gaps or out-of-step streams. Reads only.

    python -m acqApp.saving.check_session FOLDER [FOLDER ...] [-v]

A FOLDER is one recording (it holds *_settings.json) or anything above
recordings (a day, a routine's folder): every recording under it is checked.
Prints a line per recording, its problems under it; exits 1 if any has one.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from array import array
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from acqApp.saving.config import BAD_SUFFIX

GAP_X = 5.0             # a gap: this many median sample intervals
RATE_TOL = 0.05         # measured vs set rate
LAG_S = 1.0             # a stream starting/ending this far from the rest
DCIMG_SHORT = 0.98      # .dcimg frames under this share of rate x time

# What each module writes to the data CSV, beyond events.
MODULE_STREAMS = {
    "wheel": ("wheel_voltage", "wheel_speed", "wheel_distance"),
    "stage": ("stage_x_um", "stage_y_um"),
}
# Sampled streams worth a rate and gap check, and the setting naming the rate.
RATED = {"wheel_voltage": ("wheel_rate_actual_hz", "wheel_rate_hz")}


@dataclass
class Findings:
    path: Path
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Stream:
    t: array = field(default_factory=lambda: array("d"))
    v: array = field(default_factory=lambda: array("d"))


def find_recordings(root: Path) -> list[Path]:
    if any(root.glob("*_settings.json")):
        return [root]
    return sorted({p.parent for p in root.rglob("*_settings.json")})


def read_streams(csv_path: Path) -> dict[str, Stream]:
    """The long-format data CSV, per stream. array('d'): an hour of 1 kHz
    wheel is millions of rows."""
    out: dict[str, Stream] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = csv.reader(f)
        next(rows, None)                            # header
        for row in rows:
            if len(row) < 3:
                continue                            # a killed process's last line
            try:
                t, v = float(row[0]), float(row[2])
            except ValueError:
                continue
            s = out.setdefault(row[1], Stream())
            s.t.append(t)
            s.v.append(v)
    return out


def _rate_and_gaps(t: np.ndarray) -> tuple[float, int, float]:
    """(median rate Hz, gaps, longest gap s) of a sampled stream."""
    dt = np.diff(t)
    if dt.size == 0:
        return 0.0, 0, 0.0
    med = float(np.median(dt))
    if med <= 0:
        return 0.0, 0, 0.0
    big = dt[dt > GAP_X * med]
    return 1.0 / med, int(big.size), float(big.max(initial=0.0))


def check_recording(folder: Path) -> Findings:
    fx = Findings(folder)
    bad, note = fx.problems.append, fx.notes.append
    settings = next(iter(sorted(folder.glob("*_settings.json"))), None)
    try:
        meta = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError) as e:
        bad(f"settings unreadable ({e})")
        return fx
    if folder.name.endswith(BAD_SUFFIX):
        note("marked _BAD: every run in it was interrupted")
    if "recorder_dropped_samples" not in meta:
        bad("never closed cleanly: no final counts (the app stopped "
            "mid-recording?)")
    for key, what in (("recorder_dropped_samples", "dropped (buffer full)"),
                      ("recorder_late_samples", "after the file closed"),
                      ("recorder_unstamped_samples", "before the clock started")):
        n = meta.get(key) or 0
        if n:
            (bad if key == "recorder_dropped_samples" else note)(
                f"{n} samples {what}")
    modules = set(filter(None, str(meta.get("modules", "")).split(",")))

    streams: dict[str, Stream] = {}
    data = next(iter(folder.glob("*_data.csv")), None)
    if data is not None:
        try:
            streams = read_streams(data)
        except OSError as e:
            bad(f"data CSV unreadable ({e})")
    for mod, names in MODULE_STREAMS.items():
        missing = [s for s in names if mod in modules and s not in streams]
        if missing:
            bad(f"{mod} loaded but no {', '.join(missing)} in the data")

    spans: dict[str, tuple[float, float]] = {}
    for name, s in streams.items():
        t = np.frombuffer(s.t, dtype=np.float64)
        if t.size and np.any(np.diff(t) < 0):
            bad(f"{name}: timestamps go backwards")
        if t.size:
            spans[name] = (float(t[0]), float(t[-1]))
        if name in RATED and t.size > 2:
            hz, gaps, worst = _rate_and_gaps(t)
            want = next((meta[k] for k in RATED[name] if meta.get(k)), None)
            if want and abs(hz - want) > RATE_TOL * want:
                bad(f"{name}: {hz:.1f} Hz, set {want:g} Hz")
            if gaps:
                bad(f"{name}: {gaps} gap(s), longest {worst:.3f} s")
    idx = streams.get("voltage_cam_index")
    if idx is not None and len(idx.v) > 1:
        jumps = np.diff(np.frombuffer(idx.v, dtype=np.float64))
        lost = int(np.sum(np.clip(jumps - 1, 0, None)))
        if lost:
            bad(f"camera: {lost} frame(s) dropped (index jumps)")

    for ts in sorted(folder.glob("*_timestamps.csv")):
        stream = ts.stem.removeprefix(folder.name + "_").removesuffix("_timestamps")
        try:
            t = np.loadtxt(ts, delimiter=",", skiprows=1, usecols=1, ndmin=1)
        except (OSError, ValueError) as e:
            bad(f"{stream}: frame timestamps unreadable ({e})")
            continue
        if t.size:
            spans[stream] = (float(t[0]), float(t[-1]))
        hz, gaps, worst = _rate_and_gaps(t)
        want = meta.get("pupil_rate_hz") if stream == "pupil_cam" else None
        if want and t.size > 2 and abs(hz - want) > RATE_TOL * want:
            bad(f"{stream}: {hz:.1f} fps, set {want:g}")
        if gaps:
            bad(f"{stream}: {gaps} gap(s) in frames, longest {worst:.3f} s")
        note(f"{stream}: {t.size} frames")

    if "voltage_cam" in modules:
        _check_camera(folder, meta, bad, note)

    if len(spans) > 1:
        t0 = min(a for a, _ in spans.values())
        t1 = max(b for _, b in spans.values())
        note(f"{t1 - t0:.1f} s")
        for name, (a, b) in sorted(spans.items()):
            if name in ("routine", "puffer", "dmd", "mirror", "vis_stim",
                        "voltage_cam_stale"):
                continue                            # events, not continuous
            if a - t0 > LAG_S:
                bad(f"{name} starts {a - t0:.1f} s after the first stream")
            if t1 - b > LAG_S:
                bad(f"{name} ends {t1 - b:.1f} s before the last stream")
    return fx


def _check_camera(folder: Path, meta: dict, bad, note) -> None:
    dcimg = sorted(folder.glob("*.dcimg"))
    tiff = sorted(folder.glob("*_voltage_cam.tiff"))
    if "cam_dcimg_frames" in meta or dcimg:
        if not dcimg or dcimg[0].stat().st_size == 0:
            bad("camera: the .dcimg is missing or empty")
        frames = meta.get("cam_dcimg_frames")
        missing = meta.get("cam_dcimg_missing") or 0
        if missing:
            bad(f"camera: {missing} frame(s) missing from the .dcimg")
        t0, t1 = meta.get("cam_dcimg_t0_s"), meta.get("cam_dcimg_t1_s")
        if t1 is None:
            note("camera: no .dcimg close time (cam_dcimg_t1_s)")
        hz = meta.get("cam_rate_hz") or 0
        if (frames is not None and hz and t0 is not None and t1 is not None
                and not meta.get("cam_burst_frames")):
            want = (t1 - t0) * hz
            if frames < DCIMG_SHORT * want:
                bad(f"camera: {frames} frames in the .dcimg, ~{want:.0f} "
                    f"expected at {hz:g} Hz")
        if frames is not None:
            note(f"camera: {frames} frames (.dcimg)")
    elif not tiff:
        bad("camera loaded but no frames file (.dcimg or .tiff)")
    if meta.get("cam_dropped_frames"):
        bad(f"camera: {meta['cam_dropped_frames']} frame(s) dropped")


def main(argv=None) -> int:
    from acqApp.console import enable_safe_console
    enable_safe_console()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folders", type=Path, nargs="+")
    ap.add_argument("-v", action="store_true", help="notes too, for clean ones")
    a = ap.parse_args(argv)
    found = [r for f in a.folders for r in find_recordings(f)]
    if not found:
        print("no recordings (no *_settings.json) under "
              + ", ".join(map(str, a.folders)))
        return 1
    failed = 0
    for rec in found:
        fx = check_recording(rec)
        failed += bool(fx.problems)
        head = (f"{len(fx.problems)} problem(s)" if fx.problems else "OK")
        print(f"{rec}  {head}" + (f"  ({'; '.join(fx.notes)})"
                                  if a.v or not fx.problems else ""))
        for p in fx.problems:
            print(f"    x {p}")
    print(f"\n{len(found) - failed}/{len(found)} recording(s) clean")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
