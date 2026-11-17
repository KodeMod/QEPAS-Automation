# QEPAS bench control

One window for the SR830 lock-in, the CO2/N2 gas bench and the ITC4005QCL
laser controller, writing a single annotated CSV per session.

## Files

Put all of these in one folder. Nothing else is needed.

| File | What it is |
|---|---|
| `combined_gui.py` | **run this** — the four-tab application |
| `sr830.py`, `sr830_gui.py`, `sr830_meta.py`, `sr830_daq.py` | your lock-in code, unmodified |
| `mfc_control.py` | MFC config, limits, set-point maths, CLI tools |
| `mfc_worker.py` | serial thread owning the RS-485 bus |
| `mfc_gui.py` | the per-MFC channel card widget |
| `itc4005.py` | ITC SCPI driver, limits and interlocks |
| `itc_worker.py` | VISA thread owning the ITC |
| `itc_panel.py` | laser / TEC / modulation panel |
| `humidity.py` | water-vapour maths (Magnus, mole fraction, RH) |
| `scope_gui.py` | your scope code, unmodified (also runs standalone) |
| `scope_panel.py` | frame wrapper so the scope panel fits in a tab |
| `tbs_acquire.py` | your command-line scope tool, unmodified |

## Running

```
cd <folder>
python combined_gui.py --demo      # no hardware: fake lock-in, simulated MFCs and ITC
python combined_gui.py             # everything live
python combined_gui.py --prologix --resource COM3 --addr 8
python combined_gui.py --list-interfaces
```

Launch from Anaconda Prompt, not Spyder F5. Tk windows and the Spyder console
fight over the event loop.

**Close the "get red-y" software first** — Windows gives one process exclusive
use of COM4.

## The four tabs

**Lock-in (SR830)** — your existing panel, embedded whole. Reference, gain,
filters, acquisition, live trace, experiment context.

**Gas bench (MFC)** — three channel cards with live measured flow, setpoint
and deviation. Manual blend by CO2 percentage and total flow. ALL STOP zeroes
every channel.

**Laser / TEC (ITC4005)** — TEC setpoint and stability, LD current with ramp,
modulation (internal or external: shape, frequency, depth), interlock lamps,
current-ripple measurement. LASER OFF is always reachable.

**Scope (TBS1052B)** — your `scope_gui.py` panel, whole: channel and timebase,
trigger, autoset, live time-domain and FFT plots, DC/RMS/Vpp/f0/SNR readouts,
and its own CSV logging. The instrument layer, analysis and worker are imported
from your file unchanged.

**Linked run** — presets or a custom series, with hold and flush times, driving
the gas while annotating the lock-in CSV.

## Series

| Series | Levels | Duration at 300 s + 60 s |
|---|---|---|
| Preset A | 12 → 100 % in 2 % steps (45 points) | 4.5 h |
| Preset B | 15 → 100 % in 5 % steps (18 points) | 1.8 h |
| Custom | whatever you type, e.g. `20,30,40,50,60` | shown in the Plan panel |

Hold and flush times apply to whichever series is selected. The Plan panel
shows duration and flags any level whose flows fall below a controller's
usable floor before you start.

## One CSV per session

Start the recording on the Lock-in tab **before** the run and leave it running
across every level. Each lock-in row carries the gas and laser state that
produced it:

```
conc_pct  analyte_sccm  matrix_sccm  tec_setpoint_C  ld_current_mA  scope_vpp_mV  scope_2f_mV  scope_snr_dB  note
      15          28.5        161.5        17.5123          152.0         9.375       1.1057         52.00  hold step 1/3
       0           0.0        190.0        17.5129          152.0         9.375       1.0847         52.26  flush step 2/3
      20          38.0        152.0        17.5129          152.0         9.375       1.0854         52.05  hold step 2/3
```

The scope columns only mean anything while the scope is streaming — press
**Run** on the Scope tab before starting a linked run, or every row carries the
same stale capture. The GUI asks if it finds the scope connected but idle.

`MetadataPanel` ignores column names that are not its own fields, and
`sr830_gui` fixes the column set when the file is opened. So the combined GUI
wraps the panel's change callback to merge the scope columns in, and seeds them
at startup. Neither `sr830_gui.py` nor `sr830_meta.py` is modified.

plus `# MARK` comment lines at every transition. To analyse:

