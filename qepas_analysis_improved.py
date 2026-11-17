"""
qepas_analysis.py -- Robust signal processing for SR830 QEPAS logs.

Main improvements
-----------------
1. Reports background-subtracted gas phase.
2. Uses time-local/interpolated nitrogen blanks to track background drift.
3. Stores MARK mode (flush/hold) separately.
4. Defines phase and uncertainty blocks in seconds, not samples.
5. Reports both instantaneous SNR and SNR of the averaged vector difference.
6. Fits sensitivity using a signed projection on the calibration direction,
   avoiding positive magnitude bias near zero.
7. Checks X/Y/R scaling consistency and permits explicit scale factors.
8. Coerces numeric fields, removes invalid rows, and reports removals.
9. Produces time-series, XY, calibration, and Allan-deviation plots.

Expected columns
----------------
t_s, X_V, Y_V, R_V
Optional: conc_pct, overload

Expected MARK format
--------------------
# MARK t=120.0s flush :: step 1/6 CO2=0%
# MARK t=300.0s hold  :: step 2/6 CO2=20%

Examples
--------
python qepas_analysis.py run.csv
python qepas_analysis.py run.csv --sensitivity 0.109
python qepas_analysis.py run.csv --discard 60 --tail 5
python qepas_analysis.py run.csv --plot-file result.png

From Python / Spyder
--------------------
import qepas_analysis as qa
qa.report("run.csv", sensitivity_uV_per_ppm=0.109)
"""

import argparse
import re
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


_MARK = re.compile(
    r"^#\s*MARK\s+t=([\d.]+)s\s+(flush|hold)\s*::\s*"
    r"step\s+(\d+)/(\d+)\s+CO2=([\d.]+)%",
    re.IGNORECASE,
)

REQUIRED_COLUMNS = ("t_s", "X_V", "Y_V", "R_V")
OPTIONAL_NUMERIC_COLUMNS = ("conc_pct", "overload")


def _finite_mean(v):
    v = np.asarray(v, float)
    return float(np.mean(v[np.isfinite(v)])) if np.isfinite(v).any() else np.nan


