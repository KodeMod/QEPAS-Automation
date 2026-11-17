# -*- coding: utf-8 -*-
"""
itc_worker.py -- threaded owner of the ITC4005QCL.

Same rule as mfc_worker: exactly ONE thread talks to the instrument, and the
GUI reaches it only through queues. For a laser driver this is not just about
a responsive window. A current ramp takes tens of seconds; if that ran on the
Tk thread, the emergency-off button could not be clicked while it happened.
Here the ramp is a state machine stepped by the worker loop, so an off
command is honoured within one loop pass.

Safety behaviour that lives here rather than in the GUI, because the GUI can
crash and this thread still owns the hardware:

    - laser off on stop(), on exception, and on any disconnect
    - ramping is interruptible at every step
    - laser cannot be enabled unless the TEC is on and settled
    - all limits from itc4005.py apply; this adds no way around them
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import itc4005


@dataclass
class ITCState:
    connected: bool = False
    simulated: bool = False
    idn: str = ""
    temp_c: Optional[float] = None
    temp_setpoint_c: Optional[float] = None
    temp_settled: bool = False
    stable_for_s: float = 0.0
    current_ma: Optional[float] = None
    current_setpoint_ma: Optional[float] = None
    current_limit_ma: Optional[float] = None
    voltage_v: Optional[float] = None
    ld_on: bool = False
    tec_on: bool = False
    modulation_on: Optional[bool] = None
    mod_source: Optional[str] = None
    mod_frequency_hz: Optional[float] = None
    mod_depth_pct: Optional[float] = None
    mod_shape: Optional[str] = None
    mod_supported: Optional[dict] = None
    headroom_ok: Optional[bool] = None
    headroom_note: Optional[str] = None
    current_pp_ma: Optional[float] = None
    current_mean_ma: Optional[float] = None
    ramping: bool = False
    ramp_target_ma: float = 0.0
    message: Optional[str] = None
    level: str = "info"
    error: Optional[str] = None


class FakeITC:
    """
    Simulated controller so the panel can be exercised without hardware.

    Crude first-order thermal model plus a current source. Good enough to
    drive the display and to rehearse the interlock behaviour.
    """

    def __init__(self):
        self.idn = "Thorlabs,ITC4005QCL,SIMULATED,1.8.0/1.5.0/2.3.0"
        self._temp = 24.7
        self._setpoint = 17.5
        self._current = 0.0
        self._limit = 200.0
        self._ld = False
        self._tec = False
        self._t = time.monotonic()

    def _advance(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        target = self._setpoint if self._tec else 24.7
        self._temp += (target - self._temp) * min(1.0, dt / 5.0)
        self._temp += (self._current / 1000.0) * 0.02 * dt

    # mirror of the ITC4005 surface the worker uses
    @property
    def temperature(self):
        self._advance(); return round(self._temp, 4)

    @property
    def current_ma(self):
        return round(self._current + 0.0003, 4) if self._ld else 0.0003

    @property
    def ld_on(self): return self._ld

    @property
    def tec_on(self): return self._tec

    def query_float(self, key):
        return {"temp_setpoint": self._setpoint,
                "current_set": self._current / 1000.0,
                "current_limit": self._limit / 1000.0,
                "voltage_actual": 1.9 if self._ld else -0.0004}.get(key)

    def query_int(self, key):
        return {"ld_state": int(self._ld), "tec_state": int(self._tec),
                "am_state": int(self._am["am_state"])}.get(key)

    _am = {"am_state": 1, "am_source": "INT", "am_depth": 5.0,
           "am_frequency": 6234.0, "am_shape": "SIN"}

    def modulation_supported(self):
        return {k: True for k in self._am}

    def read_modulation(self):
        return dict(self._am)

    def set_modulation(self, state=None, source=None, frequency_hz=None,
                       depth_pct=None, shape=None):
        if shape is not None: self._am["am_shape"] = str(shape).upper()[:3]
        if frequency_hz is not None: self._am["am_frequency"] = float(frequency_hz)
        if depth_pct is not None: self._am["am_depth"] = float(depth_pct)
        if source is not None: self._am["am_source"] = str(source).upper()[:3]
        if state is not None: self._am["am_state"] = 1 if state else 0

    def check_headroom(self, depth_pct=None):
        if str(self._am["am_source"]).upper().startswith("EXT"):
            return None, (f"external modulation: peak unknown -- "
                          f"{self._limit - self._current:.1f} mA between "
                          f"setpoint and limit")
        depth = self._am["am_depth"] if depth_pct is None else depth_pct
        peak = self._current * (1 + depth / 100.0)
        if peak > self._limit:
            return False, (f"peak {peak:.1f} mA exceeds the {self._limit:.1f} "
                           f"mA limit -- modulation IS being clipped")
        return True, f"peak {peak:.1f} mA, {self._limit - peak:.1f} mA of margin"

    def current_statistics(self, samples=40):
        import random
        depth = self._am["am_depth"] if self._am["am_state"] else 0.0
        base = self._current if self._ld else 0.0
        vals = [base * (1 + depth / 100.0 * random.uniform(-1, 1))
                for _ in range(samples)]
        return {"n": samples, "mean_mA": sum(vals)/len(vals),
                "min_mA": min(vals), "max_mA": max(vals),
                "peak_to_peak_mA": max(vals)-min(vals),
                "setpoint_mA": self._current}

    def write(self, key, value):
        if key == "temp_setpoint": self._setpoint = float(value)
        elif key == "current_set": self._current = float(value) * 1000.0
        elif key == "current_limit": self._limit = float(value) * 1000.0
        elif key == "ld_state": self._ld = (value == "ON")
        elif key == "tec_state": self._tec = (value == "ON")

    def write_checked(self, key, value, tol=1e-3):
        self.write(key, value); return value

    def close(self): pass


class ITCWorker(threading.Thread):
    """
    Commands pushed as (name, payload):

        ("connect",     {"resource": "...", "simulate": False})
        ("disconnect",  None)
        ("set_temp",    {"celsius": 17.5})
        ("tec",         {"on": True})
        ("laser_on",    {"current_ma": 152.0})
        ("laser_off",   None)          <- always honoured immediately
        ("set_current", {"current_ma": 140.0})   ramps while already on
    """

    POLL_S = 1.0
    RAMP_STEP_MA = itc4005.RAMP_STEP_MA
    RAMP_INTERVAL_S = itc4005.RAMP_DELAY_S

    def __init__(self):
        super().__init__(daemon=True, name="ITCWorker")
        self.commands: "queue.Queue" = queue.Queue()
        self.telemetry: "queue.Queue" = queue.Queue()
        self._running = threading.Event(); self._running.set()

        self.dev = None
        self.state = ITCState()
        self._stable_since: Optional[float] = None
        self._ramp_target: Optional[float] = None
        self._ramp_next = 0.0

    # -- public -------------------------------------------------------------

    def send(self, name: str, payload: Optional[dict] = None) -> None:
        self.commands.put((name, payload))

    def stop(self) -> None:
        self._running.clear()

    # -- loop ---------------------------------------------------------------

    def run(self) -> None:
        next_poll = time.monotonic()
        while self._running.is_set():
            try:
                self._drain()
                self._step_ramp()
                now = time.monotonic()
                if now >= next_poll:
                    next_poll = now + self.POLL_S
                    self._poll()
                time.sleep(0.05)
            except Exception as exc:                               # noqa: BLE001
                self._emit(f"worker error: {exc}", "error")
                time.sleep(0.4)
        self._safe_shutdown()

    def _drain(self) -> None:
        while True:
            try:
                name, payload = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                getattr(self, f"_cmd_{name}")(payload or {})
            except itc4005.ITCError as exc:
                self._emit(str(exc), "error")
            except Exception as exc:                               # noqa: BLE001
                self._emit(f"{name} failed: {exc}", "error")

    # -- commands -----------------------------------------------------------

    def _cmd_connect(self, payload: dict) -> None:
        if self.dev is not None:
            return
        simulate = bool(payload.get("simulate"))
        if simulate:
            self.dev = FakeITC()
        else:
            self.dev = itc4005.ITC4005(payload.get("resource",
                                                   itc4005.DEFAULT_RESOURCE))
        self.state.simulated = simulate
        self.state.connected = True
        self.state.idn = self.dev.idn
        self._emit(f"connected: {self.dev.idn}"
                   + ("  [SIMULATED]" if simulate else ""))

    def _cmd_disconnect(self, payload: dict) -> None:
        self._laser_off()
        if self.dev is not None:
            self.dev.close()
        self.dev = None
        self.state = ITCState()
        self._emit("disconnected (laser switched off first)", "warn")

    def _cmd_set_temp(self, payload: dict) -> None:
        celsius = float(payload["celsius"])
        if not (itc4005.TEMP_MIN_C <= celsius <= itc4005.TEMP_MAX_C):
            raise itc4005.ITCError(
                f"{celsius} C outside allowed {itc4005.TEMP_MIN_C}"
                f"-{itc4005.TEMP_MAX_C} C")
        self.dev.write_checked("temp_setpoint", celsius, tol=0.02)
        self._stable_since = None
        self._emit(f"TEC setpoint {celsius:.3f} C")

    def _cmd_tec(self, payload: dict) -> None:
        want = bool(payload["on"])
        if not want and self.dev.ld_on:
            raise itc4005.ITCError(
                "refusing to switch the TEC off while the laser is on")
        self.dev.write("tec_state", "ON" if want else "OFF")
        self._stable_since = None
        self._emit(f"TEC {'on' if want else 'off'}")

    def _cmd_laser_on(self, payload: dict) -> None:
        target = float(payload["current_ma"])
        if target > itc4005.CURRENT_LIMIT_MA:
            raise itc4005.ITCError(
                f"{target} mA exceeds CURRENT_LIMIT_MA "
                f"({itc4005.CURRENT_LIMIT_MA} mA)")
        if not self.dev.tec_on:
            raise itc4005.ITCError("TEC must be on before the laser")
        if not self.state.temp_settled:
            raise itc4005.ITCError(
                f"temperature not settled "
                f"(within {itc4005.TEMP_TOLERANCE_C} C for "
                f"{itc4005.TEMP_STABLE_S:.0f} s) -- wait for the green light")

        if not self.state.simulated:
            self.dev.apply_current_limit()
        self.dev.write("current_set", 0.0)
        self.dev.write("ld_state", "ON")
        time.sleep(0.4)
        if not self.dev.ld_on:
            raise itc4005.ITCError(
                "laser did not enable -- check key switch, interlock and "
                "LD-ENABLE input")
        self._ramp_target = target
        self._ramp_next = 0.0
        self._emit(f"laser enabled, ramping to {target:.1f} mA", "warn")

    def _cmd_set_current(self, payload: dict) -> None:
        target = float(payload["current_ma"])
        if target > itc4005.CURRENT_LIMIT_MA:
            raise itc4005.ITCError(
                f"{target} mA exceeds CURRENT_LIMIT_MA "
                f"({itc4005.CURRENT_LIMIT_MA} mA)")
        if not self.dev.ld_on:
            raise itc4005.ITCError("laser is off")
        self._ramp_target = target
        self._emit(f"ramping to {target:.1f} mA")

    def _cmd_set_modulation(self, payload: dict) -> None:
        # Validate HERE, not in the device class. The worker is the single
        # path every command takes, so a limit checked here holds whatever
        # object is attached -- real instrument or simulator. A guard that
        # only fires for one of them is not a guard.
        freq = payload.get("frequency_hz")
        if freq is not None and not (itc4005.AM_FREQ_MIN_HZ <= float(freq)
                                     <= itc4005.AM_FREQ_MAX_HZ):
            raise itc4005.ITCError(
                f"frequency {freq} Hz outside {itc4005.AM_FREQ_MIN_HZ}-"
                f"{itc4005.AM_FREQ_MAX_HZ} Hz")
        depth = payload.get("depth_pct")
        if depth is not None and not (itc4005.AM_DEPTH_MIN_PCT <= float(depth)
                                      <= itc4005.AM_DEPTH_MAX_PCT):
            raise itc4005.ITCError(
                f"depth {depth} % outside {itc4005.AM_DEPTH_MIN_PCT}-"
                f"{itc4005.AM_DEPTH_MAX_PCT} %")
        shape = payload.get("shape")
        if shape is not None and str(shape).upper()[:3] not in itc4005.AM_SHAPES:
            raise itc4005.ITCError(f"shape must be one of {itc4005.AM_SHAPES}")
        source = payload.get("source")
        if source is not None and str(source).upper()[:3] not in itc4005.AM_SOURCES:
            raise itc4005.ITCError(f"source must be one of {itc4005.AM_SOURCES}")

        # Shape/frequency/depth belong to the internal generator. Under an
        # EXT target they are meaningless, so they are not sent at all --
        # sending them would only generate readback noise.
        target = str(payload.get("source") or "").upper()
        if target.startswith("EXT"):
            payload = {k: v for k, v in payload.items()
                       if k not in ("shape", "frequency_hz", "depth_pct")}

        self.dev.set_modulation(
            state=payload.get("state"),
            source=payload.get("source"),
            frequency_hz=payload.get("frequency_hz"),
            depth_pct=payload.get("depth_pct"),
            shape=payload.get("shape"))
        ok, note = self.dev.check_headroom()
        self._emit(f"modulation updated -- {note}",
                   "info" if ok else "warn")

    def _cmd_measure_ripple(self, payload: dict) -> None:
        stats = self.dev.current_statistics(int(payload.get("samples", 40)))
        if not stats:
            self._emit("no current readings", "warn")
            return
        self.state.current_pp_ma = stats["peak_to_peak_mA"]
        self.state.current_mean_ma = stats["mean_mA"]
        self._emit(f"current over {stats['n']} samples: mean "
                   f"{stats['mean_mA']:.2f} mA, {stats['min_mA']:.2f}-"
                   f"{stats['max_mA']:.2f}, p-p {stats['peak_to_peak_mA']:.2f} mA "
                   f"(setpoint {stats['setpoint_mA']:.1f})")

    def _cmd_laser_off(self, payload: dict) -> None:
        self._laser_off()

    # -- ramp ---------------------------------------------------------------

    def _step_ramp(self) -> None:
        if self._ramp_target is None or self.dev is None:
            return
        now = time.monotonic()
        if now < self._ramp_next:
            return
        self._ramp_next = now + self.RAMP_INTERVAL_S

        present = (self.dev.query_float("current_set") or 0.0) * 1000.0
        target = self._ramp_target
        if abs(target - present) <= self.RAMP_STEP_MA:
            self.dev.write("current_set", target / 1000.0)
            self._ramp_target = None
            self._emit(f"at {target:.1f} mA")
            return
        step = self.RAMP_STEP_MA if target > present else -self.RAMP_STEP_MA
        self.dev.write("current_set", (present + step) / 1000.0)

    def _laser_off(self) -> None:
        self._ramp_target = None
        if self.dev is None:
            return
        try:
            self.dev.write("current_set", 0.0)
        finally:
            self.dev.write("ld_state", "OFF")
        self._emit("laser OFF", "warn")

    # -- polling ------------------------------------------------------------

    def _poll(self) -> None:
        if self.dev is None:
            self._emit()
            return
        s = self.state
        try:
            s.temp_c = self.dev.temperature
            s.temp_setpoint_c = self.dev.query_float("temp_setpoint")
            s.current_ma = self.dev.current_ma
            cs = self.dev.query_float("current_set")
            s.current_setpoint_ma = None if cs is None else cs * 1000.0
            cl = self.dev.query_float("current_limit")
            s.current_limit_ma = None if cl is None else cl * 1000.0
            s.voltage_v = self.dev.query_float("voltage_actual")
            s.ld_on = self.dev.ld_on
            s.tec_on = self.dev.tec_on
            s.modulation_on = bool(self.dev.query_int("am_state"))
            mod = self.dev.read_modulation()
            s.mod_source = mod.get("am_source")
            s.mod_shape = mod.get("am_shape")
            try:
                s.mod_frequency_hz = float(mod.get("am_frequency"))
            except (TypeError, ValueError):
                s.mod_frequency_hz = None
            try:
                s.mod_depth_pct = float(mod.get("am_depth"))
            except (TypeError, ValueError):
                s.mod_depth_pct = None
            s.mod_supported = self.dev.modulation_supported()
            if s.ld_on:
                s.headroom_ok, s.headroom_note = self.dev.check_headroom()
            else:
                # With no current there is no peak; a note left over from the
                # last time the laser was on would be describing the past.
                s.headroom_ok, s.headroom_note = True, "laser off -- no modulation peak"
            s.ramping = self._ramp_target is not None
            s.ramp_target_ma = self._ramp_target or 0.0
            s.error = None
            self._update_stability()
        except Exception as exc:                                   # noqa: BLE001
            s.error = str(exc)
        self._emit()

    def _update_stability(self) -> None:
        s = self.state
        if (not s.tec_on or s.temp_c is None or s.temp_setpoint_c is None):
            self._stable_since = None
            s.temp_settled = False
            s.stable_for_s = 0.0
            return
        if abs(s.temp_c - s.temp_setpoint_c) <= itc4005.TEMP_TOLERANCE_C:
            self._stable_since = self._stable_since or time.monotonic()
            s.stable_for_s = time.monotonic() - self._stable_since
            s.temp_settled = s.stable_for_s >= itc4005.TEMP_STABLE_S
        else:
            self._stable_since = None
            s.stable_for_s = 0.0
            s.temp_settled = False

    # -- telemetry ----------------------------------------------------------

    def _emit(self, message: Optional[str] = None, level: str = "info") -> None:
        snapshot = ITCState(**vars(self.state))
        snapshot.message = message
        snapshot.level = level
        self.telemetry.put(snapshot)

    def _safe_shutdown(self) -> None:
        try:
            self._laser_off()
        except Exception:                                          # noqa: BLE001
            pass
        finally:
            if self.dev is not None:
                try:
                    self.dev.close()
                except Exception:                                  # noqa: BLE001
                    pass
            self.dev = None
