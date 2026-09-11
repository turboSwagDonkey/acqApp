"""
SplitWriter — each stream in its own file instead of one composite .h5.

Covers TiffFileWriter (multi-page TIFF + timestamp sidecar round-trips),
LongCsvWriter (long-format rows, routine_step decoded from the `routine`
stream's +/-(index+1) encoding and carried onto every other row), and
SplitWriter itself (routes by stream shape, JSON settings round-trip,
mode "x" refuses an existing session folder like HDF5Writer's mode "x").

Cheap and hardware-free: no QApplication, no device, ~1 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_split_writer.py
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import tifffile

from _harness import Report

from acqApp.acq.writer import LongCsvWriter, SplitWriter, TiffFileWriter


def check_tiff_roundtrip(r: Report, tmp: Path) -> None:
    frames = [np.full((16, 16), i, dtype=np.uint16) for i in range(4)]
    p = tmp / "cam.tiff"
    w = TiffFileWriter(p)
    for i, f in enumerate(frames):
        w.write(i * 0.1, f)
    w.close()

    data = tifffile.imread(p)
    r.check(data.shape == (4, 16, 16), f"all frames present ({data.shape})")
    ok = all(np.array_equal(data[i], frames[i]) for i in range(4))
    r.check(ok, "every frame reads back byte-identical")

    ts_path = tmp / "cam_timestamps.csv"
    with open(ts_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    r.check([row["frame"] for row in rows] == ["0", "1", "2", "3"],
            "one timestamp row per frame, in order")
    r.check(abs(float(rows[2]["timestamp"]) - 0.2) < 1e-9,
            "timestamps match what was written")


def check_csv_routine_step(r: Report, tmp: Path) -> None:
    """Not just the routine stream's own rows: every OTHER stream's row in
    the open window is tagged too, and rows outside any step read "" —
    the control that proves the tagging isn't vacuously always-on."""
    p = tmp / "data.csv"
    w = LongCsvWriter(p)
    w.write("wheel", 0.0, 1.5)                 # before any step: untagged
    w.write("routine", 1.0, 1.0)                # entering step 0 (encoded +1)
    w.write("wheel", 2.0, 1.6)
    w.write("puffer", 2.5, 0.1)
    w.write("routine", 3.0, -1.0)               # leaving step 0 (encoded -1)
    w.write("wheel", 4.0, 1.7)                  # after: untagged again
    w.close()

    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    r.check([row["stream"] for row in rows]
            == ["wheel", "routine", "wheel", "puffer", "routine", "wheel"],
            "rows in write order, long format")
    r.check(rows[0]["routine_step"] == "",
            "a sample before any step is untagged")
    r.check(rows[2]["routine_step"] == "0" and rows[3]["routine_step"] == "0",
            "wheel and puffer rows inside the open step both get step 0")
    r.check(rows[4]["routine_step"] == "0",
            "the routine stream's own closing row is tagged too")
    r.check(rows[5]["routine_step"] == "",
            "a sample after the step closes goes back to untagged")


def check_split_writer_routes(r: Report, tmp: Path) -> None:
    """Image streams -> their own TIFF; scalars -> the one shared CSV;
    metadata (incl. a nested dict, like routine_protocol) -> JSON."""
    session = tmp / "sess_001"
    w = SplitWriter()
    meta = {"subject": "m1", "emulated": True,
            "routine_protocol": {"name": "r1", "steps": [{"label": "a"}]}}
    w.open(session, meta)

    frame = np.full((8, 8), 42, dtype=np.uint16)
    w.write("voltage_cam", 0.0, frame)
    w.write("wheel", 0.0, 1.5)
    w.write("puffer", 0.1, 0.05)
    w.update_metadata({"cam_dropped_frames": 0})
    w.close()

    tiff_path = session / "sess_001_voltage_cam.tiff"
    r.check(tiff_path.is_file(), "the image stream got its own TIFF")
    # A single-page stack reads back 2-D (tifffile squeezes the page axis),
    # not (1, H, W) — reshape rather than index [0] into the wrong axis.
    got = tifffile.imread(tiff_path).reshape(frame.shape)
    r.check(np.array_equal(got, frame), "…with the right frame data")

    csv_path = session / "sess_001_data.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    r.check({row["stream"] for row in rows} == {"wheel", "puffer"},
            "scalar streams share the one CSV, not the TIFF")

    json_path = session / "sess_001_settings.json"
    with open(json_path, encoding="utf-8") as f:
        saved = json.load(f)
    r.check(saved.get("subject") == "m1" and saved.get("emulated") is True,
            "flat metadata round-trips with native JSON types")
    r.check(saved.get("routine_protocol") == meta["routine_protocol"],
            "nested metadata (the routine protocol) stays a real object, "
            "not a stringified blob")
    r.check(saved.get("cam_dropped_frames") == 0,
            "update_metadata() after close is reflected in the final JSON")


def check_refuses_existing_folder(r: Report, tmp: Path) -> None:
    """Mirrors HDF5Writer's mode 'x': an existing session is hours of
    animal time with no undo."""
    session = tmp / "sess_002"
    session.mkdir()
    w = SplitWriter()
    try:
        w.open(session, {})
        r.check(False, "opening an existing session folder should raise")
    except FileExistsError:
        r.check(True, "opening an existing session folder raises, like "
                      "HDF5Writer's mode 'x'")


def main() -> int:
    r = Report("split-writer")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_splitwriter_"))
    try:
        check_tiff_roundtrip(r, tmp)
        check_csv_routine_step(r, tmp)
        check_split_writer_routes(r, tmp)
        check_refuses_existing_folder(r, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
