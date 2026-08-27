"""
Closed loop (phase 5): a rule that fires an output from a live signal.

Four layers, because the ways this can be wrong are at four different levels:

  1. `LoopRule` as a pure function. Every gate (hold, refractory, retrigger,
     max) exists to stop a bare threshold misbehaving on a real signal, so each
     is driven past a control that removes it — an ungated rule fires 3200
     times on the same trace where the real one fires 4. Without that control
     the whole file would keep passing if the gating quietly stopped working.
  2. `_EncoderBase.snapshot()` really is non-consuming, measured against the
     control that motivated it: a second `get_latest()` consumer halves what
     the display receives, and `snapshot()` costs it nothing.
  3. The reported and live wheel speeds are ~1 s apart. That is why the panel
     offers both, and it is measured here rather than asserted from the
     constants — a rule on the recorded speed acts a second late by design.
  4. The whole app: arm a rule, run a session, and check the puffer actually
     fired and the file recorded it.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_closed_loop.py
"""
from __future__ import annotations

import shutil
import sys
import time

from _harness import Report, isolate_user_state, pump, qt_app

RATE = 200.0            # synthetic trace rate, Hz
DUR  = 20.0             # synthetic trace length, s


def trace(t: float) -> float:
    """100 while 'running' (2–10 s and 12–20 s), 0 otherwise."""
    return 100.0 if (2.0 <= t < 10.0 or 12.0 <= t < 20.0) else 0.0


def run_rule(rule, values=trace) -> list[float]:
    """Feed the synthetic trace through a rule; return the fire times."""
    fires = []
    for i in range(int(RATE * DUR)):
        t = i / RATE
        if rule.update(values(t), t):
            fires.append(t)
    return fires


# ── 1. the rule, as a pure function ──────────────────────────────────────────

def check_rule(r: Report) -> None:
    from acqApp.closed_loop import LoopRule, LoopSettings

    base = dict(comparison="above", threshold=50.0, hold_s=0.25,
                refractory_s=5.0, retrigger=True)

    fires = run_rule(LoopRule(LoopSettings(**base)))
    r.check(fires == [2.25, 7.25, 12.25, 17.25],
            f"holds 0.25 s, then re-fires every 5 s while it lasts (got {fires})")

    # CONTROL: the same trace with every gate removed. If this does not fire
    # wildly, the trace is not exercising the gates and the check above is
    # vacuous.
    ungated = run_rule(LoopRule(LoopSettings(
        **{**base, "hold_s": 0.0, "refractory_s": 0.0})))
    r.check(len(ungated) == 3200,
            f"control: ungated, the same trace fires on every sample "
            f"({len(ungated)} times — this is what the gates prevent)")
    r.info(f"gating turns {len(ungated)} actuations into {len(fires)}")

    # hold: a longer hold delays the event by exactly that much
    slow = run_rule(LoopRule(LoopSettings(**{**base, "hold_s": 1.0})))
    r.check(slow and slow[0] == 3.0,
            f"a 1 s hold delays the first fire to 3.0 s (got {slow[:1]})")

    # retrigger off: one event per bout, however long the bout
    once = run_rule(LoopRule(LoopSettings(**{**base, "retrigger": False})))
    r.check(once == [2.25, 12.25],
            f"retrigger off fires once per bout, not once per refractory "
            f"(got {once})")

    # below: the complementary condition, on the same trace
    below = run_rule(LoopRule(LoopSettings(
        **{**base, "comparison": "below"})))
    r.check(below == [0.25, 10.25],
            f"'below' fires on the stationary stretches (got {below})")

    # max_fires: a hard session ceiling
    capped = run_rule(LoopRule(LoopSettings(**{**base, "max_fires": 2})))
    r.check(capped == [2.25, 7.25], f"max_fires caps the session (got {capped})")

    # A source that is not running must not read as zero and satisfy 'below'.
    quiet = run_rule(LoopRule(LoopSettings(**{**base, "comparison": "below"})),
                     values=lambda _t: None)
    r.check(quiet == [],
            f"no signal never fires, even for 'below' (got {quiet})")
    # CONTROL: the same rule on a real zero does fire — so the check above is
    # about None, not about the rule being dead.
    zeros = run_rule(LoopRule(LoopSettings(**{**base, "comparison": "below"})),
                     values=lambda _t: 0.0)
    r.check(len(zeros) > 0,
            f"control: a real 0.0 does satisfy 'below' ({len(zeros)} fires)")

    # Re-configuring mid-session must not hand back a fresh budget.
    rule = LoopRule(LoopSettings(**base))
    for i in range(int(RATE * 8.0)):
        rule.update(trace(i / RATE), i / RATE)
    n_before = rule.n_fires
    rule.configure(LoopSettings(**{**base, "threshold": 40.0}))
    r.check(rule.n_fires == n_before,
            "a settings change keeps the fire count (no fresh max_fires budget)")
    fired_now = rule.update(100.0, 8.0 + 1 / RATE)
    r.check(not fired_now,
            "…and does not let the next sample fire inside the refractory")


