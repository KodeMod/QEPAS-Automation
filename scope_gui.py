0#!/usr/bin/env python3
"""
scope_gui.py -- desktop control panel for the bench Tektronix TBS 1052B-EDU

    TEKTRONIX,TBS 1052B-EDU,C015932,CF:91.1CT FV:v3.25
    USB0::0x0699::0x0368::C015932::INSTR

Full front-panel control, live time-domain and FFT plots, running readouts
(DC / RMS / Vpp / f0 / harmonic / SNR), and CSV logging with optional
auto-save. Built for QEPAS 2f-WMS work: set the target frequency to your
drive frequency and the SNR box tracks it live while you tune.

Run:
    python scope_gui.py

Requires: pyvisa, numpy, matplotlib (all present in a standard Anaconda
install once pyvisa is added), plus NI-VISA on the machine.

Scope USB port must be in USBTMC mode:
    Utility > Options > Rear USB Port > Computer

Close the NI VISA Test Panel before connecting -- USBTMC sessions are
exclusive and the panel will hold the port.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import numpy as np

try:
    import pyvisa
except ImportError:
    print("pyvisa not installed. Run:\n"
          '  "C:\\Users\\Admin\\anaconda3\\python.exe" -m pip install pyvisa')
    sys.exit(1)

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk)
from matplotlib.figure import Figure


# ==========================================================================
# constants
# ==========================================================================

DEFAULT_RESOURCE = "USB0::0x0699::0x0368::C015932::INSTR"
RECORD_LENGTH = 2500

VSCALE_OPTIONS = ["0.002", "0.005", "0.01", "0.02", "0.05",
                  "0.1", "0.2", "0.5", "1", "2", "5"]

SAVE_PER_CAPTURE = "One file per capture"
SAVE_SINGLE_WAVE = "Single file - waveforms"
SAVE_SINGLE_SUMMARY = "Single file - summary only"
SAVE_MODES = [SAVE_SINGLE_WAVE, SAVE_SINGLE_SUMMARY, SAVE_PER_CAPTURE]
TIMEBASE_OPTIONS = ["1e-6", "2.5e-6", "5e-6", "10e-6", "25e-6", "50e-6",
                    "100e-6", "250e-6", "500e-6", "1e-3", "2.5e-3", "5e-3",
                    "10e-3", "25e-3", "50e-3", "100e-3", "250e-3", "500e-3",
                    "1", "2.5", "5"]


# ==========================================================================
# instrument layer -- every VISA call happens on the worker thread
# ==========================================================================

class ScopeSession:
    def __init__(self):
        self.rm = None
        self.inst = None
        self.idn = ""

    def list_resources(self):
        if self.rm is None:
            self.rm = pyvisa.ResourceManager()
        return list(self.rm.list_resources())

    def connect(self, address):
        if self.rm is None:
            self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(address)
        self.inst.timeout = 15000
        self.inst.chunk_size = 1024 * 1024
        self.idn = self.inst.query("*IDN?").strip()

        # transfer format -- set once, reused for every capture
        self.inst.write("DATa:ENCdg RPBinary")
        self.inst.write("DATa:WIDth 1")
        self.inst.write(f"DATa:STARt 1")
        self.inst.write(f"DATa:STOP {RECORD_LENGTH}")
        return self.idn

    def disconnect(self):
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception:
                pass
        self.inst = None
        self.idn = ""

    @property
    def connected(self):
        return self.inst is not None

    def _try(self, cmd):
        try:
            self.inst.write(cmd)
        except Exception as exc:
            return f"rejected: {cmd} ({exc})"
        return None

    def apply_settings(self, s):
        """Push front-panel settings to the scope. Returns list of warnings."""
        warnings = []
        ch = s["channel"]

        for cmd in (
            f"{ch}:SCAle {s['vscale']}",
            f"{ch}:POSition {s['voffset']}",
            f"{ch}:COUPling {s['coupling']}",
            f"{ch}:PRObe {s['probe']}",
        ):
            w = self._try(cmd)
            if w:
                warnings.append(w)

        # TBS1000B takes HORizontal:SCAle; older TDS want the MAIn form
        try:
            self.inst.write(f"HORizontal:SCAle {s['timebase']}")
        except Exception:
            w = self._try(f"HORizontal:MAIn:SCAle {s['timebase']}")
            if w:
                warnings.append(w)

        for cmd in (
            "TRIGger:MAIn:TYPe EDGE",
            f"TRIGger:MAIn:EDGE:SOUrce {ch}",
            f"TRIGger:MAIn:EDGE:SLOpe {s['slope']}",
            f"TRIGger:MAIn:LEVel {s['trig_level']}",
            f"TRIGger:MAIn:MODe {s['trig_mode']}",
            f"ACQuire:MODe {s['acq_mode']}",
            "ACQuire:STATE RUN",
            f"DATa:SOUrce {ch}",
        ):
            w = self._try(cmd)
            if w:
                warnings.append(w)

        return warnings

    def autoset(self):
        self.inst.write("AUTOSet EXECute")
        time.sleep(2.0)

    def read_settings(self, channel):
        """Read the scope's actual state back, so the GUI can sync to it."""
        def q(cmd):
            return self.inst.query(cmd).strip()

        out = {}
        try:
            out["vscale"] = float(q(f"{channel}:SCAle?"))
            out["voffset"] = float(q(f"{channel}:POSition?"))
            out["coupling"] = q(f"{channel}:COUPling?").upper()
            out["probe"] = float(q(f"{channel}:PRObe?"))
        except Exception:
            pass
        try:
            out["timebase"] = float(q("HORizontal:SCAle?"))
        except Exception:
            try:
                out["timebase"] = float(q("HORizontal:MAIn:SCAle?"))
            except Exception:
                pass
        try:
            out["trig_level"] = float(q("TRIGger:MAIn:LEVel?"))
        except Exception:
            pass
        return out

    def acquire(self):
        """Return (time_s, volts)."""
        def q(cmd):
            return float(self.inst.query(cmd).strip())

        ymult = q("WFMPre:YMUlt?")
        yzero = q("WFMPre:YZEro?")
        yoff = q("WFMPre:YOFf?")
        xincr = q("WFMPre:XINcr?")
        xzero = q("WFMPre:XZEro?")
        ptoff = q("WFMPre:PT_Off?")

        raw = self.inst.query_binary_values(
            "CURVe?", datatype="B", container=np.array, is_big_endian=True)

        volts = (raw.astype(np.float64) - yoff) * ymult + yzero
        idx = np.arange(len(volts))
        seconds = (idx - ptoff) * xincr + xzero
        return seconds, volts


