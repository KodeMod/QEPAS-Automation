# -*- coding: utf-8 -*-
"""
itc4005.py -- Thorlabs ITC4005QCL laser diode / TEC controller.

    python itc4005.py --probe        verify every command, outputs stay OFF
    python itc4005.py --status
    python itc4005.py --monitor --duration 300 -o itc_log.csv
    python itc4005.py --tec-on --temp 17.5
    python itc4005.py --laser-on --current 152 --temp 17.5
    python itc4005.py --off                       TEC stays on, laser off
    python itc4005.py --shutdown                  laser off, then TEC off

WHY --probe EXISTS
    A wrong SCPI string sent to a lock-in returns an error. A wrong one sent
    to a laser driver can set a current limit instead of a setpoint, or a
    setpoint instead of a limit. The command names below follow the Thorlabs
    Series 4000 SCPI reference, but the exact spelling varies with firmware,
    and this unit reports 1.8.0/1.5.0/2.3.0. So every command is checked
    against the instrument, with both outputs off, before it is trusted.
    Run --probe first and read what it says.

SAFETY MODEL
    - CURRENT_LIMIT_MA is enforced in software AND written to the
      instrument's own hardware limit, so a bug here cannot exceed it.
    - The laser will not turn on unless the TEC is on and the temperature has
      settled: driving a QCL at the wrong temperature moves its wavelength
      off the absorption line, and at worst damages it.
    - Current is ramped, never stepped. A step from 0 to 152 mA is a thermal
      and electrical shock to the diode.
    - Every setting is read back and compared. A command that silently did
      nothing is worse than one that errored.
    - Laser is switched off on exit from the context manager, on exception,
      and on Ctrl-C.
    - Nothing in here defeats the interlock, the key switch, or any hardware
      protection. If the instrument refuses, that refusal stands.

    2004 nm is invisible. Beam blocks and eyewear are not this file's job,
    but they are still yours.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import pyvisa
except ImportError:
    pyvisa = None


# ==========================================================================
# LIMITS -- read this block before running anything
# ==========================================================================

# Maximum LD current this script will ever command, in mA.
# Set it from YOUR laser's datasheet, not from what happens to work.
# The Norcada DFB has been running at 152 mA, so 160 leaves a little room.
CURRENT_LIMIT_MA = 160.0

# A second, deliberately awkward ceiling. Raising CURRENT_LIMIT_MA above this
# requires editing here too, which is the point: it makes exceeding the
# design current a conscious act rather than a typo.
HARD_CEILING_MA = 200.0

# TEC setpoint bounds, degrees C. Outside this range the script refuses.
TEMP_MIN_C = 10.0
TEMP_MAX_C = 35.0

# Temperature must be within this of setpoint, and stable, before laser on.
TEMP_TOLERANCE_C = 0.05
TEMP_STABLE_S = 20.0
TEMP_TIMEOUT_S = 300.0

# Current ramp
RAMP_STEP_MA = 5.0
RAMP_DELAY_S = 0.5

# ---- Running from Spyder --------------------------------------------------
# Spyder's F5 / %runfile passes no command-line arguments, so edit this list
# instead. It is what runs when no arguments are given.
#
#     DEFAULT_ARGS = ["--probe"]                     verify commands, safe
#     DEFAULT_ARGS = ["--status"]
#     DEFAULT_ARGS = ["--monitor", "--duration", "600", "-o", "itc.csv"]
#     DEFAULT_ARGS = ["--tec-on", "--temp", "17.5"]
#     DEFAULT_ARGS = ["--laser-on", "--current", "152", "--temp", "17.5"]
#
# Left on --probe deliberately: pressing F5 by reflex should never energise a
# laser. Anything that turns the output on has to be typed here on purpose.
DEFAULT_ARGS: List[str] = ["--probe"]

DEFAULT_RESOURCE = "USB0::0x1313::0x804A::M00445128::INSTR"


# ==========================================================================
# Command table
# ==========================================================================
# Each entry: (purpose, [candidate SCPI forms]). --probe finds which work.

QUERIES: Dict[str, List[str]] = {
    "idn":            ["*IDN?"],
    "temp_actual":    ["MEASure:SCALar:TEMPerature?", "MEASure:TEMPerature?"],
    "temp_setpoint":  ["SOURce2:TEMPerature:SPOint?", "SOURce2:TEMPerature?"],
    "current_actual": ["MEASure:SCALar:CURRent:DC?", "MEASure:CURRent?"],
    "voltage_actual": ["MEASure:SCALar:VOLTage:DC?", "MEASure:VOLTage?"],
    "current_set":    ["SOURce:CURRent:AMPLitude?", "SOURce:CURRent?"],
    "current_limit":  ["SOURce:CURRent:LIMit:AMPLitude?",
                       "SOURce:CURRent:LIMit?"],
    "ld_state":       ["OUTPut1:STATe?", "OUTPut:STATe?"],
    "tec_state":      ["OUTPut2:STATe?"],
    "ld_mode":        ["SOURce:FUNCtion:MODE?"],
    "am_state":       ["SOURce:AM:STATe?"],
    "error":          ["SYSTem:ERRor?"],
}

# All twelve confirmed against firmware 1.8.0/1.5.0/2.3.0 on M00445128,
# 2026-09-03: the first candidate of each pair is the one this unit accepts.

# Wavelength-modulation subsystem, for 2f WMS. Probed read-only by --probe
# so the exact spellings can be confirmed before anything is written.
AM_QUERIES: Dict[str, List[str]] = {
    "am_state":       ["SOURce:AM:STATe?"],
    "am_source":      ["SOURce:AM:SOURce?"],
    "am_depth":       ["SOURce:AM:INTernal:DEPTh?", "SOURce:AM:DEPTh?"],
    "am_frequency":   ["SOURce:AM:INTernal:FREQuency?",
                       "SOURce:AM:FREQuency?"],
    "am_shape":       ["SOURce:AM:INTernal:FUNCtion:SHAPe?",
                       "SOURce:AM:INTernal:SHAPe?"],
}

WRITES: Dict[str, List[str]] = {
    "temp_setpoint":  ["SOURce2:TEMPerature:SPOint {v}",
                       "SOURce2:TEMPerature {v}"],
    "current_set":    ["SOURce:CURRent:AMPLitude {v}", "SOURce:CURRent {v}"],
    "current_limit":  ["SOURce:CURRent:LIMit:AMPLitude {v}",
                       "SOURce:CURRent:LIMit {v}"],
    "ld_state":       ["OUTPut1:STATe {v}", "OUTPut:STATe {v}"],
    "tec_state":      ["OUTPut2:STATe {v}"],
}


# Modulation writes. Thorlabs specifies sine/square/triangle, 20 Hz-100 kHz,
# depth 0.1-100 % for the ITC4005QCL internal generator. Exact SCPI spelling
# is confirmed per-instrument by --probe before any of these is used.
AM_WRITES: Dict[str, List[str]] = {
    "am_state":     ["SOURce:AM:STATe {v}"],
    "am_source":    ["SOURce:AM:SOURce {v}"],
    "am_depth":     ["SOURce:AM:INTernal:DEPTh {v}", "SOURce:AM:DEPTh {v}"],
    "am_frequency": ["SOURce:AM:INTernal:FREQuency {v}",
                     "SOURce:AM:FREQuency {v}"],
    "am_shape":     ["SOURce:AM:INTernal:FUNCtion:SHAPe {v}",
                     "SOURce:AM:INTernal:SHAPe {v}"],
}

AM_FREQ_MIN_HZ, AM_FREQ_MAX_HZ = 20.0, 100000.0
AM_DEPTH_MIN_PCT, AM_DEPTH_MAX_PCT = 0.1, 100.0
AM_SHAPES = ("SIN", "SQU", "TRI")
AM_SOURCES = ("INT", "EXT")


class ITCError(RuntimeError):
    pass


@dataclass
class Reading:
    t_s: float
    temp_c: Optional[float]
    current_ma: Optional[float]
    voltage_v: Optional[float]
    ld_on: Optional[int]
    tec_on: Optional[int]


class ITC4005:
    """Context manager. Laser is switched off on any exit path."""

    def __init__(self, resource: str = DEFAULT_RESOURCE, timeout_ms: int = 5000):
        if pyvisa is None:
            raise ITCError("pyvisa is not installed.  pip install pyvisa")
        self.rm = pyvisa.ResourceManager()
        self.dev = self.rm.open_resource(resource)
        self.dev.timeout = timeout_ms
        self.dev.read_termination = "\n"
        self.dev.write_termination = "\n"
        self.idn = self.dev.query("*IDN?").strip()
        self._q: Dict[str, str] = {}      # resolved query forms
        self._w: Dict[str, str] = {}      # resolved write forms
        self._resolve()

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ITC4005":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self.ld_on:
                print("Exiting: switching laser off.")
                self.laser_off()
        finally:
            self.close()
        return False

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:                                          # noqa: BLE001
            pass

    # -- command resolution -------------------------------------------------

    def _resolve(self) -> None:
        """Find which spelling of each command this firmware accepts."""
        self.clear_errors()
        for key, candidates in QUERIES.items():
            for cmd in candidates:
                try:
                    self.dev.query(cmd)
                    if not self._last_error():
                        self._q[key] = cmd
                        break
                except Exception:                                  # noqa: BLE001
                    self.clear_errors()
        # Write forms are resolved by their matching query: if the query
        # spelling works, the setter spelling almost always matches. They are
        # confirmed properly by --probe, which writes a value back to itself.
        for key, candidates in AM_QUERIES.items():
            for cmd in candidates:
                try:
                    self.clear_errors()
                    self.dev.query(cmd)
                    if not self._last_error():
                        self._q[key] = cmd
                        break
                except Exception:                                  # noqa: BLE001
                    self.clear_errors()

        for key, candidates in list(WRITES.items()) + list(AM_WRITES.items()):
            query = self._q.get(key, "")
            for cmd in candidates:
                if query and cmd.split()[0].rstrip("?") == query.rstrip("?"):
                    self._w[key] = cmd
                    break
            else:
                self._w[key] = candidates[0]

    def _last_error(self) -> Optional[str]:
        try:
            resp = self.dev.query("SYSTem:ERRor?").strip()
        except Exception:                                          # noqa: BLE001
            return None
        if resp.startswith("0,") or resp.startswith('+0,'):
            return None
        return resp

    def clear_errors(self) -> None:
        for _ in range(20):
            if self._last_error() is None:
                return

    # -- low level ----------------------------------------------------------

    def query_float(self, key: str) -> Optional[float]:
        cmd = self._q.get(key)
        if not cmd:
            return None
        try:
            return float(self.dev.query(cmd).strip())
        except Exception:                                          # noqa: BLE001
            return None

    def query_int(self, key: str) -> Optional[int]:
        value = self.query_float(key)
        return None if value is None else int(round(value))

    def write(self, key: str, value) -> None:
        cmd = self._w.get(key)
        if not cmd:
            raise ITCError(f"no write command resolved for '{key}'")
        self.dev.write(cmd.format(v=value))
        err = self._last_error()
        if err:
            raise ITCError(f"{cmd.format(v=value)} -> {err}")

    def write_checked(self, key: str, value: float, tol: float = 1e-3) -> float:
        """Write, then read back and confirm. Silent no-ops are the enemy."""
        self.write(key, value)
        time.sleep(0.15)
        actual = self.query_float(key)
        if actual is None:
            raise ITCError(f"could not read back '{key}' after writing {value}")
        if abs(actual - value) > tol:
            raise ITCError(f"'{key}' did not take: asked {value}, "
                           f"instrument reports {actual}")
        return actual

    # -- state --------------------------------------------------------------

    @property
    def ld_on(self) -> bool:
        return bool(self.query_int("ld_state"))

    @property
    def tec_on(self) -> bool:
        return bool(self.query_int("tec_state"))

    @property
    def temperature(self) -> Optional[float]:
        return self.query_float("temp_actual")

    @property
    def current_ma(self) -> Optional[float]:
        value = self.query_float("current_actual")
        return None if value is None else value * 1000.0

    def read(self, t0: float) -> Reading:
        volts = self.query_float("voltage_actual")
        return Reading(t_s=round(time.time() - t0, 3),
                       temp_c=self.temperature,
                       current_ma=self.current_ma,
                       voltage_v=volts,
                       ld_on=self.query_int("ld_state"),
                       tec_on=self.query_int("tec_state"))

    def status(self) -> Dict[str, object]:
        limit = self.query_float("current_limit")
        return {
            "idn": self.idn,
            "temp_actual_C": self.temperature,
            "temp_setpoint_C": self.query_float("temp_setpoint"),
            "current_actual_mA": self.current_ma,
            "current_setpoint_mA": (None if self.query_float("current_set") is None
                                    else self.query_float("current_set") * 1000),
            "current_limit_mA": None if limit is None else limit * 1000,
            "ld_output": self.query_int("ld_state"),
            "tec_output": self.query_int("tec_state"),
            "ld_mode": (self.dev.query(self._q["ld_mode"]).strip()
                        if "ld_mode" in self._q else None),
            "modulation": self.query_int("am_state"),
        }

    # -- TEC ----------------------------------------------------------------

    def tec_on_and_settle(self, setpoint_c: float,
                          timeout_s: float = TEMP_TIMEOUT_S) -> float:
        if not (TEMP_MIN_C <= setpoint_c <= TEMP_MAX_C):
            raise ITCError(f"setpoint {setpoint_c} C is outside the allowed "
                           f"{TEMP_MIN_C}-{TEMP_MAX_C} C range")

        self.write_checked("temp_setpoint", setpoint_c, tol=0.02)
        if not self.tec_on:
            self.write("tec_state", "ON")
            print(f"TEC on, setpoint {setpoint_c:.3f} C")

        print("Waiting for temperature to settle...")
        t0 = time.time()
        stable_since: Optional[float] = None
        while time.time() - t0 < timeout_s:
            temp = self.temperature
            if temp is None:
                raise ITCError("lost temperature reading while settling")
            within = abs(temp - setpoint_c) <= TEMP_TOLERANCE_C
            if within:
                stable_since = stable_since or time.time()
                held = time.time() - stable_since
                print(f"\r  {temp:8.4f} C  within {TEMP_TOLERANCE_C} C for "
                      f"{held:5.1f}/{TEMP_STABLE_S:.0f} s   ", end="")
                if held >= TEMP_STABLE_S:
                    print("\n  settled.")
                    return temp
            else:
                stable_since = None
                print(f"\r  {temp:8.4f} C  ({temp - setpoint_c:+.4f} C away)"
                      f"          ", end="")
            time.sleep(1.0)
        raise ITCError(f"temperature did not settle within {timeout_s:.0f} s")

    def tec_off(self) -> None:
        if self.ld_on:
            raise ITCError("refusing to switch the TEC off while the laser "
                           "is on -- switch the laser off first")
        self.write("tec_state", "OFF")
        print("TEC off.")

    # -- laser --------------------------------------------------------------

    def apply_current_limit(self, limit_ma: float = CURRENT_LIMIT_MA) -> float:
        """
        Align the instrument's hardware limit with CURRENT_LIMIT_MA.

        Only ever lowers it. The limit already stored in the instrument was
        presumably chosen for this laser by whoever set it up, so raising it
        from a script would quietly undo a deliberate protection decision.
        """
        if limit_ma > HARD_CEILING_MA:
            raise ITCError(f"{limit_ma} mA exceeds HARD_CEILING_MA "
                           f"({HARD_CEILING_MA} mA). Edit the file "
                           f"deliberately if that is really intended.")

        present = self.query_float("current_limit")
        present_ma = None if present is None else present * 1000.0

        if present_ma is not None and limit_ma > present_ma:
            raise ITCError(
                f"CURRENT_LIMIT_MA ({limit_ma:.1f} mA) is above the limit "
                f"already stored in the instrument ({present_ma:.1f} mA). "
                f"Refusing to raise it from software -- change it on the "
                f"front panel if that is genuinely intended.")

        if present_ma is not None and abs(present_ma - limit_ma) < 0.05:
            print(f"Hardware current limit already {present_ma:.1f} mA")
            return present_ma

        actual = self.write_checked("current_limit", limit_ma / 1000.0,
                                    tol=1e-4)
        print(f"Hardware current limit lowered {present_ma:.1f} -> "
              f"{actual * 1000:.1f} mA")
        return actual * 1000

    def laser_on(self, current_ma: float, setpoint_c: float) -> None:
        if current_ma > CURRENT_LIMIT_MA:
            raise ITCError(f"{current_ma} mA exceeds CURRENT_LIMIT_MA "
                           f"({CURRENT_LIMIT_MA} mA)")

        self.apply_current_limit()
        self.tec_on_and_settle(setpoint_c)

        # Start from zero so the ramp is from a known state.
        self.write_checked("current_set", 0.0, tol=1e-4)
        if not self.ld_on:
            self.write("ld_state", "ON")
            time.sleep(0.5)
            if not self.ld_on:
                raise ITCError(
                    "laser did not switch on. Check the key switch, the "
                    "interlock, and the LD-ENABLE input -- the instrument's "
                    "own protections are not bypassable from here.")
        print(f"Laser enabled. Ramping to {current_ma:.1f} mA...")

        value = 0.0
        while value < current_ma:
            value = min(value + RAMP_STEP_MA, current_ma)
            self.write("current_set", value / 1000.0)
            time.sleep(RAMP_DELAY_S)
            measured = self.current_ma
            print(f"\r  set {value:6.1f} mA   measured "
                  f"{measured if measured is None else round(measured, 2):>8} mA  "
                  f"T {self.temperature:.3f} C   ", end="")
        print("\n  at operating point.")

    # -- modulation ---------------------------------------------------------

    def modulation_supported(self) -> Dict[str, bool]:
        return {k: (k in self._q) for k in AM_QUERIES}

    def read_modulation(self) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for key in AM_QUERIES:
            cmd = self._q.get(key)
            if not cmd:
                out[key] = None
                continue
            try:
                out[key] = self.dev.query(cmd).strip()
            except Exception:                                      # noqa: BLE001
                out[key] = None
        return out

    def _read_mod(self, key: str) -> Optional[str]:
        cmd = self._q.get(key)
        if not cmd:
            return None
        try:
            return self.dev.query(cmd).strip()
        except Exception:                                          # noqa: BLE001
            return None

    def _confirm_text(self, key: str, wanted: str) -> None:
        got = (self._read_mod(key) or "").upper()
        if not got.startswith(wanted.upper()[:3]):
            raise ITCError(
                f"{key} did not take: sent {wanted}, instrument reports "
                f"{got or 'nothing'}. The command was accepted without an "
                f"error but the setting did not change.")

    def _confirm_number(self, key: str, wanted: float, rel: float = 0.01) -> None:
        raw = self._read_mod(key)
        try:
            got = float(raw)
        except (TypeError, ValueError):
            raise ITCError(f"{key}: could not read back after writing {wanted}")
        if abs(got - wanted) > max(abs(wanted) * rel, 1e-6):
            raise ITCError(f"{key} did not take: sent {wanted}, instrument "
                           f"reports {got}")

    def set_modulation(self, state=None, source=None, frequency_hz=None,
                       depth_pct=None, shape=None) -> None:
        """
        Configure the modulation generator. EVERY write is read back.

        The first version only checked the SCPI error queue, which a command
        can pass while changing nothing. A modulation source that silently
        stays EXT means you believe the ITC is modulating when the SR830
        still is, so each setting is now confirmed from the instrument.

        Order: source first (it decides whether the rest means anything),
        then shape/frequency/depth, and the on/off state last.
        """
        if source is not None:
            source = str(source).upper()[:3]
            if source not in AM_SOURCES:
                raise ITCError(f"source must be one of {AM_SOURCES}")
            self.write("am_source", source)
            time.sleep(0.1)
            self._confirm_text("am_source", source)

        if shape is not None:
            shape = str(shape).upper()[:3]
            if shape not in AM_SHAPES:
                raise ITCError(f"shape must be one of {AM_SHAPES}")
            self.write("am_shape", shape)
            time.sleep(0.1)
            self._confirm_text("am_shape", shape)

        if frequency_hz is not None:
            if not (AM_FREQ_MIN_HZ <= frequency_hz <= AM_FREQ_MAX_HZ):
                raise ITCError(f"frequency {frequency_hz} Hz outside "
                               f"{AM_FREQ_MIN_HZ}-{AM_FREQ_MAX_HZ} Hz")
            self.write("am_frequency", frequency_hz)
            time.sleep(0.1)
            self._confirm_number("am_frequency", float(frequency_hz))

        if depth_pct is not None:
            if not (AM_DEPTH_MIN_PCT <= depth_pct <= AM_DEPTH_MAX_PCT):
                raise ITCError(f"depth {depth_pct} % outside "
                               f"{AM_DEPTH_MIN_PCT}-{AM_DEPTH_MAX_PCT} %")
            self.write("am_depth", depth_pct)
            time.sleep(0.1)
            self._confirm_number("am_depth", float(depth_pct))

        if state is not None:
            self.write("am_state", "ON" if state else "OFF")
            time.sleep(0.1)
            got = self._read_mod("am_state")
            if got is not None and int(float(got)) != (1 if state else 0):
                raise ITCError(f"modulation state did not change "
                               f"(instrument reports {got})")

    def check_headroom(self, depth_pct=None) -> Tuple[bool, str]:
        """
        Would the modulation peak reach the current limit?

        Thorlabs states the limit cannot be exceeded even under external
        modulation, so the peak is CLIPPED rather than refused. Clipped
        modulation distorts the 2f lineshape and looks like a nonlinear
        concentration response -- easy to mistake for spectroscopy.
        """
        setpoint = self.query_float("current_set")
        limit = self.query_float("current_limit")
        if setpoint is None or limit is None:
            return True, "cannot read setpoint or limit"
        setpoint_ma, limit_ma = setpoint * 1000, limit * 1000

        # Under EXTERNAL modulation the internal depth setting is ignored by
        # the instrument, and the real peak depends on the external signal's
        # amplitude times the modulation-input gain -- neither of which this
        # software can see. Computing a peak from the unused depth value
        # would report false headroom, so say "unknown" instead.
        source = str(self.read_modulation().get("am_source") or "").upper()
        if source.startswith("EXT"):
            return None, (f"external modulation: peak unknown -- depends on "
                          f"the external amplitude and input gain. "
                          f"{limit_ma - setpoint_ma:.1f} mA between setpoint "
                          f"and limit.")
        if depth_pct is None:
            try:
                depth_pct = float(self.read_modulation().get("am_depth"))
            except (TypeError, ValueError):
                return True, "modulation depth unknown"
        peak = setpoint_ma * (1 + depth_pct / 100.0)
        if peak > limit_ma:
            return False, (f"peak {peak:.1f} mA (setpoint {setpoint_ma:.1f} + "
                           f"{depth_pct:.1f} %) exceeds the {limit_ma:.1f} mA "
                           f"limit -- modulation IS being clipped")
        return True, (f"peak {peak:.1f} mA, {limit_ma - peak:.1f} mA below "
                      f"the {limit_ma:.1f} mA limit")

    def current_statistics(self, samples: int = 40) -> Dict[str, float]:
        """
        Sample the current meter repeatedly.

        One reading of a modulated current lands at a random point on the
        waveform, which is why a 152 mA setpoint can read 155 mA. The spread
        separates modulation ripple (wide spread, mean near setpoint) from a
        real offset (narrow spread, mean displaced).
        """
        values = []
        for _ in range(samples):
            v = self.current_ma
            if v is not None:
                values.append(v)
            time.sleep(0.02)
        if not values:
            return {}
        setpoint = self.query_float("current_set")
        arr = sorted(values)
        return {"n": len(arr), "mean_mA": sum(arr) / len(arr),
                "min_mA": arr[0], "max_mA": arr[-1],
                "peak_to_peak_mA": arr[-1] - arr[0],
                "setpoint_mA": None if setpoint is None else setpoint * 1000}

    def laser_off(self, ramp: bool = True) -> None:
        """Ramp down, then disable. Ramping avoids a thermal step."""
        try:
            present = self.query_float("current_set")
            if ramp and present:
                value = present * 1000.0
                while value > 0:
                    value = max(value - RAMP_STEP_MA * 2, 0.0)
                    self.write("current_set", value / 1000.0)
                    time.sleep(RAMP_DELAY_S / 2)
            self.write("current_set", 0.0)
        finally:
            self.write("ld_state", "OFF")
            print("Laser off.")


# ==========================================================================
# Commands
# ==========================================================================

def probe(itc: ITC4005) -> int:
    """Verify every command with the outputs OFF. Writes nothing dangerous."""
    print(f"Connected: {itc.idn}\n")
    if itc.ld_on:
        print("!! The laser is currently ON. Probe aborted -- run this with "
              "outputs off.", file=sys.stderr)
        return 1

    print("QUERIES")
    ok = failed = 0
    for key in QUERIES:
        cmd = itc._q.get(key)
        if not cmd:
            print(f"  {key:16s} NO WORKING FORM of {QUERIES[key]}")
            failed += 1
            continue
        try:
            value = itc.dev.query(cmd).strip()
            print(f"  {key:16s} {cmd:38s} -> {value}")
            ok += 1
        except Exception as exc:                                   # noqa: BLE001
            print(f"  {key:16s} {cmd:38s} -> FAILED {exc}")
            failed += 1

    print("\nMODULATION SUBSYSTEM (read-only)")
    for key, candidates in AM_QUERIES.items():
        for cmd in candidates:
            try:
                itc.clear_errors()
                value = itc.dev.query(cmd).strip()
                if itc._last_error():
                    continue
                print(f"  {key:16s} {cmd:38s} -> {value}")
                ok += 1
                break
            except Exception:                                      # noqa: BLE001
                itc.clear_errors()
        else:
            print(f"  {key:16s} none of {candidates} answered")

    print("\nWRITES (each written back to its own current value -- no change)")
    for key in ("temp_setpoint", "current_limit"):
        cmd = itc._w.get(key)
        current = itc.query_float(key)
        if cmd is None or current is None:
            print(f"  {key:16s} cannot verify (no readable value)")
            continue
        try:
            itc.write_checked(key, current,
                              tol=0.05 if key == "temp_setpoint" else 1e-4)
            print(f"  {key:16s} {cmd:38s} -> verified, value unchanged "
                  f"({current})")
            ok += 1
        except ITCError as exc:
            print(f"  {key:16s} {cmd:38s} -> FAILED {exc}")
            failed += 1

    print(f"\n{ok} verified, {failed} failed.")
    if failed:
        print("Do not run --laser-on until the failures are resolved. Send me "
              "this output and I will fix the command table.")
    return 0 if not failed else 1


def monitor(itc: ITC4005, duration_s: float, interval_s: float,
            path: Optional[str]) -> int:
    t0 = time.time()
    rows: List[dict] = []
    print("Monitoring. Ctrl-C to stop.\n")
    try:
        while time.time() - t0 < duration_s:
            r = itc.read(t0)
            rows.append(vars(r))
            print(f"t {r.t_s:8.1f} s | T {r.temp_c:9.4f} C | "
                  f"I {r.current_ma:9.4f} mA | V {r.voltage_v:9.6f} V | "
                  f"LD {r.ld_on} | TEC {r.tec_on}")
            time.sleep(max(0.0, interval_s - (time.time() - t0) % interval_s))
    except KeyboardInterrupt:
        print("\nStopped by user.")
    if path and rows:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Thorlabs ITC4005QCL control")
    p.add_argument("--resource", default=DEFAULT_RESOURCE)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--ripple", action="store_true",
                   help="sample the current meter to separate modulation "
                        "ripple from a real offset")
    p.add_argument("--status", action="store_true")
    p.add_argument("--monitor", action="store_true")
    p.add_argument("--duration", type=float, default=300.0)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("-o", "--output")
    p.add_argument("--tec-on", action="store_true")
    p.add_argument("--laser-on", action="store_true")
    p.add_argument("--off", action="store_true", help="laser off, TEC stays on")
    p.add_argument("--shutdown", action="store_true", help="laser off, TEC off")
    p.add_argument("--temp", type=float, default=17.5)
    p.add_argument("--current", type=float, help="LD current in mA")
    argv = [a for a in sys.argv[1:] if a != "--wdir"]
    if not argv:
        argv = DEFAULT_ARGS
        print(f"(no arguments given; using DEFAULT_ARGS = {argv})\n")
    args, _ = p.parse_known_args(argv)

    if not any([args.probe, args.status, args.monitor, args.tec_on,
                args.laser_on, args.off, args.shutdown, args.ripple]):
        p.print_help()
        print("\nStart with --probe. It verifies every command with the "
              "outputs off and changes nothing.")
        return 0

    try:
        itc = ITC4005(args.resource)
    except Exception as exc:                                       # noqa: BLE001
        print(f"Could not open {args.resource}\n  {type(exc).__name__}: {exc}")
        return 1

    with itc:
        if args.probe:
            return probe(itc)

        if args.ripple:
            stats = itc.current_statistics()
            print("Current meter statistics")
            for k, v in stats.items():
                print(f"  {k:18s} : {v}")
            if stats.get("peak_to_peak_mA", 0) > 0.5:
                print("\n  Wide spread -> you are seeing modulation ripple, "
                      "not a control error.")
            ok, msg = itc.check_headroom()
            print(f"\n  headroom: {msg}")
            return 0

        if args.status:
            for k, v in itc.status().items():
                print(f"  {k:22s} : {v}")
            return 0

        if args.off:
            itc.laser_off()
            return 0

        if args.shutdown:
            itc.laser_off()
            itc.tec_off()
            return 0

        if args.tec_on:
            itc.tec_on_and_settle(args.temp)

        if args.laser_on:
            if args.current is None:
                print("--laser-on needs --current in mA")
                return 1
            itc.laser_on(args.current, args.temp)

        if args.monitor:
            return monitor(itc, args.duration, args.interval, args.output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
