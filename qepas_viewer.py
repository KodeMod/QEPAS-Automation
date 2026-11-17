"""
qepas_viewer.py -- GUI for QEPAS log analysis.

Load one or more SR830 CSV logs and either
    * analyse a single file  (time series, vector plane, calibration, Allan), or
    * compare several files  (noise, signal, SNR, phase lock, Allan, vectors).

All the numbers come from qepas_analysis_improved.py, which must sit in the
same folder. This file only handles loading, plotting and export.

Run:
    python qepas_viewer.py
    python qepas_viewer.py run1.csv run2.csv        (preload files)

In Spyder:  %run qepas_viewer.py
"""

import contextlib
import csv
import io
import os
import re
import sys
import warnings
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

try:
    import qepas_analysis_improved as qa
except ImportError:                      # fall back to the plain module
    import qepas_analysis as qa

PALETTE = ["#2E5C8A", "#B4472E", "#4A7C59", "#7B4B94", "#C8962E",
           "#3A8A8C", "#8A5A44", "#5A6B7A"]
mV = 1e3


# ---------------------------------------------------------------------------
# Analysis wrapper
# ---------------------------------------------------------------------------

def short_label(path):
    """'1790050057772_clear_0_004__amplitude_manually.csv' -> 'clear 0 004 amplitude manually'."""
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"^\d{8,}_", "", name)
    name = re.sub(r"_+", " ", name).strip()
    return name or os.path.basename(path)


def run_analysis(path, params, cylinder_pct):
    """Call qa.report quietly. Returns (result dict or None, report text, warnings)."""
    buf = io.StringIO()
    with warnings.catch_warnings(record=True) as caught, contextlib.redirect_stdout(buf):
        warnings.simplefilter("always")
        res = qa.report(
            path,
            sensitivity_uV_per_ppm=params["sensitivity"],
            discard_s=params["discard"],
            cylinder_pct=cylinder_pct,
            tail_s=params["tail"],
            phase_block_s=params["phase_block"],
            uncertainty_block_s=params["unc_block"],
            make_plot=False,
        )
    return res, buf.getvalue(), [str(w.message) for w in caught]


def dense_allan(res, n=28):
    """Allan deviation on a fine tau grid, pooled RMS over continuous blanks."""
    blanks = [s for s in res["segments"] if s["conc"] == 0 and len(s["t"]) > 10]
    if not blanks:
        return np.array([]), np.array([])
    longest = max(b["t"][-1] - b["t"][0] for b in blanks)
    taus = np.logspace(0, np.log10(max(longest / 2.2, 2.0)), n)
    rows = []
    for b in blanks:
        dt = float(np.median(np.diff(b["t"])))
        rows.append(qa.allan_xy(b["X"], b["Y"], dt, taus))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ad = np.sqrt(np.nanmean(np.asarray(rows) ** 2, axis=0))
    ok = np.isfinite(ad)
    return taus[ok], ad[ok]


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _style(ax, title):
    ax.set_title(title, loc="left", fontsize=10, weight="bold", pad=7)
    ax.grid(alpha=0.2, lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)


BLANK_COLOR = "#8A8A8A"


def _level_colors(levels):
    """Grey for N2; gas levels coloured along viridis by concentration."""
    gas = sorted(c for c in levels if c > 0)
    cols = {0: BLANK_COLOR}
    for i, c in enumerate(gas):
        cols[c] = matplotlib.cm.viridis(0.08 + 0.84 * i / max(len(gas) - 1, 1))
    return cols


def _vector_limits(ax, xs, ys, pad=1.25):
    """Symmetric square limits around the origin.

    Arrow annotations do not take part in autoscaling, so the limits have
    to be set from the data explicitly or the arrows fall off the axes.
    """
    vals = [abs(v) for v in list(xs) + list(ys) if np.isfinite(v)]
    lim = max(max(vals) * pad if vals else 0.1, 1e-3)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal", adjustable="box")