def load_segments(path, discard_s=60.0, tail_s=5.0,
                  x_scale=1.0, y_scale=1.0, r_scale=1.0,
                  min_points=20):
    """Load an SR830 log and split it at MARK lines.

    Invalid numeric rows are removed. Each segment records MARK mode, step,
    concentration, start/end times, arrays, and overload count.
    """
    path = Path(path)
    marks = []
    with path.open("r", errors="ignore") as fh:
        for line_no, line in enumerate(fh, start=1):
            m = _MARK.match(line.strip())
            if m:
                marks.append({
                    "t0": float(m.group(1)),
                    "mode": m.group(2).lower(),
                    "step": int(m.group(3)),
                    "nsteps": int(m.group(4)),
                    "conc": float(m.group(5)),
                    "line_no": line_no,
                })

    try:
        df = pd.read_csv(path, comment="#", on_bad_lines="warn")
    except pd.errors.ParserError as exc:
        raise ValueError(f"Could not parse CSV: {exc}") from exc

    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Missing required CSV columns: " + ", ".join(missing) +
            ". Required: " + ", ".join(REQUIRED_COLUMNS)
        )

    numeric = list(REQUIRED_COLUMNS) + [
        c for c in OPTIONAL_NUMERIC_COLUMNS if c in df.columns
    ]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    before = len(df)
    df = df.dropna(subset=list(REQUIRED_COLUMNS)).copy()
    df = df[np.isfinite(df[list(REQUIRED_COLUMNS)]).all(axis=1)].copy()
    removed = before - len(df)
    if removed:
        warnings.warn(f"Removed {removed} rows with missing/non-numeric/non-finite values.")
    if len(df) < 2:
        raise ValueError("Fewer than two valid numerical rows remain.")

    df = df.sort_values("t_s", kind="stable").drop_duplicates("t_s", keep="last")
    t = df["t_s"].to_numpy(float)
    if np.any(np.diff(t) <= 0):
        raise ValueError("Time values must be strictly increasing after cleaning.")

    df["X_V"] *= float(x_scale)
    df["Y_V"] *= float(y_scale)
    df["R_V"] *= float(r_scale)

    # Scaling check: R should be approximately hypot(X,Y) sample by sample.
    xy_r = np.hypot(df["X_V"].to_numpy(), df["Y_V"].to_numpy())
    logged_r = np.abs(df["R_V"].to_numpy())
    valid = (xy_r > 0) & np.isfinite(xy_r) & np.isfinite(logged_r)
    r_mismatch = np.nan
    if valid.sum() >= 20:
        denom = np.maximum(xy_r[valid], np.finfo(float).eps)
        r_mismatch = float(np.median(np.abs(logged_r[valid] - xy_r[valid]) / denom))
        if r_mismatch > 0.05:
            warnings.warn(
                f"Median R versus hypot(X,Y) mismatch is {100*r_mismatch:.1f}%. "
                "Check SR830 units/scaling or use --x-scale/--y-scale/--r-scale."
            )

    if not marks:
        conc = float(df["conc_pct"].iloc[0]) if "conc_pct" in df else 0.0
        marks = [{
            "t0": float(t[0]), "mode": "unmarked", "step": 1,
            "nsteps": 1, "conc": conc, "line_no": None,
        }]
        discard_s = 0.0
        tail_s = 0.0

    marks.sort(key=lambda z: z["t0"])
    segs = []
    skipped = []
    x_all = df["X_V"].to_numpy(float)
    y_all = df["Y_V"].to_numpy(float)
    r_all = df["R_V"].to_numpy(float)
    ov_all = (df["overload"].fillna(0).to_numpy(float)
              if "overload" in df else np.zeros(len(df)))

    for i, mark in enumerate(marks):
        t0 = mark["t0"]
        t1 = marks[i + 1]["t0"] if i + 1 < len(marks) else float(t[-1])
        use_start = t0 + float(discard_s)
        use_end = t1 - float(tail_s)
        sel = (t >= use_start) & (t < use_end)
        n = int(sel.sum())
        if n < min_points:
            skipped.append((mark["step"], mark["mode"], mark["conc"], n))
            continue
        segs.append({
            **mark,
            "t1": t1,
            "use_start": use_start,
            "use_end": use_end,
            "center_t": float(np.mean(t[sel])),
            "t": t[sel],
            "X": x_all[sel],
            "Y": y_all[sel],
            "R": r_all[sel],
            "overloads": int(np.nansum(ov_all[sel] > 0)),
            "removed_rows": removed,
            "r_mismatch": r_mismatch,
        })

    if skipped:
        text = "; ".join(
            f"step {st} {mode} CO2={c:g}% ({n} points)"
            for st, mode, c, n in skipped
        )
        warnings.warn("Skipped short segments: " + text)
    if not segs:
        raise ValueError(
            "No valid segment remains. Reduce --discard/--tail, collect longer holds, "
            "or check MARK times."
        )
    return segs


# ---------------------------------------------------------------------------
# Signal and uncertainty quantities
# ---------------------------------------------------------------------------
def signal_vector(X, Y):
    """Magnitude and phase of the vector mean."""
    mx, my = np.mean(X), np.mean(Y)
    return float(np.hypot(mx, my)), float(np.degrees(np.arctan2(my, mx)))


def noise_sigma(X, Y):
    """Per-component noise: sqrt((var(X)+var(Y))/2)."""
    if len(X) < 2:
        return np.nan
    return float(np.sqrt(0.5 * (np.var(X, ddof=1) + np.var(Y, ddof=1))))


def rayleigh_mean(sigma):
    """Expected mean R for pure equal-variance Gaussian X/Y noise."""
    return float(sigma * np.sqrt(np.pi / 2))


