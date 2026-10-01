"""Saving: paths and naming, the split writer, the HDF5 direct-chunk path.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_saving.py [-q] [--part NAME]
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import tifffile
from _harness import Report, run_parts
from acqApp.acq.writer import (HDF5Writer, LongCsvWriter, SplitWriter,
                               TiffFileWriter)
from acqApp.saving import SaveConfig, benchmark_drive, sanitize


# ═══ paths (was test_save_paths.py) ═════════════════════════════════════

WHEN = datetime(2026, 8, 12, 14, 30, 5)


def check_stem(r: Report) -> None:
    """Token substitution and sanitisation of operator free text."""
    cfg = SaveConfig(mouse_id="m17", project="run2",
                     template="{mouse_id}_{project}_{date}_{time}")
    r.check(cfg.stem(WHEN) == "m17_run2_20260812_143005",
            f"all four tokens substituted (got {cfg.stem(WHEN)!r})")

    # A separator would create a directory, or escape the save folder.
    cfg = SaveConfig(mouse_id=r"m17/../x", template="{mouse_id}")
    r.check("/" not in cfg.stem(WHEN) and "\\" not in cfg.stem(WHEN),
            f"path separators sanitised out of the mouse ID "
            f"(got {cfg.stem(WHEN)!r})")
    r.check(sanitize("") == "session" and sanitize("  ..  ") == "session",
            "empty/degenerate names fall back rather than producing ''")

    cfg = SaveConfig(mouse_id="m17", project="", template="{mouse_id}_{project}")
    r.check(cfg.stem(WHEN) == "m17", f"empty token tidied (got {cfg.stem(WHEN)!r})")

    # The active FOV name is an opt-in suffix, not a template token.
    cfg = SaveConfig(mouse_id="m17", template="{mouse_id}")
    r.check(cfg.stem(WHEN) == cfg.stem(WHEN, fov=""),
            "no FOV is a no-op — today's plain stem, unchanged")
    r.check(cfg.stem(WHEN, fov="window1") == "m17_window1",
            f"a FOV name is appended after the resolved stem "
            f"(got {cfg.stem(WHEN, fov='window1')!r})")
    r.check("/" not in cfg.stem(WHEN, fov="a/b") and cfg.stem(WHEN, fov="a/b")
            == "m17_a_b",
            f"the FOV name is sanitised too (got {cfg.stem(WHEN, fov='a/b')!r})")

    # FOV<name>_T<n>, but a FOV named "fov…" must not read "FOVfov1".
    for fov, want in (("1", "FOV1_T2"), ("fov1", "fov1_T2"),
                      ("FOV3", "FOV3_T2"), ("custom", "FOVcustom_T2")):
        got = cfg.resolve_routine(fov, 2, WHEN).stem
        r.check(got == want, f"routine stem for FOV {fov!r} (got {got!r})")

    p = SaveConfig(mouse_id="m17", project="").routine_base(WHEN)
    r.check(p.parent.name == "m17"
            and p.parent.parent == SaveConfig(mouse_id="m17").resolved_folder(),
            f"empty project adds no level (got {p})")
    p = SaveConfig(mouse_id="m17", project="pX").routine_base(WHEN)
    r.check(p.parent.name == "m17" and p.parent.parent.name == "pX",
            f"a set project is still a level (got {p})")


def check_unique(r: Report, tmp: Path) -> None:
    """resolve(unique=True) must never name a file that already exists."""
    for subfolder in (True, False):
        base = tmp / f"sub{int(subfolder)}"
        cfg = SaveConfig(folder=str(base), mouse_id="m17",
                         template="{mouse_id}", subfolder=subfolder)
        label = "subfolder" if subfolder else "flat"

        first = cfg.resolve(WHEN, unique=True)
        r.check(first.name == "m17.h5", f"[{label}] first recording keeps the stem")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"first recording")

        second = cfg.resolve(WHEN, unique=True)
        r.check(second != first and not second.exists(),
                f"[{label}] second resolves to a free path ({second.name})")
        r.check(second.name == "m17_001.h5",
                f"[{label}] suffix is _001 (got {second.name})")
        if subfolder:
            r.check(second.parent.name == "m17_001",
                    f"[{label}] the subfolder is renamed with it "
                    f"(got {second.parent.name})")
        second.parent.mkdir(parents=True, exist_ok=True)
        second.write_bytes(b"second recording")

        third = cfg.resolve(WHEN, unique=True)
        r.check(third.name == "m17_002.h5",
                f"[{label}] numbering continues (got {third.name})")
        r.check(first.read_bytes() == b"first recording",
                f"[{label}] the original file was never touched")

        # The preview and the metadata rely on this staying deterministic.
        r.check(cfg.resolve(WHEN) == first,
                f"[{label}] unique=False is unchanged")


def check_writer_refuses(r: Report, tmp: Path) -> None:
    """The backstop: opening onto an existing file raises, never truncates."""
    path = tmp / "existing" / "session.h5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not an hdf5 file, but it is somebody's data")
    size = path.stat().st_size

    w = HDF5Writer()
    try:
        w.open(path, {"subject": "m17"})
    except FileExistsError:
        r.check(True, "HDF5Writer.open refuses an existing path")
    except Exception as e:                      # noqa: BLE001 - report, don't hide
        r.check(False, f"expected FileExistsError, got {type(e).__name__}: {e}")
    else:
        w.close()
        r.check(False, "HDF5Writer.open OVERWROTE an existing file")
    r.check(path.stat().st_size == size, "the existing file is byte-for-byte intact")

    fresh = tmp / "existing" / "session_001.h5"
    w = HDF5Writer()
    w.open(fresh, {"subject": "m17"})
    w.write("wheel", 0.0, 1.5)
    w.close()
    r.check(fresh.is_file() and fresh.stat().st_size > 0,
            "a free path still records normally")

    w = HDF5Writer(overwrite=True)
    w.open(fresh, {"subject": "m17"})
    w.close()
    r.check(fresh.is_file(), "overwrite=True still truncates when asked for")


def check_benchmark_drive(r: Report, tmp: Path) -> None:
    """The Save panel's drive scan trusts this for a real number."""
    mbps = benchmark_drive(str(tmp), 2 << 20)      # 2 MB — fast, not realistic
    r.check(mbps is not None and mbps > 0,
            f"a writable folder returns a positive rate (got {mbps!r})")

    leftover = list(tmp.glob(".acqapp_drive_speedtest*"))
    r.check(not leftover, f"the test file is cleaned up (found {leftover})")

    missing = benchmark_drive(str(tmp / "does_not_exist_at_all"), 2 << 20)
    r.check(missing is None,
            f"an unwritable/missing path returns None, not a crash (got {missing!r})")