def _annotate_allan_min(ax, taus, ad, color):
    """Mark the minimum only if it is interior -- an endpoint is not a minimum."""
    i = int(np.argmin(ad))
    if 0 < i < len(ad) - 1:
        ax.plot(taus[i], ad[i] * mV, "o", ms=11, mfc="none", mec=color, mew=1.8)
        right = np.log(taus[i]) > 0.5 * (np.log(taus[0]) + np.log(taus[-1]))
        ax.annotate(f"min {ad[i]*mV:.3f} mV\nat {taus[i]:.0f} s", (taus[i], ad[i] * mV),
                    xytext=(-10 if right else 10, 12), textcoords="offset points",
                    fontsize=8, color=color, weight="bold",
                    ha="right" if right else "left")
    else:
        ax.text(0.03, 0.97, "no turnover within the blank length \u2014\n"
                "record a longer N$_2$ blank to find the optimum",
                transform=ax.transAxes, fontsize=7.5, color="#777777", style="italic",
                va="top")


def plot_single(fig, label, res):
    fig.clear()
    axs = fig.subplots(2, 2)
    ax_t, ax_v, ax_c, ax_a = axs.ravel()
    segs = res["segments"]
    recs = res["gas_records"]
    levels = sorted({s["conc"] for s in segs})
    cols = _level_colors(levels)
    many = len(segs) > 10

    # (a) time series with segment shading
    for s in segs:
        c = cols[s["conc"]]
        ax_t.plot(s["t"], s["X"] * mV, lw=0.7, color=c, alpha=0.9)
        ax_t.plot(s["t"], s["Y"] * mV, lw=0.7, color=c, alpha=0.45, ls="--")
        ax_t.axvspan(s["use_start"], s["use_end"], color=c, alpha=0.07)
        if not many:
            ax_t.text((s["use_start"] + s["use_end"]) / 2, 1.0,
                      "N$_2$" if s["conc"] == 0 else f"{s['conc']:g}%",
                      transform=ax_t.get_xaxis_transform(), ha="center", va="top",
                      fontsize=8, color=c, weight="bold")
    ax_t.set_xlabel("time (s)")
    ax_t.set_ylabel("X solid, Y dashed (mV)")
    _style(ax_t, "(a)  Lock-in output, used windows shaded")

    # (b) vector plane
    ax_v.axhline(0, color="#CCCCCC", lw=0.8)
    ax_v.axvline(0, color="#CCCCCC", lw=0.8)
    bx = [np.mean(s["X"]) * mV for s in segs if s["conc"] == 0]
    by = [np.mean(s["Y"]) * mV for s in segs if s["conc"] == 0]
    ax_v.plot(bx, by, "o", ms=6, mfc="white", mec=BLANK_COLOR, mew=1.5, ls="none")
    for r in recs:
        c = cols[r["segment"]["conc"]]
        ax_v.annotate("", xy=(r["dx"] * mV, r["dy"] * mV), xytext=(0, 0),
                      arrowprops=dict(arrowstyle="->", color=c, lw=1.8))
        if len(recs) <= 6:
            ax_v.annotate(f"{r['ppm']:.0f} ppm", (r["dx"] * mV, r["dy"] * mV),
                          xytext=(4, 4), textcoords="offset points", fontsize=7.5,
                          color=c, annotation_clip=True)
    _vector_limits(ax_v, [r["dx"] * mV for r in recs] + bx,
                   [r["dy"] * mV for r in recs] + by)
    if len(recs) > 6:
        ppms = [r["ppm"] for r in recs]
        sm = matplotlib.cm.ScalarMappable(
            cmap="viridis", norm=matplotlib.colors.Normalize(min(ppms), max(ppms)))
        fig.colorbar(sm, ax=ax_v, fraction=0.046, pad=0.02).set_label("CO$_2$ (ppm)", fontsize=8)
    ax_v.set_xlabel("X (mV)")
    ax_v.set_ylabel("Y (mV)")
    _style(ax_v, "(b)  Blank-subtracted gas vectors")
    ax_v.text(0.02, 0.02, "open circles = N$_2$ blank means", transform=ax_v.transAxes,
              fontsize=7, color="#666666", style="italic")

    # (c) calibration, or signal bars if only one level
    cal = res["calibration"]
    if cal is not None:
        o = np.argsort(cal["ppm"])
        ax_c.plot(cal["ppm"], cal["projected"] * mV, "o", color=PALETTE[0], ms=7)
        ax_c.plot(cal["ppm"][o], cal["predicted"][o] * mV, "--", color=PALETTE[1], lw=1.4,
                  label=f"{cal['slope']*1e6:.4f} \u00b5V/ppm, R\u00b2 = {cal['r2']:.3f}")
        ax_c.set_xlabel("CO$_2$ (ppm)")
        ax_c.set_ylabel("projected signal (mV)")
        ax_c.legend(fontsize=8, frameon=False)
        _style(ax_c, "(c)  Signed calibration")
    elif recs:
        x = np.arange(len(recs))
        S = [r["S"] * mV for r in recs]
        U = [r["u"] * mV if np.isfinite(r["u"]) else 0 for r in recs]
        M = [r["magnitude_estimate"] * mV for r in recs]
        ax_c.bar(x - 0.18, S, 0.36, yerr=U, capsize=4, color=PALETTE[0], label="vector S")
        ax_c.bar(x + 0.18, M, 0.36, color=PALETTE[1], alpha=0.55, label="magnitude estimate")
        ax_c.set_xticks(x)
        ax_c.set_xticklabels([f"{r['ppm']:.0f} ppm" for r in recs])
        ax_c.set_ylabel("signal (mV)")
        ax_c.legend(fontsize=8, frameon=False)
        _style(ax_c, "(c)  Signal (one level \u2014 no calibration)")
        for i, r in enumerate(recs):
            ax_c.text(i - 0.18, S[i] + U[i] + 0.02, f"lock {r['lock']:.2f}",
                      ha="center", fontsize=7.5, color="#444444")
    else:
        ax_c.text(0.5, 0.5, "blank only \u2014 no gas segments", ha="center",
                  va="center", transform=ax_c.transAxes, color="#777777")
        _style(ax_c, "(c)  Signal")
        ax_c.set_xticks([])
        ax_c.set_yticks([])

    # (d) Allan deviation
    taus, ad = dense_allan(res)
    if len(taus):
        ax_a.loglog(taus, ad * mV, "o-", ms=3.5, lw=1.4, color=PALETTE[0])
        _annotate_allan_min(ax_a, taus, ad, PALETTE[1])
    ax_a.set_xlabel("averaging time \u03c4 (s)")
    ax_a.set_ylabel("Allan deviation (mV)")
    _style(ax_a, "(d)  N$_2$ Allan deviation")
    ax_a.grid(alpha=0.2, lw=0.6, which="both")

    fig.suptitle(label, fontsize=12.5, weight="bold", x=0.02, ha="left")
    fig.text(0.985, 0.995,
             f"noise \u03c3 = {res['blank_sigma_V']*mV:.3f} mV per component   |   "
             f"{len(segs)} segment{'s' if len(segs) != 1 else ''}, "
             f"{len(recs)} gas level{'s' if len(recs) != 1 else ''}",
             fontsize=8.5, color="#555555", ha="right", va="top")