def magnitude_estimate(R, sigma):
    """Noise-corrected RMS magnitude for a phase-rotating signal."""
    return float(np.sqrt(max(np.mean(np.asarray(R, float) ** 2) - 2 * sigma ** 2, 0.0)))


def _samples_for_seconds(t, seconds, minimum=1):
    if len(t) < 2:
        return minimum
    dt = float(np.median(np.diff(t)))
    if not np.isfinite(dt) or dt <= 0:
        raise ValueError("Cannot determine a positive sampling interval.")
    return max(minimum, int(round(float(seconds) / dt)))


def phase_lock(X, Y, t=None, block_s=12.0, block_samples=None):
    """Circular resultant length of block-mean phases: 1 locked, 0 random."""
    block = (int(block_samples) if block_samples is not None
             else _samples_for_seconds(t, block_s, minimum=1))
    n = len(X) // block
    if n < 3:
        return float("nan")
    phases = np.array([
        np.arctan2(np.mean(Y[i*block:(i+1)*block]),
                   np.mean(X[i*block:(i+1)*block]))
        for i in range(n)
    ])
    return float(np.hypot(np.cos(phases).mean(), np.sin(phases).mean()))


def vector_mean_uncertainty(X, Y, t, block_s=60.0):
    """Standard uncertainty of the X/Y vector mean from block scatter.

    Returns (combined per-component uncertainty, ux, uy, number_of_blocks).
    Block duration is specified in seconds.
    """
    block = _samples_for_seconds(t, block_s, minimum=1)
    n = len(X) // block
    if n < 3:
        return np.nan, np.nan, np.nan, n
    bx = np.array([np.mean(X[i*block:(i+1)*block]) for i in range(n)])
    by = np.array([np.mean(Y[i*block:(i+1)*block]) for i in range(n)])
    ux = float(bx.std(ddof=1) / np.sqrt(n))
    uy = float(by.std(ddof=1) / np.sqrt(n))
    u = float(np.sqrt(0.5 * (ux**2 + uy**2)))
    return u, ux, uy, n


def allan_deviation(v, dt, taus):
    """Overlapping Allan deviation of series v sampled every dt seconds."""
    v = np.asarray(v, float)
    c = np.cumsum(np.insert(v, 0, 0.0))
    out = []
    for tau in np.asarray(taus, float):
        m = max(1, int(round(tau / dt)))
        if 2 * m >= len(v):
            out.append(np.nan)
            continue
        avg = (c[m:] - c[:-m]) / m
        delta = avg[m:] - avg[:-m]
        out.append(np.sqrt(0.5 * np.mean(delta**2)))
    return np.asarray(out)


def allan_xy(X, Y, dt, taus):
    ax = allan_deviation(X, dt, taus)
    ay = allan_deviation(Y, dt, taus)
    return np.sqrt(0.5 * (ax**2 + ay**2))


def segment_summary(seg, uncertainty_block_s=60.0):
    mx = float(np.mean(seg["X"]))
    my = float(np.mean(seg["Y"]))
    u, ux, uy, nblocks = vector_mean_uncertainty(
        seg["X"], seg["Y"], seg["t"], uncertainty_block_s
    )
    return {
        "center_t": seg["center_t"], "mx": mx, "my": my,
        "u": u, "ux": ux, "uy": uy, "nblocks": nblocks,
    }


