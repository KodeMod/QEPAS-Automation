"""
sr830_gui.py -- Control panel and data logger for the SRS SR830 lock-in amplifier.

Run it:
    python sr830_gui.py                 # NI/VISA GPIB, address 8
    python sr830_gui.py --demo          # no hardware, simulated instrument

In Spyder, set  Run > Configuration > Execute in an external system terminal,
or just run it from the Anaconda Prompt. Tk GUIs and the Spyder console fight
over the event loop otherwise.

Requires sr830.py in the same folder.

Design notes
------------
All GPIB traffic happens in ONE background thread. The GUI never touches the
instrument directly; it posts callables onto a command queue and receives
readings back on a data queue. Two threads talking to a lock-in at once will
interleave queries and replies and hand you the wrong numbers.

Recording opens the CSV once and flushes after every row, so a crash or power
cut costs you at most one sample rather than the whole run.
"""

import argparse
import csv
# +++ ADDED
from sr830_meta import MetadataPanel
import datetime as dt
import math
import os
import queue
import random
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox, ttk

from sr830 import SAMPLE_RATE, SENSITIVITY, TIME_CONSTANT, SR830

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAVE_PLOT = True
except Exception:
    HAVE_PLOT = False


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_PREFIX = [(1e-12, "p"), (1e-9, "n"), (1e-6, "\u00b5"), (1e-3, "m"),
           (1.0, ""), (1e3, "k")]


def eng(value, unit=""):
    """Engineering notation, e.g. 5e-7 -> '500 nV'."""
    if value == 0:
        return f"0 {unit}".strip()
    for scale, pre in reversed(_PREFIX):
        if abs(value) >= scale:
            n = value / scale
            txt = f"{n:g}"
            return f"{txt} {pre}{unit}".strip()
    return f"{value:g} {unit}".strip()


SENS_LABELS = [eng(v, "V") for v in SENSITIVITY]
TC_LABELS = [eng(v, "s") for v in TIME_CONSTANT]
RATE_LABELS = [f"{r:g} Hz" for r in SAMPLE_RATE]


# ---------------------------------------------------------------------------
# Simulated instrument for testing the GUI without hardware
# ---------------------------------------------------------------------------

class FakeSR830:
    idn = "Stanford_Research_Systems,SR830,s/n00000,ver1.03 [SIMULATED]"

    def __init__(self, *a, **kw):
        self._f, self._a, self._p, self._h = 1000.0, 0.1, 0.0, 1
        self._sens, self._tc, self._slope = 1e-3, 0.1, 24
        self._int = True
        self._t0 = time.time()

    # The real SR830 keeps 5 significant digits (0.1 Hz near 6 kHz), so the
    # simulator must too -- otherwise --demo accepts frequencies the
    # instrument would quietly round, and the rounding is only discovered at
    # the bench.
    frequency = property(lambda s: s._f,
                         lambda s, v: setattr(s, "_f", float(f"{float(v):.5g}")))
    amplitude = property(lambda s: s._a,
                         lambda s, v: setattr(s, "_a", float(v)))
    phase = property(lambda s: s._p, lambda s, v: setattr(s, "_p", float(v)))
    harmonic = property(lambda s: s._h, lambda s, v: setattr(s, "_h", int(v)))
    sensitivity = property(lambda s: s._sens,
                           lambda s, v: setattr(s, "_sens", float(v)))
    time_constant = property(lambda s: s._tc,
                             lambda s, v: setattr(s, "_tc", float(v)))
    slope = property(lambda s: s._slope,
                     lambda s, v: setattr(s, "_slope", int(v)))
    internal_reference = property(lambda s: s._int,
                                  lambda s, v: setattr(s, "_int", bool(v)))

    def snap(self, *p):
        t = time.time() - self._t0
        base = self._a * 1e-3 * (1 + 0.3 * math.sin(2 * math.pi * 0.05 * t))
        x = base * math.cos(math.radians(self._p)) + random.gauss(0, base * .04)
        y = base * math.sin(math.radians(self._p)) + random.gauss(0, base * .04)
        vals = {"X": x, "Y": y, "R": math.hypot(x, y),
                "theta": math.degrees(math.atan2(y, x)), "freq": self._f}
        return {k: vals[k] for k in (p or ("X", "Y", "R", "theta"))}

    def read(self, p="R"):
        return self.snap("R", "X")[p if p in ("R", "X") else "R"]

    def status(self):
        return {"frequency_Hz": self._f, "amplitude_Vrms": self._a,
                "phase_deg": self._p, "harmonic": self._h,
                "internal_ref": self._int, "input_V_fullscale": self._sens,
                "time_constant_s": self._tc, "slope_dB_oct": self._slope}

    def overload(self):
        return {"input": False, "filter": False, "output": False}

    def unlocked(self):
        return False

    def autorange(self, target=0.3):
        r = self.read("R")
        for v in SENSITIVITY:
            if v >= r / target:
                self._sens = v
                break
        return self._sens

    def settle(self, n=None):
        time.sleep(0.05)

    def auto_phase(self):
        self._p = 0.0

    def input_config(self, **kw):
        pass

    def filters(self, **kw):
        for k, v in kw.items():
            if v is not None and hasattr(self, k):
                setattr(self, k, v)

    def reference(self, **kw):
        for k, v in kw.items():
            if v is not None and k in ("freq", "amplitude", "phase", "harmonic"):
                setattr(self, {"freq": "frequency"}.get(k, k), v)
        return self.status()

    def aux_out(self, ch, v):
        pass

    def write(self, c):
        pass

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Acquisition worker -- owns the instrument, sole GPIB user
# ---------------------------------------------------------------------------

