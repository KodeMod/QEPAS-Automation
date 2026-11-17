# -*- coding: utf-8 -*-
"""
itc_panel.py -- laser diode / TEC control panel for the ITC4005QCL.

A ttk.Frame, so it drops into the combined GUI as a tab, or runs alone:

    python itc_panel.py --simulate
    python itc_panel.py

DISPLAY PHILOSOPHY
    Setpoint and measured value are always shown side by side, never merged.
    On the gas bench that distinction was a convenience; here it is a safety
    property. "Current commanded 152 mA" and "current flowing 152 mA" are
    different statements, and only the second one means the laser is lasing.

INTERLOCK VISIBLE IN THE UI
    The LASER ON button stays disabled until the TEC is on AND the
    temperature has held setpoint for the required time. A traffic light
    shows why it is disabled, so the operator is never left guessing.
    LASER OFF is enabled whenever the laser is on, unconditionally.
"""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk
from typing import Optional

import itc4005
import itc_worker

PANEL = "#282d36"
FG = "#e6e9ef"
MUTED = "#98a0ae"
ACCENT = "#4c9be8"
GREEN = "#3fb950"
AMBER = "#d29922"
RED = "#e5534b"
GREY = "#5a6472"

TREND_POINTS = 600


class ITCPanel(ttk.Frame):
    def __init__(self, parent, worker: Optional[itc_worker.ITCWorker] = None,
                 simulate: bool = False, standalone: bool = False, **kw):
        super().__init__(parent, padding=8, **kw)
        self.worker = worker or itc_worker.ITCWorker()
        self._owns_worker = worker is None
        if self._owns_worker:
            self.worker.start()
        self.simulate = simulate
        self.standalone = standalone
        self.state = itc_worker.ITCState()
        self.history = {"t": deque(maxlen=TREND_POINTS),
                        "T": deque(maxlen=TREND_POINTS),
                        "I": deque(maxlen=TREND_POINTS)}
        self._t0 = time.time()

        self._build()
        self.after(200, self._pump)

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))

        ttk.Label(bar, text="VISA resource").pack(side="left")
        self.resource_var = tk.StringVar(value=itc4005.DEFAULT_RESOURCE)
        ttk.Entry(bar, textvariable=self.resource_var, width=42).pack(
            side="left", padx=6)
        self.sim_var = tk.BooleanVar(value=self.simulate)
        ttk.Checkbutton(bar, text="Simulate", variable=self.sim_var).pack(
            side="left", padx=8)
        self.connect_btn = ttk.Button(bar, text="Connect",
                                      command=self._toggle_connect)
        self.connect_btn.pack(side="left")
        self.idn_label = ttk.Label(bar, text="not connected", foreground="#777")
        self.idn_label.pack(side="left", padx=10)

        # Always reachable, never conditional on anything else.
        self.off_btn = tk.Button(bar, text="LASER OFF", bg=RED, fg="white",
                                 font=("Segoe UI", 12, "bold"), relief="flat",
                                 padx=18, pady=8, command=self._laser_off,
                                 state="disabled")
        self.off_btn.pack(side="right")

        cards = ttk.Frame(self)
        cards.pack(fill="x")
        self._build_tec_card(cards)
        self._build_ld_card(cards)
        self._build_status_card(cards)

        self._build_modulation_card(self)

        self.log = tk.Text(self, height=7, bg="#15181d", fg=FG,
                           font=("Consolas", 9), relief="flat", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.tag_configure("warn", foreground=AMBER)
        self.log.tag_configure("error", foreground=RED)
        self.log.tag_configure("info", foreground=MUTED)

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=PANEL, padx=12, pady=10)
        outer.pack(side="left", padx=(0, 10), fill="both", expand=True)
        tk.Label(outer, text=title, bg=PANEL, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        return outer

    def _build_tec_card(self, parent) -> None:
        c = self._card(parent, "Temperature (TEC)")

        self.temp_value = tk.Label(c, text="--", bg=PANEL, fg=ACCENT,
                                   font=("Consolas", 26, "bold"))
        self.temp_value.pack(anchor="w", pady=(6, 0))
        tk.Label(c, text="°C measured", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self.temp_sp_label = tk.Label(c, text="setpoint --", bg=PANEL, fg=FG,
                                      font=("Consolas", 11))
        self.temp_sp_label.pack(anchor="w", pady=(8, 0))
        self.temp_dev_label = tk.Label(c, text="", bg=PANEL, fg=MUTED,
                                       font=("Segoe UI", 8))
        self.temp_dev_label.pack(anchor="w")

        row = tk.Frame(c, bg=PANEL); row.pack(fill="x", pady=(10, 0))
        self.temp_entry = ttk.Entry(row, width=8)
        self.temp_entry.insert(0, "17.5")
        self.temp_entry.pack(side="left")
        tk.Label(row, text="°C", bg=PANEL, fg=MUTED).pack(side="left", padx=4)
        self.temp_set_btn = ttk.Button(row, text="Set", width=5,
                                       command=self._set_temp)
        self.temp_set_btn.pack(side="left", padx=4)

        row2 = tk.Frame(c, bg=PANEL); row2.pack(fill="x", pady=(8, 0))
        self.tec_on_btn = ttk.Button(row2, text="TEC ON",
                                     command=lambda: self._tec(True))
        self.tec_on_btn.pack(side="left")
        self.tec_off_btn = ttk.Button(row2, text="TEC OFF",
                                      command=lambda: self._tec(False))
        self.tec_off_btn.pack(side="left", padx=6)

    def _build_ld_card(self, parent) -> None:
        c = self._card(parent, "Laser diode")

        self.current_value = tk.Label(c, text="--", bg=PANEL, fg=ACCENT,
                                      font=("Consolas", 26, "bold"))
        self.current_value.pack(anchor="w", pady=(6, 0))
        tk.Label(c, text="mA flowing (measured)", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self.current_sp_label = tk.Label(c, text="setpoint --", bg=PANEL,
                                         fg=FG, font=("Consolas", 11))
        self.current_sp_label.pack(anchor="w", pady=(8, 0))
        self.limit_label = tk.Label(c, text="", bg=PANEL, fg=MUTED,
                                    font=("Segoe UI", 8))
        self.limit_label.pack(anchor="w")
        self.volt_label = tk.Label(c, text="", bg=PANEL, fg=MUTED,
                                   font=("Segoe UI", 8))
        self.volt_label.pack(anchor="w")

        row = tk.Frame(c, bg=PANEL); row.pack(fill="x", pady=(10, 0))
        self.current_entry = ttk.Entry(row, width=8)
        self.current_entry.insert(0, "152")
        self.current_entry.pack(side="left")
        tk.Label(row, text="mA", bg=PANEL, fg=MUTED).pack(side="left", padx=4)

        row2 = tk.Frame(c, bg=PANEL); row2.pack(fill="x", pady=(8, 0))
        self.laser_on_btn = ttk.Button(row2, text="LASER ON",
                                       command=self._laser_on)
        self.laser_on_btn.pack(side="left")
        self.ramp_btn = ttk.Button(row2, text="Ramp to value",
                                   command=self._set_current)
        self.ramp_btn.pack(side="left", padx=6)

        self.ramp_label = tk.Label(c, text="", bg=PANEL, fg=AMBER,
                                   font=("Segoe UI", 8))
        self.ramp_label.pack(anchor="w", pady=(6, 0))

    def _build_modulation_card(self, parent) -> None:
        """
        Internal / external modulation.

        Controls stay disabled unless the instrument actually answered the
        matching query at connect. A modulation command that silently fails
        would leave you believing you had set a depth you had not.
        """
        outer = tk.Frame(parent, bg=PANEL, padx=12, pady=10)
        outer.pack(fill="x", pady=(10, 0))
        tk.Label(outer, text="Modulation (2f wavelength modulation)",
                 bg=PANEL, fg=FG, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        row = tk.Frame(outer, bg=PANEL); row.pack(fill="x", pady=(8, 0))

        tk.Label(row, text="Source", bg=PANEL, fg=MUTED).pack(side="left")
        self.mod_source_var = tk.StringVar(value="INT")
        self.mod_source_box = ttk.Combobox(
            row, textvariable=self.mod_source_var, width=6, state="readonly",
            values=["INT", "EXT"])
        self.mod_source_box.pack(side="left", padx=(4, 14))
        # Re-evaluate which fields are editable the moment the choice changes,
        # not on the next telemetry packet.
        self.mod_source_var.trace_add("write",
                                      lambda *_: self._update_mod_controls())

        tk.Label(row, text="Shape", bg=PANEL, fg=MUTED).pack(side="left")
        self.mod_shape_var = tk.StringVar(value="SIN")
        self.mod_shape_box = ttk.Combobox(
            row, textvariable=self.mod_shape_var, width=6, state="readonly",
            values=["SIN", "SQU", "TRI"])
        self.mod_shape_box.pack(side="left", padx=(4, 14))

        tk.Label(row, text="Frequency", bg=PANEL, fg=MUTED).pack(side="left")
        self.mod_freq_entry = ttk.Entry(row, width=9)
        self.mod_freq_entry.insert(0, "6234")
        self.mod_freq_entry.pack(side="left", padx=4)
        tk.Label(row, text="Hz  (20 - 100k)", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 14))

        tk.Label(row, text="Depth", bg=PANEL, fg=MUTED).pack(side="left")
        self.mod_depth_entry = ttk.Entry(row, width=7)
        self.mod_depth_entry.insert(0, "5")
        self.mod_depth_entry.pack(side="left", padx=4)
        tk.Label(row, text="%  (0.1 - 100)", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 14))

        self.mod_apply_btn = ttk.Button(row, text="Apply",
                                        command=self._apply_modulation)
        self.mod_apply_btn.pack(side="left", padx=4)
        self.mod_on_btn = ttk.Button(row, text="MOD ON",
                                     command=lambda: self._mod_state(True))
        self.mod_on_btn.pack(side="left", padx=4)
        self.mod_off_btn = ttk.Button(row, text="MOD OFF",
                                      command=lambda: self._mod_state(False))
        self.mod_off_btn.pack(side="left", padx=4)
        self.ripple_btn = ttk.Button(row, text="Measure ripple",
                                     command=self._measure_ripple)
        self.ripple_btn.pack(side="left", padx=12)

        self.mod_actual = tk.Label(outer, text="", bg=PANEL, fg=MUTED,
                                   font=("Consolas", 9), justify="left")
        self.mod_actual.pack(anchor="w", pady=(8, 0))
        self.headroom_label = tk.Label(outer, text="", bg=PANEL, fg=MUTED,
                                       font=("Segoe UI", 9), justify="left")
        self.headroom_label.pack(anchor="w", pady=(4, 0))
        self.mod_unsupported = tk.Label(outer, text="", bg=PANEL, fg=AMBER,
                                        font=("Segoe UI", 8), justify="left")
        self.mod_unsupported.pack(anchor="w")

    def _update_mod_controls(self) -> None:
        """
        Enable shape/frequency/depth according to the SELECTED source.

        Earlier this followed the instrument's current source, which locked
        the fields while the instrument sat on EXT -- so you could never
        enter the values needed to switch to INT. The dropdown expresses what
        you want; the "on the instrument" line shows what actually is.
        """
        supported = self.state.mod_supported or {}
        connected = self.state.connected
        want_ext = self.mod_source_var.get().upper().startswith("EXT")

        self.mod_source_box.configure(
            state="readonly" if connected and supported.get("am_source", True)
            else "disabled")
        for widget, key in ((self.mod_shape_box, "am_shape"),
                            (self.mod_freq_entry, "am_frequency"),
                            (self.mod_depth_entry, "am_depth")):
            usable = connected and supported.get(key, False) and not want_ext
            if widget is self.mod_shape_box:
                widget.configure(state="readonly" if usable else "disabled")
            else:
                widget.configure(state="normal" if usable else "disabled")

        self.mod_apply_btn.configure(
            text="Apply (EXT)" if want_ext else "Apply (INT)")

    def _apply_modulation(self) -> None:
        if self.mod_source_var.get().upper().startswith("EXT"):
            # External: the SR830 sets frequency and amplitude. Only the
            # source is meaningful to send.
            self.worker.send("set_modulation", {"source": "EXT"})
            return
        try:
            freq = float(self.mod_freq_entry.get())
            depth = float(self.mod_depth_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Frequency and depth must be numbers.")
            return
        # Warn before sending, not after: a clipped peak is silent.
        setpoint = self.state.current_setpoint_ma or 0.0
        limit = self.state.current_limit_ma or 0.0
        peak = setpoint * (1 + depth / 100.0)
        if limit and peak > limit:
            if not messagebox.askokcancel(
                    "Modulation would clip",
                    f"Setpoint {setpoint:.1f} mA at {depth:.1f} % depth gives "
                    f"a peak of {peak:.1f} mA, above the {limit:.1f} mA "
                    f"hardware limit.\n\nThe instrument clips rather than "
                    f"refusing, which distorts the 2f waveform.\n\nApply "
                    f"anyway?"):
                return
        self.worker.send("set_modulation", {
            "source": self.mod_source_var.get(),
            "shape": self.mod_shape_var.get(),
            "frequency_hz": freq,
            "depth_pct": depth})

    def _mod_state(self, on: bool) -> None:
        self.worker.send("set_modulation", {"state": on})

    def _measure_ripple(self) -> None:
        self.worker.send("measure_ripple", {"samples": 60})

    def _build_status_card(self, parent) -> None:
        c = self._card(parent, "Interlocks")
        self.lamps = {}
        for key, text in (("tec", "TEC on"),
                          ("settled", "Temperature settled"),
                          ("ld", "Laser emitting"),
                          ("mod", "Modulation active")):
            row = tk.Frame(c, bg=PANEL); row.pack(anchor="w", pady=3)
            canvas = tk.Canvas(row, width=14, height=14, bg=PANEL,
                               highlightthickness=0)
            lamp = canvas.create_oval(2, 2, 12, 12, fill=GREY, outline="")
            canvas.pack(side="left", padx=(0, 8))
            label = tk.Label(row, text=text, bg=PANEL, fg=MUTED,
                             font=("Segoe UI", 9))
            label.pack(side="left")
            self.lamps[key] = (canvas, lamp, label)

        self.gate_label = tk.Label(c, text="", bg=PANEL, fg=AMBER,
                                   font=("Segoe UI", 8), wraplength=210,
                                   justify="left")
        self.gate_label.pack(anchor="w", pady=(10, 0))
        self.err_label = tk.Label(c, text="", bg=PANEL, fg=RED,
                                  font=("Segoe UI", 8), wraplength=210,
                                  justify="left")
        self.err_label.pack(anchor="w", pady=(6, 0))

    # -- actions ------------------------------------------------------------

    def _toggle_connect(self) -> None:
        if self.state.connected:
            self.worker.send("disconnect")
        else:
            self.worker.send("connect", {"resource": self.resource_var.get(),
                                         "simulate": self.sim_var.get()})

    def _set_temp(self) -> None:
        try:
            celsius = float(self.temp_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Temperature must be a number.")
            return
        self.worker.send("set_temp", {"celsius": celsius})

    def _tec(self, on: bool) -> None:
        self.worker.send("tec", {"on": on})

    def _read_current_entry(self) -> Optional[float]:
        try:
            return float(self.current_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Current must be a number in mA.")
            return None

    def _laser_on(self) -> None:
        current = self._read_current_entry()
        if current is None:
            return
        if not messagebox.askokcancel(
                "Enable laser",
                f"Enable the laser and ramp to {current:.1f} mA?\n\n"
                f"TEC setpoint {self.state.temp_setpoint_c} °C, "
                f"measured {self.state.temp_c} °C.\n"
                f"Hardware limit {self.state.current_limit_ma} mA.\n\n"
                f"2004 nm is invisible. Confirm the beam path is enclosed "
                f"and eyewear is in place."):
            return
        self.worker.send("laser_on", {"current_ma": current})

    def _set_current(self) -> None:
        current = self._read_current_entry()
        if current is not None:
            self.worker.send("set_current", {"current_ma": current})

    def _laser_off(self) -> None:
        self.worker.send("laser_off")

    # -- telemetry ----------------------------------------------------------

    def _pump(self) -> None:
        latest = None
        while True:
            try:
                latest = self.worker.telemetry.get_nowait()
            except Exception:                                      # noqa: BLE001
                break
            if latest.message:
                self._log(latest.message, latest.level)
        if latest is not None:
            self.state = latest
            self._render(latest)
        self.after(250, self._pump)

    def _sync_mod_fields_once(self, s: itc_worker.ITCState) -> None:
        """
        On first contact, load the controls from the instrument.

        Only once per connection: afterwards the dropdown and fields belong to
        the operator, and overwriting them on every poll would undo whatever
        they were in the middle of typing.
        """
        if not s.connected:
            self._mod_synced = False
            return
        if getattr(self, "_mod_synced", False) or not s.mod_source:
            return
        self.mod_source_var.set("EXT" if s.mod_source.upper().startswith("EXT")
                                else "INT")
        if s.mod_shape:
            self.mod_shape_var.set(s.mod_shape.upper()[:3])
        for entry, value in ((self.mod_freq_entry, s.mod_frequency_hz),
                             (self.mod_depth_entry, s.mod_depth_pct)):
            if value is not None:
                prior = entry.cget("state")
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, f"{value:g}")
                entry.configure(state=prior)
        self._mod_synced = True

    def _render(self, s: itc_worker.ITCState) -> None:
        self._sync_mod_fields_once(s)
        self.connect_btn.configure(
            text="Disconnect" if s.connected else "Connect")
        self.idn_label.configure(
            text=s.idn or "not connected",
            foreground="#137a2b" if s.connected else "#777")

        if s.temp_c is not None:
            self.temp_value.configure(text=f"{s.temp_c:7.3f}")
        if s.temp_setpoint_c is not None:
            self.temp_sp_label.configure(
                text=f"setpoint {s.temp_setpoint_c:.3f} °C")
            if s.temp_c is not None:
                delta = s.temp_c - s.temp_setpoint_c
                self.temp_dev_label.configure(
                    text=f"deviation {delta:+.4f} °C   "
                         f"stable {s.stable_for_s:.0f}/"
                         f"{itc4005.TEMP_STABLE_S:.0f} s",
                    fg=GREEN if s.temp_settled else MUTED)

        if s.current_ma is not None:
            self.current_value.configure(text=f"{s.current_ma:7.2f}")
        if s.current_setpoint_ma is not None:
            self.current_sp_label.configure(
                text=f"setpoint {s.current_setpoint_ma:6.1f} mA")
        if s.current_limit_ma is not None:
            self.limit_label.configure(
                text=f"hardware limit {s.current_limit_ma:.1f} mA   "
                     f"software {itc4005.CURRENT_LIMIT_MA:.1f} mA")
        if s.voltage_v is not None:
            self.volt_label.configure(text=f"{s.voltage_v:+.4f} V across diode")

        self.ramp_label.configure(
            text=f"ramping to {s.ramp_target_ma:.1f} mA..." if s.ramping else "")

        # Defined first: both the readout below and the control-enabling
        # further down depend on it. (It was previously assigned after its
        # first use, which raised on every redraw and froze the panel.)
        external = str(s.mod_source or "").upper().startswith("EXT")

        bits = []
        if s.mod_source: bits.append(f"source {s.mod_source}")
        if s.mod_shape: bits.append(f"shape {s.mod_shape}")
        if s.mod_frequency_hz: bits.append(f"{s.mod_frequency_hz:.1f} Hz")
        if s.mod_depth_pct is not None: bits.append(f"depth {s.mod_depth_pct:.2f} %")
        extra = ""
        if s.current_pp_ma is not None:
            extra = (f"   |   measured ripple {s.current_pp_ma:.2f} mA p-p, "
                     f"mean {s.current_mean_ma:.2f} mA")
        if external:
            bits = [b for b in bits if b.startswith("source")]
            bits.append("(shape/frequency/depth come from the external "
                        "signal -- the SR830 sine out)")
        self.mod_actual.configure(
            text=("on the instrument:  " + "   ".join(bits) + extra)
            if bits else "modulation state unknown")

        if s.headroom_note:
            colour = (AMBER if s.headroom_ok is None
                      else MUTED if s.headroom_ok else RED)
            self.headroom_label.configure(text=s.headroom_note, fg=colour)


        if s.mod_supported:
            missing = [k for k, v in s.mod_supported.items() if not v]
            self.mod_unsupported.configure(
                text=("not exposed by this firmware: " + ", ".join(missing))
                if missing else "")
        self._update_mod_controls()
        for widget in (self.mod_apply_btn, self.mod_on_btn, self.mod_off_btn,
                       self.ripple_btn):
            widget.configure(state="normal" if s.connected else "disabled")

        self._lamp("tec", s.tec_on)
        self._lamp("settled", s.temp_settled,
                   amber=s.tec_on and not s.temp_settled)
        self._lamp("ld", s.ld_on, colour=RED if s.ld_on else GREY)
        self._lamp("mod", bool(s.modulation_on))

        # Explain a disabled LASER ON rather than leaving it mysteriously grey.
        gate = ""
        if not s.connected:
            gate = "not connected"
        elif not s.tec_on:
            gate = "TEC must be on before the laser can be enabled"
        elif not s.temp_settled:
            gate = (f"waiting for temperature to hold "
                    f"±{itc4005.TEMP_TOLERANCE_C} °C for "
                    f"{itc4005.TEMP_STABLE_S:.0f} s")
        self.gate_label.configure(text=gate)
        self.err_label.configure(text=s.error or "")

        can_lase = s.connected and s.tec_on and s.temp_settled and not s.ld_on
        self.laser_on_btn.configure(state="normal" if can_lase else "disabled")
        self.ramp_btn.configure(state="normal" if s.ld_on else "disabled")
        self.off_btn.configure(state="normal" if s.ld_on else "disabled")
        for widget in (self.temp_set_btn, self.tec_on_btn, self.tec_off_btn):
            widget.configure(state="normal" if s.connected else "disabled")

    def _lamp(self, key: str, on: bool, amber: bool = False,
              colour: Optional[str] = None) -> None:
        canvas, lamp, label = self.lamps[key]
        fill = colour if colour else (GREEN if on else (AMBER if amber else GREY))
        canvas.itemconfigure(lamp, fill=fill)
        label.configure(fg=FG if on else MUTED)

    def _log(self, message: str, level: str = "info") -> None:
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n", level)
        self.log.see("end")

    # -- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        if self._owns_worker:
            self.worker.send("laser_off")
            self.worker.stop()
            self.worker.join(timeout=5.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="ITC4005QCL control panel")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--resource", default=itc4005.DEFAULT_RESOURCE)
    args, _ = parser.parse_known_args(
        [a for a in sys.argv[1:] if a != "--wdir"])

    root = tk.Tk()
    root.title("ITC4005QCL -- laser diode and TEC control")
    root.geometry("1080x620")
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    panel = ITCPanel(root, simulate=args.simulate, standalone=True)
    panel.resource_var.set(args.resource)
    panel.pack(fill="both", expand=True)

    def on_close():
        if panel.state.ld_on and not messagebox.askokcancel(
                "Laser is on", "This will switch the laser off and close. "
                               "Continue?"):
            return
        panel.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
