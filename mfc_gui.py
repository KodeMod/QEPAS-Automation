# -*- coding: utf-8 -*-
"""
Gas blending control panel for the CO2/N2 MFC bench.

    python mfc_gui.py                 talk to the real hardware
    python mfc_gui.py --simulate      run without hardware, for practice

Needs mfc_control.py and mfc_worker.py in the same folder.

WHAT THIS SHOWS
    - a live status lamp per MFC: green online, grey offline, red faulted
    - the commanded setpoint and the ACTUAL measured flow, side by side,
      with the deviation between them, because a setpoint that was accepted
      is not proof that gas is moving
    - a blend panel: enter a CO2 percentage and a total flow, and it works
      out both setpoints
    - a sequence runner for stepping through concentrations unattended
    - a rolling trend of measured flows

OPERATOR SAFETY
    - nothing opens a valve until you press Connect and then an action
    - ALL STOP is always reachable while connected and zeroes both heads
    - closing the window zeroes flows before dropping the serial link
    - the 10 ln/min downstream unit is read-only; the GUI offers no control
      for it, because writing to it would fight the two upstream loops
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

try:
    import mfc_control as core
    import mfc_worker
except ImportError as exc:                                         # noqa: BLE001
    print(f"Cannot import the control modules: {exc}\n"
          f"mfc_control.py and mfc_worker.py must sit beside this file.")
    raise SystemExit(1)

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    PLOTTING = True
except ImportError:
    PLOTTING = False


# ---- appearance -----------------------------------------------------------

BG = "#1e2229"
PANEL = "#282d36"
FG = "#e6e9ef"
MUTED = "#98a0ae"
ACCENT = "#4c9be8"
GREEN = "#3fb950"
AMBER = "#d29922"
RED = "#e5534b"
GREY = "#5a6472"

FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 8)
FONT_BIG = ("Consolas", 22, "bold")
FONT_MED = ("Consolas", 12)

TREND_SECONDS = 300


class ChannelCard(ttk.Frame):
    """One MFC: status lamp, live reading, setpoint entry."""

    def __init__(self, parent, key: str, cfg, on_set, **kwargs):
        super().__init__(parent, style="Panel.TFrame", padding=10, **kwargs)
        self.key = key
        self.cfg = cfg
        self.on_set = on_set
        self.read_only = cfg.read_only

        header = ttk.Frame(self, style="Panel.TFrame")
        header.pack(fill="x")

        self.lamp = tk.Canvas(header, width=14, height=14, bg=PANEL,
                              highlightthickness=0)
        self.lamp_id = self.lamp.create_oval(2, 2, 12, 12, fill=GREY, outline="")
        self.lamp.pack(side="left", padx=(0, 8))

        ttk.Label(header, text=cfg.label, style="CardTitle.TLabel").pack(side="left")
        ttk.Label(header, text=f"addr {cfg.slave}",
                  style="Muted.TLabel").pack(side="right")

        self.identity = ttk.Label(self, text="not connected",
                                  style="Muted.TLabel")
        self.identity.pack(anchor="w", pady=(2, 8))

        # Measured flow is the headline number: it is the physical truth,
        # whereas the setpoint is only a request.
        self.reading = ttk.Label(self, text="--", style="Reading.TLabel")
        self.reading.pack(anchor="w")
        ttk.Label(self, text="SCCM measured (live from device)",
                  style="Muted.TLabel").pack(anchor="w")

        self.bar = ttk.Progressbar(self, maximum=100, length=210)
        self.bar.pack(fill="x", pady=(8, 2))
        self.bar_label = ttk.Label(self, text="0.0 % of full scale",
                                   style="Muted.TLabel")
        self.bar_label.pack(anchor="w")

        info = ttk.Frame(self, style="Panel.TFrame")
        info.pack(fill="x", pady=(8, 0))
        self.sp_label = ttk.Label(info, text="setpoint --", style="Value.TLabel")
        self.sp_label.pack(anchor="w")
        self.dev_label = ttk.Label(info, text="", style="Muted.TLabel")
        self.dev_label.pack(anchor="w")
        self.temp_label = ttk.Label(info, text="", style="Muted.TLabel")
        self.temp_label.pack(anchor="w")

        if self.read_only:
            ttk.Label(self, text="monitor only - no setpoint",
                      style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        else:
            entry_row = ttk.Frame(self, style="Panel.TFrame")
            entry_row.pack(fill="x", pady=(10, 0))
            self.entry = ttk.Entry(entry_row, width=9, font=FONT)
            self.entry.insert(0, "0")
            self.entry.pack(side="left")
            ttk.Label(entry_row, text="SCCM", style="Muted.TLabel").pack(
                side="left", padx=4)
            self.set_button = ttk.Button(entry_row, text="Set", width=6,
                                         command=self._apply)
            self.set_button.pack(side="right")

        self.error_label = ttk.Label(self, text="", style="Error.TLabel",
                                     wraplength=210)
        self.error_label.pack(anchor="w", pady=(6, 0))

    def _apply(self) -> None:
        try:
            value = float(self.entry.get())
        except ValueError:
            messagebox.showerror("Invalid setpoint",
                                 f"'{self.entry.get()}' is not a number.")
            return
        self.on_set(self.key, value)

    def set_enabled(self, enabled: bool) -> None:
        if self.read_only:
            return
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        self.set_button.configure(state=state)

    def update_state(self, state) -> None:
        colour = GREY
        if state.error:
            colour = RED
        elif state.online:
            colour = GREEN
        self.lamp.itemconfigure(self.lamp_id, fill=colour)

        if state.serial:
            self.identity.configure(
                text=f"SN {state.serial}   {state.tag or ''}"[:38])

        if state.measured_sccm is None:
            self.reading.configure(text="--")
            self.bar["value"] = 0
            self.bar_label.configure(text="0.0 % of full scale")
        else:
            self.reading.configure(text=f"{state.measured_sccm:8.2f}")
            self.bar["value"] = min(100.0, max(0.0, state.percent_of_fs))
            self.bar_label.configure(
                text=f"{state.percent_of_fs:.1f} % of "
                     f"{state.full_scale_sccm:.0f} SCCM full scale")

        if not state.read_only:
            self.sp_label.configure(text=f"setpoint {state.setpoint_sccm:.2f} SCCM")
            deviation = state.deviation_sccm
            if deviation is None or state.setpoint_sccm == 0:
                self.dev_label.configure(text="", foreground=MUTED)
            else:
                # 5 % of commanded is the threshold for "not following".
                off = abs(deviation) > 0.05 * max(state.setpoint_sccm, 1e-9)
                self.dev_label.configure(
                    text=f"deviation {deviation:+.2f} SCCM"
                         + ("   NOT FOLLOWING" if off else ""),
                    foreground=AMBER if off else MUTED)
        else:
            self.sp_label.configure(text="downstream total-flow check")

        bits = []
        if state.temperature_c is not None:
            bits.append(f"{state.temperature_c:.1f} °C")
        # Sensor presence comes from PRESSURE_SCALE_MIN/MAX, not from the value:
        # a unit with no sensor reports 0, which is not a measurement.
        if getattr(state, "has_pressure_sensor", False):
            p = state.pressure
            unit = state.pressure_unit or ""
            text = f"P {p:.4g} {unit}" if p is not None else "P --"
            if state.pressure_setpoint is not None:
                text += f"  (sp {state.pressure_setpoint:.4g})"
            bits.append(text)
        elif getattr(state, "pressure_max", None) is not None:
            bits.append("no pressure sensor")
        if bits:
            self.temp_label.configure(text="   ".join(bits))

        self.error_label.configure(text=state.error or "")


class MFCApp(tk.Tk):
    def __init__(self, simulate: bool = False):
        super().__init__()
        self.title("MFC Gas Blending Control")
        self.configure(bg=BG)
        self.geometry("1180x900")
        self.minsize(1050, 800)

        self.simulate = simulate
        self.worker = mfc_worker.DeviceWorker()
        self.worker.start()

        self.cards: Dict[str, ChannelCard] = {}
        self.connected = False
        self.history: Dict[str, deque] = {
            "t": deque(maxlen=TREND_SECONDS),
            "n2": deque(maxlen=TREND_SECONDS),
            "co2": deque(maxlen=TREND_SECONDS),
        }
        self._t0 = time.time()

        self._build_styles()
        self._build_ui()
        self._set_controls_enabled(False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump_telemetry)

        if simulate:
            self.sim_var.set(True)
            self._log("Simulation mode: no serial traffic will occur.", "warn")

    # -- styling ------------------------------------------------------------

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=FG, font=FONT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED,
                        font=FONT_SMALL)
        style.configure("CardTitle.TLabel", background=PANEL, foreground=FG,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Reading.TLabel", background=PANEL, foreground=ACCENT,
                        font=FONT_BIG)
        style.configure("Value.TLabel", background=PANEL, foreground=FG,
                        font=FONT_MED)
        style.configure("Error.TLabel", background=PANEL, foreground=RED,
                        font=FONT_SMALL)
        style.configure("Head.TLabel", background=BG, foreground=FG,
                        font=("Segoe UI", 12, "bold"))
        style.configure("TButton", font=FONT, padding=6)
        style.configure("Stop.TButton", font=("Segoe UI", 12, "bold"),
                        padding=10, foreground="#ffffff", background=RED)
        style.map("Stop.TButton", background=[("active", "#c23b34")])
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                        font=FONT)
        style.configure("TProgressbar", background=ACCENT, troughcolor="#1a1d23")
        style.configure("TCheckbutton", background=BG, foreground=FG, font=FONT)

    # -- layout -------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_connection_bar()
        self._build_channel_row()

        middle = ttk.Frame(self)
        middle.pack(fill="x", padx=12, pady=(0, 8))
        self._build_blend_panel(middle)
        self._build_sequence_panel(middle)

        if PLOTTING:
            self._build_plot()
        self._build_log()

    def _build_connection_bar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 10))
        bar.pack(fill="x")

        ttk.Label(bar, text="COM port").pack(side="left")
        self.port_var = tk.StringVar(value=str(core.PORT))
        ttk.Entry(bar, textvariable=self.port_var, width=5,
                  font=FONT).pack(side="left", padx=(6, 14))

        self.sim_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Simulate", variable=self.sim_var,
                        style="TCheckbutton").pack(side="left", padx=(0, 14))

        self.connect_button = ttk.Button(bar, text="Connect",
                                         command=self._toggle_connection)
        self.connect_button.pack(side="left")

        self.status_label = ttk.Label(bar, text="  Offline", foreground=MUTED)
        self.status_label.pack(side="left", padx=12)

        # Always reachable while connected: the operator should never have to
        # hunt for the stop control.
        self.stop_button = ttk.Button(bar, text="ALL STOP", style="Stop.TButton",
                                      command=self._all_stop)
        self.stop_button.pack(side="right")

    def _build_channel_row(self) -> None:
        row = ttk.Frame(self, padding=(12, 0))
        row.pack(fill="x")
        for key, cfg in core.CHANNELS.items():
            card = ChannelCard(row, key, cfg, self._set_channel)
            card.pack(side="left", padx=(0, 10), fill="y")
            self.cards[key] = card

    def _build_blend_panel(self, parent) -> None:
        frame = ttk.Labelframe(parent, text=" Blend ", padding=12)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 6))

        grid = ttk.Frame(frame)
        grid.pack(fill="x")

        ttk.Label(grid, text="Total flow").grid(row=0, column=0, sticky="w")
        self.total_var = tk.StringVar(value=str(int(core.DEFAULT_TOTAL_SCCM)))
        ttk.Entry(grid, textvariable=self.total_var, width=8,
                  font=FONT).grid(row=0, column=1, padx=6)
        ttk.Label(grid, text="SCCM").grid(row=0, column=2, sticky="w")

        ttk.Label(grid, text="CO2 target").grid(row=1, column=0, sticky="w",
                                                pady=6)
        self.conc_var = tk.StringVar(value="45")
        ttk.Entry(grid, textvariable=self.conc_var, width=8,
                  font=FONT).grid(row=1, column=1, padx=6)
        self.unit_var = tk.StringVar(value="%")
        ttk.Combobox(grid, textvariable=self.unit_var, values=["%", "ppm"],
                     width=5, state="readonly").grid(row=1, column=2)

        for var in (self.total_var, self.conc_var):
            var.trace_add("write", lambda *_: self._update_preview())
        self.unit_var.trace_add("write", lambda *_: self._update_preview())

        # Label this clearly: these are computed targets, not measurements.
        # The live numbers live on the channel cards above.
        ttk.Label(frame, text="Calculated setpoints (not yet sent):",
                  foreground=MUTED, font=FONT_SMALL).pack(anchor="w",
                                                          pady=(10, 2))
        self.preview = ttk.Label(frame, text="", justify="left",
                                 font=("Consolas", 10))
        self.preview.pack(anchor="w", pady=(0, 4))
        self.preview_warn = ttk.Label(frame, text="", foreground=AMBER,
                                      wraplength=380, justify="left")
        self.preview_warn.pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        self.apply_button = ttk.Button(buttons, text="Apply blend",
                                       command=self._apply_blend)
        self.apply_button.pack(side="left")
        self.flush_button = ttk.Button(buttons, text="Flush N2",
                                       command=self._flush)
        self.flush_button.pack(side="left", padx=6)
        self.zero_button = ttk.Button(buttons, text="Zero both",
                                      command=self._all_stop)
        self.zero_button.pack(side="left")

        self._update_preview()

    def _build_sequence_panel(self, parent) -> None:
        frame = ttk.Labelframe(parent, text=" Sequence ", padding=12)
        frame.pack(side="left", fill="both", expand=True, padx=(6, 0))

        grid = ttk.Frame(frame)
        grid.pack(fill="x")

        ttk.Label(grid, text="CO2 levels %").grid(row=0, column=0, sticky="w")
        self.levels_var = tk.StringVar(value="20,30,40,50,60")
        ttk.Entry(grid, textvariable=self.levels_var, width=24,
                  font=FONT).grid(row=0, column=1, columnspan=2, padx=6)

        ttk.Label(grid, text="Hold s").grid(row=1, column=0, sticky="w", pady=6)
        self.hold_var = tk.StringVar(value="180")
        ttk.Entry(grid, textvariable=self.hold_var, width=8,
                  font=FONT).grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(grid, text="Flush s").grid(row=2, column=0, sticky="w")
        self.flush_var = tk.StringVar(value="60")
        ttk.Entry(grid, textvariable=self.flush_var, width=8,
                  font=FONT).grid(row=2, column=1, sticky="w", padx=6)

        self.seq_progress = ttk.Progressbar(frame, maximum=100, length=380)
        self.seq_progress.pack(fill="x", pady=(12, 4))
        self.seq_label = ttk.Label(frame, text="idle")
        self.seq_label.pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(buttons, text="Start sequence",
                                       command=self._start_sequence)
        self.start_button.pack(side="left")
        self.abort_button = ttk.Button(buttons, text="Abort",
                                       command=self._abort_sequence)
        self.abort_button.pack(side="left", padx=6)
        self.export_button = ttk.Button(buttons, text="Export CSV",
                                        command=self._export_csv)
        self.export_button.pack(side="left")

    def _build_plot(self) -> None:
        frame = ttk.Labelframe(self, text=" Measured flow ", padding=8)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        self.figure = Figure(figsize=(10, 2.6), dpi=100, facecolor=PANEL)
        self.axes = self.figure.add_subplot(111)
        self.axes.set_facecolor(PANEL)
        for spine in self.axes.spines.values():
            spine.set_color(GREY)
        self.axes.tick_params(colors=MUTED, labelsize=8)
        self.axes.set_xlabel("seconds", color=MUTED, fontsize=8)
        self.axes.set_ylabel("SCCM", color=MUTED, fontsize=8)
        self.figure.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.figure, master=frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_log(self) -> None:
        frame = ttk.Labelframe(self, text=" Log ", padding=6)
        frame.pack(fill="both", expand=False, padx=12, pady=(0, 12))
        self.log = tk.Text(frame, height=7, bg="#15181d", fg=FG,
                           font=("Consolas", 9), relief="flat", wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("warn", foreground=AMBER)
        self.log.tag_configure("error", foreground=RED)
        self.log.tag_configure("info", foreground=MUTED)

    # -- actions ------------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.connected:
            self.worker.send("disconnect")
        else:
            try:
                port = int(self.port_var.get())
            except ValueError:
                messagebox.showerror("Invalid port",
                                     "COM port must be a number, e.g. 4")
                return
            self.worker.send("connect", {"port": port,
                                         "simulate": self.sim_var.get()})

    def _set_channel(self, key: str, sccm: float) -> None:
        self.worker.send("setpoint", {"channel": key, "sccm": sccm})

    def _parse_blend(self):
        total = float(self.total_var.get())
        value = float(self.conc_var.get())
        percent = value if self.unit_var.get() == "%" else value / 10000.0
        return percent, total

    def _update_preview(self) -> None:
        try:
            percent, total = self._parse_blend()
        except ValueError:
            self.preview.configure(text="enter numbers for flow and target")
            self.preview_warn.configure(text="")
            return

        q_co2 = total * (percent / 100.0) / core.CYLINDER_CO2_FRACTION
        q_n2 = total - q_co2
        self.preview.configure(
            text=f"N2   {q_n2:8.2f} SCCM   ({core.sccm_to_ln(q_n2):.4f} ln/min)\n"
                 f"CO2  {q_co2:8.2f} SCCM   ({core.sccm_to_ln(q_co2):.4f} ln/min)")

        # Live feasibility, so an impossible point is visible before it is sent.
        warnings: List[str] = []
        floor_co2 = (core.MIN_CONTROLLABLE_FRACTION
                     * core.ln_to_sccm(core.CHANNELS["co2"].full_scale_ln))
        floor_n2 = (core.MIN_CONTROLLABLE_FRACTION
                    * core.ln_to_sccm(core.CHANNELS["n2"].full_scale_ln))
        if 0 < q_co2 < floor_co2:
            warnings.append(f"CO2 {q_co2:.1f} SCCM is below its {floor_co2:.0f} "
                            f"SCCM floor - flow will not be accurate")
        if 0 < q_n2 < floor_n2:
            warnings.append(f"N2 {q_n2:.1f} SCCM is below its {floor_n2:.0f} "
                            f"SCCM floor")
        if q_co2 > core.ln_to_sccm(core.CHANNELS["co2"].full_scale_ln):
            warnings.append("CO2 exceeds full scale")
        if percent > core.CYLINDER_CO2_FRACTION * 100:
            warnings.append(f"target exceeds the cylinder "
                            f"({core.CYLINDER_CO2_FRACTION * 100:.1f} %)")
        self.preview_warn.configure(text="\n".join(warnings))

    def _apply_blend(self) -> None:
        try:
            percent, total = self._parse_blend()
        except ValueError:
            messagebox.showerror("Invalid input", "Flow and target must be numbers.")
            return
        self.worker.send("blend", {"percent": percent, "total_sccm": total})

    def _flush(self) -> None:
        try:
            total = float(self.total_var.get())
        except ValueError:
            return
        self.worker.send("flush", {"total_sccm": total})

    def _all_stop(self) -> None:
        self.worker.send("stop_all")

    def _start_sequence(self) -> None:
        try:
            levels = [float(x) for x in self.levels_var.get().split(",") if x.strip()]
            hold = float(self.hold_var.get())
            flush = float(self.flush_var.get())
            total = float(self.total_var.get())
        except ValueError:
            messagebox.showerror("Invalid sequence",
                                 "Levels, hold and flush must be numbers.")
            return
        if not levels:
            messagebox.showerror("Invalid sequence", "No levels given.")
            return

        minutes = len(levels) * (hold + flush) / 60.0
        # An unattended run that opens gas deserves an explicit confirmation.
        if not messagebox.askokcancel(
                "Start sequence",
                f"{len(levels)} steps at {total:.0f} SCCM total.\n"
                f"Hold {hold:.0f} s, flush {flush:.0f} s each.\n"
                f"Estimated duration {minutes:.1f} minutes.\n\n"
                f"Gas will flow for the whole run. Continue?"):
            return

        steps = [{"percent_co2": level, "hold_s": hold, "total_sccm": total,
                  "flush_s": flush, "tag": f"co2_{level:g}pct"}
                 for level in levels]
        self.worker.send("run_sequence", {"steps": steps})

    def _abort_sequence(self) -> None:
        self.worker.send("abort_sequence")

    def _export_csv(self) -> None:
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "mfc_logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder,
                            "gui_" + time.strftime("%Y%m%d_%H%M%S") + ".csv")
        count = self.worker.export_csv(path)
        if count:
            self._log(f"Exported {count} rows to {path}")
        else:
            self._log("Nothing to export yet.", "warn")

    # -- telemetry ----------------------------------------------------------

    def _pump_telemetry(self) -> None:
        latest = None
        while True:
            try:
                latest = self.worker.telemetry.get_nowait()
            except Exception:                                      # noqa: BLE001
                break
            if latest.message:
                self._log(latest.message, latest.level)

        if latest is not None:
            self._render(latest)
        self.after(200, self._pump_telemetry)

    def _render(self, packet) -> None:
        if packet.connected != self.connected:
            self.connected = packet.connected
            self.connect_button.configure(
                text="Disconnect" if self.connected else "Connect")
            self.status_label.configure(
                text="  Online" if self.connected else "  Offline",
                foreground=GREEN if self.connected else MUTED)
            self._set_controls_enabled(self.connected)

        for key, state in packet.channels.items():
            card = self.cards.get(key)
            if card:
                card.update_state(state)

        if packet.sequence_active:
            done = max(0, packet.sequence_step - 1)
            self.seq_progress["value"] = 100.0 * done / max(packet.sequence_total, 1)
            self.seq_label.configure(
                text=f"step {packet.sequence_step}/{packet.sequence_total} "
                     f"({packet.sequence_label}) "
                     f"{packet.sequence_remaining_s:.0f} s remaining")
        else:
            self.seq_progress["value"] = 0
            self.seq_label.configure(text="idle")

        if PLOTTING and packet.connected:
            self._update_plot(packet)

    def _update_plot(self, packet) -> None:
        elapsed = packet.timestamp - self._t0
        self.history["t"].append(elapsed)
        for key in ("n2", "co2"):
            state = packet.channels.get(key)
            self.history[key].append(
                state.measured_sccm if state and state.measured_sccm else 0.0)

        if len(self.history["t"]) % 2:      # redraw every other packet
            return

        self.axes.clear()
        self.axes.set_facecolor(PANEL)
        self.axes.plot(self.history["t"], self.history["n2"],
                       color=ACCENT, linewidth=1.4, label="N2")
        self.axes.plot(self.history["t"], self.history["co2"],
                       color=GREEN, linewidth=1.4, label="CO2")
        self.axes.tick_params(colors=MUTED, labelsize=8)
        self.axes.set_xlabel("seconds", color=MUTED, fontsize=8)
        self.axes.set_ylabel("SCCM", color=MUTED, fontsize=8)
        legend = self.axes.legend(loc="upper right", fontsize=8,
                                  facecolor=PANEL, edgecolor=GREY)
        for text in legend.get_texts():
            text.set_color(MUTED)
        for spine in self.axes.spines.values():
            spine.set_color(GREY)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    # -- helpers ------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (self.apply_button, self.flush_button, self.zero_button,
                       self.start_button, self.abort_button):
            widget.configure(state=state)
        for card in self.cards.values():
            card.set_enabled(enabled)
        # ALL STOP stays live whenever there is a link to stop.
        self.stop_button.configure(state="normal" if enabled else "disabled")

    def _log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"{stamp}  {message}\n", level)
        self.log.see("end")
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.0")

    def _on_close(self) -> None:
        if self.connected and not messagebox.askokcancel(
                "Close", "This will zero both setpoints and disconnect. Continue?"):
            return
        self.worker.stop()
        self.worker.join(timeout=4.0)
        self.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="MFC gas blending GUI")
    parser.add_argument("--simulate", action="store_true",
                        help="run without hardware")
    args, _ = parser.parse_known_args(
        [a for a in sys.argv[1:] if a != "--wdir"])

    app = MFCApp(simulate=args.simulate)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
