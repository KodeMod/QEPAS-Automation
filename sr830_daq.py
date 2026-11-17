"""
sr830_daq.py -- Measurement routines for the SR830.

Every routine returns a list of dicts and writes a CSV with a commented
header recording the instrument state at acquisition time, so a data file is
self-describing months later.

Command line:
    python sr830_daq.py status
    python sr830_daq.py log     --duration 300 --interval 1.0 -o run.csv
    python sr830_daq.py capture --duration 30 --rate 64 -o noise.csv
    python sr830_daq.py fsweep  --start 100 --stop 10000 --points 100 -o fsweep.csv
    python sr830_daq.py asweep  --start 0.004 --stop 1.0 --points 40 -o asweep.csv

    add  --resource COM4 --prologix  for a Prologix adapter
"""

import argparse
import csv
import datetime as _dt
import sys
import time

import numpy as np

from sr830 import SR830

DEFAULT_PARAMS = ("X", "Y", "R", "theta")


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(path, rows, meta=None):
    """Write rows (list of dicts) with instrument state as commented header."""
    if not rows:
        raise ValueError("no rows to write")
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        f.write(f"# acquired {_dt.datetime.now().isoformat(timespec='seconds')}\n")
        for k, v in (meta or {}).items():
            f.write(f"# {k} = {v}\n")
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------

def log(li, duration_s, interval_s=1.0, params=DEFAULT_PARAMS, path=None,
        verbose=True):
    """Polled logging at a software-timed interval.

    Simple and unlimited in length, but the timing jitters by however long the
    GPIB round trip takes. For anything above roughly 2 Hz use capture().
    """
    rows = []
    t0 = time.time()
    while time.time() - t0 < duration_s:
        tick = time.time()
        d = li.snap(*params)
        rows.append({"t_s": round(tick - t0, 4), **d})
        if verbose:
            print(f"\r{tick - t0:8.1f} s  " +
                  "  ".join(f"{k}={d[k]:+.4e}" for k in params), end="")
        time.sleep(max(0.0, interval_s - (time.time() - tick)))
    if verbose:
        print()
    if path:
        write_csv(path, rows, li.status())
    return rows


def capture(li, duration_s, rate_hz=64.0, ch1="X", ch2="Y", path=None):
    """Hardware-timed acquisition via the SR830's internal buffer.

    Evenly spaced and gap-free, which is what you need for anything
    frequency-domain: noise spectra, drift analysis, transients.
    """
    t, a, b = li.capture(duration_s, rate_hz, ch1=ch1, ch2=ch2)
    rows = [{"t_s": float(ti), ch1: float(ai), ch2: float(bi)}
            for ti, ai, bi in zip(t, a, b)]
    if path:
        write_csv(path, rows, {**li.status(), "sample_rate_Hz": len(t) / max(t[-1], 1e-9)})
    return rows


def frequency_sweep(li, start_hz, stop_hz, points=100, log_spacing=False,
                    settle_tc=None, autorange=False, params=DEFAULT_PARAMS,
                    path=None, verbose=True):
    """Sweep the internal oscillator and record the response at each step.

    This is the standard transfer-function / resonance measurement: the SR830
    drives the system from SINE OUT and measures what comes back at the same
    frequency.
    """
    freqs = (np.logspace(np.log10(start_hz), np.log10(stop_hz), points)
             if log_spacing else np.linspace(start_hz, stop_hz, points))
    rows = []
    for i, f in enumerate(freqs):
        li.frequency = float(f)
        if autorange:
            li.autorange()
        else:
            li.settle(settle_tc)
        d = li.snap(*params)
        ov = li.overload()
        rows.append({"freq_Hz": float(f), **d,
                     "sensitivity_V": li.sensitivity,
                     "overload": int(any(ov.values()))})
        if verbose:
            print(f"\r[{i + 1}/{points}] {f:10.3f} Hz  "
                  f"R={d.get('R', float('nan')):.4e}"
                  f"{'  OVERLOAD' if any(ov.values()) else ''}    ", end="")
    if verbose:
        print()
    if path:
        write_csv(path, rows, li.status())
    return rows


def amplitude_sweep(li, start_v, stop_v, points=40, settle_tc=None,
                    autorange=True, params=DEFAULT_PARAMS, path=None,
                    verbose=True):
    """Sweep SINE OUT amplitude. Use this to check linearity of a response."""
    amps = np.linspace(start_v, stop_v, points)
    rows = []
    for i, a in enumerate(amps):
        li.amplitude = float(a)
        if autorange:
            li.autorange()
        else:
            li.settle(settle_tc)
        d = li.snap(*params)
        rows.append({"amplitude_Vrms": float(a), **d,
                     "sensitivity_V": li.sensitivity})
        if verbose:
            print(f"\r[{i + 1}/{points}] {a:.4f} Vrms  "
                  f"R={d.get('R', float('nan')):.4e}    ", end="")
    if verbose:
        print()
    if path:
        write_csv(path, rows, li.status())
    return rows


