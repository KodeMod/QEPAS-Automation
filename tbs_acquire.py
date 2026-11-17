#!/usr/bin/env python3
"""
tbs_acquire.py -- waveform acquisition for the bench Tektronix TBS 1052B-EDU

Confirmed instrument:
    TEKTRONIX,TBS 1052B-EDU,C015932,CF:91.1CT sFV:v3.25
    USB0::0x0699::0x0368::C015932::INSTR

Replaces the broken 'Oscilloscopio.vi': grabs CH1, scales to real volts
and seconds, reports RMS / DC / peak-to-peak, does an FFT, and writes CSV.
Also works with any TDS1000/2000-series scope -- same SCPI dialect.

Usage
-----
    python tbs_acquire.py --list          # show VISA resources
    python tbs_acquire.py                 # single capture from the bench scope

    # check the 6.775 kHz drive sine going into ITC MOD IN
    python tbs_acquire.py --timebase 200e-6 --vscale 0.02 \
        --coupling AC --target 6775

    # watch it live while you tune the SR830 sine amplitude
    python tbs_acquire.py --continuous --interval 2 --no-save \
        --timebase 200e-6 --coupling AC --target 6775

Requires: pip install pyvisa numpy
Plus NI-VISA (or pyvisa-py) installed on the machine.
Scope USB port must be in USBTMC mode:
    Utility > Options > Rear USB Port > Computer
"""

import argparse
import csv
import sys
import time
from datetime import datetime

import numpy as np
import pyvisa

# --- this bench's scope -------------------------------------------------
# TEKTRONIX,TBS 1052B-EDU,C015932,CF:91.1CT sFV:v3.25
DEFAULT_RESOURCE = "USB0::0x0699::0x0368::C015932::INSTR"
EXPECTED_SERIAL = "C015932"
EXPECTED_MODEL = "TBS 1052B-EDU"
# ------------------------------------------------------------------------

# Tektronix USB vendor ID -- used to autodetect if the address above moves
TEK_VENDOR_ID = "0x0699"
RECORD_LENGTH = 2500  # fixed on TBS1000B / TDS1000-2000 series


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def list_resources(rm):
    resources = rm.list_resources()
    if not resources:
        print("No VISA resources found.")
        print("Check: scope powered on, USB cable seated, port set to USBTMC.")
        return
    print("VISA resources found:")
    for r in resources:
        try:
            inst = rm.open_resource(r)
            inst.timeout = 2000
            idn = inst.query("*IDN?").strip()
            inst.close()
        except Exception:
            idn = "(no response to *IDN?)"
        print(f"  {r}\n      {idn}")


def resolve_address(rm, explicit=None):
    """Pick a VISA address: explicit -> known bench scope -> autodetect."""
    if explicit:
        return explicit

    available = rm.list_resources()
    if DEFAULT_RESOURCE in available:
        return DEFAULT_RESOURCE

    # the USB address changes if the scope is plugged into a different port
    # on some systems, so fall back to searching by vendor ID / serial
    if DEFAULT_RESOURCE not in available:
        for r in available:
            if EXPECTED_SERIAL in r:
                print(f"Note: scope found at {r} (not the usual address).")
                return r

    return autodetect(rm)


def autodetect(rm):
    """Return the VISA address of the first Tektronix USB instrument."""
    for r in rm.list_resources():
        if TEK_VENDOR_ID.lower() in r.lower() and r.upper().startswith("USB"):
            return r
    # fall back: anything that answers *IDN? with TEKTRONIX
    for r in rm.list_resources():
        try:
            inst = rm.open_resource(r)
            inst.timeout = 2000
            idn = inst.query("*IDN?")
            inst.close()
            if "TEKTRONIX" in idn.upper():
                return r
        except Exception:
            continue
    return None


