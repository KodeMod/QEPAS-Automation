# -*- coding: utf-8 -*-
"""
Serial worker for the MFC GUI.

DESIGN NOTE -- why this file exists separately from the GUI:

An RS-485 bus is a single shared resource. Two threads issuing Modbus
transactions at once interleave their frames and both get garbage, or worse,
one reads the other's reply and believes it. So exactly ONE thread touches
viapi: this one. The GUI never calls a device method directly.

Communication is by queue in both directions:

    GUI  --command queue-->  worker  --telemetry queue-->  GUI

The GUI thread therefore never blocks on serial I/O, which is what keeps the
window responsive while a 3-minute sequence step is running.

A second consequence worth stating: because the worker owns the devices, it
is also the only thing that can guarantee flows are zeroed. Shutdown, abort,
and error paths all route through here.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

try:
    import mfc_control as core
except ImportError as exc:                                         # noqa: BLE001
    raise ImportError(
        "mfc_control.py must sit in the same folder as this file. "
        f"Original error: {exc}"
    ) from exc


# ==========================================================================
# Messages
# ==========================================================================

@dataclass
class ChannelState:
    """Everything the GUI needs to draw one channel."""
    key: str
    label: str
    address: int
    online: bool = False
    setpoint_sccm: float = 0.0
    measured_sccm: Optional[float] = None
    full_scale_sccm: float = 2000.0
    temperature_c: Optional[float] = None
    pressure: Optional[float] = None
    pressure_unit: Optional[str] = None
    has_pressure_sensor: bool = False
    pressure_min: Optional[float] = None
    pressure_max: Optional[float] = None
    pressure_setpoint: Optional[float] = None
    pressure_mode: Optional[int] = None
    pressure_type: Optional[int] = None
    pressure_flow_limit_sccm: Optional[float] = None
    serial: Optional[str] = None
    tag: Optional[str] = None
    read_only: bool = False
    error: Optional[str] = None

    @property
    def percent_of_fs(self) -> float:
        if not self.full_scale_sccm or self.measured_sccm is None:
            return 0.0
        return 100.0 * self.measured_sccm / self.full_scale_sccm

    @property
    def deviation_sccm(self) -> Optional[float]:
        """Measured minus commanded. None when there is nothing to compare."""
        if self.measured_sccm is None:
            return None
        return self.measured_sccm - self.setpoint_sccm


@dataclass
class Telemetry:
    """One poll cycle's worth of state."""
    timestamp: float
    connected: bool
    channels: Dict[str, ChannelState] = field(default_factory=dict)
    message: Optional[str] = None
    level: str = "info"                 # info | warn | error
    sequence_active: bool = False
    sequence_step: int = 0
    sequence_total: int = 0
    sequence_label: str = ""
    sequence_remaining_s: float = 0.0


@dataclass
class SequenceStep:
    percent_co2: float
    hold_s: float
    total_sccm: float
    flush_s: float = 0.0
    tag: str = ""


# ==========================================================================
# Worker
# ==========================================================================

