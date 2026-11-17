# -*- coding: utf-8 -*-
"""
combined_gui.py -- one window for the SR830 lock-in and the CO2/N2 gas bench.

    python combined_gui.py                     both instruments live
    python combined_gui.py --demo              fake lock-in, simulated MFCs
    python combined_gui.py --prologix --resource COM3 --addr 8

NOTHING IN THE EXISTING FILES IS MODIFIED. This module imports them and
drives their public surface:

    sr830_gui.App          embedded whole, as a tab (it is a ttk.Frame)
    App.meta.set(...)      writes experiment context -> becomes CSV columns
    App.worker.mark(...)   writes "# MARK t=..." comment lines into the CSV
    mfc_worker.DeviceWorker    the same threaded serial owner as before
    mfc_gui.ChannelCard        the same per-MFC display widget

HOW THE SINGLE-FILE LOGGING WORKS
    Your SR830 worker already appends every key of Worker.extra as extra
    columns on each sampled row, and MetadataPanel already carries the fields
    gas_matrix / analyte / analyte_sccm / matrix_sccm / conc_pct / note.
    So the linked runner does not need a CSV of its own: at every gas
    transition it updates those fields and drops a MARK line. The lock-in
    keeps writing to the one open file throughout, which is what lets you
    plot signal against CO2 concentration from a single CSV afterwards.

    Consequence worth knowing: the CSV column set is fixed when recording
    starts. Start the recording BEFORE the run, and let it run across every
    blend and flush. Stopping and restarting gives you a second file, which
    is the thing you asked to avoid.

WHAT WAS ADDED RATHER THAN CHANGED
    - a linked sequence runner that drives the MFCs and annotates the
      lock-in CSV at each transition
    - two fixed presets plus the existing free-form custom series
    - a feasibility check on a preset before it is allowed to start
"""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

import mfc_control as core
import mfc_gui
import mfc_worker
import itc_panel
import itc_worker
import scope_panel
import sr830_gui
from sr830 import SR830
from sr830_gui import App as SR830App, FakeSR830

# Colours reused from the MFC panel so the cards look as they did.
PANEL = mfc_gui.PANEL
FG = mfc_gui.FG
MUTED = mfc_gui.MUTED
ACCENT = mfc_gui.ACCENT
GREEN = mfc_gui.GREEN
AMBER = mfc_gui.AMBER
RED = mfc_gui.RED
GREY = mfc_gui.GREY

LIGHT_BG = "#f4f4f4"
LIGHT_FG = "#1a1a1a"


# ==========================================================================
# Presets
# ==========================================================================

def ramp(start: float, stop: float, step: float) -> List[float]:
    """Inclusive ramp that avoids floating-point drift on the last point."""
    n = int(round((stop - start) / step))
    return [round(start + i * step, 6) for i in range(n + 1)]


PRESETS: Dict[str, List[float]] = {
    "Preset A  12-100 % in 2 % steps": ramp(12, 100, 2),
    "Preset B  15-100 % in 5 % steps": ramp(15, 100, 5),
}
CUSTOM_LABEL = "Custom series (type levels below)"

# Extra CSV columns contributed by instruments the SR830 metadata panel does
# not know about. MetadataPanel.set() ignores keys that are not its own
# fields, and sr830_gui fixes the column set when the file is opened, so
# these have to be merged into worker.extra -- and seeded before recording
# starts, or they never become columns at all.
SCOPE_COLUMNS = ("scope_vpp_mV", "scope_ac_rms_mV", "scope_1f_mV",
                 "scope_2f_mV", "scope_snr_dB")


# ==========================================================================
# Process limit
# ==========================================================================
# Total flow through the ADM01 must not exceed 0.200 ln/min (200 SCCM).
# Above that the cell sees flow noise and back-pressure it was not intended
# for, and the acoustic measurement degrades in a way that looks like a
# spectroscopy problem rather than a plumbing one.
#
# Defaults alone do not enforce this -- an operator can type any number into
# the total-flow box, and the per-channel Set buttons bypass the blend maths
# completely. So the limit is checked on EVERY path that can command flow:
# manual blend, manual flush, a per-channel setpoint, and starting a run.
#
# Deliberately a refusal, not a silent clamp: quietly running 200 when 800
# was asked for produces data that looks fine and is not what the operator
# believed they measured.
MAX_TOTAL_SCCM = 200.0


def total_within_limit(total_sccm: float) -> bool:
    return total_sccm <= MAX_TOTAL_SCCM + 1e-9


# ==========================================================================
# Linked runner
# ==========================================================================