# ==========================================================================
# analysis
# ==========================================================================

def analyse(t, v, target=None, harmonic=2):
    dt = float(np.mean(np.diff(t)))
    n = len(v)
    dc = float(np.mean(v))

    res = {
        "fs": 1.0 / dt,
        "n": n,
        "duration": float(t[-1] - t[0]),
        "dc": dc,
        "rms": float(np.sqrt(np.mean(v ** 2))),
        "ac_rms": float(np.sqrt(np.mean((v - dc) ** 2))),
        "vpp": float(np.max(v) - np.min(v)),
        "vmax": float(np.max(v)),
        "vmin": float(np.min(v)),
    }

    win = np.hanning(n)
    gain = win.mean()
    spec = np.fft.rfft((v - dc) * win)
    freqs = np.fft.rfftfreq(n, dt)
    mag = np.abs(spec) / (n * gain) * 2.0

    res["freqs"] = freqs
    res["mag"] = mag
    res["df"] = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0

    pk = int(np.argmax(mag[1:]) + 1) if n > 2 else 0
    res["f0"] = float(freqs[pk])
    res["f0_amp"] = float(mag[pk])

    # amplitude at the user's target and its harmonic
    res["target_amp"] = None
    res["harm_amp"] = None
    res["harm_freq"] = None
    res["snr_db"] = None
    res["noise_floor"] = None

    if target and target > 0 and target <= freqs[-1]:
        j = int(np.argmin(np.abs(freqs - target)))
        res["target_amp"] = float(mag[j])

        mask = np.ones_like(mag, dtype=bool)
        mask[max(0, j - 3):min(len(mag), j + 4)] = False
        mask[0] = False
        if mask.any():
            floor = float(np.median(mag[mask]))
            res["noise_floor"] = floor
            if floor > 0:
                res["snr_db"] = 20.0 * np.log10(mag[j] / floor)

        hf = target * harmonic
        if hf <= freqs[-1]:
            k = int(np.argmin(np.abs(freqs - hf)))
            res["harm_freq"] = float(freqs[k])
            res["harm_amp"] = float(mag[k])

    return res


def _session_meta(idn, settings):
    """Comment block written once at the top of a session file."""
    return [
        ["# instrument", idn],
        ["# session_started", datetime.now().isoformat(timespec="seconds")],
        ["# channel", settings.get("channel", "")],
        ["# volts_per_div", settings.get("vscale", "")],
        ["# sec_per_div", settings.get("timebase", "")],
        ["# coupling", settings.get("coupling", "")],
        ["# probe_atten", settings.get("probe", "")],
        ["# target_Hz", settings.get("target", "")],
        ["# harmonic", settings.get("harmonic", "")],
    ]


def append_waveform_csv(path, t, v, res, idn, settings,
                        capture_no, elapsed, new_file):
    """Append a full waveform to one continuous session file.

    elapsed_s is a running time axis across the whole session, so the
    file reads as one voltage-vs-time record rather than N restarts.
    """
    import csv
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            for row in _session_meta(idn, settings):
                w.writerow(row)
            w.writerow(["# note", "elapsed_s runs across the whole session; "
                                  "time_s is relative to each trigger"])
            w.writerow(["capture", "iso_time", "elapsed_s",
                        "time_s", "volts_V"])
        stamp = datetime.now().isoformat(timespec="milliseconds")
        for ti, vi in zip(t, v):
            w.writerow([capture_no, stamp, f"{elapsed + ti:.9g}",
                        f"{ti:.9g}", f"{vi:.9g}"])


