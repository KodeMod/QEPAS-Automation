"""
sr830.py -- General-purpose driver for the Stanford Research Systems SR830
            DSP lock-in amplifier over GPIB.

Transports:
    NI / Keysight GPIB via VISA   ->  SR830("GPIB0::8::INSTR")
    Prologix GPIB-USB (COM port)  ->  SR830("COM4", prologix=True, addr=8)

Minimal use:
    from sr830 import SR830
    with SR830("GPIB0::8::INSTR") as li:
        print(li.idn)
        li.reference(freq=1000.0, amplitude=0.1)
        li.filters(time_constant=0.1, slope=24)
        li.autorange()
        print(li.snap())

Note on OUTX: the SR830 listens on GPIB and RS232 simultaneously, but only
replies on the interface selected by OUTX. If it is set wrong, writes appear
to work and every query times out. This driver sets it on connect.
"""

import time

import numpy as np

__all__ = ["SR830", "SENSITIVITY", "TIME_CONSTANT", "SAMPLE_RATE"]

# ---------------------------------------------------------------------------
# Index <-> physical value tables (SR830 manual)
# ---------------------------------------------------------------------------

SENSITIVITY = [
    2e-9, 5e-9, 10e-9, 20e-9, 50e-9, 100e-9, 200e-9, 500e-9,
    1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6, 200e-6, 500e-6,
    1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3, 1.0,
]  # V full scale (A when a current input is selected)

TIME_CONSTANT = [
    10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3,
    1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1e3, 3e3, 10e3, 30e3,
]  # s

SAMPLE_RATE = [
    62.5e-3, 125e-3, 250e-3, 500e-3, 1.0, 2.0, 4.0, 8.0,
    16.0, 32.0, 64.0, 128.0, 256.0, 512.0,
]  # Hz; index 14 = external trigger

BUFFER_DEPTH = 16383

_SLOPE = {6: 0, 12: 1, 18: 2, 24: 3}
_INPUT = {"A": 0, "A-B": 1, "I1M": 2, "I100M": 3}
_COUPLING = {"AC": 0, "DC": 1}
_RESERVE = {"high": 0, "normal": 1, "lownoise": 2}
_NOTCH = {"none": 0, "line": 1, "2xline": 2, "both": 3}
_REF_SLOPE = ["sine", "rising", "falling"]

PARAMS = {
    "X": 1, "Y": 2, "R": 3, "theta": 4,
    "aux1": 5, "aux2": 6, "aux3": 7, "aux4": 8,
    "freq": 9, "ch1": 10, "ch2": 11,
}

_CH1_DISP = {"X": 0, "R": 1, "Xnoise": 2, "aux1": 3, "aux2": 4}
_CH2_DISP = {"Y": 0, "theta": 1, "Ynoise": 2, "aux3": 3, "aux4": 4}


def _index_ge(table, value):
    """Smallest index whose value is >= `value` (clamped to the table)."""
    arr = np.asarray(table, float)
    hits = np.nonzero(arr >= value)[0]
    return int(hits[0]) if len(hits) else len(arr) - 1


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class _VisaLink:
    def __init__(self, resource, timeout_ms):
        import pyvisa
        self._rm = pyvisa.ResourceManager()
        self._dev = self._rm.open_resource(resource)
        self._dev.timeout = timeout_ms
        self._dev.read_termination = "\n"
        self._dev.write_termination = "\n"

    def write(self, cmd):
        self._dev.write(cmd)

    def query(self, cmd):
        return self._dev.query(cmd).strip()

    def close(self):
        self._dev.close()
        self._rm.close()