class LinkedRunner:
    """
    Sequence engine for the combined run.

    Deliberately driven from the Tk thread with after(), not from a worker:
    every transition has to touch Tk widgets (the metadata panel) AND the MFC
    worker queue. Timing resolution is 200 ms against steps measured in
    minutes, so nothing is lost, and it keeps all Tk calls on the Tk thread
    where they belong.

    The MFC serial traffic still happens in DeviceWorker, and the lock-in
    traffic still happens in the SR830 Worker. This only sequences them.
    """

    TICK_MS = 200

    def __init__(self, app: "CombinedApp"):
        self.app = app
        self.steps: List[dict] = []
        self.index = 0
        self.phase = ""          # "" | "flush" | "hold"
        self.deadline = 0.0
        self.active = False
        self._tick_job = None

    # -- control -----------------------------------------------------------

    def start(self, levels: List[float], hold_s: float, flush_s: float,
              total_sccm: float) -> None:
        self.steps = [{"percent": level, "hold_s": hold_s,
                       "flush_s": flush_s, "total_sccm": total_sccm}
                      for level in levels]
        self.index = 0
        self.phase = ""
        self.deadline = 0.0
        self.active = True
        self.app.log(f"Linked run started: {len(self.steps)} levels, "
                     f"{hold_s:.0f} s hold, {flush_s:.0f} s flush, "
                     f"{total_sccm:.0f} SCCM total")
        self.app.annotate("run_start",
                          f"levels={len(self.steps)} hold={hold_s:.0f}s "
                          f"flush={flush_s:.0f}s total={total_sccm:.0f}sccm")
        self._tick()

    def abort(self, reason: str = "aborted by operator") -> None:
        if not self.active:
            return
        self.active = False
        if self._tick_job:
            self.app.after_cancel(self._tick_job)
            self._tick_job = None
        self.app.mfc.send("stop_all")
        self.app.annotate("run_abort", reason)
        self.app.log(f"Linked run {reason}; flows zeroed", "warn")

    # -- engine ------------------------------------------------------------

    def _tick(self) -> None:
        if not self.active:
            return
        now = time.monotonic()
        if now >= self.deadline:
            self._advance(now)
        self.app.update_run_status(self)
        if self.active:
            self._tick_job = self.app.after(self.TICK_MS, self._tick)

    def _advance(self, now: float) -> None:
        if self.index >= len(self.steps):
            self.active = False
            self.app.mfc.send("stop_all")
            self.app.annotate("run_end", "sequence complete, flows zeroed")
            self.app.log("Linked run complete; flows zeroed")
            return

        step = self.steps[self.index]
        total = step["total_sccm"]

        # Flush first when one is configured, so each concentration starts
        # from a swept cell rather than the previous mixture.
        if self.phase == "" and step["flush_s"] > 0:
            self.phase = "flush"
            self.deadline = now + step["flush_s"]
            self.app.mfc.send("flush", {"total_sccm": total})
            self.app.set_context(phase="flush", percent=0.0,
                                 q_n2=total, q_co2=0.0,
                                 step_i=self.index + 1, n=len(self.steps))
            return

        if self.phase in ("", "flush"):
            self.phase = "hold"
            self.deadline = now + step["hold_s"]
            percent = step["percent"]
            q_co2 = total * (percent / 100.0) / core.CYLINDER_CO2_FRACTION
            q_n2 = max(0.0, total - q_co2)
            self.app.mfc.send("blend", {"percent": percent,
                                        "total_sccm": total})
            self.app.set_context(phase="hold", percent=percent,
                                 q_n2=q_n2, q_co2=q_co2,
                                 step_i=self.index + 1, n=len(self.steps))
            return

        self.phase = ""
        self.index += 1

    # -- reporting ---------------------------------------------------------

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def total_duration_s(self) -> float:
        return sum(s["hold_s"] + s["flush_s"] for s in self.steps)


# ==========================================================================
# Application
# ==========================================================================