def aux_sweep(li, channel, start_v, stop_v, points=50, settle_tc=None,
              params=DEFAULT_PARAMS, path=None, verbose=True):
    """Sweep an Aux Output (a DC bias into your DUT) and record the response."""
    volts = np.linspace(start_v, stop_v, points)
    rows = []
    for i, v in enumerate(volts):
        li.aux_out(channel, float(v))
        li.settle(settle_tc)
        d = li.snap(*params)
        rows.append({f"aux{channel}_V": float(v), **d})
        if verbose:
            print(f"\r[{i + 1}/{points}] {v:+.3f} V  "
                  f"R={d.get('R', float('nan')):.4e}    ", end="")
    if verbose:
        print()
    if path:
        write_csv(path, rows, li.status())
    return rows


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def summarize(rows, key="R"):
    """Mean, std, and mean/std of one column."""
    v = np.array([r[key] for r in rows], float)
    mean, std = v.mean(), v.std(ddof=1) if len(v) > 1 else 0.0
    return {"n": len(v), "mean": mean, "std": std,
            "ratio": mean / std if std else float("inf")}


def noise_density(rows, rate_hz, key="X"):
    """Amplitude spectral density in V/sqrt(Hz) from a capture().

    Only meaningful on evenly sampled data, so feed it capture() output rather
    than log() output. Also remember the SR830's own output filter rolls off
    the spectrum above roughly 1/(2*pi*TC).
    """
    v = np.array([r[key] for r in rows], float)
    v = v - v.mean()
    win = np.hanning(len(v))
    fft = np.fft.rfft(v * win)
    freqs = np.fft.rfftfreq(len(v), 1.0 / rate_hz)
    enbw = rate_hz * (win ** 2).sum() / win.sum() ** 2
    asd = np.abs(fft) * np.sqrt(2.0) / win.sum() / np.sqrt(enbw)
    return freqs, asd


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _connect(args):
    return SR830(args.resource, prologix=args.prologix, addr=args.addr)


def main(argv=None):
    p = argparse.ArgumentParser(description="SR830 data acquisition")
    p.add_argument("--resource", default="GPIB0::8::INSTR")
    p.add_argument("--prologix", action="store_true")
    p.add_argument("--addr", type=int, default=8)
    p.add_argument("-o", "--output")
    p.add_argument("--tc", type=float, help="time constant, s")
    p.add_argument("--slope", type=int, choices=[6, 12, 18, 24])
    p.add_argument("--harmonic", type=int)
    p.add_argument("--sens", type=float, help="sensitivity full scale, V")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    q = sub.add_parser("log")
    q.add_argument("--duration", type=float, required=True)
    q.add_argument("--interval", type=float, default=1.0)

    q = sub.add_parser("capture")
    q.add_argument("--duration", type=float, required=True)
    q.add_argument("--rate", type=float, default=64.0)

    q = sub.add_parser("fsweep")
    q.add_argument("--start", type=float, required=True)
    q.add_argument("--stop", type=float, required=True)
    q.add_argument("--points", type=int, default=100)
    q.add_argument("--log", action="store_true", help="logarithmic spacing")
    q.add_argument("--amplitude", type=float)
    q.add_argument("--autorange", action="store_true")

    q = sub.add_parser("asweep")
    q.add_argument("--start", type=float, required=True)
    q.add_argument("--stop", type=float, required=True)
    q.add_argument("--points", type=int, default=40)
    q.add_argument("--freq", type=float)

    args = p.parse_args(argv)

    with _connect(args) as li:
        print(li.idn)

        if args.tc is not None:
            li.time_constant = args.tc
        if args.slope is not None:
            li.slope = args.slope
        if args.harmonic is not None:
            li.harmonic = args.harmonic
        if args.sens is not None:
            li.sensitivity = args.sens

        if args.cmd == "status":
            for k, v in li.status().items():
                print(f"  {k:>20} : {v}")
            print(f"  {'overload':>20} : {li.overload()}")
            print(f"  {'reading':>20} : {li.snap()}")
            return

        if args.cmd == "log":
            rows = log(li, args.duration, args.interval, path=args.output)
            print(summarize(rows))

        elif args.cmd == "capture":
            rows = capture(li, args.duration, args.rate, path=args.output)
            print(f"{len(rows)} points")
            print(summarize(rows, "X"))

        elif args.cmd == "fsweep":
            if args.amplitude:
                li.amplitude = args.amplitude
            rows = frequency_sweep(li, args.start, args.stop, args.points,
                                   log_spacing=args.log,
                                   autorange=args.autorange, path=args.output)
            peak = max(rows, key=lambda r: r["R"])
            print(f"peak R = {peak['R']:.4e} V at {peak['freq_Hz']:.3f} Hz")

        elif args.cmd == "asweep":
            if args.freq:
                li.frequency = args.freq
            rows = amplitude_sweep(li, args.start, args.stop, args.points,
                                   path=args.output)
            print(f"{len(rows)} points")

        if args.output:
            print(f"wrote {args.output}")


if __name__ == "__main__":
    sys.exit(main())