```python
import pandas as pd
df = pd.read_csv("session.csv", comment="#")
df.groupby("conc_pct")["X_V"].agg(["mean", "std", "count"])
```

Use **X**, not R. R is a magnitude, so it cannot go negative and reads high
when the signal is weak — the N2 blank measured 1.014e-3 V in R, which is
rectified noise almost exactly matching the predicted 1.006e-3 V.

Discard roughly the first 60 s of each phase. Allan analysis showed averaging
stops helping beyond about a minute, so a longer hold adds drift, not
precision.

## Pressure

The Gas bench tab has a **Pressure** panel. Pick a channel; the controls enable
only if that unit has a pressure sensor fitted, judged from
`PRESSURE_SCALE_MIN/MAX` in the Voegtlin register map. A unit without one
reports a pressure of 0 and would accept a setpoint that controls nothing.

| Control | Register | Notes |
|---|---|---|
| Set pressure | `PRESSURE_SETPOINT` | refused outside the sensor's own range |
| Set limit | `PRESSURE_CONTROL_FLOW_LIMIT` | max flow while holding pressure |
| Set mode | `PRESSURE_CONTROL_MODE` | switches flow control <-> pressure control |

**The mode numbers are not documented.** Learn them once:

1. In get red-y, set the unit to pressure control. Close get red-y.
2. `python mfc_control.py --pressure-info` and note the value of `mode`.
3. Set it back to flow control in get red-y, repeat, note that value.
4. Record both in `PRESSURE_MODE_NAMES` in `mfc_control.py`,
   e.g. `{0: "flow control", 1: "pressure control"}`.

A unit in pressure-control mode stops regulating flow. On MFC1 or MFC2 that
breaks the CO2/N2 blend, and the GUI asks for confirmation before doing it.

## Three gas lines and humidity

| Line | MFC | Address | Gas |
|---|---|---|---|
| dry N2 | MFC1 | 1 | N2 straight to the mixer |
| CO2 | MFC2 | 2 | from the CO2 cylinder (set its ppm in the GUI) |
| humid N2 | MFC3 | 246 | N2 through the bubbler |

**Gas bench tab** gained two panels. *Three-line flow* takes an independent
set point for each line, shows the resulting total, CO2 in the cell, H2O in
ppmv and cell RH, and flags any line below its own floor. "Fill boxes"
computes the three flows from a CO2 ppm target, a wet fraction and a total.
*Humidity and cylinder* holds bubbler T, cell T, pressure, bubbler efficiency
and the CO2 cylinder concentration -- every water figure depends on these.

**Linked run tab** has three series modes:

- **CO2 series** -- the original two-line presets, unchanged.
- **Humidity series (Case 1)** -- CO2 closed, wet fraction stepped up then
  back down. The down-steps are what reveal hysteresis from water coming off
  the tubing walls.
- **CO2 x humidity grid (Case 2)** -- humidity as the slow outer loop, CO2
  stepped fast inside each block. Each block opens with a water-only blank,
  and **each flush keeps the block's humidity** -- a dry flush would discard
  the 20 minutes spent equilibrating.

The Plan panel lists every step with its three flows, H2O in ppmv and cell
RH, and refuses to start quietly if any line falls below its floor or the RH
passes 70 %.

### What differs from the spreadsheet

The plan assumes 190 sccm full-scale MFCs with a 12 % rule, giving a 22.8
sccm floor on every line. The real heads are 2000 SCCM:

| Line | Grade | Floor |
|---|---|---|
| dry N2, CO2 | Hi-Performance, 1:100 | 20 SCCM |
| humid N2 | Standard, 1:50 | **40 SCCM** |

So the plan's **0.13 and 0.20 wet fractions are not reachable** -- the lowest
is 40/190 = 0.211. The defaults use 0.22 instead. Block C is 0.29 rather than
0.30, because at 6000 ppm a 0.30 wet fraction leaves dry N2 at 19 SCCM, just
under its floor.

CO2 is unaffected: with a 10000 ppm cylinder the lowest non-zero level is
1053 ppm, better than the plan's 1200.

### Measured RH curve (2026-11-04)