def check_renumber(r: Report, tmp: Path) -> None:
    """Renumbering a closed trial in each save layout; an open file refuses
    and changes nothing."""
    from acqApp.saving.config import rename_trial

    # split folder
    base = tmp / "split"
    d = base / "FOV2_T19"
    d.mkdir(parents=True)
    for suffix in ("_voltage_cam.dcimg", "_data.csv", "_settings.json"):
        (d / f"FOV2_T19{suffix}").write_text("x")
    (d / "fov.json").write_text("{}")
    new = rename_trial(d, "FOV2_T20")
    names = sorted(p.name for p in new.iterdir())
    r.check(new == base / "FOV2_T20" and not d.exists(),
            f"split: the folder is renumbered ({new.name})")
    r.check(names == ["FOV2_T20_data.csv", "FOV2_T20_settings.json",
                      "FOV2_T20_voltage_cam.dcimg", "fov.json"],
            f"…and every file named after it ({names})")

    # .h5 in its own folder, target taken
    base = tmp / "sub"
    (base / "FOV2_T20").mkdir(parents=True)
    d = base / "FOV2_T19"
    d.mkdir()
    (d / "FOV2_T19.h5").write_text("x")
    new = rename_trial(d / "FOV2_T19.h5", "FOV2_T20")
    r.check(new == base / "FOV2_T20_001" / "FOV2_T20_001.h5" and new.exists(),
            f"subfolder .h5: a taken name gets the next free one ({new})")

    # flat .h5 with its sidecar; a neighbour sharing the prefix is untouched
    base = tmp / "flat"
    base.mkdir()
    (base / "FOV2_T19.h5").write_text("x")
    (base / "FOV2_T19.fov.json").write_text("{}")
    (base / "FOV2_T190.h5").write_text("x")
    new = rename_trial(base / "FOV2_T19.h5", "FOV2_T20")
    r.check(sorted(p.name for p in base.iterdir())
            == ["FOV2_T190.h5", "FOV2_T20.fov.json", "FOV2_T20.h5"],
            f"flat .h5: file and sidecar renumbered, T190 untouched "
            f"({sorted(p.name for p in base.iterdir())})")

    # an open file (the camera still holding its .dcimg)
    d = tmp / "busy" / "FOV2_T19"
    d.mkdir(parents=True)
    held = open(d / "FOV2_T19_voltage_cam.dcimg", "w")
    try:
        try:
            rename_trial(d, "FOV2_T20")
            refused = False
        except OSError:
            refused = True
        r.check(refused and d.exists() and not (d.parent / "FOV2_T20").exists(),
                "a folder with a file still open refuses, unchanged")
    finally:
        held.close()
    r.check(rename_trial(d, "FOV2_T20").exists(), "…and succeeds once it's closed")