def append_summary_csv(path, res, idn, settings,
                       capture_no, elapsed, new_file):
    """Append one row of measurements per capture -- no raw waveform."""
    import csv

    def g(key):
        val = res.get(key)
        return "" if val is None else f"{val:.9g}"

    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            for row in _session_meta(idn, settings):
                w.writerow(row)
            w.writerow(["capture", "iso_time", "elapsed_s",
                        "dc_V", "rms_V", "ac_rms_V", "vpp_V",
                        "vmax_V", "vmin_V", "f0_Hz", "f0_amp_V",
                        "target_amp_V", "harm_Hz", "harm_amp_V",
                        "noise_floor_V", "snr_dB",
                        "sample_rate_Hz", "fft_bin_Hz"])
        w.writerow([
            capture_no,
            datetime.now().isoformat(timespec="milliseconds"),
            f"{elapsed:.6f}",
            g("dc"), g("rms"), g("ac_rms"), g("vpp"),
            g("vmax"), g("vmin"), g("f0"), g("f0_amp"),
            g("target_amp"), g("harm_freq"), g("harm_amp"),
            g("noise_floor"), g("snr_db"),
            g("fs"), g("df"),
        ])


def save_csv(path, t, v, res, idn, settings):
    import csv
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# instrument", idn])
        w.writerow(["# timestamp", datetime.now().isoformat(timespec="seconds")])
        w.writerow(["# channel", settings.get("channel", "")])
        w.writerow(["# volts_per_div", settings.get("vscale", "")])
        w.writerow(["# sec_per_div", settings.get("timebase", "")])
        w.writerow(["# coupling", settings.get("coupling", "")])
        w.writerow(["# probe_atten", settings.get("probe", "")])
        w.writerow(["# sample_rate_Hz", f"{res['fs']:.9g}"])
        w.writerow(["# fft_bin_Hz", f"{res['df']:.9g}"])
        w.writerow(["# dc_V", f"{res['dc']:.9g}"])
        w.writerow(["# rms_V", f"{res['rms']:.9g}"])
        w.writerow(["# ac_rms_V", f"{res['ac_rms']:.9g}"])
        w.writerow(["# vpp_V", f"{res['vpp']:.9g}"])
        w.writerow(["# f0_Hz", f"{res['f0']:.9g}"])
        if res["snr_db"] is not None:
            w.writerow(["# target_Hz", settings.get("target", "")])
            w.writerow(["# target_amp_V", f"{res['target_amp']:.9g}"])
            w.writerow(["# noise_floor_V", f"{res['noise_floor']:.9g}"])
            w.writerow(["# snr_dB", f"{res['snr_db']:.9g}"])
        w.writerow(["time_s", "volts_V"])
        for ti, vi in zip(t, v):
            w.writerow([f"{ti:.9g}", f"{vi:.9g}"])


# ==========================================================================
# worker thread -- owns the VISA session
# ==========================================================================