class CombinedApp(tk.Tk):
    def __init__(self, instrument, simulate_mfc: bool = False):
        super().__init__()
        self.title("QEPAS bench -- SR830 lock-in and CO2/N2 gas blending")
        self.geometry("1320x960")
        self.minsize(1150, 820)

        self.simulate_mfc = simulate_mfc
        self.mfc = mfc_worker.DeviceWorker()
        self.mfc.start()
        self.cards: Dict[str, mfc_gui.ChannelCard] = {}
        self.setpoints: Dict[str, float] = {}
        self.mfc_connected = False
        self.runner = LinkedRunner(self)

        self._build_styles()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # The SR830 GUI is embedded whole. It keeps its own worker, its own
        # CSV, its own plot -- nothing about how you drive the lock-in changes.
        lockin_tab = ttk.Frame(notebook)
        notebook.add(lockin_tab, text="  Lock-in (SR830)  ")
        self.sr = SR830App(lockin_tab, instrument)

        gas_tab = ttk.Frame(notebook)
        notebook.add(gas_tab, text="  Gas bench (MFC)  ")
        self._build_gas_tab(gas_tab)

        laser_tab = ttk.Frame(notebook)
        notebook.add(laser_tab, text="  Laser / TEC (ITC4005)  ")
        self.itc = itc_panel.ITCPanel(laser_tab, simulate=self.simulate_mfc)
        self.itc.pack(fill="both", expand=True)

        self._install_extra_columns()

        scope_tab = ttk.Frame(notebook)
        notebook.add(scope_tab, text="  Scope (TBS1052B)  ")
        self.scope = scope_panel.ScopePanel(scope_tab,
                                            simulate=self.simulate_mfc)
        self.scope.pack(fill="both", expand=True)

        run_tab = ttk.Frame(notebook)
        notebook.add(run_tab, text="  Linked run  ")
        self._build_run_tab(run_tab)

        self._build_statusbar()
        self._set_gas_controls(False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._pump_mfc)

    # -- styling -----------------------------------------------------------

    def _build_styles(self) -> None:
        """
        Only the MFC card styles are dark. Base ttk styles are left light so
        the embedded SR830 panel looks exactly as it does standalone.
        """
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=LIGHT_BG)
        style.configure("TLabel", background=LIGHT_BG, foreground=LIGHT_FG)
        style.configure("TLabelframe", background=LIGHT_BG)
        style.configure("TLabelframe.Label", background=LIGHT_BG,
                        foreground="#444")
        style.configure("TNotebook", background=LIGHT_BG)

        style.configure("Panel.TFrame", background=PANEL, relief="flat")
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED,
                        font=("Segoe UI", 8))
        style.configure("CardTitle.TLabel", background=PANEL, foreground=FG,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Reading.TLabel", background=PANEL, foreground=ACCENT,
                        font=("Consolas", 22, "bold"))
        style.configure("Value.TLabel", background=PANEL, foreground=FG,
                        font=("Consolas", 12))
        style.configure("Error.TLabel", background=PANEL, foreground=RED,
                        font=("Segoe UI", 8))
        style.configure("Stop.TButton", font=("Segoe UI", 11, "bold"),
                        padding=8, foreground="#ffffff", background=RED)
        style.map("Stop.TButton", background=[("active", "#c23b34")])

    # -- gas tab -----------------------------------------------------------

    def _install_extra_columns(self) -> None:
        """
        Make the scope columns part of every CSV row.

        sr830_gui sets worker.extra = panel.values() on every edit, which
        would drop anything this GUI adds. Rather than editing their file,
        the panel's callback is wrapped so the merged dict is what reaches
        the worker, and it is pushed once now so the keys exist before the
        operator presses START RECORDING.
        """
        self._scope_fields = {k: "" for k in SCOPE_COLUMNS}
        meta = self.sr.meta

        def merged(values):
            out = dict(values)
            out.update(self._scope_fields)
            self.sr.worker.extra = out

        meta._on_change = merged
        meta._changed()

    def _push_meta(self) -> None:
        """Re-send the merged context after only the scope fields changed."""
        try:
            self.sr.meta._changed()
        except Exception:                                          # noqa: BLE001
            pass

    def _build_gas_tab(self, parent) -> None:
        bar = ttk.Frame(parent, padding=(8, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="COM port").pack(side="left")
        self.port_var = tk.StringVar(value=str(core.PORT))
        ttk.Entry(bar, textvariable=self.port_var, width=5).pack(
            side="left", padx=(6, 12))

        self.sim_var = tk.BooleanVar(value=self.simulate_mfc)
        ttk.Checkbutton(bar, text="Simulate MFCs",
                        variable=self.sim_var).pack(side="left", padx=(0, 12))

        self.connect_btn = ttk.Button(bar, text="Connect",
                                      command=self._toggle_mfc)
        self.connect_btn.pack(side="left")
        self.gas_status = ttk.Label(bar, text="  Offline", foreground="#777")
        self.gas_status.pack(side="left", padx=10)

        ttk.Button(bar, text="ALL STOP", style="Stop.TButton",
                   command=self._all_stop).pack(side="right")

        cards = ttk.Frame(parent, padding=(8, 0))
        cards.pack(fill="x")
        for key, cfg in core.CHANNELS.items():
            card = mfc_gui.ChannelCard(cards, key, cfg, self._set_channel)
            card.pack(side="left", padx=(0, 10), fill="y")
            self.cards[key] = card

        blend = ttk.LabelFrame(parent, text=" Manual blend ", padding=10)
        blend.pack(fill="x", padx=8, pady=8)

        grid = ttk.Frame(blend)
        grid.pack(side="left")
        ttk.Label(grid, text="Total flow").grid(row=0, column=0, sticky="w")
        self.total_var = tk.StringVar(value=str(int(core.DEFAULT_TOTAL_SCCM)))
        ttk.Entry(grid, textvariable=self.total_var, width=8).grid(
            row=0, column=1, padx=6)
        ttk.Label(grid,
                  text=f"SCCM   (max {MAX_TOTAL_SCCM:.0f})").grid(
            row=0, column=2, sticky="w")

        ttk.Label(grid, text="CO2 target %").grid(row=1, column=0, sticky="w",
                                                  pady=6)
        self.conc_var = tk.StringVar(value="45")
        ttk.Entry(grid, textvariable=self.conc_var, width=8).grid(
            row=1, column=1, padx=6)

        self.blend_preview = ttk.Label(blend, text="", justify="left",
                                       font=("Consolas", 10))
        self.blend_preview.pack(side="left", padx=24)

        # The typed value and the sent value are different things. Without
        # this, a target sitting in the entry box looks identical to one that
        # was actually commanded, which is exactly how you end up believing
        # you ran 100 % when the hardware was given 0 %.
        self.applied_label = ttk.Label(blend, justify="left",
                                       font=("Consolas", 10),
                                       foreground="#0a5c2a",
                                       text="last APPLIED: nothing yet")
        self.applied_label.pack(side="left", padx=24)
        for var in (self.total_var, self.conc_var):
            var.trace_add("write", lambda *_: self._update_preview())

        buttons = ttk.Frame(blend)
        buttons.pack(side="right")
        self.apply_btn = ttk.Button(buttons, text="Apply blend",
                                    command=self._apply_blend)
        self.apply_btn.pack(side="left", padx=4)
        self.flush_btn = ttk.Button(buttons, text="Flush N2",
                                    command=self._flush)
        self.flush_btn.pack(side="left", padx=4)
        self.zero_btn = ttk.Button(buttons, text="Zero both",
                                   command=self._all_stop)
        self.zero_btn.pack(side="left", padx=4)

        self._update_preview()
        self._build_pressure_panel(parent)

    def _build_pressure_panel(self, parent) -> None:
        """
        Pressure read and control, per channel.

        Controls enable only for a channel whose PRESSURE_SCALE_MIN/MAX shows a
        fitted sensor. Writing a pressure setpoint to a unit without one is
        accepted by the register and does nothing.
        """
        box = ttk.LabelFrame(parent, text=" Pressure ", padding=10)
        box.pack(fill="x", padx=8, pady=(0, 8))

        top = ttk.Frame(box); top.pack(fill="x")
        ttk.Label(top, text="Channel").pack(side="left")
        self.p_channel = tk.StringVar(value="monitor")
        labels = {k: c.label for k, c in core.CHANNELS.items()}
        self._p_labels = labels
        combo = ttk.Combobox(top, width=14, state="readonly",
                             values=[labels[k] for k in labels])
        combo.set(labels["monitor"])
        combo.pack(side="left", padx=(6, 18))
        combo.bind("<<ComboboxSelected>>",
                   lambda e: self.p_channel.set(
                       {v: k for k, v in labels.items()}[combo.get()]))

        self.p_readout = ttk.Label(top, text="", font=("Consolas", 10))
        self.p_readout.pack(side="left")

        row = ttk.Frame(box); row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Setpoint").pack(side="left")
        self.p_sp_entry = ttk.Entry(row, width=9); self.p_sp_entry.pack(side="left", padx=4)
        self.p_sp_unit = ttk.Label(row, text=""); self.p_sp_unit.pack(side="left")
        self.p_sp_btn = ttk.Button(row, text="Set pressure",
                                   command=self._set_pressure)
        self.p_sp_btn.pack(side="left", padx=(6, 22))

        ttk.Label(row, text="Flow limit").pack(side="left")
        self.p_lim_entry = ttk.Entry(row, width=8); self.p_lim_entry.pack(side="left", padx=4)
        ttk.Label(row, text="SCCM").pack(side="left")
        self.p_lim_btn = ttk.Button(row, text="Set limit",
                                    command=self._set_pressure_limit)
        self.p_lim_btn.pack(side="left", padx=(6, 22))

        ttk.Label(row, text="Mode").pack(side="left")
        self.p_mode_entry = ttk.Entry(row, width=5); self.p_mode_entry.pack(side="left", padx=4)
        self.p_mode_btn = ttk.Button(row, text="Set mode",
                                     command=self._set_pressure_mode)
        self.p_mode_btn.pack(side="left", padx=6)

        self.p_note = ttk.Label(box, text="", foreground="#a06000",
                                wraplength=1150, justify="left")
        self.p_note.pack(anchor="w", pady=(6, 0))

        # Redraw immediately on a channel change. Waiting for the next poll
        # shows the PREVIOUS channel's readings under the new channel's name
        # for up to a second -- long enough to act on the wrong numbers.
        self.p_channel.trace_add("write", lambda *_: self._render_pressure())

    def _p_state(self):
        return getattr(self, "_mfc_states", {}).get(self.p_channel.get())

    def _set_pressure(self) -> None:
        try:
            value = float(self.p_sp_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Pressure setpoint must be a number.")
            return
        st = self._p_state()
        if st is not None and st.pressure_mode == 0 and not messagebox.askokcancel(
                "Unit is in flow-control mode",
                "This unit is not in pressure-control mode, so the setpoint "
                "will be stored but will not control anything until the mode "
                "is changed.\n\nStore it anyway?"):
            return
        self.mfc.send("pressure_setpoint",
                      {"channel": self.p_channel.get(), "value": value})

    def _set_pressure_limit(self) -> None:
        try:
            sccm = float(self.p_lim_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Flow limit must be a number in SCCM.")
            return
        if sccm > MAX_TOTAL_SCCM and not messagebox.askokcancel(
                "Above the cell limit",
                f"{sccm:.0f} SCCM is above the {MAX_TOTAL_SCCM:.0f} SCCM cell "
                f"limit. While holding pressure the controller may flow up "
                f"to this much.\n\nSet it anyway?"):
            return
        self.mfc.send("pressure_flow_limit",
                      {"channel": self.p_channel.get(), "sccm": sccm})

    def _set_pressure_mode(self) -> None:
        try:
            mode = int(self.p_mode_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Mode must be an integer.")
            return
        key = self.p_channel.get()
        name = core.PRESSURE_MODE_NAMES.get(mode, "MEANING NOT YET KNOWN")
        blend_warning = ""
        if key in ("n2", "co2"):
            blend_warning = ("\n\nThis is one of the two blending channels. "
                             "In pressure-control mode it stops regulating "
                             "flow, so the CO2/N2 blend will no longer be "
                             "what the GUI reports.")
        if not messagebox.askokcancel(
                "Change control mode",
                f"Write PRESSURE_CONTROL_MODE = {mode} ({name}) to "
                f"{self._p_labels[key]}?{blend_warning}\n\nThe register map "
                f"does not document the mode numbers. Confirm the value in "
                f"get red-y first (see --pressure-info)."):
            return
        self.mfc.send("pressure_mode", {"channel": key, "mode": mode})

    def _render_pressure(self) -> None:
        st = self._p_state()
        widgets = (self.p_sp_entry, self.p_sp_btn, self.p_lim_entry,
                   self.p_lim_btn, self.p_mode_entry, self.p_mode_btn)
        if st is None or not st.online:
            self.p_readout.configure(text="not connected")
            for w in widgets: w.configure(state="disabled")
            return
        if not st.has_pressure_sensor:
            self.p_readout.configure(
                text=f"no pressure sensor fitted "
                     f"(scale {st.pressure_min}-{st.pressure_max})")
            self.p_note.configure(text="This unit cannot measure or control "
                                       "pressure. Choose another channel.")
            for w in widgets: w.configure(state="disabled")
            return

        unit = st.pressure_unit or ""
        mode_name = core.PRESSURE_MODE_NAMES.get(st.pressure_mode,
                                                 "meaning unknown")
        limit = ("--" if st.pressure_flow_limit_sccm is None
                 else f"{st.pressure_flow_limit_sccm:.1f} SCCM")
        self.p_readout.configure(
            text=f"measured {st.pressure:.4g} {unit}   "
                 f"setpoint {st.pressure_setpoint} {unit}   "
                 f"range {st.pressure_min:g}-{st.pressure_max:g} {unit}   "
                 f"mode {st.pressure_mode} ({mode_name})   flow limit {limit}")
        self.p_sp_unit.configure(text=unit)
        self.p_note.configure(
            text="" if core.PRESSURE_MODE_NAMES else
            "Mode numbers are not documented. Learn them with "
            "`mfc_control.py --pressure-info` (see README) before switching.")
        for w in widgets: w.configure(state="normal")

    # -- linked run tab ----------------------------------------------------

    def _build_run_tab(self, parent) -> None:
        top = ttk.LabelFrame(parent, text=" Series ", padding=10)
        top.pack(fill="x", padx=8, pady=8)

        self.preset_var = tk.StringVar(value=list(PRESETS)[0])
        for label in list(PRESETS) + [CUSTOM_LABEL]:
            ttk.Radiobutton(top, text=label, value=label,
                            variable=self.preset_var,
                            command=self._preset_changed).pack(anchor="w")

        custom = ttk.Frame(top)
        custom.pack(fill="x", pady=(6, 0))
        ttk.Label(custom, text="Custom levels %").pack(side="left")
        self.levels_var = tk.StringVar(value="20,30,40,50,60")
        self.levels_entry = ttk.Entry(custom, textvariable=self.levels_var,
                                      width=48)
        self.levels_entry.pack(side="left", padx=6)
        self.levels_var.trace_add("write", lambda *_: self._update_plan())

        timing = ttk.LabelFrame(parent, text=" Timing (applies to any series) ",
                                padding=10)
        timing.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(timing, text="Blend hold").grid(row=0, column=0, sticky="w")
        self.hold_var = tk.StringVar(value="300")
        ttk.Entry(timing, textvariable=self.hold_var, width=8).grid(
            row=0, column=1, padx=6)
        ttk.Label(timing, text="s at each CO2 level").grid(row=0, column=2,
                                                           sticky="w")

        ttk.Label(timing, text="N2 flush").grid(row=1, column=0, sticky="w",
                                                pady=6)
        self.flushs_var = tk.StringVar(value="60")
        ttk.Entry(timing, textvariable=self.flushs_var, width=8).grid(
            row=1, column=1, padx=6)
        ttk.Label(timing, text="s before each level (0 to skip)").grid(
            row=1, column=2, sticky="w")

        for var in (self.hold_var, self.flushs_var):
            var.trace_add("write", lambda *_: self._update_plan())

        plan = ttk.LabelFrame(parent, text=" Plan ", padding=10)
        plan.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.plan_label = ttk.Label(plan, text="", justify="left",
                                    font=("Consolas", 9))
        self.plan_label.pack(anchor="w")
        self.plan_warn = ttk.Label(plan, text="", justify="left",
                                   foreground="#a06000", wraplength=1100)
        self.plan_warn.pack(anchor="w", pady=(6, 0))

        controls = ttk.Frame(parent, padding=(8, 0))
        controls.pack(fill="x")
        self.start_btn = ttk.Button(controls, text="Start linked run",
                                    command=self._start_run)
        self.start_btn.pack(side="left")
        self.abort_btn = ttk.Button(controls, text="Abort",
                                    command=lambda: self.runner.abort())
        self.abort_btn.pack(side="left", padx=6)
        ttk.Button(controls, text="Mark now",
                   command=self._manual_mark).pack(side="left", padx=6)

        self.run_progress = ttk.Progressbar(parent, maximum=100)
        self.run_progress.pack(fill="x", padx=8, pady=(8, 2))
        self.run_status = ttk.Label(parent, text="idle")
        self.run_status.pack(anchor="w", padx=8)

        self._preset_changed()

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x", side="bottom")
        self.status_line = ttk.Label(bar, text="ready", foreground="#444")
        self.status_line.pack(side="left")
        self.rec_line = ttk.Label(bar, text="", foreground="#a06000")
        self.rec_line.pack(side="right")

    # -- plan / feasibility -------------------------------------------------

    def _preset_changed(self) -> None:
        custom = self.preset_var.get() == CUSTOM_LABEL
        self.levels_entry.configure(state="normal" if custom else "disabled")
        self._update_plan()

    def current_levels(self) -> List[float]:
        label = self.preset_var.get()
        if label in PRESETS:
            return list(PRESETS[label])
        try:
            return [float(x) for x in self.levels_var.get().split(",")
                    if x.strip()]
        except ValueError:
            return []

    def _update_plan(self) -> None:
        # The gas tab is built before the run tab, and its preview calls in
        # here; skip until the run widgets exist.
        if not hasattr(self, "preset_var"):
            return
        levels = self.current_levels()
        try:
            hold = float(self.hold_var.get())
            flush = float(self.flushs_var.get())
            total = float(self.total_var.get())
        except (ValueError, AttributeError):
            self.plan_label.configure(text="enter numeric timing values")
            return

        if not levels:
            self.plan_label.configure(text="no levels selected")
            self.plan_warn.configure(text="")
            return

        seconds = len(levels) * (hold + flush)
        self.plan_label.configure(
            text=f"{len(levels)} levels: "
                 f"{levels[0]:g} % to {levels[-1]:g} %\n"
                 f"{hold:.0f} s hold + {flush:.0f} s flush each\n"
                 f"total duration {seconds / 3600:.2f} h "
                 f"({seconds / 60:.0f} min)\n"
                 f"total flow {total:.0f} SCCM")

        # Feasibility, computed the same way mfc_control does it, so the
        # limits shown here match --feasibility exactly.
        floor_co2 = (core.MIN_CONTROLLABLE_FRACTION
                     * core.ln_to_sccm(core.CHANNELS["co2"].full_scale_ln))
        floor_n2 = (core.MIN_CONTROLLABLE_FRACTION
                    * core.ln_to_sccm(core.CHANNELS["n2"].full_scale_ln))
        bad_co2, bad_n2 = [], []
        for level in levels:
            q_co2 = total * level / 100.0 / core.CYLINDER_CO2_FRACTION
            q_n2 = total - q_co2
            if 0 < q_co2 < floor_co2:
                bad_co2.append(level)
            if 0 < q_n2 < floor_n2:
                bad_n2.append(level)

        warnings = []
        if not total_within_limit(total):
            warnings.append(
                f"Total flow {total:.0f} SCCM exceeds the "
                f"{MAX_TOTAL_SCCM:.0f} SCCM cell limit -- the run will be "
                f"refused until this is lowered")
        if bad_co2:
            warnings.append(
                f"CO2 below its {floor_co2:.0f} SCCM floor at: "
                f"{', '.join(f'{v:g}%' for v in bad_co2)}")
        if bad_n2:
            warnings.append(
                f"N2 below its {floor_n2:.0f} SCCM floor at: "
                f"{', '.join(f'{v:g}%' for v in bad_n2)} "
                f"(100 % is fine -- the N2 valve is simply shut)")
        self.plan_warn.configure(text="\n".join(warnings))

    # -- annotation ---------------------------------------------------------

    def set_context(self, phase: str, percent: float, q_n2: float,
                    q_co2: float, step_i: int, n: int) -> None:
        """
        Push the gas state into the lock-in's metadata panel and drop a MARK.

        This is the whole integration: MetadataPanel fields are already CSV
        columns in your SR830 worker, so every subsequent lock-in row carries
        the concentration that produced it.
        """
        fields = dict(
            gas_matrix="N2",
            analyte="CO2",
            conc_pct=f"{percent:g}",
            analyte_sccm=f"{q_co2:.2f}",
            matrix_sccm=f"{q_n2:.2f}",
            note=f"{phase} step {step_i}/{n}",
        )
        # Laser state travels with the data too. The Allan analysis of the
        # first run showed drift that averaging could not remove; logging
        # actual TEC temperature and LD current alongside every lock-in row
        # is what lets you test whether that drift tracks the laser.
        laser = getattr(self, "itc", None)
        if laser is not None and laser.state.connected:
            if laser.state.temp_c is not None:
                fields["tec_setpoint_C"] = f"{laser.state.temp_c:.4f}"
            if laser.state.current_ma is not None:
                fields["ld_current_mA"] = f"{laser.state.current_ma:.3f}"

        # Scope measurements too, when it has captured something. The lock-in
        # reports the demodulated 2f amplitude; the scope reports the raw
        # waveform it was demodulated from. Having both on the same row is
        # what distinguishes "the gas changed" from "the drive changed".
        scope = getattr(self, "scope", None)
        if scope is not None and scope.connected:
            latest = scope.latest() or {}
            def mv(key):
                v = latest.get(key)
                return "" if v is None else f"{v * 1e3:.4f}"
            snr = latest.get("snr_db")
            self._scope_fields.update({
                "scope_vpp_mV": mv("vpp"),
                "scope_ac_rms_mV": mv("ac_rms"),
                "scope_1f_mV": mv("target_amp"),
                "scope_2f_mV": mv("harm_amp"),
                "scope_snr_dB": "" if snr is None else f"{snr:.2f}",
            })

        self.sr.meta.set(**fields)   # panel fields; fires the merged callback
        self._push_meta()            # covers the scope-only case
        self.annotate(phase,
                      f"step {step_i}/{n} CO2={percent:g}% "
                      f"N2={q_n2:.2f}sccm CO2={q_co2:.2f}sccm")
        if hasattr(self, "applied_label"):
            self.applied_label.configure(
                text=f"last APPLIED  {time.strftime('%H:%M:%S')}\n"
                     f"  {phase}: CO2 {percent:g} %\n"
                     f"  N2 {q_n2:.2f} + CO2 {q_co2:.2f} SCCM")
        self.log(f"{phase}: step {step_i}/{n}, CO2 {percent:g} %, "
                 f"N2 {q_n2:.1f} + CO2 {q_co2:.1f} SCCM")

    def annotate(self, tag: str, detail: str) -> None:
        """Write a MARK comment into the lock-in CSV, if it is recording."""
        try:
            self.sr.worker.mark(f"{tag} :: {detail}")
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"could not mark CSV: {exc}", "warn")

    def _manual_mark(self) -> None:
        self.annotate("manual", time.strftime("%H:%M:%S"))
        self.log("manual mark written")

    def _recording(self) -> bool:
        """True when the SR830 worker has a CSV open."""
        return getattr(self.sr.worker, "_csv_writer", None) is not None

    # -- gas actions --------------------------------------------------------

    def _toggle_mfc(self) -> None:
        if self.mfc_connected:
            self.mfc.send("disconnect")
        else:
            try:
                port = int(self.port_var.get())
            except ValueError:
                messagebox.showerror("Invalid port", "COM port must be a number.")
                return
            self.mfc.send("connect", {"port": port,
                                      "simulate": self.sim_var.get()})

    def _set_channel(self, key: str, sccm: float) -> None:
        """
        Guard a single-channel setpoint against the cell's total-flow limit.

        The card only knows its own channel, so the check has to consider what
        the other controlled head is already flowing.
        """
        other = sum(state_sccm for k, state_sccm in self.setpoints.items()
                    if k != key and k != "monitor")
        if not total_within_limit(sccm + other):
            messagebox.showerror(
                "Total flow limit",
                f"{sccm:.0f} SCCM on {key.upper()} plus {other:.0f} SCCM on "
                f"the other channel is {sccm + other:.0f} SCCM total.\n\n"
                f"The cell limit is {MAX_TOTAL_SCCM:.0f} SCCM "
                f"({MAX_TOTAL_SCCM / 1000:.3f} ln/min). Setpoint not sent.")
            return
        self.mfc.send("setpoint", {"channel": key, "sccm": sccm})

    def _update_preview(self) -> None:
        try:
            total = float(self.total_var.get())
            percent = float(self.conc_var.get())
        except ValueError:
            self.blend_preview.configure(text="enter numbers")
            return
        q_co2 = total * percent / 100.0 / core.CYLINDER_CO2_FRACTION
        q_n2 = max(0.0, total - q_co2)
        self._pending_percent = percent
        over = "" if total_within_limit(total) else (
            f"\n  OVER LIMIT: {total:.0f} > {MAX_TOTAL_SCCM:.0f} SCCM")
        self.blend_preview.configure(
            text=f"calculated setpoints (not yet sent)\n"
                 f"  N2  {q_n2:8.2f} SCCM\n"
                 f"  CO2 {q_co2:8.2f} SCCM{over}",
            foreground="#b00" if over else "#1a1a1a")
        self._update_plan()

    def _apply_blend(self) -> None:
        try:
            total = float(self.total_var.get())
            percent = float(self.conc_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Flow and target must be numbers.")
            return
        if not self._check_total(total):
            return
        self.mfc.send("blend", {"percent": percent, "total_sccm": total})
        q_co2 = total * percent / 100.0 / core.CYLINDER_CO2_FRACTION
        self.set_context("manual_blend", percent, total - q_co2, q_co2, 1, 1)

    def _flush(self) -> None:
        try:
            total = float(self.total_var.get())
        except ValueError:
            return
        if not self._check_total(total):
            return
        self.mfc.send("flush", {"total_sccm": total})
        self.set_context("manual_flush", 0.0, total, 0.0, 1, 1)

    def _check_total(self, total: float) -> bool:
        if total_within_limit(total):
            return True
        messagebox.showerror(
            "Total flow limit",
            f"{total:.0f} SCCM exceeds the {MAX_TOTAL_SCCM:.0f} SCCM "
            f"({MAX_TOTAL_SCCM / 1000:.3f} ln/min) limit for the cell.\n\n"
            f"Nothing was sent. Lower the total flow.")
        return False

    def _all_stop(self) -> None:
        self.runner.abort("ALL STOP pressed")
        self.mfc.send("stop_all")
        self.annotate("all_stop", "every setpoint zeroed")

    # -- run control --------------------------------------------------------

    def _start_run(self) -> None:
        if not self.mfc_connected:
            messagebox.showerror("Not connected",
                                 "Connect to the MFCs before starting a run.")
            return
        levels = self.current_levels()
        if not levels:
            messagebox.showerror("No levels", "The series is empty.")
            return
        try:
            hold = float(self.hold_var.get())
            flush = float(self.flushs_var.get())
            total = float(self.total_var.get())
        except ValueError:
            messagebox.showerror("Invalid timing", "Timing values must be numbers.")
            return

        if not self._check_total(total):
            return

        laser = getattr(self, "itc", None)
        if laser is not None and laser.state.connected and not laser.state.ld_on:
            if not messagebox.askokcancel(
                    "Laser is off",
                    "The ITC reports the laser is not emitting. A run now "
                    "would step gas and record baseline noise only.\n\n"
                    "Start anyway?"):
                return

        # A scope that is connected but idle would stamp every row of a
        # multi-hour run with one stale capture, which looks like data and
        # is not.
        scope = getattr(self, "scope", None)
        if (scope is not None and scope.connected
                and not scope.worker.streaming):
            if not messagebox.askokcancel(
                    "Scope is not streaming",
                    "The scope is connected but not running, so every row "
                    "would carry the same stale capture.\n\n"
                    "Press Run on the Scope tab first, or continue without "
                    "scope columns being meaningful.\n\nContinue anyway?"):
                return

        seconds = len(levels) * (hold + flush)
        recording = self._recording()
        warning = "" if recording else (
            "\n\nWARNING: the lock-in is NOT recording to a CSV.\n"
            "Gas will be stepped but nothing will be logged. Start the "
            "recording on the Lock-in tab first.")

        if not messagebox.askokcancel(
                "Start linked run",
                f"{len(levels)} levels from {levels[0]:g} % to {levels[-1]:g} % "
                f"CO2\n"
                f"{hold:.0f} s hold + {flush:.0f} s flush at {total:.0f} SCCM\n"
                f"Estimated duration {seconds / 3600:.2f} h\n\n"
                f"Gas flows for the whole run.{warning}\n\nContinue?"):
            return

        self.runner.start(levels, hold, flush, total)

    def update_run_status(self, runner: LinkedRunner) -> None:
        if not runner.active:
            self.run_progress["value"] = 0
            self.run_status.configure(text="idle")
            return
        done = max(0, runner.index)
        self.run_progress["value"] = 100.0 * done / max(len(runner.steps), 1)
        step = runner.steps[min(runner.index, len(runner.steps) - 1)]
        self.run_status.configure(
            text=f"step {runner.index + 1}/{len(runner.steps)}  "
                 f"{step['percent']:g} % CO2  phase {runner.phase}  "
                 f"{runner.remaining_s():.0f} s remaining")

    # -- telemetry ----------------------------------------------------------

    def _pump_mfc(self) -> None:
        latest = None
        while True:
            try:
                latest = self.mfc.telemetry.get_nowait()
            except Exception:                                      # noqa: BLE001
                break
            if latest.message:
                self.log(latest.message, latest.level)

        if latest is not None:
            if latest.connected != self.mfc_connected:
                self.mfc_connected = latest.connected
                self.connect_btn.configure(
                    text="Disconnect" if self.mfc_connected else "Connect")
                self.gas_status.configure(
                    text="  Online" if self.mfc_connected else "  Offline",
                    foreground="#137a2b" if self.mfc_connected else "#777")
                self._set_gas_controls(self.mfc_connected)
            self._mfc_states = latest.channels
            for key, state in latest.channels.items():
                card = self.cards.get(key)
                if card:
                    card.update_state(state)
                if not state.read_only:
                    self.setpoints[key] = state.setpoint_sccm
            self._render_pressure()

        self.rec_line.configure(
            text="recording to CSV" if self._recording()
            else "lock-in not recording")
        self.after(250, self._pump_mfc)

    def _set_gas_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (self.apply_btn, self.flush_btn, self.zero_btn,
                       self.start_btn, self.abort_btn):
            widget.configure(state=state)
        for card in self.cards.values():
            card.set_enabled(enabled)

    def log(self, message: str, level: str = "info") -> None:
        self.status_line.configure(
            text=time.strftime("%H:%M:%S ") + message,
            foreground={"info": "#444", "warn": "#a06000",
                        "error": "#b00"}.get(level, "#444"))

    # -- shutdown -----------------------------------------------------------

    def _on_close(self) -> None:
        if self.runner.active and not messagebox.askokcancel(
                "Run in progress",
                "A linked run is active. Stop it, zero the flows and close?"):
            return
        self.runner.abort("window closed")
        try:
            self.itc.shutdown()          # laser off first, always
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self.scope.shutdown()        # stop streaming, release VISA
        except Exception:                                          # noqa: BLE001
            pass
        self.mfc.send("stop_all")
        self.mfc.stop()
        self.mfc.join(timeout=4.0)
        try:
            self.sr.on_close()       # closes its CSV and stops its worker
        except Exception:                                          # noqa: BLE001
            self.destroy()


def list_interfaces() -> int:
    """
    Show every instrument interface this machine actually has.

    VI_ERROR_INTF_NUM_NCONFIG means VISA parsed the address fine but there is
    no such GPIB board configured -- so the useful question is not "is the
    SR830 on?" but "what interfaces exist here at all?".
    """
    print("Instrument interfaces on this machine")
    print("-" * 62)

    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        print(f"  VISA backend : {rm}")
        resources = rm.list_resources()
        if resources:
            print("  VISA resources:")
            for name in resources:
                print(f"      {name}")
        else:
            print("  VISA resources: none found")
            print("      No GPIB board is configured. Either the NI-488.2 "
                  "driver is not\n      installed, or the adapter is not "
                  "plugged in, or it is a Prologix\n      adapter -- which is "
                  "NOT a VISA GPIB board, it is a COM port.")
    except ImportError:
        print("  VISA backend : pyvisa not installed")
        print("      pip install pyvisa   (and the NI-VISA runtime for NI "
              "hardware)")
    except Exception as exc:                                       # noqa: BLE001
        print(f"  VISA backend : failed to start -- {exc}")

    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        print("\n  Serial ports:")
        if ports:
            for port in ports:
                print(f"      {port.device}  {port.description}")
        else:
            print("      none detected")
    except ImportError:
        print("\n  Serial ports: pyserial not installed")

    print("""
Which one is your SR830?

  Prologix GPIB-USB  -> it appears as a COM port above, not as a VISA
                        resource. Start with:
                            python combined_gui.py --prologix \\
                                --resource COM5 --addr 8
                        substituting the COM port that is NOT your MFC bus.

  NI / Keysight GPIB -> it should appear above as GPIB0::...::INSTR. If it
                        does not, install the NI-488.2 driver and check the
                        adapter in NI MAX.

  Not connected here -> run the gas bench alone:
                            python combined_gui.py --demo

The GPIB address must also match the SR830 front panel: press SETUP and read
the GPIB address. The default assumed here is 8.
""")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Combined SR830 + MFC control panel")
    parser.add_argument("--resource", default="GPIB0::8::INSTR")
    parser.add_argument("--prologix", action="store_true")
    parser.add_argument("--addr", type=int, default=8)
    parser.add_argument("--list-interfaces", action="store_true",
                        help="show VISA resources and COM ports, then exit")
    parser.add_argument("--demo", action="store_true",
                        help="fake lock-in and simulated MFCs")
    parser.add_argument("--simulate-mfc", action="store_true",
                        help="real lock-in, simulated MFCs")
    args, _ = parser.parse_known_args(
        [a for a in sys.argv[1:] if a != "--wdir"])

    if args.list_interfaces:
        return list_interfaces()

    try:
        instrument = (FakeSR830() if args.demo
                      else SR830(args.resource, prologix=args.prologix,
                                 addr=args.addr))
    except Exception as exc:                                       # noqa: BLE001
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Lock-in connection failed",
            f"Could not open {args.resource}\n\n{type(exc).__name__}: {exc}\n\n"
            "Run this to see what interfaces this machine actually has:\n"
            "    python combined_gui.py --list-interfaces\n\n"
            "A Prologix GPIB-USB adapter is a COM port, not a VISA GPIB "
            "board -- it needs --prologix --resource COMx.\n\n"
            "To work on the gas bench alone: python combined_gui.py --demo")
        return 1

    app = CombinedApp(instrument,
                      simulate_mfc=args.demo or args.simulate_mfc)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