def _part_paths() -> int:
    r = Report("save-paths")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_savepaths_"))
    try:
        check_stem(r)
        check_unique(r, tmp)
        check_writer_refuses(r, tmp)
        check_benchmark_drive(r, tmp)
        check_renumber(r, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ split (was test_split_writer.py) ═══════════════════════════════════

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
    """Every stream's row inside an open step is tagged; rows outside read ""
    (the control against tagging being always on)."""
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
    # tifffile squeezes a single page to 2-D; [0] would index the wrong axis.
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
    """As HDF5Writer's mode 'x': a session is animal time with no undo."""
    session = tmp / "sess_002"
    session.mkdir()
    w = SplitWriter()
    try:
        w.open(session, {})
        r.check(False, "opening an existing session folder should raise")
    except FileExistsError:
        r.check(True, "opening an existing session folder raises, like "
                      "HDF5Writer's mode 'x'")


def _part_split() -> int:
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


# ═══ chunks (was test_writer_chunks.py) ═════════════════════════════════

# >8 MB, so chunk_frames is 1 and the direct path is taken, as for the
# 4432x2368 camera frame.
BIG = (2048, 2048)          # uint16 -> 8.4 MB
SMALL = (64, 64)            # uint16 -> 8 KB, so 16 frames share a chunk

# Run as a child: an unguarded direct write of uint8 bytes into a uint16
# chunk, then a read. Expected NOT to reach the last line.
DEMO_SRC = '''
import sys
import numpy as np
import h5py

path = sys.argv[1]
src = np.zeros((2048, 2048), dtype=np.uint8)
with h5py.File(path, "w") as f:
    d = f.create_dataset("frames", shape=(1, 2048, 2048), dtype=np.uint16,
                         chunks=(1, 2048, 2048))
    d.id.write_direct_chunk((0, 0, 0), memoryview(src).cast("B"))
with h5py.File(path, "r") as f:
    _ = f["frames"][0]
print("SURVIVED")
'''


def _frames(shape, n, dtype=np.uint16):
    """n distinct frames, so a mix-up of two of them cannot pass unnoticed."""
    rng = np.random.default_rng(7)
    return [rng.integers(0, 4000, size=shape, dtype=dtype) for _ in range(n)]


def _write(path, frames, stream="cam", **kw):
    w = HDF5Writer(**kw)
    w.open(path, {"bench": False})
    for i, f in enumerate(frames):
        w.write(stream, i * 0.01, f)
    direct = w._streams[stream]["direct"]
    w.close()
    return direct


def check_direct_roundtrip(r: Report, tmp: Path) -> None:
    """The fast path stores exactly what it was given."""
    frames = _frames(BIG, 5)
    p = tmp / "direct.h5"
    direct = _write(p, frames)

    r.check(direct, "a full-size frame takes the direct-chunk path "
                    "(if this fails, the round-trip checks are vacuous)")

    with h5py.File(p, "r") as f:
        d = f["cam/frames"]
        r.check(d.shape == (5,) + BIG, f"trimmed to what was written ({d.shape})")
        r.check(d.chunks == (1,) + BIG, f"one frame per chunk ({d.chunks})")
        ok = all(np.array_equal(d[i], frames[i]) for i in range(5))
        r.check(ok, "every frame reads back byte-identical")
        ts = f["cam/timestamps"][:]
        r.check(np.array_equal(ts, np.arange(5) * 0.01) and not np.isnan(ts).any(),
                "timestamps trimmed, no NaN tail after a clean close")


def check_guard_rejects(r: Report, tmp: Path) -> None:
    """A frame the direct write would corrupt goes the slow way instead."""
    base = _frames(BIG, 3)

    # A transpose: right shape and dtype, wrong memory layout.
    view = np.ascontiguousarray(base[0]).T
    r.check(not view.flags.c_contiguous, "the transposed frame really is "
                                         "non-contiguous (control)")
    p = tmp / "noncontig.h5"
    _write(p, [view])
    with h5py.File(p, "r") as f:
        r.check(np.array_equal(f["cam/frames"][0], view),
                "a non-contiguous frame still round-trips (slice fallback)")

    # Unguarded, contiguity raises in the writer thread: a lost recording.
    try:
        memoryview(view).cast("B")
        raised = False
    except TypeError:
        raised = True
    r.check(raised, "a non-contiguous frame cannot be cast to a byte buffer at "
                    "all, so the guard prevents a raise (control)")

    # Unguarded, a dtype mismatch writes and closes cleanly, then the READER
    # dies with an access violation (0xC0000005, measured 2026-08-25) — hence
    # a child process.
    demo = tmp / "demo_corrupt.py"
    demo.write_text(DEMO_SRC, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(demo), str(tmp / "corrupt.h5")],
                          capture_output=True, text=True, timeout=120)
    r.check(proc.returncode != 0 and "SURVIVED" not in proc.stdout,
            f"bypassing the guard on a dtype mismatch writes a file that kills "
            f"the reader (child exit {proc.returncode}) — so the guard is not "
            f"superstition (control)")


