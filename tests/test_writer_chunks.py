"""
The writer's direct-chunk path — that it stores the frame it was handed.

`HDF5Writer.write()` hands HDF5 the frame's own buffer where a frame is exactly
one chunk (2026-08-24: 1304 -> 2696 MB/s at full frame, which is what took bin-1
recording from 53 % of its frames to 100 %). That write does **no conversion**:
hand it a frame whose dtype, shape or memory layout differs from the dataset's
and it stores those bytes anyway. The result is not an exception — it is a file
full of frames that open, plot and look like data.

So the guard (`_writable_chunk`) is the whole safety of the change, and every
check here is paired with a control that fails without it:

  * the direct path is really being taken (else the round trips are vacuous),
  * a frame the guard rejects still round-trips, via the slice fallback,
  * bypassing the guard really does break the file, in two different ways: a
    non-contiguous frame raises in the Recorder's writer thread (a lost
    session, not a lost frame), and a dtype mismatch writes a file that reads
    back as an ACCESS VIOLATION — no exception at write time, no exception at
    close, the process simply dies in whoever opens it months later,
  * compression and multi-frame chunks disable the path and still store data,
  * scalars, trimming and the NaN fill are unchanged.

Cheap and hardware-free: no QApplication, no device, ~2 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_writer_chunks.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from _harness import Report

from acqApp.acq.writer import HDF5Writer

import h5py

# Small enough to be fast, >8 MB so chunk_frames is 1 and the direct path is
# chosen — the same branch the 4432x2368 camera frame takes.
BIG = (2048, 2048)          # uint16 -> 8.4 MB
SMALL = (64, 64)            # uint16 -> 8 KB, so 16 frames share a chunk

# Run as a child by check_guard_rejects: half-fills a uint16 chunk with uint8
# bytes exactly as an unguarded direct write would, then reads it back. It is
# expected NOT to reach the last line.
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

    # The control for everything below: if this is False the round trips still
    # pass, but they are testing the old slice assignment and prove nothing.
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

    # Non-C-contiguous: a transpose has the right shape and dtype, and only its
    # memory layout is wrong.
    view = np.ascontiguousarray(base[0]).T
    r.check(not view.flags.c_contiguous, "the transposed frame really is "
                                         "non-contiguous (control)")
    p = tmp / "noncontig.h5"
    _write(p, [view])
    with h5py.File(p, "r") as f:
        r.check(np.array_equal(f["cam/frames"][0], view),
                "a non-contiguous frame still round-trips (slice fallback)")

    # The two rejected cases fail in DIFFERENT ways, and the difference is the
    # point. Contiguity: the buffer cannot even be taken, so bypassing the
    # guard raises inside the writer thread — a lost recording, not a lost
    # frame.
    try:
        memoryview(view).cast("B")
        raised = False
    except TypeError:
        raised = True
    r.check(raised, "a non-contiguous frame cannot be cast to a byte buffer at "
                    "all, so the guard prevents a raise (control)")

    # dtype: the bad one. uint8 bytes into a uint16 chunk fill half of it and
    # HDF5 accepts them — the write returns, the file closes, and the damage
    # only lands on whoever opens it. Measured 2026-08-25: reading that file
    # back does not raise, it takes the process out with an access violation
    # (0xC0000005). So this control runs in a CHILD process; in-process it
    # would abort the suite, which is the finding.
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
    # uint8 into a uint16 dataset: 8.4 MB of bytes where 16.8 MB belong. A raw
    # write would half-fill the chunk and leave the rest stale.
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
    # Multi-frame chunks: the offset arithmetic assumes one frame per chunk.
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

    # Compression: a raw write would store uncompressed bytes under a filter
    # that says they are deflated, and the file would not read back at all.
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


def main() -> int:
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


if __name__ == "__main__":
    sys.exit(main())
