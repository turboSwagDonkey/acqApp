"""The routine tests' shared fixtures: rig limits and a fake rig the
engine drives on a fake clock."""
from __future__ import annotations

from acqApp.routines.engine import Phase, RoutineEngine, RoutineHooks
from acqApp.routines.settings import RigLimits


FULL_RIG = RigLimits(x_um=(-5000.0, 5000.0), y_um=(-5000.0, 5000.0),
                     has_stage=True, has_dmd=True, has_frames=True,
                     has_puffer=True)
# FULL_RIG plus a Z axis; most rigs have none.
Z_RIG = RigLimits(x_um=FULL_RIG.x_um, y_um=FULL_RIG.y_um,
                  z_um=(-500.0, 500.0), has_stage=True, has_z=True,
                  has_dmd=True, has_frames=True, has_puffer=True)
DT = 0.01                      # the worker's tick, near enough


# ── the fake rig ──────────────────────────────────────────────────────────────

class FakeRig:
    """A stage, a projector, a puffer and a camera, on a clock the test drives.
    Every actuating call lands in `log`, which the ordering checks read."""

    def __init__(self, hz: float = 106.0, travel_s: float = 0.0) -> None:
        self.t = 0.0
        self.hz = hz
        self.travel_s = travel_s
        self.log: list[tuple] = []
        self.lit = False
        self.led_on = False
        self.frames_running = True
        self._arrive_at: float | None = None
        self.fail_move = False
        self.fail_light = False
        self.fail_begin = False        # begin_recording (the FILE) raises
        self.fail_arm = False          # arm_trigger raises
        self.puffed = 0
        self.begun: list = []
        self.ended: list = []
        # `gated`: External edge mode, no edge yet — the clock runs, no frames.
        # `_gated_total` is the frame-time lost to it.
        self.gated = False
        self.rearms = 0
        self._gated_total = 0.0
        # Opt-in `trigger_gate`: off, the engine uses its settle heuristic. On,
        # a re-arm lands `rearm_latency` after the ask (frames keep streaming
        # until then) and a `fire_trigger` before that is lost.
        self.report_gate = False
        self.rearm_latency = 0.0
        self.gate_seq = 0
        self._gate_frame0 = 0
        self._gate_at: float | None = None

    # clock + camera
    def now(self) -> float:
        return self.t

    def frames(self) -> int | None:
        if not self.frames_running:
            return None
        return int((self.t - self._gated_total) * self.hz)

    def advance(self, dt: float = DT) -> None:
        self.t += dt
        if self.gated:
            self._gated_total += dt
        if self._gate_at is not None and self.t >= self._gate_at:
            self._gate_at = None
            self.gated = True
            self.gate_seq += 1
            self._gate_frame0 = self.frames() or 0

    # external trigger
    def arm_trigger(self) -> None:
        """Re-gate, so the next edge shows up as frames starting again."""
        self.rearms += 1
        self.log.append(("arm_trigger",))
        if self.fail_arm:
            raise RuntimeError("camera could not be re-armed")
        if self.report_gate:
            self._gate_at = self.t + self.rearm_latency
        else:
            self.gated = True

    def trigger_gate(self) -> tuple[int, int] | None:
        if not self.report_gate:
            return None
        return self.gate_seq, (self.frames() or 0) - self._gate_frame0

    def fire_trigger(self) -> None:
        """The external edge arrives: frames start flowing again."""
        self.gated = False

    # stage
    def move(self, x, y, z=None) -> None:
        self.log.append(("move", x, y, z))
        if self.fail_move:
            raise RuntimeError("serial link down")
        self._arrive_at = self.t + self.travel_s

    def moving(self) -> bool:
        return self._arrive_at is not None and self.t < self._arrive_at

    def stop_motion(self) -> None:
        self.log.append(("stop_motion",))
        self._arrive_at = None

    # projector
    def set_pattern(self, path: str) -> None:
        self.log.append(("pattern", path))

    def light(self, on: bool) -> None:
        if self.fail_light and on:
            raise RuntimeError("ALP not ready")
        self.log.append(("light", bool(on)))
        self.lit = bool(on)

    def led(self, on: bool) -> None:
        self.log.append(("led", bool(on)))
        self.led_on = bool(on)

    # puffer
    def puff(self) -> None:
        self.log.append(("puff",))
        self.puffed += 1

    def prepare_recording(self, run) -> None:
        self.log.append(("prepare", run.region, run.cycle, run.start_index))

    # file boundaries
    def begin_recording(self, run) -> None:
        if self.fail_begin:
            raise RuntimeError("file could not open")
        self.log.append(("begin", run.region, run.cycle, run.attempt))
        self.begun.append(run)

    def end_recording(self, run) -> None:
        self.log.append(("end", run.region, run.cycle, run.attempt))
        self.ended.append(run)

    def hooks(self) -> RoutineHooks:
        return RoutineHooks(now=self.now, frames=self.frames, move=self.move,
                            moving=self.moving, stop_motion=self.stop_motion,
                            set_pattern=self.set_pattern, light=self.light,
                            led=self.led, puff=self.puff,
                            arm_trigger=self.arm_trigger,
                            trigger_gate=self.trigger_gate,
                            prepare_recording=self.prepare_recording,
                            begin_recording=self.begin_recording,
                            end_recording=self.end_recording,
                            log=lambda _m: None)


def drive(eng: RoutineEngine, rig: FakeRig, *, limit_s: float = 60.0,
          until=None) -> None:
    """Tick the engine on the fake clock until it stops, or `until(eng)`."""
    while rig.t < limit_s:
        if until is not None and until(eng):
            return
        if eng.phase in (Phase.DONE, Phase.IDLE, Phase.PAUSED):
            return
        rig.advance()
        eng.tick()


def ticks(eng: RoutineEngine, rig: FakeRig, n: int = 1) -> None:
    """Advance the fake clock and tick the engine `n` times, no stop test."""
    for _ in range(n):
        rig.advance()
        eng.tick()
