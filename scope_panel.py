# -*- coding: utf-8 -*-
"""
scope_panel.py -- the TBS 1052B-EDU control panel as an embeddable frame.

scope_gui.App is a tk.Tk subclass, so it owns a window and cannot be put
inside a notebook tab. This module provides the same panel as a ttk.Frame.

NOTHING IN scope_gui.py IS REIMPLEMENTED. The instrument layer
(ScopeSession), the analysis (analyse), the CSV writers and the worker
thread are imported and used unchanged; every layout and handler method is
bound straight off scope_gui.App, so the behaviour in the tab is the same
code that runs in the standalone window.

What is genuinely new here:
  - __init__ / shutdown, because a frame has no window to create or destroy
  - FakeScopeSession, so --demo exercises the panel with no instrument
  - latest(), so a linked run can read the most recent measurements

scope_gui.py still runs on its own exactly as before.
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional

import numpy as np

import scope_gui
from scope_gui import App as _App


class FakeScopeSession:
    """
    Stand-in for ScopeSession so the panel can be driven without hardware.

    Generates a 2f-looking waveform: a fundamental at the target frequency
    plus a second harmonic and noise, scaled so the readouts land in the
    same millivolt range as the real bench signal.
    """

    RECORD = scope_gui.RECORD_LENGTH

    def __init__(self):
        self.inst = object()          # truthy, so .connected is True
        self.idn = "TEKTRONIX,TBS 1052B-EDU,SIMULATED,CF:91.1CT FV:v3.25"
        self._settings = {}
        self._t0 = time.time()

    # -- the ScopeSession surface the worker uses ---------------------------

    def list_resources(self):
        return ["SIMULATED::TBS1052B::INSTR"]

    def connect(self, address):
        self.inst = object()
        return self.idn

    def disconnect(self):
        self.inst = None
        self.idn = ""

    @property
    def connected(self):
        return self.inst is not None

    def apply_settings(self, s):
        self._settings = dict(s)
        return []

    def autoset(self):
        time.sleep(0.2)

    def read_settings(self, channel):
        s = self._settings
        def f(key, default):
            try:
                return float(s.get(key, default))
            except (TypeError, ValueError):
                return default
        return {"vscale": f("vscale", 0.02), "voffset": f("voffset", 0.0),
                "coupling": str(s.get("coupling", "AC")).upper(),
                "probe": f("probe", 1.0), "timebase": f("timebase", 250e-6),
                "trig_level": f("trig_level", 0.0)}

    def acquire(self):
        s = self._settings
        try:
            tb = float(s.get("timebase", 250e-6))
        except (TypeError, ValueError):
            tb = 250e-6
        try:
            target = float(s.get("target", 6233.7))
        except (TypeError, ValueError):
            target = 6233.7

        n = self.RECORD
        span = tb * 10.0                      # ten divisions across the screen
        t = np.linspace(0.0, span, n, endpoint=False)
        dt = span / n

        rng = np.random.default_rng()
        drift = 1.0 + 0.02 * np.sin((time.time() - self._t0) / 20.0)
        v = (4.0e-3 * drift * np.sin(2 * np.pi * target * t)
             + 1.1e-3 * drift * np.sin(2 * np.pi * 2 * target * t + 0.6)
             + rng.normal(0.0, 1.2e-4, n))

        # quantise the way an 8-bit scope does, so Vpp and the noise floor
        # behave like the real instrument rather than being suspiciously clean
        try:
            vscale = float(s.get("vscale", 0.02))
        except (TypeError, ValueError):
            vscale = 0.02
        lsb = vscale * 8.0 / 256.0
        v = np.round(v / lsb) * lsb
        time.sleep(0.05)
        return t, v


class ScopePanel(ttk.Frame):
    """The scope control panel, embeddable in a notebook tab."""

    # ---- bound verbatim from scope_gui.App --------------------------------
    send = _App.send
    log = _App.log
    collect_settings = _App.collect_settings
    push_settings = _App.push_settings

    _build = _App._build
    _build_connection = _App._build_connection
    _build_controls = _App._build_controls
    _build_acquisition = _App._build_acquisition
    _build_saving = _App._build_saving
    _build_readouts = _App._build_readouts
    _build_plots = _App._build_plots
    _build_log = _App._build_log

    on_connect = _App.on_connect
    on_disconnect = _App.on_disconnect
    on_single = _App.on_single
    on_run = _App.on_run
    on_stop = _App.on_stop
    on_browse = _App.on_browse
    on_save_now = _App.on_save_now

    _poll = _App._poll
    _handle = _App._handle
    _update_readouts = _App._update_readouts
    _redraw = _App._redraw
    # -----------------------------------------------------------------------

    def __init__(self, parent, simulate: bool = False, **kw):
        super().__init__(parent, **kw)
        self.simulate = simulate

        self.cmd_q = queue.Queue()
        self.out_q = queue.Queue()
        self.worker = scope_gui.Worker(self.cmd_q, self.out_q)
        if simulate:
            # Swap the instrument layer before the thread starts, so no VISA
            # call is ever attempted.
            self.worker.scope = FakeScopeSession()
        self.worker.start()

        self.last_capture = None
        self.connected = False

        self._build()
        if simulate:
            self.v_resource.set("SIMULATED::TBS1052B::INSTR")
        self.send({"action": "list"})
        self.after(80, self._poll)

    # -- for the linked run --------------------------------------------------

    def latest(self) -> Optional[dict]:
        """
        Most recent measurements, or None if nothing has been captured.

        Only scalars: the waveform and spectrum arrays stay out, because the
        caller writes these into one CSV row per lock-in sample.
        """
        if self.last_capture is None:
            return None
        _, _, res = self.last_capture
        keys = ("dc", "rms", "ac_rms", "vpp", "f0", "f0_amp",
                "target_amp", "harm_freq", "harm_amp", "noise_floor",
                "snr_db", "fs", "df")
        return {k: res.get(k) for k in keys}

    # -- shutdown ------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop streaming and release the VISA session."""
        try:
            self.send({"action": "quit"})
            self.worker.join(timeout=3.0)
        except Exception:                                          # noqa: BLE001
            pass


def main() -> int:
    """Run the panel on its own, for checking it in isolation."""
    import argparse
    import sys
    p = argparse.ArgumentParser(description="TBS 1052B-EDU panel")
    p.add_argument("--simulate", action="store_true")
    args, _ = p.parse_known_args([a for a in sys.argv[1:] if a != "--wdir"])

    root = tk.Tk()
    root.title("TBS 1052B-EDU  |  Scope Control")
    root.geometry("1280x820")
    panel = ScopePanel(root, simulate=args.simulate)
    panel.pack(fill="both", expand=True)

    def close():
        panel.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