# ── 2 + 3. the wheel's snapshot: non-consuming, and two speeds ───────────────

def check_snapshot(r: Report) -> None:
    from acqApp.devices.wheel.acquisition import MockEncoderWorker

    w = MockEncoderWorker(4.912, 150.0)
    w.start()
    try:
        # Steady forward spin is 0.4 rev/s; in mm/s that is 0.4·π·150.
        steady = 0.4 * 3.14159265 * 150.0
        half = steady / 2.0
        t_live = t_rep = None
        t0 = time.perf_counter()
        while (el := time.perf_counter() - t0) < 2.2:
            snap = w.snapshot()
            if snap is not None:
                _v, speed, live, _at = snap
                if t_live is None and live > half:
                    t_live = el
                if t_rep is None and speed > half:
                    t_rep = el
            time.sleep(0.002)

        ok = (t_live is not None and t_rep is not None and t_live < t_rep)
        r.check(ok, f"the live speed leads the recorded one "
                    f"(live {t_live if t_live is None else round(t_live, 2)} s, "
                    f"recorded {t_rep if t_rep is None else round(t_rep, 2)} s)")
        if ok:
            r.info(f"a rule on the recorded speed acts "
                   f"{t_rep - t_live:.2f} s later than one on the live speed — "
                   f"which is why the panel offers both")

        # Non-consuming: measure what a ~200 Hz display tick receives.
        def display_window(seconds: float) -> tuple[int, int, int]:
            got_latest = got_snap = calls = 0
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                calls += 1
                if w.get_latest() is not None:
                    got_latest += 1
                if w.snapshot() is not None:
                    got_snap += 1
                time.sleep(0.005)
            return got_latest, got_snap, calls

        alone, snaps, calls = display_window(0.6)
        r.check(snaps >= calls - 2,
                f"snapshot() returns a value on every call ({snaps} of {calls}); "
                f"get_latest() only on new samples ({alone})")

        # CONTROL: a second *thread* pulling get_latest() — which is what the
        # loop would be if it consumed instead of watching. The display's share
        # collapses, and that is the bug snapshot() exists to avoid.
        import threading
        stop = threading.Event()

        def rival() -> None:
            while not stop.is_set():
                w.get_latest()
                time.sleep(0.001)

        th = threading.Thread(target=rival, daemon=True)
        th.start()
        try:
            shared, snaps2, calls2 = display_window(0.6)
        finally:
            stop.set()
            th.join(timeout=1.0)
        r.check(shared < alone / 2,
                f"control: a second get_latest() consumer starves the display "
                f"({shared} vs {alone} samples in the same window)")
        r.check(snaps2 >= calls2 - 2,
                f"…while snapshot() is unaffected by it "
                f"({snaps2} of {calls2}) — watching costs the display nothing")
    finally:
        w.stop()


# ── 4. the worker, against a source we control ───────────────────────────────