class Worker(threading.Thread):
    def __init__(self, li, out_queue):
        super().__init__(daemon=True)
        self.li = li
        self.out = out_queue
        self.cmds = queue.Queue()
        self._halt = threading.Event()

        self.acquiring = False
        self.interval = 0.5
        self.state = {}

        self._csv_file = None
        self._csv_writer = None
        self._csv_path = None
        self._csv_rows = 0
        # +++ ADDED: experiment context columns and pending comment marks
        self.extra = {}
        self._marks = []
        self._csv_cols = []
        self._t0 = None
        self._next = 0.0
        self._last_ovl_check = 0.0
        self._ovl = {"input": False, "filter": False, "output": False}

    # -- public, called from the GUI thread ---------------------------------

    def post(self, fn):
        """Queue a callable f(instrument) to run in the worker thread."""
        self.cmds.put(fn)

    def shutdown(self):
        self._halt.set()

    # -- recording -----------------------------------------------------------

    def _open_csv(self, path, append):
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self._csv_file = open(path, "a" if append else "w",
                              newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        if not (append and exists):
            st = self.state
            self._csv_file.write(
                f"# SR830 log started {dt.datetime.now().isoformat(timespec='seconds')}\n")
            for k, v in st.items():
                self._csv_file.write(f"# {k} = {v}\n")
            # +++ CHANGED: header now ends with the experiment-context columns
            self._csv_cols = list(self.extra.keys())
            self._csv_writer.writerow([
                "timestamp", "t_s", "X_V", "Y_V", "R_V", "theta_deg",
                "freq_Hz", "sensitivity_V", "time_constant_s", "harmonic",
                "overload"] + self._csv_cols)
        else:
            self._csv_file.write(
                f"# resumed {dt.datetime.now().isoformat(timespec='seconds')}\n")
            self._csv_cols = list(self.extra.keys())   # +++ ADDED
        self._csv_file.flush()
        self._csv_path = path
        self._csv_rows = 0

    def _close_csv(self):
        if self._csv_file:
            self._csv_file.close()
        self._csv_file = self._csv_writer = None

    # -- main loop -----------------------------------------------------------

    def run(self):
        self._refresh()
        while not self._halt.is_set():
            # 1. run every pending GUI command
            while True:
                try:
                    fn = self.cmds.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(self)
                except Exception as exc:
                    self.out.put(("error", f"{type(exc).__name__}: {exc}"))

            # 2. take a sample if it is time
            now = time.time()
            if self.acquiring and now >= self._next:
                self._next = now + self.interval
                try:
                    self._sample(now)
                except Exception as exc:
                    self.acquiring = False
                    self.out.put(("error", f"acquisition stopped: {exc}"))
                    self.out.put(("acq", False))

            time.sleep(0.01)
        self._close_csv()

    def _sample(self, now):
        d = self.li.snap("X", "Y", "R", "theta", "freq")
        t = now - self._t0

        if now - self._last_ovl_check > 1.0:
            self._last_ovl_check = now
            self._ovl = self.li.overload()

        ovl = any(self._ovl.values())
        rec = {"t": t, "ovl": ovl, "ovl_detail": dict(self._ovl), **d}

        if self._csv_writer:
            # +++ ADDED: write any pending event marks as comment lines
            while self._marks:
                self._csv_file.write(f"# MARK t={t:.3f}s {self._marks.pop(0)}\n")
            self._csv_writer.writerow([
                dt.datetime.now().isoformat(timespec="milliseconds"),
                f"{t:.4f}",
                f"{d['X']:.6e}", f"{d['Y']:.6e}",
                f"{d['R']:.6e}", f"{d['theta']:.4f}",
                f"{d['freq']:.4f}",
                f"{self.state.get('input_V_fullscale', float('nan')):.3e}",
                f"{self.state.get('time_constant_s', float('nan')):.4g}",
                self.state.get("harmonic", ""),
                int(ovl)]
                + [self.extra.get(k, "") for k in self._csv_cols])   # +++ CHANGED
            self._csv_file.flush()          # survive a crash
            self._csv_rows += 1
            rec["rows"] = self._csv_rows
            rec["path"] = self._csv_path

        self.out.put(("data", rec))

    # +++ ADDED
    def mark(self, text):
        """Queue a labelled event; written into the CSV at the next sample."""
        self._marks.append(text)

    def _refresh(self):
        try:
            self.state = self.li.status()
            self.out.put(("state", dict(self.state)))
        except Exception as exc:
            self.out.put(("error", f"could not read state: {exc}"))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(ttk.Frame):
    PLOT_POINTS = 600

    def __init__(self, master, li):
        super().__init__(master, padding=8)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.q = queue.Queue()
        self.worker = Worker(li, self.q)
        self.worker.start()

        self.t_hist = deque(maxlen=self.PLOT_POINTS)
        self.r_hist = deque(maxlen=self.PLOT_POINTS)
        self.x_hist = deque(maxlen=self.PLOT_POINTS)
        self.y_hist = deque(maxlen=self.PLOT_POINTS)
        self.th_hist = deque(maxlen=self.PLOT_POINTS)   # +++ ADDED

        self._suppress = True          # ignore widget callbacks while loading
        self._build(li.idn)
        self._suppress = False

        self.after(80, self._pump)

    # -- layout --------------------------------------------------------------

    def _build(self, idn):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text=idn, font=("TkDefaultFont", 9, "bold")
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        left = ttk.Frame(self)
        left.grid(row=1, column=0, sticky="nsw", padx=(0, 10))
        right = ttk.Frame(self)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)   # +++ CHANGED: plot moved down one row

        self._build_reference(left)
        self._build_input(left)
        self._build_filters(left)
        self._build_aux(left)

        self._build_readout(right)
        self._build_meta(right)          # +++ ADDED
        self._build_recording(right)
        self._build_plot(right)

        self.status = ttk.Label(self, text="ready", relief="sunken",
                                anchor="w", padding=3)
        self.status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _section(self, parent, title):
        f = ttk.LabelFrame(parent, text=title, padding=6)
        f.pack(fill="x", pady=(0, 8))
        f.columnconfigure(1, weight=1)
        return f

    # -- reference -----------------------------------------------------------

    def _build_reference(self, parent):
        f = self._section(parent, "Reference / oscillator")

        self.v_freq = tk.StringVar(value="1000.0")
        self.v_ampl = tk.StringVar(value="0.100")
        self.v_phase = tk.StringVar(value="0.00")
        self.v_harm = tk.StringVar(value="1")
        self.v_src = tk.StringVar(value="Internal")
        self.v_f0 = tk.StringVar(value="")       # measured 2f resonance
        self.v_fwhm = tk.StringVar(value="")     # optional, from the Q sweep

        rows = [("Frequency (Hz)", self.v_freq, self._set_freq),
                ("Amplitude (V rms)", self.v_ampl, self._set_ampl),
                ("Phase (deg)", self.v_phase, self._set_phase),
                ("Harmonic", self.v_harm, self._set_harm)]
        self._ref_entries = {}
        r = 0
        for label, var, cb in rows:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=1)
            e = ttk.Entry(f, textvariable=var, width=12, justify="right")
            e.grid(row=r, column=1, sticky="ew", pady=1)
            e.bind("<Return>", cb)
            e.bind("<FocusOut>", cb)
            self._ref_entries[str(var)] = e
            r += 1

        ttk.Label(f, text="Source").grid(row=r, column=0, sticky="w", pady=1)
        c = ttk.Combobox(f, textvariable=self.v_src, state="readonly", width=10,
                         values=["Internal", "External"])
        c.grid(row=r, column=1, sticky="ew", pady=1)
        c.bind("<<ComboboxSelected>>", self._set_refsrc)
        r += 1

        ttk.Label(f, text="detect at", foreground="#555").grid(
            row=r, column=0, sticky="w", pady=(4, 0))
        self.lbl_detect = ttk.Label(f, text="1.000 kHz", foreground="#555")
        self.lbl_detect.grid(row=r, column=1, sticky="e", pady=(4, 0))
        r += 1

        ttk.Button(f, text="Auto Phase", command=self._auto_phase).grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        r += 1

        # -- drive-at-f0/2 helper ------------------------------------------
        # The QTF resonance is measured as a 2f number (12467.372 Hz), but the
        # oscillator must be driven at half of it. Halving by hand is exactly
        # where the decimals get dropped, and at this Q a rounded 6234 Hz
        # costs about a quarter of the signal.
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        r += 1

        ttk.Label(f, text="QTF f0 (Hz)").grid(row=r, column=0, sticky="w",
                                              pady=1)
        e_f0 = ttk.Entry(f, textvariable=self.v_f0, width=12, justify="right")
        e_f0.grid(row=r, column=1, sticky="ew", pady=1)
        e_f0.bind("<Return>", self._set_half_f0)
        r += 1

        ttk.Label(f, text="FWHM (Hz)").grid(row=r, column=0, sticky="w", pady=1)
        ttk.Entry(f, textvariable=self.v_fwhm, width=12,
                  justify="right").grid(row=r, column=1, sticky="ew", pady=1)
        r += 1

        ttk.Button(f, text="Drive at f0 / 2",
                   command=self._set_half_f0).grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        r += 1

        self.lbl_detune = ttk.Label(f, text="", foreground="#555",
                                    wraplength=200, justify="left")
        self.lbl_detune.grid(row=r, column=0, columnspan=2, sticky="w")

    # -- input ---------------------------------------------------------------

    def _build_input(self, parent):
        f = self._section(parent, "Signal input")

        self.v_isrc = tk.StringVar(value="A")
        self.v_icpl = tk.StringVar(value="AC")
        self.v_ignd = tk.StringVar(value="Ground")
        self.v_ilin = tk.StringVar(value="none")

        combos = [("Source", self.v_isrc, ["A", "A-B", "I1M", "I100M"]),
                  ("Coupling", self.v_icpl, ["AC", "DC"]),
                  ("Shield", self.v_ignd, ["Float", "Ground"]),
                  ("Line notch", self.v_ilin,
                   ["none", "line", "2xline", "both"])]
        for i, (label, var, vals) in enumerate(combos):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=1)
            c = ttk.Combobox(f, textvariable=var, state="readonly",
                             width=10, values=vals)
            c.grid(row=i, column=1, sticky="ew", pady=1)
            c.bind("<<ComboboxSelected>>", self._set_input)

    # -- gain and filters ----------------------------------------------------

    def _build_filters(self, parent):
        f = self._section(parent, "Gain and filters")

        self.v_sens = tk.StringVar(value=eng(1e-3, "V"))
        self.v_tc = tk.StringVar(value=eng(0.1, "s"))
        self.v_slope = tk.StringVar(value="24")
        self.v_res = tk.StringVar(value="normal")
        self.v_sync = tk.BooleanVar(value=False)

        ttk.Label(f, text="Sensitivity").grid(row=0, column=0, sticky="w")
        c = ttk.Combobox(f, textvariable=self.v_sens, state="readonly",
                         width=10, values=SENS_LABELS)
        c.grid(row=0, column=1, sticky="ew", pady=1)
        c.bind("<<ComboboxSelected>>", self._set_sens)

        ttk.Label(f, text="Time constant").grid(row=1, column=0, sticky="w")
        c = ttk.Combobox(f, textvariable=self.v_tc, state="readonly",
                         width=10, values=TC_LABELS)
        c.grid(row=1, column=1, sticky="ew", pady=1)
        c.bind("<<ComboboxSelected>>", self._set_filters)

        ttk.Label(f, text="Slope (dB/oct)").grid(row=2, column=0, sticky="w")
        c = ttk.Combobox(f, textvariable=self.v_slope, state="readonly",
                         width=10, values=["6", "12", "18", "24"])
        c.grid(row=2, column=1, sticky="ew", pady=1)
        c.bind("<<ComboboxSelected>>", self._set_filters)

        ttk.Label(f, text="Reserve").grid(row=3, column=0, sticky="w")
        c = ttk.Combobox(f, textvariable=self.v_res, state="readonly", width=10,
                         values=["high", "normal", "lownoise"])
        c.grid(row=3, column=1, sticky="ew", pady=1)
        c.bind("<<ComboboxSelected>>", self._set_filters)

        ttk.Checkbutton(f, text="Sync filter (ref < 200 Hz)",
                        variable=self.v_sync, command=self._set_filters
                        ).grid(row=4, column=0, columnspan=2, sticky="w",
                               pady=(3, 0))

        ttk.Button(f, text="Auto Range", command=self._autorange).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(f, text="settle time", foreground="#555").grid(
            row=6, column=0, sticky="w")
        self.lbl_settle = ttk.Label(f, text="-", foreground="#555")
        self.lbl_settle.grid(row=6, column=1, sticky="e")

    # -- aux outputs ---------------------------------------------------------

    def _build_aux(self, parent):
        f = self._section(parent, "Aux outputs (V)")
        self.v_aux = []
        for i in range(4):
            var = tk.StringVar(value="0.000")
            self.v_aux.append(var)
            ttk.Label(f, text=f"Aux {i + 1}").grid(row=i, column=0, sticky="w")
            e = ttk.Entry(f, textvariable=var, width=12, justify="right")
            e.grid(row=i, column=1, sticky="ew", pady=1)
            e.bind("<Return>", lambda ev, ch=i + 1: self._set_aux(ch))

    # -- readout -------------------------------------------------------------

    def _build_readout(self, parent):
        f = ttk.LabelFrame(parent, text="Readout", padding=8)
        f.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for c in range(4):
            f.columnconfigure(c, weight=1)

        self.big = {}
        for i, name in enumerate(("X", "Y", "R", "theta")):
            ttk.Label(f, text=name if name != "theta" else "\u03b8",
                      foreground="#555").grid(row=0, column=i)
            lb = ttk.Label(f, text="--", font=("Consolas", 15, "bold"))
            lb.grid(row=1, column=i)
            self.big[name] = lb

        self.lbl_ovl = ttk.Label(f, text="", foreground="#b00",
                                 font=("TkDefaultFont", 9, "bold"))
        self.lbl_ovl.grid(row=2, column=0, columnspan=4, pady=(4, 0))

    # -- acquisition and recording -------------------------------------------

    # +++ ADDED
    def _build_meta(self, parent):
        self.meta = MetadataPanel(parent, on_change=self._meta_changed)
        self.meta.grid(row=1, column=0, sticky="ew", pady=(0, 6))

    # +++ ADDED
    def _meta_changed(self, values):
        self.worker.extra = values

    def _build_recording(self, parent):
        f = ttk.LabelFrame(parent, text="Acquisition and recording", padding=8)
        f.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Sample interval (s)").grid(row=0, column=0, sticky="w")
        self.v_interval = tk.StringVar(value="0.5")
        e = ttk.Entry(f, textvariable=self.v_interval, width=8, justify="right")
        e.grid(row=0, column=1, sticky="w", padx=4)
        e.bind("<Return>", self._set_interval)
        e.bind("<FocusOut>", self._set_interval)

        self.btn_mon = ttk.Button(f, text="Start monitor",
                                  command=self._toggle_monitor)
        self.btn_mon.grid(row=0, column=2, sticky="ew", padx=4)

        ttk.Label(f, text="CSV file").grid(row=1, column=0, sticky="w",
                                           pady=(6, 0))
        self.v_path = tk.StringVar(value="")
        ttk.Entry(f, textvariable=self.v_path).grid(
            row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        ttk.Button(f, text="Browse...", command=self._browse).grid(
            row=1, column=2, sticky="ew", padx=4, pady=(6, 0))

        self.btn_rec = ttk.Button(f, text="\u25cf  START RECORDING",
                                  command=self._toggle_record)
        self.btn_rec.grid(row=2, column=0, columnspan=3, sticky="ew",
                          pady=(8, 0), ipady=4)

        self.lbl_rec = ttk.Label(f, text="not recording", foreground="#555")
        self.lbl_rec.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.recording = False
        self.monitoring = False

    # -- plot ----------------------------------------------------------------

    def _build_plot(self, parent):
        f = ttk.LabelFrame(parent, text="Live trace", padding=4)
        f.grid(row=3, column=0, sticky="nsew")   # +++ CHANGED: was row 2
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, sticky="ew")
        self.v_trace = tk.StringVar(value="R")
        for name in ("R", "X", "Y", "X & Y", "\u03b8"):   # +++ CHANGED: theta added
            ttk.Radiobutton(bar, text=name, value=name, variable=self.v_trace,
                            command=self._redraw).pack(side="left")
        ttk.Button(bar, text="Clear", command=self._clear_plot
                   ).pack(side="right")
        # +++ ADDED
        ttk.Button(bar, text="Mark event", command=self._mark_event
                   ).pack(side="right", padx=(0, 6))

        if not HAVE_PLOT:
            ttk.Label(f, text="matplotlib not available -- plot disabled",
                      foreground="#888").grid(row=1, column=0, pady=20)
            self.canvas = None
            return

        self.fig = Figure(figsize=(6, 3.2), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("time (s)")
        self.ax.set_ylabel("V")
        self.ax.grid(alpha=0.3)
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew")

    # -----------------------------------------------------------------------
    # Instrument commands (each runs in the worker thread)
    # -----------------------------------------------------------------------

    def _post_set(self, fn, note=""):
        def job(w):
            fn(w.li)
            w.state = w.li.status()
            w.out.put(("state", dict(w.state)))
        self.worker.post(job)
        if note:
            self._say(note)

    @staticmethod
    def _fmt_hz(v):
        """
        Frequency as text, without losing sub-Hz digits.

        "{:g}" defaults to 6 significant digits, which silently rounds
        12467.372 to 12467.4. The SR830 stores 5 significant digits, so four
        decimals is always enough and never shows noise.
        """
        return f"{float(v):.4f}".rstrip("0").rstrip(".")

    def _set_var_idle(self, var, entry, text):
        """
        Update a field only when nobody is typing in it.

        The periodic state refresh used to overwrite whatever was half-typed,
        so "6233.7" became the instrument's old value mid-keystroke and the
        edit was lost before Return was ever pressed.
        """
        try:
            if entry is not None and entry.focus_get() is entry:
                return
        except Exception:
            pass
        var.set(text)

    def _num(self, var, name, lo=None, hi=None, cast=float):
        raw = var.get().strip().replace(",", ".")   # accept a German comma
        try:
            v = cast(raw)
        except ValueError:
            self._say(f"{name}: not a number", error=True)
            return None
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            self._say(f"{name} must be between {lo} and {hi}", error=True)
            return None
        return v

    def _set_freq(self, *_):
        if self._suppress:
            return
        v = self._num(self.v_freq, "Frequency", 0.001, 102000)
        if v is None:
            return

        # The SR830 keeps 5 significant digits (0.1 Hz near 6 kHz), so say
        # up front what it will actually store rather than letting the field
        # quietly change under the operator.
        grid = float(f"{v:.5g}")
        if abs(grid - v) > 1e-9:
            self._say(f"frequency rounded to the SR830's 5-digit grid: "
                      f"{self._fmt_hz(v)} -> {self._fmt_hz(grid)} Hz")

        def apply(li):
            li.frequency = v
            got = li.frequency                      # read back, do not assume
            self.q.put(("freq_set", got))
        self._post_set(apply)

    def _set_half_f0(self, *_):
        """
        Set the oscillator to half the measured QTF resonance.

        Reports the detuning that survives the SR830's 5-digit grid, in units
        of the resonance linewidth, because that is what decides how much
        signal the offset costs -- not the raw hertz.
        """
        f0 = self._num(self.v_f0, "QTF f0", 0.002, 204000)
        if f0 is None:
            return
        want = f0 / 2.0
        grid = float(f"{want:.5g}")
        detune = 2 * grid - f0

        self.v_freq.set(self._fmt_hz(grid))
        self._set_freq()

        note = (f"f0/2 = {self._fmt_hz(want)} Hz, "
                f"nearest settable {self._fmt_hz(grid)} Hz   "
                f"-> 2f detuned {detune:+.4f} Hz")
        try:
            fwhm = float(self.v_fwhm.get())
            if fwhm > 0:
                rel = 1.0 / (1.0 + (2 * detune / fwhm) ** 2) ** 0.5
                note += f"   ({abs(2 * detune / fwhm):.3f} FWHM, " \
                        f"{rel * 100:.1f} % of peak)"
        except Exception:
            pass
        self.lbl_detune.config(text=note)
        self._say(note)

    def _set_ampl(self, *_):
        if self._suppress:
            return
        v = self._num(self.v_ampl, "Amplitude", 0.004, 5.0)
        if v is not None:
            self._post_set(lambda li: setattr(li, "amplitude", v))

    def _set_phase(self, *_):
        if self._suppress:
            return
        v = self._num(self.v_phase, "Phase", -360, 729.99)
        if v is not None:
            self._post_set(lambda li: setattr(li, "phase", v))

    def _set_harm(self, *_):
        if self._suppress:
            return
        v = self._num(self.v_harm, "Harmonic", 1, 19999, int)
        if v is None:
            return
        f = self._num(self.v_freq, "Frequency") or 0
        if v * f > 102000:
            self._say(f"harmonic x frequency = {v * f:.0f} Hz exceeds "
                      f"the 102 kHz detection limit", error=True)
            return
        self._post_set(lambda li: setattr(li, "harmonic", v))

    def _set_refsrc(self, *_):
        if self._suppress:
            return
        internal = self.v_src.get() == "Internal"
        self._post_set(lambda li: setattr(li, "internal_reference", internal))

    def _set_input(self, *_):
        if self._suppress:
            return
        src, cpl = self.v_isrc.get(), self.v_icpl.get()
        gnd = self.v_ignd.get() == "Ground"
        notch = self.v_ilin.get()
        self._post_set(lambda li: li.input_config(source=src, coupling=cpl,
                                                  shield_ground=gnd,
                                                  notch=notch))

    def _set_sens(self, *_):
        if self._suppress:
            return
        v = SENSITIVITY[SENS_LABELS.index(self.v_sens.get())]
        self._post_set(lambda li: setattr(li, "sensitivity", v))

    def _set_filters(self, *_):
        if self._suppress:
            return
        tc = TIME_CONSTANT[TC_LABELS.index(self.v_tc.get())]
        slope = int(self.v_slope.get())
        res = self.v_res.get()
        sync = bool(self.v_sync.get())
        self._post_set(lambda li: li.filters(time_constant=tc, slope=slope,
                                             reserve=res, sync=sync))

    def _set_aux(self, ch):
        v = self._num(self.v_aux[ch - 1], f"Aux {ch}", -10.5, 10.5)
        if v is not None:
            self._post_set(lambda li: li.aux_out(ch, v),
                           f"Aux {ch} set to {v:+.3f} V")

    def _autorange(self):
        self._say("auto ranging...")

        def job(w):
            fs = w.li.autorange()
            w.state = w.li.status()
            w.out.put(("state", dict(w.state)))
            w.out.put(("note", f"sensitivity set to {eng(fs, 'V')}"))
        self.worker.post(job)

    def _auto_phase(self):
        self._say("auto phasing...")

        def job(w):
            w.li.auto_phase()
            w.state = w.li.status()
            w.out.put(("state", dict(w.state)))
            w.out.put(("note", "auto phase complete"))
        self.worker.post(job)

    # -----------------------------------------------------------------------
    # Acquisition control
    # -----------------------------------------------------------------------

    def _set_interval(self, *_):
        v = self._num(self.v_interval, "Interval", 0.05, 3600)
        if v is None:
            return
        tc = self.worker.state.get("time_constant_s", 0)
        if tc and v < 2 * tc:
            self._say(f"interval {v:g} s is shorter than 2x the {eng(tc, 's')} "
                      f"time constant -- samples will be correlated", error=True)

        def job(w):
            w.interval = v
        self.worker.post(job)

    def _toggle_monitor(self):
        self._start_acq() if not self.monitoring else self._stop_acq()

    def _start_acq(self):
        v = self._num(self.v_interval, "Interval", 0.05, 3600) or 0.5

        def job(w):
            w.interval = v
            if w._t0 is None:
                w._t0 = time.time()
            w._next = 0.0
            w.acquiring = True
        self.worker.post(job)
        self.monitoring = True
        self.btn_mon.config(text="Stop monitor")
        self._say("monitoring")

    def _stop_acq(self):
        if self.recording:
            self._stop_record()

        def job(w):
            w.acquiring = False
        self.worker.post(job)
        self.monitoring = False
        self.btn_mon.config(text="Start monitor")
        self._say("stopped")

    def _browse(self):
        default = f"sr830_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            confirmoverwrite=False)
        if p:
            self.v_path.set(p)

    def _toggle_record(self):
        self._start_record() if not self.recording else self._stop_record()

    def _start_record(self):
        path = self.v_path.get().strip()
        if not path:
            self._browse()
            path = self.v_path.get().strip()
            if not path:
                return

        append = False
        if os.path.exists(path) and os.path.getsize(path) > 0:
            ans = messagebox.askyesnocancel(
                "File exists",
                f"{os.path.basename(path)} already exists.\n\n"
                "Yes  = append to it\n"
                "No   = overwrite it\n"
                "Cancel = pick another file")
            if ans is None:
                return
            append = bool(ans)

        err = []

        def job(w):
            try:
                w._open_csv(path, append)
                if w._t0 is None:
                    w._t0 = time.time()
                w.acquiring = True
                w.out.put(("note", f"recording to {os.path.basename(path)}"))
            except Exception as exc:
                err.append(str(exc))
                w.out.put(("error", f"could not open file: {exc}"))
        self.worker.post(job)

        self.recording = True
        self.monitoring = True
        self.btn_mon.config(text="Stop monitor")
        self.btn_rec.config(text="\u25a0  STOP RECORDING")
        self.lbl_rec.config(text=f"recording -> {path}", foreground="#0a0")

    def _stop_record(self):
        def job(w):
            w._close_csv()
            w.out.put(("note", "recording stopped"))
        self.worker.post(job)
        self.recording = False
        self.btn_rec.config(text="\u25cf  START RECORDING")
        self.lbl_rec.config(text="not recording", foreground="#555")

    # -----------------------------------------------------------------------
    # Queue pump -- the only place the GUI is updated
    # -----------------------------------------------------------------------

    def _pump(self):
        dirty = False
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "data":
                    self._on_data(payload)
                    dirty = True
                elif kind == "state":
                    self._on_state(payload)
                elif kind == "freq_set":
                    self._say(f"SR830 oscillator now at "
                              f"{self._fmt_hz(payload)} Hz   "
                              f"(2f = {self._fmt_hz(2 * payload)} Hz)")
                elif kind == "note":
                    self._say(payload)
                elif kind == "error":
                    self._say(payload, error=True)
                elif kind == "acq":
                    self.monitoring = bool(payload)
                    self.btn_mon.config(
                        text="Stop monitor" if payload else "Start monitor")
        except queue.Empty:
            pass
        if dirty:
            self._redraw()
        self.after(80, self._pump)

    def _on_data(self, d):
        self.big["X"].config(text=eng(d["X"], "V"))
        self.big["Y"].config(text=eng(d["Y"], "V"))
        self.big["R"].config(text=eng(d["R"], "V"))
        self.big["theta"].config(text=f"{d['theta']:+.2f}\u00b0")

        if d["ovl"]:
            flags = ", ".join(k for k, v in d["ovl_detail"].items() if v)
            self.lbl_ovl.config(text=f"OVERLOAD: {flags}")
        else:
            self.lbl_ovl.config(text="")

        self.t_hist.append(d["t"])
        self.r_hist.append(d["R"])
        self.x_hist.append(d["X"])
        self.y_hist.append(d["Y"])
        self.th_hist.append(d["theta"])   # +++ ADDED

        if "rows" in d:
            self.lbl_rec.config(
                text=f"recording -> {os.path.basename(d['path'])}   "
                     f"{d['rows']} rows   {d['t']:.0f} s",
                foreground="#0a0")

    def _on_state(self, st):
        self._suppress = True
        try:
            E = getattr(self, "_ref_entries", {})
            self._set_var_idle(self.v_freq, E.get(str(self.v_freq)),
                               self._fmt_hz(st["frequency_Hz"]))
            self._set_var_idle(self.v_ampl, E.get(str(self.v_ampl)),
                               f"{st['amplitude_Vrms']:.3f}")
            self._set_var_idle(self.v_phase, E.get(str(self.v_phase)),
                               f"{st['phase_deg']:.2f}")
            self._set_var_idle(self.v_harm, E.get(str(self.v_harm)),
                               str(st["harmonic"]))
            self.v_src.set("Internal" if st["internal_ref"] else "External")
            self.v_sens.set(eng(st["input_V_fullscale"], "V"))
            self.v_tc.set(eng(st["time_constant_s"], "s"))
            self.v_slope.set(str(st["slope_dB_oct"]))

            self.lbl_detect.config(
                text=f"{st['frequency_Hz'] * st['harmonic'] / 1000:.4g} kHz")
            n_tc = {6: 5, 12: 7, 18: 9, 24: 11}[st["slope_dB_oct"]]
            self.lbl_settle.config(
                text=f"~{n_tc * st['time_constant_s']:.3g} s")
        except Exception:
            pass
        finally:
            self._suppress = False

    def _clear_plot(self):
        for d in (self.t_hist, self.r_hist, self.x_hist, self.y_hist,
                  self.th_hist):                       # +++ CHANGED
            d.clear()
        self._redraw()

    def _redraw(self):
        if not HAVE_PLOT or self.canvas is None or not self.t_hist:
            return
        self.ax.clear()
        self.ax.grid(alpha=0.3)
        self.ax.set_xlabel("time (s)")
        self.ax.set_ylabel("V")
        t = list(self.t_hist)
        mode = self.v_trace.get()
        if mode == "R":
            self.ax.plot(t, list(self.r_hist), lw=1.2, label="R")
        elif mode == "X":
            self.ax.plot(t, list(self.x_hist), lw=1.2, label="X")
        elif mode == "Y":
            self.ax.plot(t, list(self.y_hist), lw=1.2, label="Y")
        elif mode == "\u03b8":                              # +++ ADDED
            self.ax.plot(t, list(self.th_hist), lw=1.2, label="\u03b8")
            self.ax.set_ylabel("degrees")
            self.ax.set_ylim(-190, 190)
        else:
            self.ax.plot(t, list(self.x_hist), lw=1.2, label="X")
            self.ax.plot(t, list(self.y_hist), lw=1.2, label="Y")
        self.ax.legend(loc="upper right", fontsize=8)
        self.fig.tight_layout()
        self.canvas.draw_idle()

    # +++ ADDED
    def _mark_event(self):
        note = self.meta.values().get("note", "") or "mark"
        self.worker.mark(note)
        self._say(f"marked: {note}")

    def _say(self, msg, error=False):
        self.status.config(text=msg, foreground="#b00" if error else "#000")

    # -----------------------------------------------------------------------

    def on_close(self):
        if self.recording:
            if not messagebox.askokcancel(
                    "Still recording",
                    "A recording is in progress. Close and stop it?"):
                return
            self._stop_record()
        self.worker.shutdown()
        self.worker.join(timeout=2.0)
        self.master.destroy()


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="SR830 control panel")
    p.add_argument("--resource", default="GPIB0::8::INSTR")
    p.add_argument("--prologix", action="store_true",
                   help="adapter is a Prologix GPIB-USB on a COM port")
    p.add_argument("--addr", type=int, default=8)
    p.add_argument("--demo", action="store_true",
                   help="run against a simulated instrument")
    args = p.parse_args()

    root = tk.Tk()
    root.title("SR830 lock-in amplifier")

    try:
        li = (FakeSR830() if args.demo
              else SR830(args.resource, prologix=args.prologix, addr=args.addr))
    except Exception as exc:
        root.withdraw()
        messagebox.showerror(
            "Connection failed",
            f"Could not open {args.resource}\n\n{type(exc).__name__}: {exc}\n\n"
            "Check that the GPIB address matches (SETUP key on the front "
            "panel) and that the adapter appears in NI MAX or Device Manager.")
        return 1

    app = App(root, li)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.minsize(980, 760)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