def try_write(scope, cmd):
    """Send a command, warn but don't die if the scope rejects it."""
    try:
        scope.write(cmd)
        return True
    except Exception as exc:
        print(f"  ! command rejected: {cmd}  ({exc})", file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def configure(scope, args):
    """Apply front-panel settings. Every field is optional."""
    ch = args.channel

    if args.vscale is not None:
        try_write(scope, f"{ch}:SCAle {args.vscale}")
    if args.voffset is not None:
        try_write(scope, f"{ch}:POSition {args.voffset}")
    if args.coupling is not None:
        try_write(scope, f"{ch}:COUPling {args.coupling}")
    if args.probe is not None:
        try_write(scope, f"{ch}:PRObe {args.probe}")

    if args.timebase is not None:
        # TBS1000B accepts HORizontal:SCAle; older TDS want HORizontal:MAIn:SCAle
        if not try_write(scope, f"HORizontal:SCAle {args.timebase}"):
            try_write(scope, f"HORizontal:MAIn:SCAle {args.timebase}")

    # edge trigger on the acquisition channel
    try_write(scope, "TRIGger:MAIn:TYPe EDGE")
    try_write(scope, f"TRIGger:MAIn:EDGE:SOUrce {ch}")
    try_write(scope, f"TRIGger:MAIn:EDGE:SLOpe {args.slope}")
    try_write(scope, f"TRIGger:MAIn:LEVel {args.trig_level}")
    try_write(scope, "TRIGger:MAIn:MODe AUTO")

    try_write(scope, "ACQuire:MODe SAMple")
    try_write(scope, "ACQuire:STATE RUN")

    # waveform transfer format
    scope.write(f"DATa:SOUrce {ch}")
    scope.write("DATa:ENCdg RPBinary")   # unsigned 8-bit binary
    scope.write("DATa:WIDth 1")
    scope.write(f"DATa:STARt {args.start}")
    scope.write(f"DATa:STOP {args.stop}")


# --------------------------------------------------------------------------
# acquisition
# --------------------------------------------------------------------------

def read_preamble(scope):
    """Fetch the scaling constants needed to turn ADC counts into volts."""
    def q(cmd):
        return float(scope.query(cmd).strip())

    return {
        "ymult": q("WFMPre:YMUlt?"),
        "yzero": q("WFMPre:YZEro?"),
        "yoff": q("WFMPre:YOFf?"),
        "xincr": q("WFMPre:XINcr?"),
        "xzero": q("WFMPre:XZEro?"),
        "ptoff": q("WFMPre:PT_Off?"),
    }


def acquire(scope):
    """Return (time_array, volts_array)."""
    pre = read_preamble(scope)

    raw = scope.query_binary_values(
        "CURVe?", datatype="B", container=np.array, is_big_endian=True
    )

    volts = (raw.astype(np.float64) - pre["yoff"]) * pre["ymult"] + pre["yzero"]
    idx = np.arange(len(volts))
    seconds = (idx - pre["ptoff"]) * pre["xincr"] + pre["xzero"]

    return seconds, volts, pre


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyse(t, v, harmonics=(1, 2)):
    """DC, RMS, Vpp, and the dominant FFT peak (plus requested harmonics)."""
    dt = float(np.mean(np.diff(t)))
    fs = 1.0 / dt
    n = len(v)

    dc = float(np.mean(v))
    rms = float(np.sqrt(np.mean(v ** 2)))
    ac_rms = float(np.sqrt(np.mean((v - dc) ** 2)))
    vpp = float(np.max(v) - np.min(v))

    # Hann window keeps leakage from smearing the 1f/2f peaks together
    win = np.hanning(n)
    coherent_gain = win.mean()
    spec = np.fft.rfft((v - dc) * win)
    freqs = np.fft.rfftfreq(n, dt)
    mag = np.abs(spec) / (n * coherent_gain) * 2.0   # amplitude in volts

    peak_i = int(np.argmax(mag[1:]) + 1) if n > 2 else 0
    f0 = float(freqs[peak_i])

    result = {
        "fs": fs,
        "n": n,
        "duration": float(t[-1] - t[0]),
        "df": float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0,
        "dc": dc,
        "rms": rms,
        "ac_rms": ac_rms,
        "vpp": vpp,
        "f0": f0,
        "f0_amp": float(mag[peak_i]),
        "freqs": freqs,
        "mag": mag,
    }

    # amplitude at each requested harmonic of the fundamental
    result["harmonics"] = []
    for h in harmonics:
        target = f0 * h
        if target <= freqs[-1]:
            j = int(np.argmin(np.abs(freqs - target)))
            result["harmonics"].append((h, float(freqs[j]), float(mag[j])))

    return result


def snr_at(res, f_signal, noise_band=None):
    """Crude SNR: peak amplitude at f_signal over median noise floor."""
    freqs, mag = res["freqs"], res["mag"]
    j = int(np.argmin(np.abs(freqs - f_signal)))
    signal = mag[j]

    # exclude a few bins either side of the signal when estimating the floor
    mask = np.ones_like(mag, dtype=bool)
    lo, hi = max(0, j - 3), min(len(mag), j + 4)
    mask[lo:hi] = False
    mask[0] = False
    if noise_band is not None:
        mask &= (freqs >= noise_band[0]) & (freqs <= noise_band[1])

    floor = float(np.median(mag[mask])) if mask.any() else float("nan")
    if floor <= 0 or np.isnan(floor):
        return float("nan"), signal, floor
    return 20.0 * np.log10(signal / floor), signal, floor


def report(res, target=None):
    print(f"  samples      : {res['n']}  over {res['duration']*1e3:.4g} ms")
    print(f"  sample rate  : {res['fs']/1e6:.4g} MS/s   (FFT bin = {res['df']:.4g} Hz)")
    print(f"  DC           : {res['dc']*1e3:+.4g} mV")
    print(f"  RMS (total)  : {res['rms']*1e3:.4g} mV")
    print(f"  RMS (AC)     : {res['ac_rms']*1e3:.4g} mV")
    print(f"  Vpp          : {res['vpp']*1e3:.4g} mV")
    print(f"  dominant f0  : {res['f0']:.6g} Hz   amp {res['f0_amp']*1e3:.4g} mV")

    for h, f, a in res["harmonics"]:
        if h == 1:
            continue
        print(f"  harmonic {h}f  : {f:.6g} Hz   amp {a*1e3:.4g} mV")

    if target is not None:
        snr, sig, floor = snr_at(res, target)
        print(f"  @ {target:.6g} Hz : amp {sig*1e3:.4g} mV, "
              f"floor {floor*1e6:.4g} uV, SNR {snr:.1f} dB")


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def save_csv(path, t, v, res, idn):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# instrument", idn])
        w.writerow(["# timestamp", datetime.now().isoformat(timespec="seconds")])
        w.writerow(["# sample_rate_Hz", f"{res['fs']:.9g}"])
        w.writerow(["# dc_V", f"{res['dc']:.9g}"])
        w.writerow(["# rms_V", f"{res['rms']:.9g}"])
        w.writerow(["# ac_rms_V", f"{res['ac_rms']:.9g}"])
        w.writerow(["# vpp_V", f"{res['vpp']:.9g}"])
        w.writerow(["# f0_Hz", f"{res['f0']:.9g}"])
        w.writerow(["time_s", "volts_V"])
        for ti, vi in zip(t, v):
            w.writerow([f"{ti:.9g}", f"{vi:.9g}"])


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Acquire and analyse a waveform from a Tektronix TBS/TDS scope.")

    p.add_argument("--list", action="store_true",
                   help="list VISA resources and exit")
    p.add_argument("--resource", default=None,
                   help="VISA address; autodetected if omitted")
    p.add_argument("--channel", default="CH1", choices=["CH1", "CH2"])

    p.add_argument("--vscale", type=float, default=None,
                   help="volts per division, e.g. 0.5")
    p.add_argument("--voffset", type=float, default=None,
                   help="vertical position in divisions")
    p.add_argument("--coupling", default=None, choices=["AC", "DC", "GND"])
    p.add_argument("--probe", type=float, default=None,
                   help="probe attenuation, e.g. 1 or 10")

    p.add_argument("--timebase", type=float, default=None,
                   help="seconds per division, e.g. 200e-6")
    p.add_argument("--trig-level", type=float, default=0.0,
                   help="trigger level in volts")
    p.add_argument("--slope", default="RISe", choices=["RISe", "FALL"])

    p.add_argument("--start", type=int, default=1)
    p.add_argument("--stop", type=int, default=RECORD_LENGTH)

    p.add_argument("--target", type=float, default=None,
                   help="expected signal frequency in Hz; enables an SNR estimate")
    p.add_argument("--out", default=None,
                   help="CSV output path; timestamped name if omitted")
    p.add_argument("--no-save", action="store_true",
                   help="analyse only, write nothing")

    p.add_argument("--continuous", action="store_true",
                   help="repeat until Ctrl-C")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between captures in continuous mode")

    return p


def main():
    args = build_parser().parse_args()
    rm = pyvisa.ResourceManager()

    if args.list:
        list_resources(rm)
        return 0

    address = resolve_address(rm, args.resource)
    if address is None:
        print("No Tektronix scope found. Run with --list to see what VISA sees.",
              file=sys.stderr)
        print("If nothing appears: set Utility > Options > Rear USB Port to "
              "'Computer' (USBTMC), not 'USB Flash Drive'.", file=sys.stderr)
        return 1

    scope = rm.open_resource(address)
    scope.timeout = 15000
    scope.chunk_size = 1024 * 1024

    try:
        idn = scope.query("*IDN?").strip()
        print(f"Connected: {idn}")

        # sanity-check we are driving the instrument we think we are
        if EXPECTED_SERIAL not in idn:
            print(f"  ! Serial does not match {EXPECTED_SERIAL} -- "
                  f"different unit on the bench?", file=sys.stderr)
        if "TBS" not in idn.upper() and "TDS" not in idn.upper():
            print("  ! Not a TBS/TDS scope -- the WFMPre/CURVe command set "
                  "may not apply.", file=sys.stderr)

        configure(scope, args)
        time.sleep(0.3)  # let the front panel settle before the first grab

        capture = 0
        while True:
            capture += 1
            t, v, _ = acquire(scope)
            res = analyse(t, v)

            print(f"\n--- capture {capture} "
                  f"[{datetime.now().strftime('%H:%M:%S')}] ---")
            report(res, target=args.target)

            if not args.no_save:
                path = args.out
                if path is None:
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = f"scope_{stamp}.csv"
                elif args.continuous:
                    base = path.rsplit(".", 1)[0]
                    path = f"{base}_{capture:04d}.csv"
                save_csv(path, t, v, res, idn)
                print(f"  saved -> {path}")

            if not args.continuous:
                break
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        scope.close()
        rm.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