def check_worker(r: Report, app) -> None:
    from acqApp.closed_loop import ClosedLoopWorker, LoopSettings, SignalSource

    box = {"v": 0.0}
    src = SignalSource("test", "Test", "u",
                       lambda: (box["v"], time.perf_counter()))
    s = LoopSettings(source="test", comparison="above", threshold=50.0,
                     hold_s=0.05, refractory_s=0.15, target="puffer",
                     duration_s=0.07)

    fired: list[tuple] = []
    events: list[tuple] = []
    w = ClosedLoopWorker(src, s)
    w.fired.connect(lambda t, d, v: fired.append((t, d, v)))
    w.set_sink(events.append)

    # Disarmed first: the condition is met and must NOT fire.
    box["v"] = 100.0
    w.start()
    pump(app, 0.4)
    r.check(fired == [], f"disarmed, a met condition does not fire (got {fired})")
    latest = w.get_latest()
    r.check(latest is not None and latest[1] is True,
            f"…but the readout still shows the condition is met (got {latest})")

    # Arm it: now the same condition fires, repeatedly, paced by the refractory.
    #
    # Waited for, not slept through. This used to be `pump(app, 0.5)` against a
    # 0.15 s refractory — three fires' worth of room, and it failed about twice
    # in fifteen full-suite runs and never once on its own. The claim is that it
    # re-fires on the refractory, NOT that this machine schedules a thread
    # inside a particular half-second, so a busy machine was failing a claim the
    # test was not making. The deadline is what keeps it from hanging if the
    # worker really is dead.
    t_arm = time.perf_counter()
    w.set_armed(True)
    while len(fired) < 3 and time.perf_counter() - t_arm < 4.0:
        pump(app, 0.02)
    waited = time.perf_counter() - t_arm
    # stop() joins the thread, so `events` is final after it. `fired` is not:
    # the sink is called ON the worker's thread while `fired` crosses back as a
    # QUEUED signal, so it lags by however long the GUI thread takes to get
    # round to it — and the worker can fire once more in that gap. Comparing
    # the two counters before flushing the queue is a race, and it is the race
    # this test was actually losing (`one recorded event per fire`, 4 vs 3),
    # not the pacing it looked like.
    w.stop()
    pump(app, 0.2)
    n = len(fired)
    r.info(f"{n} fires in {waited:.2f}s "
           f"(hold {s.hold_s:g}s, refractory {s.refractory_s:g}s)")
    r.check(n >= 2, f"armed, it fires and re-fires (got {n} in {waited:.2f}s)")
    # …and the pacing itself, which the old fixed window only implied. A gap
    # SHORTER than the refractory is the bug that check was reaching for.
    # events are (value, at) — `at` is the sample's own instant, and that is
    # what the refractory is measured against, not the GUI hop after it.
    gaps = [b[1] - a[1] for a, b in zip(events, events[1:])]
    r.check(gaps and min(gaps) >= s.refractory_s - 0.02,
            f"…paced by the refractory, never faster "
            f"(gaps {[round(g, 3) for g in gaps]} vs {s.refractory_s:g}s)")
    r.check(all(f[0] == "puffer" and abs(f[1] - 0.07) < 1e-9 for f in fired),
            f"every fire carries the configured target and duration "
            f"(got {fired[:2]})")
    r.check(len(events) == n,
            f"one recorded event per fire ({len(events)} vs {n})")
    r.check(all(abs(e[0] - 100.0) < 1e-9 for e in events),
            "the recorded event carries the value that caused it")
    r.check(w.n_fires == n, f"the worker's count matches ({w.n_fires} vs {n})")


# ── 5. the whole app ─────────────────────────────────────────────────────────