The bench's own RH-vs-wet-flow measurement at 20 degC, 190 SCCM total, is
built in as `humidity.BENCH_RH_CALIBRATION` and used by default (the "use the
bench's measured RH curve" tick on the gas tab). It replaces the formula,
because it is a measurement of this bubbler at these flows.

What it showed, against the plan's assumptions:

| | Plan assumed | Measured |
|---|---|---|
| bubbler efficiency | 0.90 | **0.748** (fit over 9 points) |
| RH at 100 % wet | 90 % | **70.5 %** |
| "dry" line RH | 0 | **4.45 %** |

With the formula and 0.90 the GUI over-predicted RH by up to 19.5 pp. With
the fitted efficiency and the dry-line offset it is within 4.3 pp; with the
measured curve it is exact.

Two things the single-number model cannot capture, and the curve does:
efficiency rises from about 0.70 at 38 SCCM to 0.79 at 152 SCCM, then falls
to 0.705 at full flow as residence time in the bubbler shortens.

The dry line carrying 4.45 % RH means **the dry blank is not a zero-water
blank** -- about 1030 ppmv of water is present even with the bubbler closed.

### MFC3's floor

The Standard grade puts it at 2 % of 2000 = 40 SCCM, but the bench runs 38
SCCM repeatedly and the RH it produces sits on the measured curve. 38 SCCM is
recorded as `verified_min_sccm` on that channel, which overrides the
spec-derived figure. Set one only from data you have -- it loosens a check.

### Water columns in the CSV

Every lock-in row carries `wet_n2_sccm`, `dry_n2_sccm`, `wet_fraction`,
`h2o_ppmv`, `h2o_percent`, `rh_percent`, `h2o_mol_m3`. These are **planned**
values from the flows. The BME688 at the cell outlet is the measurement --
analyse with the measured RH/T/p.

`python humidity.py` self-checks the formulas against the spreadsheet's own
numbers.

**Measure the bubbler efficiency once.** It is the only assumed number in the
water calculation (0.90 by default) and every H2O figure scales linearly with
it: run the wet line alone, read the outlet RH, put the real value in.

## Resonance and drive frequency

The SR830 stores 5 significant digits, so near 6 kHz the oscillator moves in
0.1 Hz steps. That matters at this Q: with f0 = 12467.372 Hz and FWHM 1.427 Hz,
driving at a rounded 6234 Hz instead of 6233.7 costs about a quarter of the
signal.

Put the measured resonance into **QTF f0** and **FWHM** on the Lock-in tab and
press **Drive at f0 / 2**. It sets the nearest settable frequency and reports
what the rounding costs:

```
f0/2 = 6233.686 Hz, nearest settable 6233.7 Hz
-> 2f detuned +0.0280 Hz   (0.039 FWHM, 99.9 % of peak)
```

On the Scope tab, the FFT bin is set by the record: 2500 points at 250 us/div
spans 2.5 ms, so bins are 400 Hz and the peak reads 6400 Hz rather than 6233.7.
To resolve the drive frequency itself, use a slower timebase -- 10 ms/div gives
a 100 ms record and 10 Hz bins. The scope is for checking the waveform; the
lock-in is what measures the frequency.

## Safety behaviour

- Nothing opens a valve or energises the laser until you connect and act
- ALL STOP zeroes all three MFCs; LASER OFF is never conditional
- Laser cannot be enabled until the TEC is on and has held setpoint
- Current is ramped, never stepped, and the ramp is interruptible
- Total gas flow is capped at 200 SCCM on every command path
- Closing the window switches the laser off, then zeroes the flows
- The modulation panel warns before a depth that would clip at the current
  limit

## Settings to confirm on your hardware

- `CURRENT_LIMIT_MA` in `itc4005.py` — currently 160 mA; your instrument's own
  limit was 200 mA. At 152 mA setpoint, 160 mA clips anything above ~5 %
  modulation depth.
- `CYLINDER_CO2_FRACTION` in `mfc_control.py` — assumes ~100 % CO2
- `MIN_CONTROLLABLE_FRACTION` — assumes 1:100 turndown
- `ACCURACY_FRACTION_OF_FS` — placeholder 0.5 % FS
- Both MFC heads are calibrated for **Air**, not N2/CO2. See the gas
  conversion note in `mfc_control.py`.

## Command-line tools (no GUI)

```
python mfc_control.py --table --total-sccm 190
python mfc_control.py --feasibility --levels 12,20,45,90
python mfc_control.py --scan
python mfc_control.py --pressure-info
python itc4005.py --probe
python itc4005.py --ripple
python tbs_acquire.py --list
python scope_gui.py                  # the scope panel in its own window
python scope_panel.py --simulate
```
