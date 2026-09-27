#!/usr/bin/env python3
"""Grow a running fleet: rent and gate cards in the background, hand over only the ones that pass.

⛔ WHY THIS EXISTS. Capacity is what caps a fleet, not money. Measured 2026-09-27 at 45 requested of
each type with SECURE stock: RTX 4090 granted 1, RTX PRO 4500 SE granted 4, RTX PRO 6000 granted 38.
Two catalogue snapshots two minutes apart disagreed on 7 of 22 types (hazync#504), and the same card
went 26 available -> 0 -> 18 across one evening. You cannot survey and then decide; you can only be
trying at the moment stock appears. A run that rents once and never grows takes whatever existed in
the second it started and holds that for the whole session.

⛔ WHAT MAKES THIS TRACTABLE AT ALL. In mode 6 the aggregate LISTENS on 9110 and workers dial IN, and
seg-connect reconnects. A card that joins is a worker; the aggregate is never re-elected. So growth
is an orchestration problem, not a transport one.

⛔ IT MUST NEVER DELAY A BLOCK. Gating a recruit takes minutes -- the prover fetch alone measured 14
minutes across 29 cards on 2026-09-27 -- while the gap between tip blocks can be seconds. So every
expensive step happens on this thread, off the clock, and the session loop only ever DRAINS what is
already finished. A recruit that is not ready when a block starts simply misses that block.

⚠ A recruit that fails a gate is released here and never reaches a proof. That is the same standard
the run's own pre-clock gates hold, and for the same reason: a card that cannot prove costs the whole
fleet's wall clock, not just its own.
"""
import threading
import time


def wanted(*, have, pending, ready, target):
    """How many more to start renting right now.

    ⛔ PENDING AND READY BOTH COUNT. Counting only the cards already IN the fleet would start a new
    rental on every poll while the first was still being gated -- a 30 s poll against a multi-minute
    gate is 10+ pods rented for 1 slot, every one of them billing from the moment RunPod grants it.
    """
    return max(0, int(target) - int(have) - int(pending) - int(ready))


class Recruiter:
    """A background thread that keeps a fleet topped up toward `target`.

    Everything that touches the world is injected, so the policy above can be tested without
    renting, and the thread can be driven by a fake clock.

        rent_fn()        -> pod dict, or None when there is no capacity
        gate_fn(pod)     -> (ok, card, why)   the SAME gates the run applies before its own clock
        release_fn(pod)  -> None              called for every pod that does not pass
        have_fn()        -> how many cards the fleet currently holds
    """

    def __init__(self, *, target, have_fn, rent_fn, gate_fn, release_fn,
                 log=lambda _m: None, poll_s=30.0, sleep=time.sleep):
        self.target = int(target)
        self._have = have_fn
        self._rent = rent_fn
        self._gate = gate_fn
        self._release = release_fn
        self._log = log
        self._poll_s = float(poll_s)
        self._sleep = sleep
        self._ready = []                 # gated, waiting for the session loop to admit them
        self._pending = 0                # rented or being gated right now
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.rented = 0
        self.rejected = 0

    # ---- the session loop's side -------------------------------------------------------------
    def drain(self):
        """Every card that finished gating since the last call. Never blocks."""
        with self._lock:
            out, self._ready = self._ready, []
        return out

    def counts(self):
        with self._lock:
            return {"ready": len(self._ready), "pending": self._pending,
                    "rented": self.rented, "rejected": self.rejected}

    # ---- the thread's side -------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="recruiter", daemon=True)
        self._thread.start()
        return self

    def stop(self, join_s=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(join_s)
        # ⛔ ANYTHING GATED BUT NEVER ADMITTED IS STILL BILLING. The session ended without taking
        # these, so nobody else will release them.
        for pod in self.drain():
            try:
                self._release(pod.get("pod") or pod)
            except Exception:
                pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:      # noqa: BLE001 -- recruiting must never kill the run
                self._log(f"recruiter: {type(exc).__name__}: {exc}")
            self._sleep(self._poll_s)

    def _tick(self):
        with self._lock:
            need = wanted(have=self._have(), pending=self._pending,
                          ready=len(self._ready), target=self.target)
        if need <= 0:
            return
        pod = self._rent()
        if not pod:
            return                        # no capacity this minute; try again next poll
        with self._lock:
            self._pending += 1
            self.rented += 1
        self._log(f"recruiter: rented {pod.get('name')} — gating it off the clock")
        try:
            ok, card, why = self._gate(pod)
        except Exception as exc:          # noqa: BLE001
            ok, card, why = False, None, f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._pending -= 1
        if ok and card is not None:
            with self._lock:
                self._ready.append({"pod": pod, "card": card})
            self._log(f"recruiter: {pod.get('name')} passed every gate — waiting for a block boundary")
        else:
            with self._lock:
                self.rejected += 1
            self._log(f"recruiter: releasing {pod.get('name')} — {why}")
            try:
                self._release(pod)
            except Exception as exc:      # noqa: BLE001
                self._log(f"recruiter: ⛔ could not release {pod.get('name')}: {exc}")