def check_dtype_change(r: Report, tmp: Path) -> None:
    """A stream whose dtype changes mid-run must not be written raw."""
    p = tmp / "dtype.h5"
    w = HDF5Writer()
    w.open(p, {})
    first = _frames(BIG, 1)[0]
    w.write("cam", 0.0, first)
    r.check(w._streams["cam"]["direct"], "stream opened on the direct path")
    odd = (first // 16).astype(np.uint8)
    w.write("cam", 0.01, odd)
    w.close()
    with h5py.File(p, "r") as f:
        d = f["cam/frames"]
        r.check(np.array_equal(d[0], first), "the uint16 frame is intact")
        r.check(np.array_equal(d[1], odd.astype(np.uint16)),
                "the uint8 frame was converted, not written raw")


def check_path_disabled(r: Report, tmp: Path) -> None:
    """Where the direct write is invalid it must be off, and data still land."""
    # The offset arithmetic assumes one frame per chunk.
    small = _frames(SMALL, 20)
    p = tmp / "small.h5"
    direct = _write(p, small)
    with h5py.File(p, "r") as f:
        d = f["cam/frames"]
        r.check(d.chunks[0] > 1, f"small frames really do share a chunk "
                                 f"({d.chunks[0]} per chunk — control)")
        r.check(not direct, "multi-frame chunks disable the direct path")
        r.check(all(np.array_equal(d[i], small[i]) for i in range(20)),
                "all 20 small frames round-trip")

    # Raw bytes under a deflate filter would not read back at all.
    big = _frames(BIG, 2)
    p = tmp / "gzip.h5"
    direct = _write(p, big, compression="gzip", compression_opts=1)
    r.check(not direct, "compression disables the direct path")
    with h5py.File(p, "r") as f:
        d = f["cam/frames"]
        r.check(d.compression == "gzip", "the filter really is on (control)")
        r.check(all(np.array_equal(d[i], big[i]) for i in range(2)),
                "compressed frames round-trip")


def check_scalars(r: Report, tmp: Path) -> None:
    """Scalar streams never touch the image branch."""
    p = tmp / "mixed.h5"
    w = HDF5Writer()
    w.open(p, {})
    frames = _frames(BIG, 2)
    for i, f in enumerate(frames):
        w.write("cam", i * 0.01, f)
        w.write("wheel", i * 0.01, 1.5 + i)
    r.check(not w._streams["wheel"]["direct"], "a scalar stream is never direct")
    w.close()
    with h5py.File(p, "r") as f:
        r.check(np.array_equal(f["wheel/values"][:], [1.5, 2.5]),
                "scalar values written and trimmed")
        r.check(np.array_equal(f["cam/frames"][1], frames[1]),
                "frames unaffected by an interleaved scalar stream")


def _part_chunks() -> int:
    r = Report("writer-chunks")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_writerchunks_"))
    try:
        check_direct_roundtrip(r, tmp)
        check_guard_rejects(r, tmp)
        check_dtype_change(r, tmp)
        check_path_disabled(r, tmp)
        check_scalars(r, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ bpod (saving/bpod_match.py) ════════════════════════════════════════

def _session(n: int, seed: int = 3) -> np.ndarray:
    """Bpod trigger times with this task's outcome-dependent trial lengths
    (hit 6.7-10.2, CR 7.1, FA 9.2-13.2, miss 14.1 s) plus overhead."""
    rng = np.random.default_rng(seed)
    kind = rng.choice(4, n)
    dur = np.where(kind == 0, rng.uniform(6.7, 10.2, n),
          np.where(kind == 1, 7.1,
          np.where(kind == 2, rng.uniform(9.2, 13.2, n), 14.1)))
    return 100.0 + np.concatenate([[0.0], np.cumsum(dur[:-1] + 0.3)])


def _bpod_fixture(root: Path, bpod: np.ndarray, cam: np.ndarray):
    """The routine numbered its edges T1..T<n>; Bpod's own numbering differs.
    -> (trial folder, edge log, Bpod session file)."""
    from scipy.io import savemat
    base = root / "m1" / "20260930"
    base.mkdir(parents=True)
    edges = base / "routine_edges_test.csv"
    lines = ["edge,session_s,wall_time,fov,trial,path"]
    for j, t in enumerate(cam):
        d = base / f"FOV2_T{j + 1}"
        d.mkdir()
        (d / f"FOV2_T{j + 1}_voltage_cam.dcimg").write_text(str(j))
        lines.append(f"{j + 1},{t:.4f},x,2,{j + 1},{d}")
    edges.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trials = np.empty(len(bpod), dtype=object)
    for i in range(len(bpod)):
        trials[i] = {"States": {"CamTrigger": np.array([0.0002, 0.0102])}}
    mat = root / "session.mat"
    savemat(str(mat), {"SessionData": {"TrialStartTimestamp": bpod - 0.0002,
                                       "RawEvents": {"Trial": trials}}})
    return base, edges, mat


def check_bpod_match(r: Report, tmp: Path) -> None:
    from acqApp.saving import bpod_match as BM

    bpod = _session(40)
    missed = {0, 18}                           # Bpod trials 1 and 19
    kept = [i for i in range(40) if i not in missed]
    rng = np.random.default_rng(1)
    cam = (bpod[kept] - 97.0) * (1 + 40e-6) + rng.normal(0, 0.02, len(kept))
    m = BM.match(cam, bpod)
    r.check(not m.problem and len(m.pairs) == len(kept),
            f"every camera edge matches a Bpod trial ({len(m.pairs)}/"
            f"{len(kept)}, {m.problem!r})")
    r.check(all(m.pairs[j] == i for j, i in enumerate(kept)),
            "…the right one, although trial 1 itself was missed")
    r.check(abs(m.drift_ppm + 40) < 15,
            f"…with the clock drift recovered ({m.drift_ppm:+.0f} ppm)")

    steady = 100.0 + 7.0 * np.arange(20)
    m2 = BM.match(steady[1:] - 50.0, steady)
    r.check(bool(m2.problem),
            f"control: identical trial lengths with trial 1 missed can't be "
            f"aligned, and it refuses ({m2.problem!r})")
    m3 = BM.match(np.append(cam, cam[-1] + 3.0), bpod)
    r.check("match no Bpod" in m3.problem,
            f"an edge the stim rig never sent is refused ({m3.problem!r})")

    base, edges, mat = _bpod_fixture(tmp / "bpod", bpod, cam)
    got = BM.load_bpod_triggers(mat)
    r.check(np.allclose(got, bpod),
            "Bpod's session file reads back as trial start + CamTrigger onset")

    before = sorted(p.name for p in base.iterdir())
    r.check(BM.main([str(edges), str(mat)]) == 0
            and sorted(p.name for p in base.iterdir()) == before,
            "a dry run changes nothing")
    r.check(BM.main([str(edges), str(mat), "--apply"]) == 0,
            "--apply runs")
    names = {p.name for p in base.iterdir() if p.is_dir()}
    want = ({f"FOV2_T{i + 1}" for i in kept}
            | {"FOV2_T1_VOID", "FOV2_T19_VOID"})
    r.check(names == want, f"folders carry Bpod's trial numbers, missed ones "
                           f"VOID ({sorted(names - want)} / "
                           f"{sorted(want - names)})")
    inner = (base / "FOV2_T20" / "FOV2_T20_voltage_cam.dcimg")
    r.check(inner.exists() and inner.read_text() == "17",
            "…the data moved with its folder (Bpod 20 = the 18th edge)")
    r.check(json.loads((base / "FOV2_T19_VOID" / "void.json").read_text())
            ["bpod_trial"] == 19, "…and each VOID says which trial it was")
    with open(base / "renumber_log.csv", encoding="utf-8") as fh:
        log = list(csv.reader(fh))
    r.check(len(log) == 1 + 38 + 2,
            f"every change is logged for undoing ({len(log) - 1} rows)")


def check_bpod_rollback(r: Report, tmp: Path) -> None:
    """A file open part-way through Apply: the moved folders go back."""
    from acqApp.saving import bpod_match as BM

    bpod = _session(12)
    cam = bpod[1:] - 50.0                           # trial 1 missed
    base, edges, mat = _bpod_fixture(tmp / "rollback", bpod, cam)
    _lines, pl = BM.check(edges, mat)
    before = sorted(p.name for p in base.iterdir())
    held = open(base / "FOV2_T6" / "FOV2_T6_voltage_cam.dcimg")
    try:
        try:
            BM.apply(pl, base)
            err = ""
        except BM.ApplyError as e:
            err = str(e)
    finally:
        held.close()
    r.check("nothing was changed" in err and
            sorted(p.name for p in base.iterdir()) == before,
            f"an open file stops Apply with every folder put back ({err!r})")


def check_bpod_dialog(r: Report, tmp: Path) -> None:
    """The Save tab's button: Check shows the plan and enables Apply; any
    edit disables it again; Apply is refused while recording."""
    from PyQt6.QtWidgets import QMessageBox

    from acqApp.saving import bpod_dialog as BD

    bpod = _session(12)
    cam = bpod[1:] - 50.0
    root = tmp / "dialog"
    cfg = SaveConfig(folder=str(root), mouse_id="m1")
    today = cfg.routine_base(datetime.now())
    base, edges, mat = _bpod_fixture(root, bpod, cam)
    if not today.exists():
        base.rename(today)
    edges = today / edges.name
    lines = edges.read_text(encoding="utf-8").replace(str(base), str(today))
    edges.write_text(lines, encoding="utf-8")
    r.check(BD.newest_edge_log(cfg) == edges,
            "the dialog finds today's edge log by itself")

    recording = {"on": True}
    saved = []
    dlg = BD.BpodMatchDialog(cfg, lambda: recording["on"],
                             lambda: saved.append(cfg.bpod_folder))
    r.check(dlg._ed_edges.text() == str(edges), "…and fills it in")
    dlg._ed_bpod.setText(str(mat))
    r.check(not dlg._btn_apply.isEnabled(), "Apply starts disabled")
    dlg._check()
    r.check(dlg._btn_apply.isEnabled() and "void    trial 1" in
            dlg._report.toPlainText(), "Check shows the plan, then allows Apply")
    dlg._spn_first.setValue(2)
    r.check(not dlg._btn_apply.isEnabled(), "any edit makes the plan stale")
    dlg._spn_first.setValue(1)
    dlg._check()

    warned, asked = [], []
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    QMessageBox.question = staticmethod(
        lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1])
    before = sorted(p.name for p in today.iterdir())
    dlg._apply()
    r.check(warned and not asked and
            sorted(p.name for p in today.iterdir()) == before,
            "Apply is refused while recording, and changes nothing")
    recording["on"] = False
    dlg._apply()
    names = {p.name for p in today.iterdir() if p.is_dir()}
    r.check(asked and "FOV2_T1_VOID" in names and "FOV2_T12" in names
            and "FOV2_T1" not in names,
            f"after confirming, folders take Bpod's numbers ({sorted(names)[:4]}…)")
    r.check("Done:" in dlg._report.toPlainText()
            and not dlg._btn_apply.isEnabled(),
            "…the report says so, and Apply needs a fresh Check")


def _part_bpod() -> int:
    from _harness import isolate_user_state, qt_app
    r = Report("bpod-match")
    isolate_user_state()
    app = qt_app()                               # noqa: F841 — held
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_bpod_"))
    try:
        check_bpod_match(r, tmp)
        check_bpod_rollback(r, tmp)
        check_bpod_dialog(r, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


PARTS = {
    "paths": _part_paths,
    "split": _part_split,
    "chunks": _part_chunks,
    "bpod": _part_bpod,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