class DeviceWorker(threading.Thread):
    """
    Owns the serial link. Runs until stop() is called.

    Commands are (name, payload) tuples pushed onto self.commands:
        ("connect", {"port": 4, "simulate": False})
        ("disconnect", None)
        ("setpoint", {"channel": "co2", "sccm": 85.5})
        ("blend", {"percent": 45.0, "total_sccm": 190.0})
        ("flush", {"total_sccm": 190.0})
        ("stop_all", None)                      <- always honoured immediately
        ("run_sequence", {"steps": [...]})
        ("abort_sequence", None)
    """

    POLL_INTERVAL_S = 1.0
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, telemetry_callback: Optional[Callable] = None):
        super().__init__(daemon=True, name="MFCDeviceWorker")
        self.commands: "queue.Queue" = queue.Queue()
        self.telemetry: "queue.Queue" = queue.Queue()
        self._telemetry_callback = telemetry_callback

        self._running = threading.Event()
        self._running.set()

        self._devices: Dict[str, core.MFC] = {}
        self._states: Dict[str, ChannelState] = {}
        self._connected = False
        self._simulate = False
        self._error_streak = 0

        # Sequence state, owned by this thread only.
        self._sequence: List[SequenceStep] = []
        self._seq_index = 0
        self._seq_phase = ""        # "flush" | "hold" | ""
        self._seq_deadline = 0.0
        self._seq_active = False

        self._csv_rows: List[dict] = []
        self._sim: Dict[str, dict] = {}

    # -- public, called from the GUI thread ---------------------------------

    def send(self, name: str, payload: Optional[dict] = None) -> None:
        self.commands.put((name, payload))

    def stop(self) -> None:
        """Ask the worker to shut down. It zeroes flows on the way out."""
        self._running.clear()

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        next_poll = time.monotonic()
        while self._running.is_set():
            try:
                self._drain_commands()
                self._advance_sequence()

                now = time.monotonic()
                if now >= next_poll:
                    next_poll = now + self.POLL_INTERVAL_S
                    self._poll()

                time.sleep(0.05)
            except Exception as exc:                               # noqa: BLE001
                # A worker thread that dies silently leaves gas flowing with a
                # frozen display. Report and keep going.
                self._emit(message=f"Worker error: {exc}", level="error")
                time.sleep(0.5)

        self._safe_shutdown()

    # -- commands -----------------------------------------------------------

    def _drain_commands(self) -> None:
        while True:
            try:
                name, payload = self.commands.get_nowait()
            except queue.Empty:
                return

            payload = payload or {}
            try:
                handler = getattr(self, f"_cmd_{name}", None)
                if handler is None:
                    self._emit(message=f"Unknown command '{name}'", level="warn")
                else:
                    handler(payload)
            except Exception as exc:                               # noqa: BLE001
                self._emit(message=f"{name} failed: {exc}", level="error")

    def _cmd_connect(self, payload: dict) -> None:
        if self._connected:
            return
        self._simulate = bool(payload.get("simulate", False))
        port = payload.get("port")
        if port is not None:
            core.PORT = int(port)

        self._devices.clear()
        self._states.clear()

        for key, cfg in core.CHANNELS.items():
            device = core.MFC(cfg, dry_run=self._simulate)
            state = ChannelState(
                key=key, label=cfg.label, address=cfg.slave,
                full_scale_sccm=core.ln_to_sccm(cfg.full_scale_ln),
                read_only=cfg.read_only,
            )
            try:
                device.connect()
                state.online = True
                if not self._simulate:
                    state.serial = str(device.read_any(core.SERIAL_KEYS))
                    state.tag = str(device.read_any(core.NAME_KEYS))
                    fs = device.read_any(core.FULLSCALE_KEYS)
                    if fs is not None:
                        state.full_scale_sccm = core.ln_to_sccm(float(fs))
                else:
                    state.serial = str(cfg.expected_serial)
                    state.tag = "simulated"
            except Exception as exc:                               # noqa: BLE001
                state.online = False
                state.error = str(exc)

            self._devices[key] = device
            self._states[key] = state

        self._connected = any(s.online for s in self._states.values())
        online = sum(1 for s in self._states.values() if s.online)
        self._emit(
            message=(f"Connected: {online}/{len(self._states)} devices on COM"
                     f"{core.PORT}" + (" (SIMULATION)" if self._simulate else "")),
            level="info" if self._connected else "error",
        )

    def _cmd_disconnect(self, payload: dict) -> None:
        self._zero_all()
        for device in self._devices.values():
            try:
                device.disconnect()
            except Exception:                                      # noqa: BLE001
                pass
        self._devices.clear()
        for state in self._states.values():
            state.online = False
        self._connected = False
        self._seq_active = False
        self._emit(message="Disconnected. Setpoints were zeroed first.")

    def _cmd_setpoint(self, payload: dict) -> None:
        key = payload["channel"]
        sccm = float(payload["sccm"])
        self._write(key, sccm)

    def _cmd_blend(self, payload: dict) -> None:
        percent = float(payload["percent"])
        total = float(payload["total_sccm"])
        flows = self._split(percent, total)
        # Balance gas first so the cell never sees a CO2-rich slug.
        self._write("n2", flows["n2"])
        self._write("co2", flows["co2"])
        self._emit(message=(f"Blend {percent:.2f} % CO2 at {total:.0f} SCCM: "
                            f"N2 {flows['n2']:.2f} + CO2 {flows['co2']:.2f}"))

    def _cmd_blend3(self, payload: dict) -> None:
        """
        Write three explicit line flows: dry N2, humid N2, CO2.

        The flows arrive already computed. The two-gas _cmd_blend above is
        left exactly as it was, so the existing CO2 series behaves
        identically -- this is an additional path, not a replacement.

        Order matters: the diluents go first and CO2 last, so the cell never
        briefly sees a CO2-rich slug while the other lines are still moving.
        """
        dry = float(payload.get("dry_sccm", 0.0))
        wet = float(payload.get("wet_sccm", 0.0))
        co2 = float(payload.get("co2_sccm", 0.0))
        self._write("n2", dry)
        self._write("monitor", wet)
        self._write("co2", co2)
        self._emit(message=(f"3-line blend: dry N2 {dry:.1f} + wet N2 "
                            f"{wet:.1f} + CO2 {co2:.1f} = "
                            f"{dry + wet + co2:.1f} SCCM"))

    def _cmd_flush(self, payload: dict) -> None:
        total = float(payload["total_sccm"])
        self._write("co2", 0.0)
        self._write("n2", total)
        self._emit(message=f"Flushing with N2 at {total:.0f} SCCM")

    # -- pressure -----------------------------------------------------------

    def _cmd_pressure_setpoint(self, payload: dict) -> None:
        key, value = payload["channel"], float(payload["value"])
        state = self._states.get(key)
        if state is None or not state.online:
            raise RuntimeError(f"{key} is not connected")
        if state.read_only:
            raise RuntimeError(f"{state.label} is read-only")
        if self._simulate:
            sim = self._sim.setdefault(key, {})
            lo, hi = sim.get("scale_min", 0.0), sim.get("scale_max", 0.0)
            if not sim.get("has_sensor") or not (lo <= value <= hi):
                raise RuntimeError(f"{state.label}: {value} outside "
                                   f"sensor range {lo}-{hi}")
            sim["setpoint"] = value
        else:
            self._devices[key].write_pressure_setpoint(value)
        self._emit(message=f"{state.label}: pressure setpoint {value} "
                           f"{state.pressure_unit or ''}")

    def _cmd_pressure_flow_limit(self, payload: dict) -> None:
        key, sccm = payload["channel"], float(payload["sccm"])
        state = self._states[key]
        if self._simulate:
            self._sim.setdefault(key, {})["flow_limit"] = core.sccm_to_ln(sccm)
        else:
            self._devices[key].write_pressure_flow_limit(sccm)
        self._emit(message=f"{state.label}: pressure-control flow limit "
                           f"{sccm:.1f} SCCM")

    def _cmd_pressure_mode(self, payload: dict) -> None:
        key, mode = payload["channel"], int(payload["mode"])
        state = self._states[key]
        if self._simulate:
            self._sim.setdefault(key, {})["mode"] = mode
        else:
            self._devices[key].write_pressure_mode(mode)
        name = core.PRESSURE_MODE_NAMES.get(mode, "meaning unknown")
        self._emit(message=f"{state.label}: pressure control mode -> {mode} "
                           f"({name})", level="warn")

    def _apply_pressure(self, state, info: dict) -> None:
        def f(v):
            try:
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None
        state.has_pressure_sensor = bool(info.get("has_sensor"))
        state.pressure = f(info.get("value"))
        state.pressure_unit = info.get("unit") or state.pressure_unit
        state.pressure_min = f(info.get("scale_min"))
        state.pressure_max = f(info.get("scale_max"))
        state.pressure_setpoint = f(info.get("setpoint"))
        mode, ptype = f(info.get("mode")), f(info.get("type"))
        state.pressure_mode = None if mode is None else int(mode)
        state.pressure_type = None if ptype is None else int(ptype)
        limit = f(info.get("flow_limit"))
        state.pressure_flow_limit_sccm = (None if limit is None
                                          else core.ln_to_sccm(limit))

    def _sim_pressure(self, key: str, state) -> dict:
        """
        Simulated pressure sensor on MFC3 only, so the controls can be
        rehearsed. MFC1 and MFC2 simulate the real situation: no sensor.
        """
        sim = self._sim.setdefault(key, {})
        if key != "monitor":
            return {"has_sensor": False, "scale_min": 0.0, "scale_max": 0.0,
                    "value": 0.0, "unit": "bar a"}
        sim.setdefault("has_sensor", True)
        sim.setdefault("scale_min", 0.0)
        sim.setdefault("scale_max", 2.0)
        sim.setdefault("setpoint", 1.0)
        sim.setdefault("mode", 0)
        sim.setdefault("type", 0)
        sim.setdefault("flow_limit", 1.0)
        sim.setdefault("value", 0.96)
        target = sim["setpoint"] if sim["mode"] else 0.96
        sim["value"] += (target - sim["value"]) * 0.3
        return {"has_sensor": True, "value": round(sim["value"], 4),
                "unit": "bar a", "scale_min": sim["scale_min"],
                "scale_max": sim["scale_max"], "setpoint": sim["setpoint"],
                "mode": sim["mode"], "type": sim["type"],
                "flow_limit": sim["flow_limit"]}

    def _cmd_stop_all(self, payload: dict) -> None:
        self._seq_active = False
        self._sequence = []
        self._zero_all()
        self._emit(message="ALL STOP -- every setpoint set to zero", level="warn")

    def _cmd_run_sequence(self, payload: dict) -> None:
        self._sequence = [SequenceStep(**step) for step in payload["steps"]]
        self._seq_index = 0
        self._seq_phase = ""
        self._seq_deadline = 0.0
        self._seq_active = True
        self._csv_rows = []
        self._emit(message=f"Sequence started: {len(self._sequence)} steps")

    def _cmd_abort_sequence(self, payload: dict) -> None:
        self._seq_active = False
        self._sequence = []
        self._zero_all()
        self._emit(message="Sequence aborted, flows zeroed", level="warn")

    # -- sequence engine ----------------------------------------------------

    def _advance_sequence(self) -> None:
        """
        Non-blocking state machine. Called every loop pass; returns instantly
        unless it is time to move to the next phase. Deliberately not a
        sleep-based loop, so ALL STOP is honoured within ~50 ms at any point.
        """
        if not self._seq_active or not self._sequence:
            return

        now = time.monotonic()
        if now < self._seq_deadline:
            return

        if self._seq_index >= len(self._sequence):
            self._seq_active = False
            self._zero_all()
            self._emit(message="Sequence complete, flows zeroed")
            return

        step = self._sequence[self._seq_index]

        if self._seq_phase == "" and step.flush_s > 0:
            self._seq_phase = "flush"
            self._write("co2", 0.0)
            self._write("n2", step.total_sccm)
            self._seq_deadline = now + step.flush_s
            self._emit(message=f"Step {self._seq_index + 1}: flushing "
                               f"{step.flush_s:.0f} s")
            return

        if self._seq_phase in ("", "flush"):
            self._seq_phase = "hold"
            flows = self._split(step.percent_co2, step.total_sccm)
            self._write("n2", flows["n2"])
            self._write("co2", flows["co2"])
            self._seq_deadline = now + step.hold_s
            self._record_step(step, flows)
            self._emit(message=(f"Step {self._seq_index + 1}/"
                                f"{len(self._sequence)}: "
                                f"{step.percent_co2:.2f} % CO2 for "
                                f"{step.hold_s:.0f} s"))
            return

        self._seq_phase = ""
        self._seq_index += 1

    def _record_step(self, step: SequenceStep, flows: dict) -> None:
        self._csv_rows.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "step": self._seq_index + 1,
            "target_percent": step.percent_co2,
            "total_sccm": step.total_sccm,
            "sp_n2_sccm": flows["n2"],
            "sp_co2_sccm": flows["co2"],
            "meas_n2_sccm": self._states["n2"].measured_sccm,
            "meas_co2_sccm": self._states["co2"].measured_sccm,
            "tag": step.tag,
        })

    def export_csv(self, path: str) -> int:
        import csv as _csv
        if not self._csv_rows:
            return 0
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = _csv.DictWriter(handle, fieldnames=list(self._csv_rows[0]))
            writer.writeheader()
            writer.writerows(self._csv_rows)
        return len(self._csv_rows)

    # -- device access ------------------------------------------------------

    def _split(self, percent: float, total_sccm: float) -> Dict[str, float]:
        fraction = percent / 100.0
        q_co2 = total_sccm * fraction / core.CYLINDER_CO2_FRACTION
        return {"n2": max(0.0, total_sccm - q_co2), "co2": q_co2}

    def _write(self, key: str, sccm: float) -> None:
        device = self._devices.get(key)
        state = self._states.get(key)
        if device is None or state is None or not state.online:
            return
        if state.read_only:
            self._emit(message=f"{state.label} is a monitor; write refused",
                       level="warn")
            return
        try:
            state.setpoint_sccm = device.set_flow_sccm(sccm)
            state.error = None
        except Exception as exc:                                   # noqa: BLE001
            state.error = str(exc)
            self._emit(message=f"{state.label} write failed: {exc}",
                       level="error")

    def _zero_all(self) -> None:
        for key, device in self._devices.items():
            state = self._states.get(key)
            if state is None or state.read_only or not state.online:
                continue
            try:
                device.close_valve()
                state.setpoint_sccm = 0.0
            except Exception as exc:                               # noqa: BLE001
                self._emit(message=f"{state.label} could not be zeroed: {exc}",
                           level="error")

    def _poll(self) -> None:
        if not self._connected:
            self._emit()
            return

        errors = 0
        for key, device in self._devices.items():
            state = self._states[key]
            if not state.online:
                continue
            try:
                measured = device.read_flow_sccm()
                if self._simulate:
                    measured = self._simulated_flow(state)
                state.measured_sccm = measured
                temp = device.read_temperature()
                if temp is not None:
                    state.temperature_c = temp
                try:
                    info = (self._sim_pressure(key, state) if self._simulate
                            else device.pressure_info())
                    self._apply_pressure(state, info)
                except Exception:                                  # noqa: BLE001
                    pass
                state.error = None
            except Exception as exc:                               # noqa: BLE001
                state.error = str(exc)
                errors += 1

        if errors:
            self._error_streak += 1
            if self._error_streak >= self.MAX_CONSECUTIVE_ERRORS:
                self._emit(message=(f"{self._error_streak} consecutive poll "
                                    f"failures -- check the bus"), level="error")
        else:
            self._error_streak = 0

        self._emit()

    @staticmethod
    def _simulated_flow(state: ChannelState) -> float:
        """First-order approach to setpoint, so the GUI can be exercised."""
        import random
        current = state.measured_sccm or 0.0
        target = state.setpoint_sccm
        current += (target - current) * 0.35
        if target > 0:
            current += random.uniform(-0.004, 0.004) * state.full_scale_sccm
        return max(0.0, current)

    # -- telemetry ----------------------------------------------------------

    def _emit(self, message: Optional[str] = None, level: str = "info") -> None:
        remaining = max(0.0, self._seq_deadline - time.monotonic())
        packet = Telemetry(
            timestamp=time.time(),
            connected=self._connected,
            channels={k: _copy_state(v) for k, v in self._states.items()},
            message=message,
            level=level,
            sequence_active=self._seq_active,
            sequence_step=self._seq_index + 1 if self._seq_active else 0,
            sequence_total=len(self._sequence),
            sequence_label=self._seq_phase,
            sequence_remaining_s=remaining if self._seq_active else 0.0,
        )
        self.telemetry.put(packet)
        if self._telemetry_callback:
            self._telemetry_callback(packet)

    def _safe_shutdown(self) -> None:
        try:
            self._zero_all()
        finally:
            for device in self._devices.values():
                try:
                    device.disconnect()
                except Exception:                                  # noqa: BLE001
                    pass
            self._connected = False


def _copy_state(state: ChannelState) -> ChannelState:
    """Snapshot so the GUI never reads a field mid-update."""
    return ChannelState(**vars(state))
