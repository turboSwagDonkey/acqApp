"""HDF5Writer tests — retired with it, 2026-10-01. Not run (run_all doesn't
list archive/); kept so a restore brings its tests back too.
Was part of tests/test_saving.py ("paths" part: check_writer_refuses, and the
whole "chunks" part)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np
from _harness import Report, run_parts
from acqApp.archive.hdf5.hdf5_writer import HDF5Writer


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




PARTS = {"chunks": _part_chunks}

if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
