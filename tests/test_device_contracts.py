"""
`devices.py`'s protocols are honoured — devices (§5b A1) and window (§5b A4).

Those protocols are structural and this project ships no type checker, so one
that is never asserted catches nothing. See `devices.py` for why they exist.

  1. **Conformance** — every twin satisfies the protocol its adapter reads it
     through. Checked on the classes, never by constructing a real driver.
  2. **Parity** — both halves of a pair expose the same public API, bar an
     explicit allowlist. This is the one that catches a property added to the
     real class and forgotten on the mock.
  3. **The probes are gone** from the `modules/` adapters.
  4. **The window surface** — that `MainWindow` provides all of `ModuleHost`,
     and that no adapter reaches past it. A Protocol cannot see the second.

Each layer carries a control that must FAIL, so none can pass by being vacuous.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_device_contracts.py
"""
from __future__ import annotations

import sys

from _harness import Report, block_real_devices

block_real_devices()

from acqApp.devices import (            # noqa: E402
    CameraWorker, ClockedWorker, DeviceWorker, ExposureControl,
    OutputController, ProjectorController, RecordingOutput,
)


def has_all(cls, proto) -> list[str]:
    """Members of `proto` that `cls` does not provide.

    Checked against the CLASS, not an instance: constructing an `OrcaFireWorker`
    or a `DmdController` opens hardware, which a test must never do. Properties,
    plain methods and class attributes all answer to `hasattr` here.
    """
    wanted = [m for m in getattr(proto, "__protocol_attrs__", None)
              or _members(proto)]
    return sorted(m for m in wanted if not hasattr(cls, m))


def _members(proto) -> set[str]:
    """Protocol members, walking the protocol's own bases (not object's)."""
    out: set[str] = set()
    for klass in proto.__mro__:
        if klass.__name__ in ("Protocol", "Generic", "object"):
            continue
        out |= {n for n in vars(klass) if not n.startswith("_")}
        out |= {n for n in getattr(klass, "__annotations__", {})
                if not n.startswith("_")}
    return out


def public(cls, base) -> set[str]:
    """What `cls` adds to `base`, publicly."""
    return ({n for n in dir(cls) if not n.startswith("_")}
            - {n for n in dir(base) if not n.startswith("_")})