def local_blank_at(time_s, blank_summaries):
    """Interpolate blank vector and uncertainty at a gas segment time.

    Between two blanks, X/Y are linearly interpolated. Outside the blank time
    range, the nearest blank is used. Independent blank uncertainties are
    propagated with interpolation weights.
    """
    b = sorted(blank_summaries, key=lambda z: z["center_t"])
    times = np.array([z["center_t"] for z in b])
    if len(b) == 1 or time_s <= times[0]:
        z = b[0]
        return {**z, "method": "nearest", "weights": (1.0, 0.0)}
    if time_s >= times[-1]:
        z = b[-1]
        return {**z, "method": "nearest", "weights": (1.0, 0.0)}

    j = int(np.searchsorted(times, time_s))
    left, right = b[j - 1], b[j]
    w_right = (time_s - left["center_t"]) / (right["center_t"] - left["center_t"])
    w_left = 1.0 - w_right

    def interp(key):
        return w_left * left[key] + w_right * right[key]

    def propagated(key):
        vals = np.array([left[key], right[key]], float)
        if not np.isfinite(vals).all():
            return np.nan
        return float(np.sqrt((w_left * vals[0])**2 + (w_right * vals[1])**2))

    ux = propagated("ux")
    uy = propagated("uy")
    u = float(np.sqrt(0.5 * (ux**2 + uy**2))) if np.isfinite([ux, uy]).all() else np.nan
    return {
        "center_t": time_s,
        "mx": interp("mx"), "my": interp("my"),
        "ux": ux, "uy": uy, "u": u,
        "nblocks": min(left["nblocks"], right["nblocks"]),
        "method": "interpolated", "weights": (w_left, w_right),
    }