def plot_compare(fig, entries):
    """entries: list of (label, color, res)."""
    fig.clear()
    axs = fig.subplots(2, 3)
    a_n, a_s, a_snr, a_a, a_l, a_v = axs.ravel()

    # (a) noise bars
    labels = [e[0] for e in entries]
    noise = [e[2]["blank_sigma_V"] * mV for e in entries]
    x = np.arange(len(entries))
    a_n.bar(x, noise, color=[e[1] for e in entries], edgecolor="white")
    for i, v in enumerate(noise):
        a_n.text(i, v, f"{v:.3f}", ha="center", va="bottom", fontsize=8, weight="bold")
    a_n.set_xticks(x)
    a_n.set_xticklabels([f"#{i+1}" for i in x])
    a_n.set_ylabel("\u03c3 per component (mV)")
    _style(a_n, "(a)  Noise on the N$_2$ blank")

    for lab, col, res in entries:
        recs = sorted(res["gas_records"], key=lambda r: r["ppm"])
        if not recs:
            continue
        ppm = np.array([r["ppm"] for r in recs])
        S = np.array([r["S"] for r in recs]) * mV
        U = np.array([r["u"] if np.isfinite(r["u"]) else 0 for r in recs]) * mV
        M = np.array([r["magnitude_estimate"] for r in recs]) * mV
        snr = np.array([r["mean_snr"] for r in recs])
        lock = np.array([r["lock"] for r in recs])

        a_s.errorbar(ppm, S, yerr=U, fmt="o-", color=col, ms=6, capsize=3, lw=1.4, label=lab)
        a_s.plot(ppm, M, "o", mfc="none", mec=col, ms=9, mew=1.3)
        a_snr.plot(ppm, snr, "s-", color=col, ms=6, lw=1.4)
        a_l.plot(ppm, lock, "D-", color=col, ms=6, lw=1.4)
        for r in recs:
            a_v.annotate("", xy=(r["dx"] * mV, r["dy"] * mV), xytext=(0, 0),
                         arrowprops=dict(arrowstyle="->", color=col, lw=1.8))

        taus, ad = dense_allan(res)
        if len(taus):
            a_a.loglog(taus, ad * mV, "o-", ms=3, lw=1.3, color=col)

    a_s.set_xlabel("CO$_2$ (ppm)")
    a_s.set_ylabel("signal (mV)")
    _style(a_s, "(b)  Signal vs concentration")
    a_s.text(0.02, 0.03, "filled: vector S \u00b1 u\nopen: magnitude estimate",
             transform=a_s.transAxes, va="bottom", fontsize=7, color="#555555", style="italic")

    a_snr.set_xlabel("CO$_2$ (ppm)")
    a_snr.set_ylabel("SNR of the averaged signal")
    a_snr.axhline(3, color="#999999", ls="--", lw=1)
    a_snr.text(0.98, 3, " 3\u03c3", transform=a_snr.get_yaxis_transform(),
               fontsize=7.5, color="#777777", va="bottom", ha="right")
    _style(a_snr, "(c)  Signal-to-noise")

    a_a.set_xlabel("averaging time \u03c4 (s)")
    a_a.set_ylabel("Allan deviation (mV)")
    _style(a_a, "(d)  Allan deviation")
    a_a.grid(alpha=0.2, lw=0.6, which="both")

    a_l.set_xlabel("CO$_2$ (ppm)")
    a_l.set_ylabel("phase lock (0 random \u2026 1 locked)")
    a_l.set_ylim(0, 1)
    a_l.axhspan(0, 0.25, color="#B4472E", alpha=0.07)
    a_l.text(0.02, 0.03, "below 0.25: reference not locked", transform=a_l.transAxes,
             fontsize=7, color="#B4472E", style="italic")
    _style(a_l, "(e)  Phase lock")

    a_v.axhline(0, color="#CCCCCC", lw=0.8)
    a_v.axvline(0, color="#CCCCCC", lw=0.8)
    a_v.set_xlabel("X (mV)")
    a_v.set_ylabel("Y (mV)")
    _style(a_v, "(f)  Blank-subtracted vectors")
    allx = [r["dx"] * mV for _, _, res in entries for r in res["gas_records"]]
    ally = [r["dy"] * mV for _, _, res in entries for r in res["gas_records"]]
    _vector_limits(a_v, allx, ally)

    handles = [matplotlib.lines.Line2D([], [], color=c, lw=2.5, marker="o")
               for _, c, _ in entries]
    names = [f"#{i+1}  {l}" for i, (l, _, _) in enumerate(entries)]
    kw = dict(fontsize=8, frameon=False, ncol=min(4, len(entries)))
    try:                                  # matplotlib >= 3.7 reserves space for it
        fig.legend(handles, names, loc="outside upper right", **kw)
    except ValueError:
        fig.legend(handles, names, loc="upper right", **kw)
    fig.suptitle(f"Comparison of {len(entries)} files", fontsize=12.5, weight="bold",
                 x=0.02, ha="left")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class Viewer:
    def __init__(self, root, preload=()):
        self.root = root
        root.title("QEPAS viewer")
        root.geometry("1450x880")
        self.files = []          # dicts: path, label, cylinder, color
        self.cache = {}
        self.last_mode = None

        # Pack order matters in Tk: the status bar must claim the bottom edge
        # before the side panels are packed, or it is squeezed between them.
        self.status = tk.StringVar(value="Add one or more CSV files to begin.")
        ttk.Label(root, textvariable=self.status, anchor="w", relief="sunken",
                  padding=(6, 2)).pack(side="bottom", fill="x")
        self._build_left()
        self._build_right()
        for p in preload:
            self._add_path(p)

    # ---- layout -----------------------------------------------------------
    def _build_left(self):
        left = ttk.Frame(self.root, padding=8)
        left.pack(side="left", fill="y")

        f = ttk.LabelFrame(left, text="Files", padding=6)
        f.pack(fill="x")
        cols = ("n", "label", "cyl")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=10,
                                 selectmode="extended")
        for c, w, h in (("n", 32, "#"), ("label", 250, "Label"), ("cyl", 60, "Cyl %")):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "label" else "center")
        self.tree.pack(fill="x")
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(6, 0))
        for txt, cmd in (("Add\u2026", self.add_files), ("Edit", self.edit_selected),
                         ("Remove", self.remove_selected), ("Clear", self.clear_files)):
            ttk.Button(bar, text=txt, command=cmd, width=8).pack(side="left", padx=1)
        ttk.Label(f, text="Double-click a row to rename it or set its cylinder.",
                  foreground="#777777", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(4, 0))

        p = ttk.LabelFrame(left, text="Analysis parameters", padding=6)
        p.pack(fill="x", pady=(10, 0))
        self.vars = {}
        for key, label, default in (
                ("discard", "Discard after mark (s)", "60"),
                ("tail", "Trim before next mark (s)", "5"),
                ("phase_block", "Phase-lock block (s)", "12"),
                ("unc_block", "Uncertainty block (s)", "60"),
                ("sensitivity", "Sensitivity (\u00b5V/ppm)", "")):
            row = ttk.Frame(p)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=24).pack(side="left")
            v = tk.StringVar(value=default)
            ttk.Entry(row, textvariable=v, width=10).pack(side="right")
            self.vars[key] = v
        ttk.Label(p, text="Leave sensitivity blank to use the fitted slope.",
                  foreground="#777777", font=("TkDefaultFont", 8)).pack(anchor="w")

        m = ttk.LabelFrame(left, text="View", padding=6)
        m.pack(fill="x", pady=(10, 0))
        self.mode = tk.StringVar(value="single")
        ttk.Radiobutton(m, text="Single file (selected row)", value="single",
                        variable=self.mode).pack(anchor="w")
        ttk.Radiobutton(m, text="Compare all files", value="compare",
                        variable=self.mode).pack(anchor="w")

        a = ttk.Frame(left)
        a.pack(fill="x", pady=(12, 0))
        ttk.Button(a, text="\u25b6  Analyse and plot", command=self.analyse).pack(fill="x", ipady=4)
        ttk.Button(a, text="Save figure\u2026", command=self.save_figure).pack(fill="x", pady=(6, 0))
        ttk.Button(a, text="Export results CSV\u2026", command=self.export_csv).pack(fill="x", pady=(3, 0))

    def _build_right(self):
        right = ttk.Notebook(self.root)
        right.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)

        pf = ttk.Frame(right)
        right.add(pf, text="  Plot  ")
        # Request a modest size and let the widget expand: a figure larger than the
        # panel gets cropped by Tk instead of shrinking to fit.
        self.fig = Figure(figsize=(8, 5.5), dpi=100, constrained_layout=True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=pf)
        NavigationToolbar2Tk(self.canvas, pf).update()
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.fig.text(0.5, 0.5, "No plot yet", ha="center", va="center",
                      color="#AAAAAA", fontsize=14)
        self.canvas.draw()

        rf = ttk.Frame(right)
        right.add(rf, text="  Report  ")
        self.text = ScrolledText(rf, font=("Consolas", 9), wrap="none")
        self.text.pack(fill="both", expand=True)

        self.nb = right

    # ---- files ------------------------------------------------------------
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.files):
            self.tree.insert("", "end", iid=str(i),
                             values=(i + 1, f["label"], f"{f['cylinder']:g}"))

    def _add_path(self, path):
        if any(f["path"] == path for f in self.files):
            return
        self.files.append({"path": path, "label": short_label(path), "cylinder": 1.0,
                           "color": PALETTE[len(self.files) % len(PALETTE)]})
        self._refresh_tree()

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select SR830 logs", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        for p in paths:
            self._add_path(p)
        if paths:
            self.status.set(f"{len(self.files)} file(s) loaded.")

    def _selected(self):
        return [int(i) for i in self.tree.selection()]

    def remove_selected(self):
        for i in sorted(self._selected(), reverse=True):
            del self.files[i]
        for i, f in enumerate(self.files):
            f["color"] = PALETTE[i % len(PALETTE)]
        self._refresh_tree()

    def clear_files(self):
        self.files.clear()
        self.cache.clear()
        self._refresh_tree()

    def edit_selected(self):
        sel = self._selected()
        if not sel:
            return
        f = self.files[sel[0]]
        win = tk.Toplevel(self.root)
        win.title("Edit file")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=os.path.basename(f["path"]), foreground="#666666").grid(
            row=0, column=0, columnspan=2, padx=10, pady=(10, 6), sticky="w")
        lv, cv = tk.StringVar(value=f["label"]), tk.StringVar(value=f"{f['cylinder']:g}")
        ttk.Label(win, text="Label").grid(row=1, column=0, sticky="w", padx=10)
        ttk.Entry(win, textvariable=lv, width=40).grid(row=1, column=1, padx=10, pady=3)
        ttk.Label(win, text="Cylinder CO\u2082 (%)").grid(row=2, column=0, sticky="w", padx=10)
        ttk.Entry(win, textvariable=cv, width=12).grid(row=2, column=1, sticky="w", padx=10, pady=3)
        ttk.Label(win, text="1 for the 1 % cylinder, 100 for pure CO\u2082",
                  foreground="#777777").grid(row=3, column=1, sticky="w", padx=10)

        def ok():
            try:
                f["cylinder"] = float(cv.get())
            except ValueError:
                messagebox.showerror("Invalid", "Cylinder must be a number.", parent=win)
                return
            f["label"] = lv.get().strip() or short_label(f["path"])
            self._refresh_tree()
            win.destroy()
        ttk.Button(win, text="OK", command=ok).grid(row=4, column=1, sticky="e", padx=10, pady=10)
        win.bind("<Return>", lambda e: ok())

    # ---- analysis ---------------------------------------------------------
    def _params(self):
        p = {}
        for k in ("discard", "tail", "phase_block", "unc_block"):
            p[k] = float(self.vars[k].get())
        s = self.vars["sensitivity"].get().strip()
        p["sensitivity"] = float(s) if s else None
        return p

    def _result(self, f, params):
        key = (f["path"], f["cylinder"], tuple(sorted(params.items())))
        if key not in self.cache:
            self.cache[key] = run_analysis(f["path"], params, f["cylinder"])
        return self.cache[key]

    def analyse(self):
        if not self.files:
            messagebox.showinfo("No files", "Add at least one CSV file first.")
            return
        try:
            params = self._params()
        except ValueError:
            messagebox.showerror("Parameters", "All parameters must be numbers.")
            return

        mode = self.mode.get()
        if mode == "single":
            sel = self._selected()
            targets = [self.files[sel[0] if sel else 0]]
        else:
            targets = list(self.files)

        self.root.config(cursor="watch")
        self.root.update_idletasks()
        entries, report, failed = [], [], []
        try:
            for f in targets:
                try:
                    res, text, warns = self._result(f, params)
                except Exception as exc:
                    failed.append(f"{f['label']}: {exc}")
                    continue
                report.append("#" * 96 + f"\n#  {f['label']}\n" + "#" * 96 + text)
                if warns:
                    report.append("  WARNINGS:\n" + "\n".join("    " + w for w in warns) + "\n")
                if res is None:
                    failed.append(f"{f['label']}: no N\u2082 blank (CO2 = 0 %) in the file")
                    continue
                entries.append((f["label"], f["color"], res))
        finally:
            self.root.config(cursor="")

        if entries:
            if mode == "single":
                plot_single(self.fig, entries[0][0], entries[0][2])
            else:
                plot_compare(self.fig, entries)
            self.canvas.draw()
            self.last_mode = mode
            self.last_entries = entries

        summary = self._summary_table(entries)
        self.text.delete("1.0", "end")
        self.text.insert("end", summary + "\n\n" + "\n".join(report))
        if failed:
            self.text.insert("1.0", "PROBLEMS\n" + "\n".join("  " + x for x in failed) + "\n\n")
            messagebox.showwarning("Some files were skipped", "\n".join(failed))
        self.status.set(f"Analysed {len(entries)} of {len(targets)} file(s) "
                        f"\u2014 {'single' if mode == 'single' else 'comparison'} view.")

    def _summary_table(self, entries):
        lines = ["SUMMARY", "-" * 108,
                 f"{'#':>2}  {'label':<34}{'noise':>9}{'ppm':>9}{'S (mV)':>10}"
                 f"{'u (mV)':>9}{'SNR':>7}{'lock':>7}{'mag (mV)':>10}{'k (uV/ppm)':>12}",
                 "-" * 108]
        for i, (lab, _, res) in enumerate(entries, 1):
            cal = res["calibration"]
            k = f"{cal['slope']*1e6:.4f}" if cal is not None else "-"
            recs = sorted(res["gas_records"], key=lambda r: r["ppm"])
            if not recs:
                lines.append(f"{i:>2}  {lab[:33]:<34}{res['blank_sigma_V']*mV:9.3f}"
                             f"{'blank only':>28}")
            noise = f"{res['blank_sigma_V'] * mV:.3f}"
            for j, r in enumerate(recs):
                first = j == 0
                lines.append(
                    f"{(str(i) if first else ''):>2}  {(lab[:33] if first else ''):<34}"
                    f"{(noise if first else ''):>9}"
                    f"{r['ppm']:9.0f}{r['S']*mV:10.4f}{r['u']*mV:9.4f}"
                    f"{r['mean_snr']:7.2f}{r['lock']:7.2f}{r['magnitude_estimate']*mV:10.4f}"
                    f"{(k if first else ''):>12}")
        return "\n".join(lines)

    # ---- export -----------------------------------------------------------
    def save_figure(self):
        if self.last_mode is None:
            messagebox.showinfo("Nothing to save", "Run an analysis first.")
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
        if p:
            self.fig.savefig(p, dpi=200, facecolor="white", bbox_inches="tight")
            self.status.set(f"Figure saved to {p}")

    def export_csv(self):
        if self.last_mode is None:
            messagebox.showinfo("Nothing to export", "Run an analysis first.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["label", "noise_sigma_mV", "conc_pct", "ppm", "S_mV", "phase_deg",
                        "u_mV", "snr_raw", "snr_mean", "phase_lock", "magnitude_mV",
                        "blank_method", "sensitivity_uV_per_ppm", "calibration_r2"])
            for lab, _, res in self.last_entries:
                cal = res["calibration"]
                k = cal["slope"] * 1e6 if cal is not None else ""
                r2 = cal["r2"] if cal is not None else ""
                for r in sorted(res["gas_records"], key=lambda r: r["ppm"]):
                    w.writerow([lab, res["blank_sigma_V"] * mV, r["segment"]["conc"], r["ppm"],
                                r["S"] * mV, r["phase"], r["u"] * mV, r["raw_snr"],
                                r["mean_snr"], r["lock"], r["magnitude_estimate"] * mV,
                                r["blank_method"], k, r2])
        self.status.set(f"Results exported to {p}")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    Viewer(root, preload=sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