def main() -> int:
    r = Report("contracts")

    from PyQt6.QtCore import QObject
    from acqApp.acq.worker import PullWorker
    from acqApp.voltage_cam.acquisition import OrcaFireWorker, MockCameraWorker
    from acqApp.wheel.acquisition import EncoderWorker, MockEncoderWorker
    from acqApp.pupil_cam.acquisition import (PupilCameraWorker,
                                              MockPupilCameraWorker)
    from acqApp.pupil_cam.control import LedController, MockLedController
    from acqApp.puffer.control import PufferController, MockPufferController
    from acqApp.dmd.control import DmdController, MockDmdController
    from acqApp.stage.acquisition import StagePollWorker
    from acqApp.closed_loop import ClosedLoopWorker

    # ── 1. conformance ───────────────────────────────────────────────────────
    # Each entry is what the adapter actually reads that object through.
    CONFORM = [
        (OrcaFireWorker,          CameraWorker),
        (MockCameraWorker,        CameraWorker),
        (EncoderWorker,           ClockedWorker),
        (MockEncoderWorker,       ClockedWorker),
        (PupilCameraWorker,       ExposureControl),
        (MockPupilCameraWorker,   ExposureControl),
        (PupilCameraWorker,       DeviceWorker),
        (MockPupilCameraWorker,   DeviceWorker),
        (StagePollWorker,         DeviceWorker),
        (ClosedLoopWorker,        DeviceWorker),
        (PufferController,        RecordingOutput),
        (MockPufferController,    RecordingOutput),
        (DmdController,           ProjectorController),
        (MockDmdController,       ProjectorController),
    ]
    for cls, proto in CONFORM:
        missing = has_all(cls, proto)
        r.check(not missing,
                f"{cls.__name__} satisfies {proto.__name__}"
                + (f" — MISSING {missing}" if missing else ""))

    # The LED is an output that is deliberately NOT a RecordingOutput: its
    # on/off state is illumination, not an experimental event. `detach_sink`
    # asks exactly this question, so assert the answer rather than assume it.
    for cls in (LedController, MockLedController):
        r.check(bool(has_all(cls, RecordingOutput)),
                f"{cls.__name__} is deliberately NOT a RecordingOutput "
                f"(no set_sink to detach)")
        r.check(bool(has_all(cls, OutputController)),
                f"…nor an OutputController (it has no apply_settings)")

    # CONTROL: a stand-in missing one member must fail, or `has_all` is vacuous
    # and every check above passes for free.
    class AlmostAProjector:
        def apply_settings(self, s): ...
        def close(self): ...
        def set_sink(self, s): ...
        device_name = "fake"
        resolution = (1, 1)
        # on_pixels deliberately absent — this is the exact omission A1 names

    missing = has_all(AlmostAProjector, ProjectorController)
    r.check(missing == ["on_pixels"],
            f"control: a projector missing only on_pixels is caught "
            f"(got {missing})")

    # ── 2. parity ────────────────────────────────────────────────────────────
    # Differences that are deliberate. Anything else is drift and fails.
    PAIRS = [
        (OrcaFireWorker, MockCameraWorker, PullWorker,
         # Qt signals and a query that only means something against a device.
         {"drops_update", "timing_update", "achievable_fps"}, set()),
        (EncoderWorker, MockEncoderWorker, PullWorker,
         set(), {"RATE"}),                      # mock's synthetic sample rate
        (PupilCameraWorker, MockPupilCameraWorker, PullWorker,
         set(), {"W", "H"}),                    # mock's synthetic frame size
        (LedController, MockLedController, object, set(), set()),
        (PufferController, MockPufferController, QObject, set(), set()),
        (DmdController, MockDmdController, QObject, set(), set()),
    ]
    for real, mock, base, ok_real, ok_mock in PAIRS:
        real_only = public(real, base) - public(mock, base) - ok_real
        mock_only = public(mock, base) - public(real, base) - ok_mock
        r.check(not real_only and not mock_only,
                f"{real.__name__} / {mock.__name__} expose the same API"
                + (f" — real-only {sorted(real_only)}" if real_only else "")
                + (f" — mock-only {sorted(mock_only)}" if mock_only else ""))

    # CONTROL: the drift this test exists to catch. `skipped_frames` was on
    # OrcaFireWorker only until A1, and `cam_dropped_frames` read it through a
    # getattr default — so a real lossy run and a mock both filed 0.
    class MockCameraWithoutSkipped(MockCameraWorker):
        skipped_frames = property(lambda self: (_ for _ in ()).throw(
            AttributeError("skipped_frames")))
    drifted = public(OrcaFireWorker, PullWorker) - (
        public(MockCameraWorker, PullWorker) | {"drops_update", "timing_update",
                                                "achievable_fps"})
    r.check(not drifted, f"control target: skipped_frames is now on both "
                         f"(residual drift {sorted(drifted)})")
    r.check("skipped_frames" in public(MockCameraWorker, PullWorker),
            "the mock camera declares skipped_frames, so cam_dropped_frames "
            "is read rather than defaulted")

    # ── 3. the probes are actually gone from the adapters ────────────────────
    # Comments are stripped first: the code that replaced these probes explains
    # itself by naming them, and a search over raw text would read that prose as
    # the thing it warns about. (It did, on the first run of this test.)
    #
    # The whole `modules/` package is scanned, not one file — A5 split it into
    # one file per instrument, and a scan pinned to a single path would have
    # gone quietly vacuous the moment the DMD's adapter moved out of it.
    import io
    import tokenize
    from pathlib import Path

    def strip(raw: str, *kinds: int) -> str:
        return tokenize.untokenize(
            tok for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
            if tok.type not in kinds)

    pkg = Path(__file__).resolve().parents[1] / "modules"
    sources = sorted(pkg.glob("*.py"))
    raw = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    # Stripped per file: `untokenize` works from token positions, so feeding it
    # a concatenation of files would hand it coordinates from several of them.
    text = "\n".join(strip(p.read_text(encoding="utf-8"), tokenize.COMMENT)
                     for p in sources)
    r.check(len(sources) >= 8 and "device_name" in text
            and len(text) > len(raw) / 2,
            f"control: the comment-stripped source is still real code "
            f"({len(sources)} files)")
    for needle, why in (
        ('getattr(c, "device_name"', "the DMD device name"),
        ('getattr(c, "on_pixels"', "the DMD's on-pixel count"),
        ('getattr(self.worker, "timestamp_source"', "a worker's timebase"),
        ('getattr(self.worker, "skipped_frames"', "camera-side frame drops"),
        ('hasattr(self.worker, "set_exposure")', "pupil exposure"),
    ):
        r.check(needle not in text,
                f"the adapters no longer guess {why} with a default")

    # ── 4. the window surface stays narrow (A4) ──────────────────────────────
    # The protocols above point down (module -> device); `ModuleHost` points up
    # (module -> window), and is the reason main.py and modules/ stay
    # independently readable. It needs BOTH halves checked, because each misses
    # what the other catches: conformance alone allows an adapter to reach past
    # the surface into `win._save_panel`, and the source scan alone allows the
    # window to drop a service every adapter still calls.
    import re
    from acqApp.devices import ModuleHost

    # Safe to import: block_real_devices() stubbed pylablib, so main.py's
    # DCAM pre-init falls into its own "no camera" branch. No QApplication is
    # built at import.
    from acqApp.main import MainWindow

    missing = has_all(MainWindow, ModuleHost)
    r.check(not missing,
            "MainWindow provides the whole ModuleHost surface"
            + (f" — MISSING {missing}" if missing else ""))

    declared = _members(ModuleHost)
    # Docstrings go too, not just comments: the surface is now documented by
    # naming it, and the package header says "every `self.win.X`" in prose.
    # Section 3 keeps its strings — its needles are code *containing* string
    # literals, and stripping those would make it pass by finding nothing.
    used = set(re.findall(
        r"self\.win\.(\w+)",
        "\n".join(strip(p.read_text(encoding="utf-8"),
                        tokenize.COMMENT, tokenize.STRING) for p in sources)))
    extra = sorted(used - declared)
    r.check(not extra,
            f"every self.win.X in modules/ is declared on ModuleHost"
            + (f" — undeclared {extra}" if extra else ""))

    # CONTROL 1: the scan has to actually find the calls. A regex that matched
    # nothing would pass the check above for free, forever.
    r.check(len(used) >= 7 and "status" in used,
            f"control: the scan found the adapters' calls ({len(used)} distinct)")

    # CONTROL 2: and it has to reject one. This is the drift A4 exists to stop —
    # an adapter helping itself to a private widget on the window.
    rogue = set(re.findall(r"self\.win\.(\w+)",
                           "self.win._save_panel.setEnabled(False)")) - declared
    r.check(rogue == {"_save_panel"},
            f"control: a reach past the surface is caught (got {sorted(rogue)})")

    # CONTROL 3: conformance must fail on an incomplete host. `signal_sources`
    # is the newest member and the exact shape of the drift that prompted A4 —
    # two services added to the window with only a docstring to notice.
    class AlmostAHost:
        sync = None
        cam_handle = None
        def status(self, m): ...
        def add_dock(self, t, w, a, accent="sync"): ...
        def register_pg_view(self, v): ...
        def set_expected_rate(self, mbps): ...
        def on_worker_error(self, m): ...
        def module_keys(self): return []
        # signal_sources deliberately absent

    r.check(has_all(AlmostAHost, ModuleHost) == ["signal_sources"],
            f"control: a host missing only signal_sources is caught "
            f"(got {has_all(AlmostAHost, ModuleHost)})")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