def calibration_projection(records):
    """Find signed calibration direction from joint X/Y slopes versus ppm."""
    ppm = np.asarray([r["ppm"] for r in records], float)
    dx = np.asarray([r["dx"] for r in records], float)
    dy = np.asarray([r["dy"] for r in records], float)
    if len(np.unique(ppm)) < 2:
        return None
    kx, bx = np.polyfit(ppm, dx, 1)
    ky, by = np.polyfit(ppm, dy, 1)
    norm = float(np.hypot(kx, ky))
    if norm <= np.finfo(float).eps:
        return None
    ux, uy = kx / norm, ky / norm
    projected = dx * ux + dy * uy
    slope, intercept = np.polyfit(ppm, projected, 1)
    predicted = slope * ppm + intercept
    ss_res = float(np.sum((projected - predicted)**2))
    ss_tot = float(np.sum((projected - projected.mean())**2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return {
        "axis_x": ux, "axis_y": uy,
        "axis_phase_deg": float(np.degrees(np.arctan2(uy, ux))),
        "projected": projected, "slope": float(slope),
        "intercept": float(intercept), "r2": r2,
        "ppm": ppm, "predicted": predicted,
    }


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def make_plots(path, segs, records, taus, allan_values, calibration,
               plot_file=None, show=False):
    path = Path(path)
    if plot_file is None:
        plot_file = path.with_name(path.stem + "_qepas_analysis.png")
    else:
        plot_file = Path(plot_file)

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    ax_t, ax_xy, ax_cal, ax_allan = axes.ravel()

    colors = plt.cm.viridis(np.linspace(0.1, 0.9, max(len(segs), 2)))
    for color, s in zip(colors, segs):
        label = f"{s['mode']} {s['conc']:g}%"
        ax_t.plot(s["t"], 1e3*s["X"], color=color, lw=1, alpha=0.85, label=label+" X")
        ax_t.plot(s["t"], 1e3*s["Y"], color=color, lw=1, ls="--", alpha=0.85, label=label+" Y")
    ax_t.set_title("Cleaned SR830 time series")
    ax_t.set_xlabel("Time (s)")
    ax_t.set_ylabel("Lock-in output (mV)")
    if len(segs) <= 8:
        ax_t.legend(fontsize=7, ncol=2)
    ax_t.grid(True, alpha=0.25)

    blanks = [s for s in segs if s["conc"] == 0]
    gases = [s for s in segs if s["conc"] > 0]
    ax_xy.scatter([1e3*np.mean(s["X"]) for s in blanks],
                  [1e3*np.mean(s["Y"]) for s in blanks],
                  marker="o", label="N2 blank means")
    if records:
        ax_xy.scatter([1e3*r["dx"] for r in records],
                      [1e3*r["dy"] for r in records],
                      marker="s", label="Blank-subtracted gas")
        for r in records:
            ax_xy.annotate(f"{r['ppm']:.0f} ppm", (1e3*r["dx"], 1e3*r["dy"]),
                           fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax_xy.axhline(0, color="0.6", lw=0.8)
    ax_xy.axvline(0, color="0.6", lw=0.8)
    ax_xy.set_title("Vector plane")
    ax_xy.set_xlabel("X (mV)")
    ax_xy.set_ylabel("Y (mV)")
    ax_xy.axis("equal")
    ax_xy.legend(fontsize=8)
    ax_xy.grid(True, alpha=0.25)

    if calibration is not None:
        order = np.argsort(calibration["ppm"])
        ax_cal.scatter(calibration["ppm"], 1e6*calibration["projected"], label="Signed projected signal")
        ax_cal.plot(calibration["ppm"][order], 1e6*calibration["predicted"][order], label="Linear fit")
        ax_cal.set_title(f"Calibration, R2 = {calibration['r2']:.4f}")
        ax_cal.set_xlabel("CO2 (ppm)")
        ax_cal.set_ylabel("Projected signal (uV)")
        ax_cal.legend(fontsize=8)
    else:
        ax_cal.text(0.5, 0.5, "Need at least two distinct gas levels", ha="center", va="center")
        ax_cal.set_title("Calibration")
    ax_cal.grid(True, alpha=0.25)

    valid = np.isfinite(allan_values)
    if valid.any():
        ax_allan.loglog(np.asarray(taus)[valid], 1e3*np.asarray(allan_values)[valid], "o-")
    ax_allan.set_title("N2 Allan deviation")
    ax_allan.set_xlabel("Averaging time, tau (s)")
    ax_allan.set_ylabel("Allan deviation per component (mV)")
    ax_allan.grid(True, which="both", alpha=0.25)

    fig.suptitle(f"QEPAS analysis: {path.name}", fontsize=14)
    fig.savefig(plot_file, dpi=180)
    if show:
        plt.show()
    else:
        plt.close(fig)
    return str(plot_file)


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------
def report(path, sensitivity_uV_per_ppm=None, discard_s=60.0,
           cylinder_pct=1.0, tail_s=5.0, phase_block_s=12.0,
           uncertainty_block_s=60.0, x_scale=1.0, y_scale=1.0,
           r_scale=1.0, make_plot=True, plot_file=None, show_plot=False):
    """Print a complete QEPAS analysis and optionally save a four-panel plot."""
    segs = load_segments(
        path, discard_s=discard_s, tail_s=tail_s,
        x_scale=x_scale, y_scale=y_scale, r_scale=r_scale,
    )
    blanks = [s for s in segs if s["conc"] == 0]
    gases = [s for s in segs if s["conc"] > 0]
    mV = 1e3

    print(f"\n{path}\n" + "=" * 96)
    print("SEGMENTS")
    print("  step  mode       CO2     points     used time (s)        overloads")
    for s in segs:
        print(f"  {s['step']:4d}  {s['mode']:<9} {s['conc']:6.2f}%  {len(s['t']):7d}  "
              f"{s['use_start']:9.1f} to {s['use_end']:9.1f}  {s['overloads']:9d}")

    if not blanks:
        print("\nNo N2 blank found. Noise, local background, SNR, Allan deviation, and LOD require CO2=0%.")
        return None

    X0 = np.concatenate([b["X"] for b in blanks])
    Y0 = np.concatenate([b["Y"] for b in blanks])
    R0 = np.concatenate([b["R"] for b in blanks])
    sig = noise_sigma(X0, Y0)
    bvec, bph = signal_vector(X0, Y0)
    dt = float(np.median(np.concatenate([np.diff(b["t"]) for b in blanks if len(b["t"]) > 1])))

    print("\nNOISE AND BACKGROUND")
    print(f"  pooled N2 sigma/component       {sig*mV:10.5f} mV")
    print(f"  pure-noise expected <R>         {rayleigh_mean(sig)*mV:10.5f} mV")
    print(f"  measured pooled N2 <R>          {np.mean(R0)*mV:10.5f} mV")
    print(f"  pooled residual vector          {bvec*mV:10.5f} mV at {bph:+8.2f} deg")
    print(f"  pooled blank phase lock         {phase_lock(X0, Y0, np.arange(len(X0))*dt, phase_block_s):10.3f}")

    blank_summaries = []
    print("\n  Individual N2 blanks, used for time-local background correction:")
    print("  step   center(s)       mean X       mean Y       phase    mean uncertainty")
    for b in blanks:
        z = segment_summary(b, uncertainty_block_s)
        blank_summaries.append(z)
        _, ph = signal_vector(b["X"], b["Y"])
        print(f"  {b['step']:4d}  {z['center_t']:9.1f}  {z['mx']*mV:10.5f}  {z['my']*mV:10.5f}  "
              f"{ph:+8.2f} deg  {z['u']*mV:10.5f} mV")

    taus = np.array([1, 3, 10, 30, 60, 100], float)
    # Allan deviation is calculated per continuous blank and pooled by RMS.
    per_blank_ad = []
    for b in blanks:
        bdt = float(np.median(np.diff(b["t"])))
        per_blank_ad.append(allan_xy(b["X"], b["Y"], bdt, taus))
    ad = np.sqrt(np.nanmean(np.asarray(per_blank_ad)**2, axis=0))
    print("\n  Allan deviation, RMS across continuous N2 blanks:")
    for T, a in zip(taus, ad):
        if np.isfinite(a):
            print(f"    tau = {T:6.1f} s       {a*mV:10.5f} mV")

    records = []
    if gases:
        print("\nGAS SIGNALS")
        print("  CO2%      ppm   vector S     phase     u(mean)   SNRraw  SNRmean  lock   blank method  ovl")

    for g in gases:
        local = local_blank_at(g["center_t"], blank_summaries)
        gx_mean = float(np.mean(g["X"]))
        gy_mean = float(np.mean(g["Y"]))
        dx = gx_mean - local["mx"]
        dy = gy_mean - local["my"]
        Ssub = float(np.hypot(dx, dy))
        phase_sub = float(np.degrees(np.arctan2(dy, dx)))

        gu, gux, guy, nblocks = vector_mean_uncertainty(
            g["X"], g["Y"], g["t"], uncertainty_block_s
        )
        if np.isfinite([gux, guy, local["ux"], local["uy"]]).all():
            dux = float(np.hypot(gux, local["ux"]))
            duy = float(np.hypot(guy, local["uy"]))
            u_difference = float(np.sqrt(0.5 * (dux**2 + duy**2)))
            snr_mean = Ssub / u_difference if u_difference > 0 else np.nan
        else:
            dux = duy = u_difference = snr_mean = np.nan

        L = phase_lock(g["X"], g["Y"], g["t"], phase_block_s)
        ppm = g["conc"] / 100.0 * cylinder_pct * 1e4
        raw_snr = Ssub / sig if sig > 0 else np.nan
        mag = magnitude_estimate(g["R"], sig)
        records.append({
            "segment": g, "ppm": ppm, "dx": dx, "dy": dy,
            "S": Ssub, "phase": phase_sub, "u": u_difference,
            "raw_snr": raw_snr, "mean_snr": snr_mean, "lock": L,
            "magnitude_estimate": mag, "blank_method": local["method"],
        })
        print(f"  {g['conc']:5.1f}  {ppm:8.0f}  {Ssub*mV:9.5f}  {phase_sub:+8.2f}  "
              f"{u_difference*mV:9.5f}  {raw_snr:7.2f}  {snr_mean:7.2f}  "
              f"{L:5.2f}   {local['method']:<12} {g['overloads']:4d}")
        if np.isfinite(L) and L < 0.25:
            print("        WARNING: low phase lock; check reference, harmonic, frequency, and signal level.")

    calibration = calibration_projection(records) if len(records) >= 2 else None
    if calibration is not None:
        for rec, proj in zip(records, calibration["projected"]):
            rec["projected_signal"] = float(proj)
        print("\nSIGNED CALIBRATION")
        print(f"  projection-axis phase           {calibration['axis_phase_deg']:+10.3f} deg")
        print(f"  sensitivity                     {calibration['slope']*1e6:10.6f} uV/ppm")
        print(f"  intercept                       {calibration['intercept']*1e3:10.6f} mV")
        print(f"  R-squared                       {calibration['r2']:10.5f}")
        print("  This fit uses signed X/Y projection, not positive-only vector magnitude.")

    k = None
    if sensitivity_uV_per_ppm is not None:
        if sensitivity_uV_per_ppm <= 0:
            raise ValueError("Sensitivity must be positive in uV/ppm.")
        k = sensitivity_uV_per_ppm * 1e-6
        source = f"provided sensitivity {sensitivity_uV_per_ppm:g} uV/ppm"
    elif calibration is not None and calibration["slope"] > 0:
        k = calibration["slope"]
        source = "signed calibration slope"

    if k is not None:
        print(f"\nDETECTION LIMIT ({source})")
        for T, a in zip(taus, ad):
            if np.isfinite(a):
                print(f"  tau = {T:6.1f} s   MDL (1 sigma) = {a/k:10.1f} ppm   "
                      f"LOD (3 sigma) = {3*a/k:10.1f} ppm")

    saved_plot = None
    if make_plot:
        saved_plot = make_plots(
            path, segs, records, taus, ad, calibration,
            plot_file=plot_file, show=show_plot,
        )
        print(f"\nPlot saved to: {saved_plot}")

    return {
        "segments": segs,
        "blank_sigma_V": sig,
        "allan_taus_s": taus,
        "allan_V": ad,
        "gas_records": records,
        "calibration": calibration,
        "plot_file": saved_plot,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Robust QEPAS SR830 log analysis")
    p.add_argument("csv", help="SR830 CSV log")
    p.add_argument("--sensitivity", type=float, help="known sensitivity in uV/ppm")
    p.add_argument("--discard", type=float, default=60.0,
                   help="seconds discarded after each MARK (default: 60)")
    p.add_argument("--tail", type=float, default=5.0,
                   help="seconds removed before the next MARK (default: 5)")
    p.add_argument("--cylinder", type=float, default=1.0,
                   help="CO2 cylinder concentration in percent (default: 1.0)")
    p.add_argument("--phase-block", type=float, default=12.0,
                   help="phase-lock block duration in seconds (default: 12)")
    p.add_argument("--uncertainty-block", type=float, default=60.0,
                   help="uncertainty block duration in seconds (default: 60)")
    p.add_argument("--x-scale", type=float, default=1.0)
    p.add_argument("--y-scale", type=float, default=1.0)
    p.add_argument("--r-scale", type=float, default=1.0)
    p.add_argument("--plot-file", help="output PNG path")
    p.add_argument("--show-plot", action="store_true", help="show plot interactively")
    p.add_argument("--no-plots", action="store_true", help="do not create a plot")
    a = p.parse_args(argv)

    report(
        a.csv,
        sensitivity_uV_per_ppm=a.sensitivity,
        discard_s=a.discard,
        cylinder_pct=a.cylinder,
        tail_s=a.tail,
        phase_block_s=a.phase_block,
        uncertainty_block_s=a.uncertainty_block,
        x_scale=a.x_scale,
        y_scale=a.y_scale,
        r_scale=a.r_scale,
        make_plot=not a.no_plots,
        plot_file=a.plot_file,
        show_plot=a.show_plot,
    )


if __name__ == "__main__":
    main()