class Worker(threading.Thread):
    def __init__(self, cmd_q, out_q):
        super().__init__(daemon=True)
        self.cmd_q = cmd_q
        self.out_q = out_q
        self.scope = ScopeSession()
        self.running = True
        self.streaming = False
        self.interval = 1.0
        self.settings = {}
        self.capture_no = 0
        self.session_path = None
        self.session_start = None

    def emit(self, kind, **kw):
        self.out_q.put({"kind": kind, **kw})

    def run(self):
        next_grab = 0.0
        while self.running:
            # drain pending commands
            try:
                while True:
                    self.handle(self.cmd_q.get_nowait())
            except queue.Empty:
                pass

            now = time.time()
            if self.streaming and self.scope.connected and now >= next_grab:
                self.grab()
                next_grab = time.time() + self.interval
            else:
                time.sleep(0.03)

    def handle(self, cmd):
        action = cmd.get("action")
        try:
            if action == "list":
                self.emit("resources", items=self.scope.list_resources())

            elif action == "connect":
                idn = self.scope.connect(cmd["address"])
                self.emit("connected", idn=idn, address=cmd["address"])

            elif action == "disconnect":
                self.streaming = False
                self.scope.disconnect()
                self.emit("disconnected")

            elif action == "settings":
                self.settings = cmd["settings"]
                if self.scope.connected:
                    warns = self.scope.apply_settings(self.settings)
                    for w in warns:
                        self.emit("log", text=w, level="warn")
                    self.emit("log", text="Settings applied.")

            elif action == "autoset":
                if self.scope.connected:
                    self.emit("log", text="Running Autoset...")
                    self.scope.autoset()
                    actual = self.scope.read_settings(
                        self.settings.get("channel", "CH1"))
                    self.emit("sync", values=actual)
                    self.emit("log", text="Autoset done, panel synced.")

            elif action == "sync":
                if self.scope.connected:
                    actual = self.scope.read_settings(
                        self.settings.get("channel", "CH1"))
                    self.emit("sync", values=actual)
                    self.emit("log", text="Read settings back from scope.")

            elif action == "single":
                self.grab()

            elif action == "start":
                self.interval = cmd.get("interval", 1.0)
                # each Run begins a new session file and a new time origin
                self.session_path = None
                self.session_start = None
                self.capture_no = 0
                self.streaming = True
                self.emit("log", text=f"Streaming every {self.interval:g}s.")

            elif action == "new_file":
                self.session_path = None
                self.session_start = None
                self.capture_no = 0
                self.emit("log", text="Next capture starts a new file.")

            elif action == "stop":
                self.streaming = False
                self.emit("log", text="Streaming stopped.")

            elif action == "reset_counter":
                self.capture_no = 0

            elif action == "quit":
                self.streaming = False
                self.scope.disconnect()
                self.running = False

        except Exception as exc:
            self.streaming = False
            self.emit("error", text=f"{type(exc).__name__}: {exc}")

    def grab(self):
        try:
            t, v = self.scope.acquire()
        except Exception as exc:
            self.streaming = False
            self.emit("error", text=f"Acquisition failed: {exc}")
            return

        self.capture_no += 1
        s = self.settings

        # a blank target is fine -- it just disables the target/SNR
        # readouts. A malformed one should warn, not kill the thread.
        target = None
        raw_target = str(s.get("target", "")).strip()
        if raw_target:
            try:
                target = float(raw_target)
                if target <= 0:
                    target = None
            except ValueError:
                self.emit("log", level="warn",
                          text=f"Target '{raw_target}' is not a number "
                               f"-- ignoring it.")

        try:
            harmonic = int(s.get("harmonic", 2))
        except (ValueError, TypeError):
            harmonic = 2

        try:
            res = analyse(t, v, target=target, harmonic=harmonic)
        except Exception as exc:
            self.streaming = False
            self.emit("error", text=f"Analysis failed: {exc}")
            return

        saved = None
        if s.get("autosave") and s.get("outdir"):
            try:
                mode = s.get("save_mode", SAVE_SINGLE_WAVE)
                prefix = s.get("prefix") or "scope"

                if mode == SAVE_PER_CAPTURE:
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    name = f"{prefix}_{stamp}_{self.capture_no:04d}.csv"
                    path = os.path.join(s["outdir"], name)
                    save_csv(path, t, v, res, self.scope.idn, s)
                    saved = path
                else:
                    new_file = self.session_path is None
                    if new_file:
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        tag = ("summary" if mode == SAVE_SINGLE_SUMMARY
                               else "log")
                        self.session_path = os.path.join(
                            s["outdir"], f"{prefix}_{stamp}_{tag}.csv")
                        self.session_start = time.time()
                        self.emit("log",
                                  text=f"Logging to "
                                       f"{os.path.basename(self.session_path)}")

                    elapsed = time.time() - self.session_start

                    if mode == SAVE_SINGLE_SUMMARY:
                        append_summary_csv(self.session_path, res,
                                           self.scope.idn, s,
                                           self.capture_no, elapsed, new_file)
                    else:
                        append_waveform_csv(self.session_path, t, v, res,
                                            self.scope.idn, s,
                                            self.capture_no, elapsed, new_file)
                    saved = self.session_path

            except Exception as exc:
                self.emit("log", text=f"Save failed: {exc}", level="warn")

        self.emit("capture", t=t, v=v, res=res,
                  n=self.capture_no, saved=saved)