def check_app(r: Report, app, tmp) -> int:
    import acqApp.main as M

    out = tmp / "loop_rec"
    enabled = {"wheel", "puffer", "closed_loop"}
    win = M.MainWindow(cam_info=None, mock=True, enabled=enabled,
                       cam_handle=None)
    mod = {m.key: m for m in win._modules}

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_subject.setText("loop")
    win._save_panel._ed_template.setText("{subject}_{date}_{time}")
    win._save_panel._on_edited()

    panel = mod["closed_loop"].panel
    srcs = [s.key for s in win.signal_sources()]
    r.check("wheel_speed_live" in srcs and "wheel_speed" in srcs,
            f"the wheel offers both speeds to the loop (got {srcs})")
    r.check(panel._cmb_source.count() == 2,
            f"…and they reach the panel (got {panel._cmb_source.count()})")
    # The DMD is not loaded in this subset, so it must not be offered as a
    # target: a rule aimed at it would fire onto the bus with nothing there.
    targets = [panel._cmb_target.itemData(i)
               for i in range(panel._cmb_target.count())]
    r.check(targets == ["puffer"],
            f"only loaded outputs are offered as targets (got {targets})")

    # A rule the mock wheel will satisfy: it spins at ~188 mm/s for 6 s.
    panel._cmb_source.setCurrentIndex(
        panel._cmb_source.findData("wheel_speed_live"))
    panel._spn_thr.setValue(50.0)
    panel._spn_hold.setValue(0.10)
    panel._spn_refr.setValue(0.40)
    panel._spn_dur.setValue(0.05)
    panel._btn_arm.setChecked(True)

    puffs: list[float] = []
    mod["puffer"].controller.puff_fired.connect(lambda _t, d: puffs.append(d))

    win._btn_run.setChecked(True)
    win._btn_rec.setChecked(True)
    path = win._rec_path
    if not r.check(path is not None, "recording started"):
        return 1

    for _ in range(40):
        win._display_tick()
        pump(app, 0.06)

    loop = mod["closed_loop"].worker
    n_fires, n_recorded = loop.n_fires, loop.recorded_fires
    win._btn_rec.setChecked(False)
    win._btn_run.setChecked(False)
    win.close()
    pump(app, 0.2)

    r.check(n_fires > 0, f"the rule fired during the session ({n_fires})")
    r.check(len(puffs) == n_fires,
            f"every fire reached the puffer through the trigger bus "
            f"({len(puffs)} puffs vs {n_fires} fires)")
    r.check(all(abs(d - 0.05) < 1e-9 for d in puffs),
            f"…carrying the rule's duration, not the puffer's default "
            f"(got {puffs[:3]})")

    import h5py
    with h5py.File(path, "r") as f:
        r.check("closed_loop" in f, f"/closed_loop recorded (streams "
                                    f"{sorted(f.keys())})")
        if "closed_loop" in f:
            vals = f["closed_loop"]["values"][:]
            ts = f["closed_loop"]["timestamps"][:]
            # Against `recorded_fires`, not `n_fires`: the rule runs under Live
            # view too, so it can fire before Record is pressed. Those actuated
            # the hardware but are in no file — which is why the two counters
            # exist and why loop_fires is the recorded one.
            r.check(len(vals) == n_recorded,
                    f"one entry per RECORDED fire ({len(vals)} vs {n_recorded})")
            r.check(n_fires >= n_recorded,
                    f"the session count is never below the recorded one "
                    f"({n_fires} vs {n_recorded})")
            r.check(all(v > 50.0 for v in vals),
                    "each entry is the speed that crossed the threshold")
            wts = f["wheel_speed"]["timestamps"][:]
            r.check(len(ts) > 0 and ts[0] >= wts[0],
                    "fires are stamped on the same session clock as the wheel")
        attrs = dict(f.attrs)
        for key in ("loop_armed", "loop_source", "loop_threshold",
                    "loop_target", "loop_comparison", "loop_fires",
                    "loop_fires_session"):
            r.check(key in attrs, f"metadata attr '{key}'")
        r.check(attrs.get("loop_armed") is True or attrs.get("loop_armed") == 1,
                f"the file records that it was armed (got "
                f"{attrs.get('loop_armed')!r}) — an unarmed rule and one that "
                f"never fired both leave /closed_loop empty")
        r.check(attrs.get("loop_source") == "wheel_speed_live",
                f"…and which signal it watched (got {attrs.get('loop_source')!r})")
        r.check(int(attrs.get("loop_fires", -1)) == len(vals),
                f"loop_fires equals the stream's own length — an attribute and "
                f"the data beside it cannot disagree "
                f"(got {attrs.get('loop_fires')} vs {len(vals)})")
        # `>=`, not `==`: the loop keeps evaluating until Live view stops, and
        # final_metadata() reads the counter after this test snapshotted it, so
        # the snapshot is a lower bound. (An equality here failed exactly once,
        # by one fire — which is the race, not a bug.)
        r.check(int(attrs.get("loop_fires_session", -1)) >= n_fires,
                f"loop_fires_session records the whole session, so it is at "
                f"least the mid-run snapshot "
                f"(got {attrs.get('loop_fires_session')} vs {n_fires})")
        r.check(int(attrs.get("loop_fires_session", -1))
                >= int(attrs.get("loop_fires", -1)),
                "…and never fewer than the fires that reached the file")
    return 0


def main() -> int:
    r = Report("closed-loop")
    tmp = isolate_user_state()
    sys.argv = ["main.py", "--mock"]
    app = qt_app()

    check_rule(r)
    check_snapshot(r)
    check_worker(r, app)
    check_app(r, app, tmp)

    shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