class _PrologixLink:
    """Prologix GPIB-USB in controller mode; enumerates as a serial port."""

    def __init__(self, port, addr, timeout_ms):
        import serial
        self._ser = serial.Serial(port, 115200, timeout=timeout_ms / 1000.0)
        time.sleep(2.0)                      # adapter resets when the port opens
        for c in ("++mode 1", f"++addr {addr}", "++auto 0",
                  "++eoi 1", "++eos 3", "++read_tmo_ms 3000"):
            self._ser.write((c + "\n").encode())

    def write(self, cmd):
        self._ser.write((cmd + "\n").encode())

    def query(self, cmd):
        self.write(cmd)
        self.write("++read eoi")
        return self._ser.readline().decode(errors="replace").strip()

    def close(self):
        self._ser.close()


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

class SR830:
    """Stanford Research Systems SR830 lock-in amplifier."""

    def __init__(self, resource, prologix=False, addr=8, timeout_ms=10000):
        self._link = (_PrologixLink(resource, addr, timeout_ms) if prologix
                      else _VisaLink(resource, timeout_ms))
        self.write("OUTX 1")                 # route replies to GPIB
        time.sleep(0.1)
        self.idn = self.query("*IDN?")
        if "SR830" not in self.idn:
            raise RuntimeError(f"Device is not an SR830: {self.idn!r}")
        self._buf_rate = None

    # -- plumbing -----------------------------------------------------------

    def write(self, cmd):
        self._link.write(cmd)

    def query(self, cmd):
        return self._link.query(cmd)

    def close(self):
        self._link.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"<SR830 {self.idn}>"

    def reset(self):
        """Return the instrument to its default configuration."""
        self.write("*RST")
        time.sleep(2.0)
        self.write("OUTX 1")

    # -- reference and oscillator -------------------------------------------

    @property
    def frequency(self):
        """Internal reference / SINE OUT frequency, Hz (0.001 to 102000)."""
        return float(self.query("FREQ?"))

    @frequency.setter
    def frequency(self, hz):
        if not 0.001 <= hz <= 102000:
            raise ValueError("FREQ must be 0.001 to 102000 Hz")
        self.write(f"FREQ {hz:.6f}")

    @property
    def amplitude(self):
        """SINE OUT amplitude, V rms (0.004 to 5.000)."""
        return float(self.query("SLVL?"))

    @amplitude.setter
    def amplitude(self, v):
        if not 0.004 <= v <= 5.0:
            raise ValueError("SLVL must be 0.004 to 5.000 V rms")
        self.write(f"SLVL {v:.3f}")

    @property
    def phase(self):
        """Reference phase shift, degrees."""
        return float(self.query("PHAS?"))

    @phase.setter
    def phase(self, deg):
        self.write(f"PHAS {deg:.2f}")

    @property
    def harmonic(self):
        """Detection harmonic (1 to 19999; n*f must stay under 102 kHz)."""
        return int(self.query("HARM?"))

    @harmonic.setter
    def harmonic(self, n):
        self.write(f"HARM {int(n)}")

    @property
    def internal_reference(self):
        return bool(int(self.query("FMOD?")))

    @internal_reference.setter
    def internal_reference(self, flag):
        self.write(f"FMOD {1 if flag else 0}")

    def external_reference(self, slope="sine"):
        """Lock to REF IN. slope: 'sine' zero-crossing, 'rising', 'falling'."""
        self.internal_reference = False
        self.write(f"RSLP {_REF_SLOPE.index(slope)}")

    def reference(self, freq=None, amplitude=None, phase=None,
                  harmonic=None, internal=True):
        """Set any subset of the reference parameters in one call."""
        self.internal_reference = internal
        if freq is not None:
            self.frequency = freq
        if amplitude is not None:
            self.amplitude = amplitude
        if phase is not None:
            self.phase = phase
        if harmonic is not None:
            self.harmonic = harmonic
        return self.status()

    def auto_phase(self):
        """Null the Y channel. Takes several time constants to complete."""
        self.write("APHS")
        time.sleep(max(2.0, 6 * self.time_constant))

    # -- input configuration -------------------------------------------------

    def input_config(self, source="A", coupling="AC", shield_ground=True,
                     notch="none"):
        """source: A | A-B | I1M | I100M.  notch: none | line | 2xline | both."""
        self.write(f"ISRC {_INPUT[source]}")
        self.write(f"ICPL {_COUPLING[coupling.upper()]}")
        self.write(f"IGND {1 if shield_ground else 0}")
        self.write(f"ILIN {_NOTCH[notch]}")

    # -- gain and filters ----------------------------------------------------

    @property
    def sensitivity(self):
        return SENSITIVITY[int(self.query("SENS?"))]

    @sensitivity.setter
    def sensitivity(self, full_scale):
        self.write(f"SENS {_index_ge(SENSITIVITY, full_scale)}")

    @property
    def time_constant(self):
        return TIME_CONSTANT[int(self.query("OFLT?"))]

    @time_constant.setter
    def time_constant(self, seconds):
        self.write(f"OFLT {_index_ge(TIME_CONSTANT, seconds)}")

    @property
    def slope(self):
        return [6, 12, 18, 24][int(self.query("OFSL?"))]

    @slope.setter
    def slope(self, db_per_octave):
        self.write(f"OFSL {_SLOPE[db_per_octave]}")

    def filters(self, time_constant=None, slope=None, reserve=None, sync=None):
        """Set any subset of the output filter parameters."""
        if time_constant is not None:
            self.time_constant = time_constant
        if slope is not None:
            self.slope = slope
        if reserve is not None:
            self.write(f"RMOD {_RESERVE[reserve]}")
        if sync is not None:
            # Synchronous filter; only active when the reference is below 200 Hz.
            self.write(f"SYNC {1 if sync else 0}")

    def settle_time(self, n_tc=None):
        """Recommended wait after a settings change, in seconds."""
        if n_tc is None:
            n_tc = {6: 5, 12: 7, 18: 9, 24: 11}[self.slope]
        return n_tc * self.time_constant + 0.05

    def settle(self, n_tc=None):
        time.sleep(self.settle_time(n_tc))

    # -- status --------------------------------------------------------------

    def overload(self):
        """Latched overload flags. Reading clears them."""
        return {
            "input": bool(int(self.query("LIAS? 0"))),
            "filter": bool(int(self.query("LIAS? 1"))),
            "output": bool(int(self.query("LIAS? 2"))),
        }

    def unlocked(self):
        """True if the reference PLL has lost lock (latched)."""
        return bool(int(self.query("LIAS? 3")))

    def status(self):
        """Snapshot of the current configuration."""
        return {
            "frequency_Hz": self.frequency,
            "amplitude_Vrms": self.amplitude,
            "phase_deg": self.phase,
            "harmonic": self.harmonic,
            "internal_ref": self.internal_reference,
            "input_V_fullscale": self.sensitivity,
            "time_constant_s": self.time_constant,
            "slope_dB_oct": self.slope,
        }

    def autorange(self, target=0.3):
        """Pick the smallest sensitivity that keeps R near `target` of full scale.

        Computes the required range from a measurement instead of stepping one
        notch at a time, so it costs two or three settling periods rather than
        twenty. Returns the full-scale value actually selected.
        """
        # Back off first if the input stage is saturated and R is meaningless.
        for _ in range(len(SENSITIVITY)):
            if not self.overload()["input"]:
                break
            idx = int(self.query("SENS?"))
            if idx >= len(SENSITIVITY) - 1:
                break
            self.write(f"SENS {idx + 1}")
            self.settle(3)

        for _ in range(3):
            self.settle()
            r = self.read("R")
            if r <= 0:
                break
            want = _index_ge(SENSITIVITY, r / target)
            if want == int(self.query("SENS?")):
                break
            self.write(f"SENS {want}")
        return self.sensitivity

    # -- reading -------------------------------------------------------------

    def read(self, param="R"):
        """Single value: X, Y, R, theta, aux1..aux4, freq."""
        return float(self.query(f"OUTP? {PARAMS[param]}"))

    def snap(self, *params):
        """Simultaneous read of 2 to 6 parameters, returned as a dict.

        Prefer this over repeated read() calls: SNAP? latches all of the
        requested values at the same instant, so X and Y stay consistent even
        while the signal drifts.
        """
        params = params or ("X", "Y", "R", "theta")
        if not 2 <= len(params) <= 6:
            raise ValueError("SNAP? accepts 2 to 6 parameters")
        codes = ",".join(str(PARAMS[p]) for p in params)
        vals = [float(v) for v in self.query(f"SNAP? {codes}").split(",")]
        return dict(zip(params, vals))

    def aux_out(self, channel, volts):
        """Set an Aux Output, -10.5 to +10.5 V. Handy for sweeping a DUT."""
        if channel not in (1, 2, 3, 4):
            raise ValueError("Aux output channel must be 1 to 4")
        if not -10.5 <= volts <= 10.5:
            raise ValueError("AUXV must be -10.5 to +10.5 V")
        self.write(f"AUXV {channel},{volts:.3f}")

    def aux_in(self, channel):
        return float(self.query(f"OAUX? {channel}"))

    # -- internal data buffer ------------------------------------------------

    def buffer_setup(self, rate_hz=16.0, ch1="X", ch2="Y", loop=False):
        """Arm the two internal buffers and return the actual sample rate.

        The buffers record whatever the CH1 and CH2 displays show, so this sets
        those first. Sampling is hardware-timed, which is what makes it better
        than polling for anything faster than slow logging.
        """
        self.write(f"DDEF 1,{_CH1_DISP[ch1]},0")
        self.write(f"DDEF 2,{_CH2_DISP[ch2]},0")
        idx = _index_ge(SAMPLE_RATE, rate_hz)
        self.write(f"SRAT {idx}")
        self.write(f"SEND {1 if loop else 0}")
        self.write("REST")
        self._buf_rate = SAMPLE_RATE[idx]
        return self._buf_rate

    def buffer_start(self):
        self.write("STRT")

    def buffer_pause(self):
        self.write("PAUS")

    def buffer_reset(self):
        self.write("REST")

    def buffer_points(self):
        return int(self.query("SPTS?"))

    def buffer_read(self, channel=1, start=0, n=None, chunk=1000):
        """Read stored points into a numpy array (ASCII transfer)."""
        if n is None:
            n = self.buffer_points() - start
        if n <= 0:
            return np.array([])
        out = []
        for i in range(start, start + n, chunk):
            k = min(chunk, start + n - i)
            raw = self.query(f"TRCA? {channel},{i},{k}")
            out.extend(float(v) for v in raw.strip().strip(",").split(","))
        return np.asarray(out)

    def capture(self, duration_s, rate_hz=16.0, ch1="X", ch2="Y"):
        """Hardware-timed acquisition. Returns (t, ch1_data, ch2_data)."""
        rate = self.buffer_setup(rate_hz, ch1, ch2, loop=False)
        if duration_s * rate > BUFFER_DEPTH:
            raise ValueError(
                f"{duration_s * rate:.0f} points exceeds the {BUFFER_DEPTH}-point "
                f"buffer; lower the rate or shorten the run")
        self.buffer_start()
        time.sleep(duration_s + 0.2)
        self.buffer_pause()
        time.sleep(0.1)
        n = self.buffer_points()
        a = self.buffer_read(1, 0, n)
        b = self.buffer_read(2, 0, n)
        t = np.arange(len(a)) / rate
        return t, a, b


if __name__ == "__main__":
    import sys
    resource = sys.argv[1] if len(sys.argv) > 1 else "GPIB0::8::INSTR"
    with SR830(resource) as li:
        print(li.idn)
        for k, v in li.status().items():
            print(f"  {k:>18} : {v}")