# ==========================================================================
# GUI
# ==========================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TBS 1052B-EDU  |  Scope Control")
        self.geometry("1280x820")
        self.minsize(1050, 700)

        self.cmd_q = queue.Queue()
        self.out_q = queue.Queue()
        self.worker = Worker(self.cmd_q, self.out_q)
        self.worker.start()

        self.last_capture = None   # (t, v, res)
        self.connected = False

        self._build()
        self.send({"action": "list"})
        self.after(80, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- plumbing ---------------------------------------------------------

    def send(self, cmd):
        self.cmd_q.put(cmd)

    def log(self, text, level="info"):
        stamp = datetime.now().strftime("%H:%M:%S")
        tag = {"info": "", "warn": "  [!] ", "error": "  [ERROR] "}[level]
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{stamp}{tag} {text}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def collect_settings(self):
        return {
            "channel": self.v_channel.get(),
            "vscale": self.v_vscale.get(),
            "voffset": self.v_voffset.get(),
            "coupling": self.v_coupling.get(),
            "probe": self.v_probe.get(),
            "timebase": self.v_timebase.get(),
            "trig_level": self.v_triglevel.get(),
            "slope": self.v_slope.get(),
            "trig_mode": self.v_trigmode.get(),
            "acq_mode": self.v_acqmode.get(),
            "target": self.v_target.get(),
            "harmonic": self.v_harmonic.get(),
            "autosave": bool(self.v_autosave.get()),
            "outdir": self.v_outdir.get(),
            "prefix": self.v_prefix.get(),
            "save_mode": self.v_savemode.get(),
        }

    def push_settings(self):
        self.send({"action": "settings", "settings": self.collect_settings()})

    # -- layout -----------------------------------------------------------

    def _build(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        left = ttk.Frame(root)
        left.pack(side="left", fill="y", padx=(0, 8))

        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True)

        self._build_connection(left)
        self._build_controls(left)
        self._build_acquisition(left)
        self._build_saving(left)

        self._build_readouts(right)
        self._build_plots(right)
        self._build_log(right)

    def _build_connection(self, parent):
        f = ttk.LabelFrame(parent, text="Connection", padding=6)
        f.pack(fill="x", pady=(0, 6))

        self.v_resource = tk.StringVar(value=DEFAULT_RESOURCE)
        self.cb_resource = ttk.Combobox(f, textvariable=self.v_resource,
                                        width=34, state="readonly")
        self.cb_resource.grid(row=0, column=0, columnspan=3,
                              sticky="ew", pady=2)

        ttk.Button(f, text="Refresh", width=10,
                   command=lambda: self.send({"action": "list"})
                   ).grid(row=1, column=0, pady=2, sticky="ew")
        self.btn_connect = ttk.Button(f, text="Connect", width=10,
                                      command=self.on_connect)
        self.btn_connect.grid(row=1, column=1, pady=2, sticky="ew")
        self.btn_disconnect = ttk.Button(f, text="Disconnect", width=10,
                                         command=self.on_disconnect,
                                         state="disabled")
        self.btn_disconnect.grid(row=1, column=2, pady=2, sticky="ew")

        self.lbl_idn = ttk.Label(f, text="not connected",
                                 foreground="#888", wraplength=270,
                                 justify="left")
        self.lbl_idn.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        for c in range(3):
            f.columnconfigure(c, weight=1)

    def _build_controls(self, parent):
        f = ttk.LabelFrame(parent, text="Scope settings", padding=6)
        f.pack(fill="x", pady=(0, 6))

        r = 0
        ttk.Label(f, text="Channel").grid(row=r, column=0, sticky="w")
        self.v_channel = tk.StringVar(value="CH1")
        ttk.Combobox(f, textvariable=self.v_channel, values=["CH1", "CH2"],
                     width=14, state="readonly").grid(row=r, column=1,
                                                      sticky="ew", pady=1)
        r += 1
        ttk.Label(f, text="Volts/div").grid(row=r, column=0, sticky="w")
        self.v_vscale = tk.StringVar(value="0.02")
        ttk.Combobox(f, textvariable=self.v_vscale, values=VSCALE_OPTIONS,
                     width=14).grid(row=r, column=1, sticky="ew", pady=1)

        r += 1
        ttk.Label(f, text="Sec/div").grid(row=r, column=0, sticky="w")
        self.v_timebase = tk.StringVar(value="250e-6")
        ttk.Combobox(f, textvariable=self.v_timebase, values=TIMEBASE_OPTIONS,
                     width=14).grid(row=r, column=1, sticky="ew", pady=1)

        r += 1
        ttk.Label(f, text="Coupling").grid(row=r, column=0, sticky="w")
        self.v_coupling = tk.StringVar(value="AC")
        ttk.Combobox(f, textvariable=self.v_coupling,
                     values=["AC", "DC", "GND"], width=14,
                     state="readonly").grid(row=r, column=1,
                                            sticky="ew", pady=1)
        r += 1
        ttk.Label(f, text="Vert. position").grid(row=r, column=0, sticky="w")
        self.v_voffset = tk.StringVar(value="0")
        ttk.Entry(f, textvariable=self.v_voffset, width=16).grid(
            row=r, column=1, sticky="ew", pady=1)

        r += 1
        ttk.Label(f, text="Probe atten.").grid(row=r, column=0, sticky="w")
        self.v_probe = tk.StringVar(value="1")
        ttk.Combobox(f, textvariable=self.v_probe, values=["1", "10", "100"],
                     width=14).grid(row=r, column=1, sticky="ew", pady=1)

        r += 1
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=5)

        r += 1
        ttk.Label(f, text="Trigger level (V)").grid(row=r, column=0, sticky="w")
        self.v_triglevel = tk.StringVar(value="0")
        ttk.Entry(f, textvariable=self.v_triglevel, width=16).grid(
            row=r, column=1, sticky="ew", pady=1)

        r += 1
        ttk.Label(f, text="Slope").grid(row=r, column=0, sticky="w")
        self.v_slope = tk.StringVar(value="RISe")
        ttk.Combobox(f, textvariable=self.v_slope, values=["RISe", "FALL"],
                     width=14, state="readonly").grid(row=r, column=1,
                                                      sticky="ew", pady=1)
        r += 1
        ttk.Label(f, text="Trigger mode").grid(row=r, column=0, sticky="w")
        self.v_trigmode = tk.StringVar(value="AUTO")
        ttk.Combobox(f, textvariable=self.v_trigmode, values=["AUTO", "NORMal"],
                     width=14, state="readonly").grid(row=r, column=1,
                                                      sticky="ew", pady=1)
        r += 1
        ttk.Label(f, text="Acquire mode").grid(row=r, column=0, sticky="w")
        self.v_acqmode = tk.StringVar(value="SAMple")
        ttk.Combobox(f, textvariable=self.v_acqmode,
                     values=["SAMple", "AVErage", "PEAKdetect"], width=14,
                     state="readonly").grid(row=r, column=1,
                                            sticky="ew", pady=1)
        r += 1
        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="Apply", command=self.push_settings).pack(
            side="left", fill="x", expand=True, padx=1)
        ttk.Button(btns, text="Autoset",
                   command=lambda: (self.push_settings(),
                                    self.send({"action": "autoset"}))).pack(
            side="left", fill="x", expand=True, padx=1)
        ttk.Button(btns, text="Read back",
                   command=lambda: self.send({"action": "sync"})).pack(
            side="left", fill="x", expand=True, padx=1)

        f.columnconfigure(1, weight=1)

    def _build_acquisition(self, parent):
        f = ttk.LabelFrame(parent, text="Acquisition", padding=6)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Target freq (Hz)").grid(row=0, column=0, sticky="w")
        self.v_target = tk.StringVar(value="6775")
        ttk.Entry(f, textvariable=self.v_target, width=16).grid(
            row=0, column=1, sticky="ew", pady=1)

        ttk.Label(f, text="Harmonic").grid(row=1, column=0, sticky="w")
        self.v_harmonic = tk.StringVar(value="2")
        ttk.Combobox(f, textvariable=self.v_harmonic,
                     values=["1", "2", "3", "4", "5", "6"],
                     width=14, state="readonly").grid(row=1, column=1,
                                                      sticky="ew", pady=1)

        ttk.Label(f, text="Interval (s)").grid(row=2, column=0, sticky="w")
        self.v_interval = tk.StringVar(value="1.0")
        ttk.Entry(f, textvariable=self.v_interval, width=16).grid(
            row=2, column=1, sticky="ew", pady=1)

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.btn_single = ttk.Button(bar, text="Single",
                                     command=self.on_single, state="disabled")
        self.btn_single.pack(side="left", fill="x", expand=True, padx=1)
        self.btn_run = ttk.Button(bar, text="Run", command=self.on_run,
                                  state="disabled")
        self.btn_run.pack(side="left", fill="x", expand=True, padx=1)
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.on_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=1)

        f.columnconfigure(1, weight=1)

    def _build_saving(self, parent):
        f = ttk.LabelFrame(parent, text="CSV output", padding=6)
        f.pack(fill="x", pady=(0, 6))

        self.v_outdir = tk.StringVar(value=os.getcwd())
        ttk.Entry(f, textvariable=self.v_outdir, width=28).grid(
            row=0, column=0, sticky="ew", pady=1)
        ttk.Button(f, text="...", width=4, command=self.on_browse).grid(
            row=0, column=1, padx=(3, 0))

        ttk.Label(f, text="Prefix").grid(row=1, column=0, sticky="w",
                                         pady=(4, 0))
        self.v_prefix = tk.StringVar(value="qepas")
        ttk.Entry(f, textvariable=self.v_prefix, width=28).grid(
            row=2, column=0, sticky="ew")

        ttk.Label(f, text="Save mode").grid(row=3, column=0, sticky="w",
                                            pady=(4, 0))
        self.v_savemode = tk.StringVar(value=SAVE_SINGLE_WAVE)
        ttk.Combobox(f, textvariable=self.v_savemode, values=SAVE_MODES,
                     width=26, state="readonly").grid(
            row=4, column=0, sticky="ew")

        self.v_autosave = tk.IntVar(value=0)
        ttk.Checkbutton(f, text="Auto-save every capture",
                        variable=self.v_autosave,
                        command=self.push_settings).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(5, 0))

        ttk.Button(f, text="Start new file",
                   command=lambda: self.send({"action": "new_file"})).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        self.btn_save = ttk.Button(f, text="Save current capture",
                                   command=self.on_save_now, state="disabled")
        self.btn_save.grid(row=7, column=0, columnspan=2,
                           sticky="ew", pady=(3, 0))

        f.columnconfigure(0, weight=1)

    def _build_readouts(self, parent):
        f = ttk.LabelFrame(parent, text="Measurements", padding=6)
        f.pack(fill="x", pady=(0, 6))

        self.readouts = {}
        self.readout_labels = {}
        fields = [
            ("dc", "DC"), ("rms", "RMS"), ("ac_rms", "AC RMS"),
            ("vpp", "Vpp"), ("f0", "Peak f0"), ("target_amp", "@ target"),
            ("harm_amp", "@ 2f"), ("snr_db", "SNR"),
            ("fs", "Sample rate"), ("df", "FFT bin"),
        ]
        for i, (key, label) in enumerate(fields):
            col = i % 5
            row = (i // 5) * 2
            lbl = ttk.Label(f, text=label, foreground="#666")
            lbl.grid(row=row, column=col, sticky="w", padx=6)
            self.readout_labels[key] = lbl
            var = tk.StringVar(value="--")
            ttk.Label(f, textvariable=var,
                      font=("Consolas", 12, "bold")).grid(
                row=row + 1, column=col, sticky="w", padx=6, pady=(0, 4))
            self.readouts[key] = var

        self.lbl_harm = self.readout_labels["harm_amp"]

        for c in range(5):
            f.columnconfigure(c, weight=1)

    def _build_plots(self, parent):
        f = ttk.Frame(parent)
        f.pack(fill="both", expand=True)

        self.fig = Figure(figsize=(8, 5), dpi=100)
        self.ax_time = self.fig.add_subplot(211)
        self.ax_fft = self.fig.add_subplot(212)

        self.ax_time.set_xlabel("Time (ms)")
        self.ax_time.set_ylabel("Volts (mV)")
        self.ax_time.grid(alpha=0.3)
        self.ax_fft.set_xlabel("Frequency (kHz)")
        self.ax_fft.set_ylabel("Amplitude (mV)")
        self.ax_fft.grid(alpha=0.3)

        self.line_time, = self.ax_time.plot([], [], lw=1.0)
        self.line_fft, = self.ax_fft.plot([], [], lw=1.0)
        self.marker_target = self.ax_fft.axvline(
            0, color="tab:red", ls="--", lw=0.9, visible=False)
        self.marker_harm = self.ax_fft.axvline(
            0, color="tab:orange", ls=":", lw=0.9, visible=False)

        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, f).update()

        opts = ttk.Frame(f)
        opts.pack(fill="x")
        self.v_logy = tk.IntVar(value=0)
        ttk.Checkbutton(opts, text="Log FFT axis", variable=self.v_logy,
                        command=self._redraw).pack(side="left")
        self.v_fftzoom = tk.IntVar(value=1)
        ttk.Checkbutton(opts, text="Zoom FFT around target",
                        variable=self.v_fftzoom,
                        command=self._redraw).pack(side="left", padx=10)

    def _build_log(self, parent):
        f = ttk.LabelFrame(parent, text="Log", padding=4)
        f.pack(fill="x", pady=(6, 0))
        self.logbox = tk.Text(f, height=6, state="disabled",
                              font=("Consolas", 9), wrap="word")
        self.logbox.pack(fill="x")

    # -- actions ----------------------------------------------------------

    def on_connect(self):
        addr = self.v_resource.get().strip()
        if not addr:
            messagebox.showwarning("No resource",
                                   "Pick a VISA resource first.")
            return
        self.log(f"Connecting to {addr} ...")
        self.send({"action": "connect", "address": addr})

    def on_disconnect(self):
        self.send({"action": "disconnect"})

    def on_single(self):
        self.push_settings()
        self.send({"action": "single"})

    def on_run(self):
        try:
            interval = float(self.v_interval.get())
        except ValueError:
            messagebox.showwarning("Bad interval", "Interval must be a number.")
            return
        self.push_settings()
        self.send({"action": "start", "interval": max(0.05, interval)})
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def on_stop(self):
        self.send({"action": "stop"})
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.v_outdir.get())
        if d:
            self.v_outdir.set(d)
            self.push_settings()

    def on_save_now(self):
        if self.last_capture is None:
            return
        t, v, res = self.last_capture
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = self.v_prefix.get() or "scope"
        path = filedialog.asksaveasfilename(
            initialdir=self.v_outdir.get(),
            initialfile=f"{prefix}_{stamp}.csv",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            save_csv(path, t, v, res, self.worker.scope.idn,
                     self.collect_settings())
            self.log(f"Saved {os.path.basename(path)}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    # -- incoming messages ------------------------------------------------

    def _poll(self):
        try:
            while True:
                msg = self.out_q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle(self, msg):
        kind = msg["kind"]

        if kind == "resources":
            items = msg["items"]
            self.cb_resource.configure(values=items)
            if items and self.v_resource.get() not in items:
                self.v_resource.set(items[0])
            self.log(f"{len(items)} VISA resource(s) found."
                     if items else "No VISA resources found. "
                                   "Check USB mode and cable.")

        elif kind == "connected":
            self.connected = True
            self.lbl_idn.configure(text=msg["idn"], foreground="#0a0")
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
            for b in (self.btn_single, self.btn_run):
                b.configure(state="normal")
            self.log(f"Connected: {msg['idn']}")
            self.push_settings()

        elif kind == "disconnected":
            self.connected = False
            self.lbl_idn.configure(text="not connected", foreground="#888")
            self.btn_connect.configure(state="normal")
            self.btn_disconnect.configure(state="disabled")
            for b in (self.btn_single, self.btn_run, self.btn_stop):
                b.configure(state="disabled")
            self.log("Disconnected.")

        elif kind == "sync":
            vals = msg["values"]
            if "vscale" in vals:
                self.v_vscale.set(f"{vals['vscale']:g}")
            if "timebase" in vals:
                self.v_timebase.set(f"{vals['timebase']:g}")
            if "coupling" in vals:
                self.v_coupling.set(vals["coupling"][:2].upper()
                                    if vals["coupling"][:2].upper() in
                                    ("AC", "DC") else "GND")
            if "voffset" in vals:
                self.v_voffset.set(f"{vals['voffset']:g}")
            if "probe" in vals:
                self.v_probe.set(f"{vals['probe']:g}")
            if "trig_level" in vals:
                self.v_triglevel.set(f"{vals['trig_level']:g}")

        elif kind == "capture":
            self.last_capture = (msg["t"], msg["v"], msg["res"])
            self.btn_save.configure(state="normal")
            self._update_readouts(msg["res"])
            self._redraw()
            if msg["saved"]:
                self.log(f"#{msg['n']} saved -> "
                         f"{os.path.basename(msg['saved'])}")

        elif kind == "log":
            self.log(msg["text"], msg.get("level", "info"))

        elif kind == "error":
            self.log(msg["text"], "error")
            self.btn_run.configure(state="normal" if self.connected
                                   else "disabled")
            self.btn_stop.configure(state="disabled")
            messagebox.showerror("Instrument error", msg["text"])

    def _update_readouts(self, res):
        def mv(x):
            return "--" if x is None else f"{x * 1e3:.4g} mV"

        self.readouts["dc"].set(f"{res['dc'] * 1e3:+.4g} mV")
        self.readouts["rms"].set(mv(res["rms"]))
        self.readouts["ac_rms"].set(mv(res["ac_rms"]))
        self.readouts["vpp"].set(mv(res["vpp"]))
        self.readouts["f0"].set(f"{res['f0'] / 1e3:.4g} kHz")
        self.readouts["target_amp"].set(mv(res["target_amp"]))
        self.readouts["harm_amp"].set(mv(res["harm_amp"]))
        self.readouts["snr_db"].set(
            "--" if res["snr_db"] is None else f"{res['snr_db']:.1f} dB")
        self.readouts["fs"].set(f"{res['fs'] / 1e6:.4g} MS/s")
        self.readouts["df"].set(f"{res['df']:.4g} Hz")

        # retitle the harmonic column to match the selected order
        try:
            h = int(self.v_harmonic.get())
        except ValueError:
            h = 2
        self.lbl_harm.configure(
            text=f"@ {h}f" if res["harm_amp"] is not None else "@ harmonic")

    def _redraw(self):
        if self.last_capture is None:
            return
        t, v, res = self.last_capture

        self.line_time.set_data(t * 1e3, v * 1e3)
        self.ax_time.relim()
        self.ax_time.autoscale_view()

        freqs, mag = res["freqs"], res["mag"]
        self.line_fft.set_data(freqs / 1e3, mag * 1e3)
        self.ax_fft.set_yscale("log" if self.v_logy.get() else "linear")

        target = None
        try:
            target = float(self.v_target.get())
        except ValueError:
            pass

        try:
            h = int(self.v_harmonic.get())
        except ValueError:
            h = 2

        if target and self.v_fftzoom.get():
            # keep the selected harmonic comfortably inside the view
            span = max(target * (h + 1.5), target * 3.0, 5e3)
            self.ax_fft.set_xlim(0, span / 1e3)
            sel = freqs <= span
            if sel.any():
                top = float(np.max(mag[sel])) * 1e3
                if not self.v_logy.get():
                    self.ax_fft.set_ylim(0, max(top * 1.2, 1e-6))
                else:
                    self.ax_fft.set_ylim(max(top * 1e-4, 1e-7), top * 2)
        else:
            self.ax_fft.relim()
            self.ax_fft.autoscale_view()

        if target:
            self.marker_target.set_xdata([target / 1e3, target / 1e3])
            self.marker_target.set_visible(True)
            # at h=1 the harmonic marker would sit exactly on the target
            # marker, so hide it rather than drawing two lines on top of
            # each other
            if h == 1:
                self.marker_harm.set_visible(False)
            else:
                self.marker_harm.set_xdata([target * h / 1e3,
                                            target * h / 1e3])
                self.marker_harm.set_visible(True)
        else:
            self.marker_target.set_visible(False)
            self.marker_harm.set_visible(False)

        self.canvas.draw_idle()

    def _on_close(self):
        self.send({"action": "quit"})
        self.after(150, self.destroy)


if __name__ == "__main__":
    App().mainloop()
