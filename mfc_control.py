# -*- coding: utf-8 -*-
"""
MFC control for low-flow CO2/N2 gas blending (Voegtlin red-y smart, viapi).

CHANNEL MAP -- confirmed against serial numbers in get red-y on COM4:

    name    addr   serial   type       tag in get red-y      FS / gas
    -------------------------------------------------------------------------
    MFC1     1     416846   P3XWVJ65   "0.94% CO2"           2 ln/min, Air
    MFC2     2     416847   P3XWVJ65   "Zero Air/~100% CO2"  2 ln/min, Air
    MFC3   247     227741   GSCC3SS    ""                   10 ln/min, Air

Verified live on 2026-09-02: serials, full scales and ln/min units all read
back as above. Plumbing confirmed at the bench the same day:
    address 1 = N2, address 2 = CO2.
The "0.94% CO2" tag on address 1 is a stale label from an earlier
experiment, not the current gas. Worth clearing it in get red-y.

Still open: CYLINDER_CO2_FRACTION assumes ~100 % CO2 on address 2. Check the
bottle. And both heads are calibrated for Air -- see the gas conversion note
below, and run --gas-info to see whether the device can be told otherwise.

Setpoints are written in ln/min (verified: setpoint 0.19 -> reading 0.189).
This module lets you work in SCCM throughout; see the UNITS note below.
MFC3 is READ-ONLY: it sits downstream as a total-flow check, and writing to
it would put a second regulator in series with the two upstream heads.

Operating point: 190 SCCM (0.190 ln/min) total, held constant while the CO2
fraction is stepped. That is 9.5 % of full scale on the 2 ln/min heads --
fine for the N2 side, but the CO2 side falls below its usable floor at low
CO2 fractions, so the reachable band is roughly 11-89 % CO2. Run

    python mfc_control.py --feasibility

before committing to a set of concentrations: it prints the required flow and
the estimated uncertainty per point, and flags the ones that are impossible.

BEFORE ANY SERIAL COMMAND: close the "get red-y" software. Windows gives one
process exclusive use of a COM port, so leaving it open makes every connect
here fail with an access error.

Entry points:

    --check          environment diagnosis, no hardware
    --update-db      cache register definitions for offline use
    --table          set-point table at a given total flow
    --feasibility    targets vs. hardware limits
    --advice         notes on low-flow accuracy
    --scan           identify devices on the bus (reads only)
    --test-channel N open one line briefly to confirm plumbing
    --dry-run        rehearse a sequence, no serial traffic
    --run            EXECUTE a live sequence

API reference: https://api.voegtlin.com/install.html
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

# ==========================================================================
# UNITS
# ==========================================================================
# Voegtlin "ln" is a normal litre: 0 degC, 1013.25 mbar.
# SCCM is a standard cm3/min, but "standard" is defined at 0 degC by some
# vendors and 20 or 25 degC by others. If both references are 0 degC then
# 1 ln/min = 1000 SCCM exactly, which is what is assumed here. If your SCCM
# convention is 20 degC, the true factor is about 1073 SCCM per ln/min -- a
# 7 % offset that would sit in your data unnoticed. Worth confirming once.

SCCM_PER_LN = 1000.0


def sccm_to_ln(value: float) -> float:
    return value / SCCM_PER_LN


def ln_to_sccm(value: float) -> float:
    return value * SCCM_PER_LN


# ==========================================================================
# CONFIG
# ==========================================================================

PORT = 4                 # COM4
BAUDRATE = 9600
PARITY = "N"

DEVICE_FAMILY = "smart"
DEVICE_VERSION = ">= 6.0.0, < 7.0"

# Lowest usable flow as a fraction of full scale. Voegtlin red-y smart is
# commonly quoted at 1:50 turndown, i.e. 2 % of FS. Some units are specified
# to 1:100. Check the datasheet for GSC-B4TA-BB23 and adjust -- this single
# number decides which of your low-flow setpoints are honest.
# Lowest usable flow as a fraction of full scale. The ADM01 operating plan
# assumes 1:100 turndown for the 2 ln/min heads, i.e. a 0.020 ln/min (20 SCCM)
# minimum. That is what is set here. Vögtlin red-y smart is sometimes quoted
# at 1:50 instead; if these units are 1:50, the floor doubles to 40 SCCM and
# the usable CO2 band at 190 SCCM narrows from 11-89 % to 21-79 %. Confirm
# against the datasheet for GSC-B4TA-BB23 -- this one number sets which of
# your set points are real.
MIN_CONTROLLABLE_FRACTION = 0.01

# Accuracy, as a fraction of FULL SCALE. This is the key number at low flow:
# a full-scale-referenced error becomes a large relative error as flow drops.
# PLACEHOLDER -- replace with the figure from your datasheet.
ACCURACY_FRACTION_OF_FS = 0.005     # 0.5 % FS
ACCURACY_FRACTION_OF_READING = 0.0  # add if your spec has a reading term


@dataclass
class ChannelConfig:
    label: str
    slave: int
    full_scale_ln: float
    expected_serial: Optional[int] = None
    gas: str = "Air"           # what is PHYSICALLY plumbed
    device_gas: str = "Air"    # what the device is CALIBRATED for
    k_factor: float = 1.0      # true flow / indicated flow
    read_only: bool = False    # monitor only -- writes are refused

    # Grade of this particular head. Vogtlin's ordering code carries a
    # version letter: T = Hi-Performance (+/-0.3 % FS +/-0.5 % of reading,
    # 1:100 turndown), S = Standard (+/-1.0 % FS, 1:50). These used to be
    # module-wide constants, which was fine while every head was the same
    # grade -- it stops being fine the moment a Standard unit shares the bus
    # with two Hi-Performance ones, because the Standard head would be
    # credited with twice the turndown and three times the accuracy it has.
    # None falls back to the module default below.
    turndown_fraction: Optional[float] = None   # smallest usable / full scale
    accuracy_fs: Optional[float] = None         # error as a fraction of FS
    accuracy_rdg: Optional[float] = None        # plus a fraction of reading

    # A floor established by measurement rather than by the datasheet. When
    # set it overrides turndown_fraction, because a set point this head has
    # been shown to hold beats one derived from a grade letter. Only set it
    # from data you have: it loosens a safety check.
    verified_min_sccm: Optional[float] = None


# ---- Gas conversion -------------------------------------------------------
# All three heads are calibrated for Air. A thermal MFC senses heat capacity,
# so another gas flows at a different true rate than indicated. For CO2 in an
# Air-calibrated head the factor is roughly 0.74.
#
# k_factor stays at 1.0 deliberately: applying a textbook number silently is
# worse than a known, uncorrected offset. Fix it either by setting the gas in
# the get red-y "Parameter" tab (preferred -- the device then corrects itself,
# leave k_factor at 1.0), or by entering the factor for your exact model here.
# Startup warns for any channel where gas != device_gas and k_factor == 1.0.

CHANNELS: Dict[str, ChannelConfig] = {
    # GSC-B4TA-BB23: the "T" is Hi-Performance -- 1:100 turndown,
    # +/-0.3 % FS +/-0.5 % of reading.
    "n2": ChannelConfig(
        label="MFC1_N2", slave=1, full_scale_ln=2.0, expected_serial=416846,
        gas="N2", device_gas="Air", k_factor=1.0,
        turndown_fraction=0.01, accuracy_fs=0.003, accuracy_rdg=0.005,
    ),
    "co2": ChannelConfig(
        label="MFC2_CO2", slave=2, full_scale_ln=2.0, expected_serial=416847,
        gas="CO2", device_gas="Air", k_factor=1.0,
        turndown_fraction=0.01, accuracy_fs=0.003, accuracy_rdg=0.005,
    ),
    # Downstream total-flow monitor, NOT a third control loop.
    # read_only=True because if this unit has an active valve, writing to it
    # puts a second regulator in series with the two upstream MFCs: it shifts
    # back-pressure and the loops fight each other. Worse, a setpoint of 0
    # would close the line and block the whole stream. It is here to check
    # that the measured total is close to Q_CO2 + Q_N2, nothing else.
    # At 190 SCCM it runs at 1.9 % of its 10 ln/min full scale, so treat its
    # reading as a coarse sanity check, not a flow reference.
    # Activated as a controllable channel on request (was read-only).
    # Keep in mind why it was read-only: if this unit sits DOWNSTREAM of the
    # mixing junction with an active valve, commanding it puts a second
    # regulator in series with the two upstream loops. They then fight over
    # back-pressure and neither settles cleanly. If it is plumbed as a third
    # independent gas line, controlling it is fine. Set read_only=True again
    # if you see the two upstream heads hunting.
    # 2026-10-15: GSC-C3SS-BB26 (SN 227741, 10 ln/min, address 247) replaced
    # by GSC-B4SS-BB23, SN 229813 -- which answers on address 246, NOT 247.
    # A full 1..247 sweep found it; probing only the service address missed
    # it entirely.
    #
    # Everything below was READ BACK FROM THE DEVICE on 2026-10-15, not
    # inferred: RANGE_VALUE = 2 ln/min, INFO_TYPECODE_1_SHORT = GSCB4SS,
    # INFO_TYPECODE_2_SHORT = BB23, INFO_SERIAL = 229813.
    #
    # The "S" is the Standard version: 1:50 turndown and +/-1.0 % FS, against
    # 1:100 and +/-0.3 % FS +/-0.5 % rdg on the two Hi-Performance "T" heads.
    # The grade letters come from Vogtlin's ordering template rather than a
    # datasheet for this exact code, so they remain worth confirming -- they
    # set this channel's floor and every uncertainty figure derived from it.
    "monitor": ChannelConfig(
        label="MFC3_WETN2", slave=246, full_scale_ln=2.0,   # confirmed
        expected_serial=229813,
        # Humid N2: dry N2 through the bubbler. Thermally still N2, so the
        # Air calibration error is the same as on MFC1, not a CO2-sized one.
        gas="N2(wet)", device_gas="Air", k_factor=1.0, read_only=False,
        turndown_fraction=0.02, accuracy_fs=0.01, accuracy_rdg=0.0,
        # The Standard grade puts the floor at 2 % of 2000 = 40 SCCM, but
        # this head has been run at 38 SCCM repeatedly and the RH it
        # produced sits on the measured curve, so 38 is what it actually
        # holds. Noted rather than assumed: at 38 SCCM the implied bubbler
        # efficiency is the lowest of the mid-range points (0.70 against a
        # 0.75 mean), which is consistent with slight under-delivery this
        # close to the limit -- the measured RH calibration absorbs it.
        verified_min_sccm=38.0,
    ),
}

# Cylinder feeding MFC2, as a mole fraction. 1.0 = pure CO2.
# Mole fraction of CO2 in the cylinder feeding MFC2.
#   1.0    = pure CO2  (the dry-gas runs up to October)
#   0.01   = 1 % / 10000 ppm in N2 (the humidity plan)
# The GUI exposes this, because switching cylinders changes every set point
# by a factor of a hundred and a stale value here would be silent.
CYLINDER_CO2_FRACTION = 1.0

# Default total flow for the low-flow regime you are targeting.
SCRIPT_VERSION = "2026-11-04.19"   # bump when this file changes


# ---- Running from Spyder --------------------------------------------------
# Spyder's F5 / %runfile does not pass command-line arguments to the script,
# so it always lands on the usage screen. Edit this list instead: whatever you
# put here is used when no arguments are given on the command line.
#
#     DEFAULT_ARGS = ["--scan"]
#     DEFAULT_ARGS = ["--table", "--total-sccm", "190"]
#     DEFAULT_ARGS = ["--test-channel", "1", "--test-flow", "100"]
#
# "--run" still has to be explicit, here or on the command line, before
# anything reaches the hardware.
DEFAULT_ARGS: List[str] = ["--check"]


# Operating point from the ADM01 plan: 190 SCCM total, kept constant while
# the CO2 fraction is stepped.
DEFAULT_TOTAL_SCCM = 190.0

# ---- ADM01 cell and dead volume ------------------------------------------
# The ADM01 sample volume is about 7 cm3. Tubing and manifold between the
# mixing junction and the cell add dead volume that must also be swept; a
# rough measure of your actual line length is worth more than this guess.
CELL_VOLUME_CM3 = 7.0
DEAD_VOLUME_CM3 = 15.0          # tubing + manifold, ESTIMATE -- measure yours
VOLUME_EXCHANGES = 10           # exchanges to wait before recording

# Register names vary across device families and firmware. Each list is tried
# in order and the first that answers wins. Run --registers to see what your
# devices actually expose, then put the right name first.
NAME_KEYS = ("INFO_DEVICE_NAME",)                 # confirmed present
SERIAL_KEYS = ("INFO_SERIAL",)                    # confirmed present
TYPECODE_KEYS = ("INFO_TYPECODE_1", "INFO_TYPECODE_1_SHORT")
FULLSCALE_KEYS = ("RANGE_VALUE",)                 # confirmed present
FLOW_KEYS = ("FLOW_VALUE",)                       # confirmed present
GAS_KEYS = ("GAS_NAME",)                          # confirmed present
UNIT_KEYS = ("FLOW_UNIT_NAME",)                   # confirmed present
TEMP_KEYS = ("TEMP_VALUE",)

# The unit the device reports flow in. Every SCCM conversion in this module
# assumes ln/min. If a device is configured for mln/min, %, or anything else,
# the setpoints written here are wrong by a factor of 1000 or more, so the
# unit is read on connect and checked rather than assumed.
EXPECTED_FLOW_UNIT = "ln/min"

# Pressure registers, from the Voegtlin smart 6.0.0 register map
# (https://api.voegtlin.com/doc/regs_smart_v600.html). Names are the
# documented ones, not guesses.
#
#   PRESSURE_VALUE               R   measured pressure
#   PRESSURE_SCALE_MIN / _MAX    R   range of the fitted pressure sensor
#   PRESSURE_SETPOINT            RW  pressure setpoint
#   PRESSURE_CONTROL_MODE        RW  flow control vs pressure control
#   PRESSURE_CONTROL_TYPE        RW  control arrangement
#   PRESSURE_CONTROL_FLOW_LIMIT  RW  max flow allowed while holding pressure
#   PRESSURE_CONTROL_UNIT_NAME   R   unit of the pressure values
#
# INLET_PRESSURE / OUTLET_PRESSURE (0x0958/0x095A) are deliberately NOT used
# as measurements: they sit among the calibration constants (next to
# CALIBRATION_TEMP and KN_SCCM) and describe calibration conditions.
PRESSURE_REG = {
    "value":      "PRESSURE_VALUE",
    "scale_min":  "PRESSURE_SCALE_MIN",
    "scale_max":  "PRESSURE_SCALE_MAX",
    "setpoint":   "PRESSURE_SETPOINT",
    "mode":       "PRESSURE_CONTROL_MODE",
    "type":       "PRESSURE_CONTROL_TYPE",
    "flow_limit": "PRESSURE_CONTROL_FLOW_LIMIT",
    "unit":       "PRESSURE_CONTROL_UNIT_NAME",
    "unit_alt":   "PRESSURE_UNIT_NAME",
}

# The register map documents PRESSURE_CONTROL_MODE as a U16 but not what
# each value means. Record the meanings here once you have learned them
# (see --pressure-info), e.g. {0: "flow control", 1: "pressure control"}.
# Until then the GUI shows raw numbers and asks for confirmation.
PRESSURE_MODE_NAMES: Dict[int, str] = {}

SETTLE_TIME = 0.1
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mfc_logs")
# Register database location.
#
# viapi resolves REPOSITORY by looking for "db/index.json" INSIDE the path you
# give it. Pointing it at a folder with no db/ subfolder produces
#     OSError: No such file or directory: '...\db\index.json'
# which is exactly what the lab-inherited setting did on any machine but the
# lab PC, where that folder happened to hold the database.
#
# None means viapi fetches definitions online as needed -- the right default.
# For offline use at the bench, run once:
#     python mfc_control.py --update-db
# which stores the database inside the viapi package itself, so this can stay
# None and still work with no network.
#
# Set this only if you keep your own copy: a folder containing db/, or a
# db.zip archive.
VIAPI_REPOSITORY: Optional[str] = None


# ==========================================================================

VIAPI_IMPORT_ERROR: Optional[str] = None
try:
    import viapi
    VIAPI_AVAILABLE = True
except Exception as _exc:                                          # noqa: BLE001
    # Deliberately broad: viapi can fail on a missing DLL or a bad driver
    # install, not only on a missing package. Keeping the real message makes
    # the difference between "not installed" and "installed but broken".
    viapi = None
    VIAPI_AVAILABLE = False
    VIAPI_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def pressure_report(dry_run: bool = False) -> int:
    """
    Read-only survey of pressure capability on every unit.

    Also the way to learn the PRESSURE_CONTROL_MODE numbers: change the mode
    in get red-y, close it, run this, and note which raw value appears.
    """
    for key, cfg in CHANNELS.items():
        probe = MFC(cfg, dry_run=dry_run)
        try:
            probe.connect()
            info = probe.pressure_info()
            print(f"\n{cfg.label} (addr {cfg.slave})")
            for field in ("has_sensor", "value", "unit", "scale_min",
                          "scale_max", "setpoint", "mode", "type",
                          "flow_limit"):
                value = info.get(field)
                note = ""
                if field == "mode" and value is not None:
                    note = f"   ({PRESSURE_MODE_NAMES.get(int(value), 'meaning unknown')})"
                print(f"    {field:12s}: {value}{note}")
        except Exception as exc:                                   # noqa: BLE001
            print(f"{cfg.label}: {exc}")
        finally:
            probe.disconnect()
    print("""
has_sensor False -> that unit cannot measure or control pressure.
To learn the mode numbers: set a unit to pressure control in get red-y,
close get red-y, run --pressure-info, and record the value of 'mode' in
PRESSURE_MODE_NAMES. Then set it back to flow control and record that too.
""")
    return 0


def gas_info(dry_run: bool = False) -> int:
    """
    Report each device's gas setting and whether it can be changed.

    This decides how to handle the Air-vs-CO2 calibration problem. If
    GAS_NAME is writable, or a gas lookup table is present, the device can
    convert internally and k_factor stays 1.0. If not, the correction has to
    be applied in software or the concentration will be systematically off.
    """
    for key in ("n2", "co2"):
        cfg = CHANNELS[key]
        probe = MFC(cfg, dry_run=dry_run)
        try:
            probe.connect()
            gas = probe.read_any(GAS_KEYS)
            print(f"\n{cfg.label} (addr {cfg.slave}) -- plumbed gas: {cfg.gas}")
            print(f"    device gas setting : {gas}")

            if probe._device is not None:
                for register in ("GAS_NAME", "GAS_NAME_X", "LUT_INFO"):
                    try:
                        exists = probe._device.exists(register)
                        writable = (probe._device.writable(register)
                                    if exists else False)
                        print(f"    {register:<12} exists={exists} "
                              f"writable={writable}")
                    except Exception as exc:                       # noqa: BLE001
                        print(f"    {register:<12} check failed: {exc}")

                # A populated gas table means the device supports multi-gas.
                try:
                    table = probe._device["GAS_NAME_X"]
                    print(f"    available gases    : {table}")
                except Exception:                                  # noqa: BLE001
                    print("    available gases    : not readable "
                          "(likely a single-gas unit)")
        except Exception as exc:                                   # noqa: BLE001
            print(f"{cfg.label}: {exc}")
        finally:
            probe.disconnect()

    print("""
If the CO2 head can be set to CO2, do that in get red-y and leave k_factor
at 1.0 -- the device then corrects internally and its reading is the truth.
If it is a single-gas Air unit, get the Air->CO2 conversion factor for this
model from Voegtlin and put it in CHANNELS. Uncorrected, an Air-calibrated
head delivers roughly 26 % less CO2 than it indicates, which lands straight
in your calibration slope.
""")
    return 0


def list_registers(filter_text: str = "") -> int:
    """
    List the register names this viapi install knows for smart 6.0, read
    straight out of the local db.zip. Use it to find the real name of a
    register when a guess returns None.
    """
    if not VIAPI_AVAILABLE:
        print(f"viapi not importable ({VIAPI_IMPORT_ERROR}).")
        return 1

    import json
    import zipfile

    db_path = os.path.join(os.path.dirname(viapi.__file__), "db.zip")
    if not os.path.isfile(db_path):
        print(f"No local database at {db_path}. Run --update-db first.")
        return 1

    def harvest(node, found):
        """Register names appear as dict keys or 'name' values; take both."""
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.isupper() and "_" in key:
                    found.add(key)
                if key == "name" and isinstance(value, str) and value.isupper():
                    found.add(value)
                harvest(value, found)
        elif isinstance(node, list):
            for item in node:
                harvest(item, found)

    found = set()
    with zipfile.ZipFile(db_path) as archive:
        for entry in archive.namelist():
            if not entry.endswith(".json"):
                continue
            if "/smart/" not in entry and "/vi-legacy/" not in entry:
                continue
            try:
                harvest(json.loads(archive.read(entry).decode("utf-8")), found)
            except Exception:                                      # noqa: BLE001
                continue

    # Comma-separated filters, so several guesses cost one run instead of
    # one round trip each.
    needles = [f.strip().upper() for f in filter_text.split(",") if f.strip()]
    if not needles:
        needles = [""]

    for needle in needles:
        names = sorted(n for n in found if needle in n)
        print(f"\n{len(names)} register(s) matching '{needle or '*'}':")
        for name in names:
            print(f"  {name}")
        if not names:
            print("  (nothing matched)")
    return 0


def _resolve_repository() -> Optional[str]:
    """
    Return a usable REPOSITORY path, or None to fetch definitions online.

    Validated rather than assumed: a path without db/index.json inside it
    makes viapi fail at the first register lookup, well after the connection
    appears to have succeeded, which is a confusing place to debug.
    """
    if not VIAPI_REPOSITORY:
        return None
    path = os.path.realpath(VIAPI_REPOSITORY)
    if path.lower().endswith(".zip") and os.path.isfile(path):
        return path
    if os.path.isfile(os.path.join(path, "db", "index.json")):
        return path
    print(f"  note: VIAPI_REPOSITORY={path} has no db/index.json inside it; "
          f"falling back to online definitions", file=sys.stderr)
    return None


def update_database() -> int:
    """Download the register database into the viapi package for offline use."""
    if not VIAPI_AVAILABLE:
        print(f"viapi not importable ({VIAPI_IMPORT_ERROR}). Install it first.")
        return 1
    print("Downloading Voegtlin register database...")
    viapi.Updater.update_database()
    print("Done. Register definitions are now available offline.")
    return 0


def environment_report() -> bool:
    """
    Diagnose the runtime before touching hardware.

    Returns True if the environment can talk to the MFCs.
    """
    print("Environment check")
    print("-" * 60)
    print(f"  script ver  : {SCRIPT_VERSION}")
    print(f"  python      : {sys.version.split()[0]}")
    print(f"  interpreter : {sys.executable}")
    print(f"  working dir : {os.getcwd()}")
    print(f"  script      : {os.path.abspath(__file__)}")

    if VIAPI_AVAILABLE:
        version = getattr(viapi, "__version__", "unknown")
        print(f"  viapi       : available (version {version})")
    else:
        print(f"  viapi       : NOT AVAILABLE -- {VIAPI_IMPORT_ERROR}")

    repo = _resolve_repository()
    if repo:
        print(f"  register db : {repo}")
    else:
        # REPOSITORY=None does not mean "online": viapi first looks for a
        # db.zip cached inside its own package, which --update-db creates.
        pkg_db = (os.path.join(os.path.dirname(viapi.__file__), "db.zip")
                  if VIAPI_AVAILABLE else "")
        if pkg_db and os.path.isfile(pkg_db):
            size_kb = os.path.getsize(pkg_db) / 1024
            print(f"  register db : cached in viapi package "
                  f"({size_kb:.0f} kB) -- works offline")
        else:
            print("  register db : none cached, will fetch online "
                  "(run --update-db)")

    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        if ports:
            print("  serial ports:")
            for port in ports:
                print(f"      {port.device}  {port.description}")
        else:
            print("  serial ports: none detected")
    except Exception:                                              # noqa: BLE001
        print("  serial ports: pyserial not available, cannot list")

    if not VIAPI_AVAILABLE:
        print("""
viapi could not be imported. The usual causes, in order of likelihood:

1. It is installed, but not in THIS interpreter.
   Spyder often runs a different environment than the one you installed into.
   Check the interpreter path printed above against where viapi lives:
       python -c "import viapi, sys; print(sys.executable, viapi.__file__)"
   In Spyder: Tools > Preferences > Python interpreter, and point it at the
   environment that has viapi. This is the most common cause when the same
   script worked on the lab PC.

2. It is not installed on this machine at all.
   The old scripts ran under a different Windows user (TUM IWC); this one is
   running from C:/Users/Admin. Install per Voegtlin's instructions at
   https://api.voegtlin.com/install.html -- use their documented command
   rather than a plain pip install, since the package is served from their
   own index.

3. It is installed but broken (missing DLL, driver, or dependency).
   The error text above says which, if so.

Until then, every non-hardware feature still works:
    --check --table --feasibility --advice --dry-run
""")
    return VIAPI_AVAILABLE


class MFC:
    """One mass flow controller. All public flows are in SCCM."""

    def __init__(self, cfg: ChannelConfig, dry_run: bool = False,
                 verbose: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        self._device = None
        self._setpoint_sccm = 0.0

    def __repr__(self) -> str:
        return f"<{self.cfg.label} addr={self.cfg.slave}>"

    # -- limits ------------------------------------------------------------

    @property
    def full_scale_sccm(self) -> float:
        return ln_to_sccm(self.cfg.full_scale_ln)

    @property
    def min_flow_sccm(self) -> float:
        """Lowest flow the device can hold to spec."""
        if self.cfg.verified_min_sccm is not None:
            return self.cfg.verified_min_sccm
        return self.turndown * self.full_scale_sccm * self.cfg.k_factor

    @property
    def turndown(self) -> float:
        """Smallest usable flow as a fraction of full scale, for THIS head."""
        v = self.cfg.turndown_fraction
        return MIN_CONTROLLABLE_FRACTION if v is None else v

    @property
    def floor_is_measured(self) -> bool:
        return self.cfg.verified_min_sccm is not None

    @property
    def max_flow_sccm(self) -> float:
        return self.full_scale_sccm * self.cfg.k_factor

    def uncertainty_sccm(self, flow_sccm: float) -> float:
        """Absolute flow uncertainty at a given setpoint."""
        fs = (ACCURACY_FRACTION_OF_FS if self.cfg.accuracy_fs is None
              else self.cfg.accuracy_fs)
        rdg = (ACCURACY_FRACTION_OF_READING if self.cfg.accuracy_rdg is None
               else self.cfg.accuracy_rdg)
        return fs * self.full_scale_sccm + rdg * abs(flow_sccm)

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> "MFC":
        if self.dry_run:
            print(f"[dry-run] {self.cfg.label}: would connect COM{PORT} "
                  f"addr {self.cfg.slave}")
            return self
        if not VIAPI_AVAILABLE:
            raise RuntimeError(
                f"viapi could not be imported ({VIAPI_IMPORT_ERROR}). "
                f"Run 'python mfc_control.py --check' for a diagnosis, or "
                f"'--dry-run' to rehearse without hardware."
            )

        self._device = viapi.Instrument(DEVICE_FAMILY, DEVICE_VERSION)
        self._device.verbose = self.verbose
        self._device.connect(viapi.IO(port=PORT, slave=self.cfg.slave,
                                      baudrate=BAUDRATE, parity=PARITY))
        self._verify_identity()
        return self

    def _verify_identity(self) -> None:
        """Serial numbers cannot be reassigned; bus addresses can."""
        serial = self.read_any(SERIAL_KEYS)
        name = self.read_any(NAME_KEYS)
        fullscale = self.read_any(FULLSCALE_KEYS)
        typecode = self.read_any(TYPECODE_KEYS)
        gas = self.read_any(GAS_KEYS)
        unit = self.read_any(UNIT_KEYS)

        print(f"{self.cfg.label}: addr {self.cfg.slave}, tag '{name}', "
              f"SN {serial}, type {typecode}")
        print(f"    full scale {fullscale} {unit}, device gas '{gas}'")

        if unit is not None and str(unit).strip().lower() != EXPECTED_FLOW_UNIT:
            print(f"  !! UNIT MISMATCH: device reports flow in '{unit}', but "
                  f"this script assumes '{EXPECTED_FLOW_UNIT}'. Every setpoint "
                  f"would be wrong. Change the unit in get red-y, or adjust "
                  f"EXPECTED_FLOW_UNIT and the SCCM conversion.", file=sys.stderr)

        # full_scale_ln <= 0 means "unknown", used by the scan probes, so
        # there is nothing to compare against and no warning to give.
        if fullscale is not None and self.cfg.full_scale_ln > 0:
            try:
                if abs(float(fullscale) - self.cfg.full_scale_ln) > 1e-6:
                    print(f"  !! FULL SCALE MISMATCH on {self.cfg.label}: "
                          f"config says {self.cfg.full_scale_ln} ln/min, "
                          f"device reports {fullscale} ln/min.\n"
                          f"     Flow limits, the turndown floor and every "
                          f"uncertainty figure are computed from this.\n"
                          f"     Fix it in CHANNELS: "
                          f"full_scale_ln={float(fullscale)}",
                          file=sys.stderr)
            except (TypeError, ValueError):
                pass

        if self.cfg.expected_serial is not None and serial is not None:
            try:
                if int(serial) != int(self.cfg.expected_serial):
                    print(f"  !! SERIAL MISMATCH: expected "
                          f"{self.cfg.expected_serial}, found {serial}. Addresses "
                          f"may have been reassigned -- run --scan and fix "
                          f"CHANNELS before measuring.", file=sys.stderr)
            except (TypeError, ValueError):
                pass

    def disconnect(self) -> None:
        if self.dry_run or self._device is None:
            return
        try:
            self._device.disconnect()
        except Exception as exc:                                   # noqa: BLE001
            print(f"{self.cfg.label}: disconnect failed -- {exc}", file=sys.stderr)
        finally:
            self._device = None

    # -- reads -------------------------------------------------------------

    def read(self, key: str):
        if self.dry_run or self._device is None:
            return None
        try:
            return self._device[key]
        except Exception:                                          # noqa: BLE001
            return None

    def read_any(self, keys: Sequence[str]):
        """
        Try several register names and return the first that answers.

        Register names differ between device families and firmware versions,
        so a single hard-coded name silently yields None on a device that
        calls it something else. --registers lists what this device actually
        has.
        """
        for key in keys:
            value = self.read(key)
            if value is not None:
                return value
        return None

    def read_flow_sccm(self) -> Optional[float]:
        if self.dry_run:
            return self._setpoint_sccm
        value = self.read_any(FLOW_KEYS)
        return ln_to_sccm(float(value)) if value is not None else None

    # -- pressure -----------------------------------------------------------

    def pressure_info(self) -> Dict[str, object]:
        """
        Everything the device reports about pressure, in one read.

        has_sensor comes from PRESSURE_SCALE_MIN/MAX rather than from the
        value itself: a unit with no sensor fitted reports a range of 0-0,
        and its PRESSURE_VALUE of 0 is then not a measurement.
        """
        if self.dry_run:
            return {"has_sensor": False}
        info: Dict[str, object] = {}
        for key, reg in PRESSURE_REG.items():
            info[key] = self.read(reg)
        if not info.get("unit"):
            info["unit"] = info.get("unit_alt")
        try:
            lo = float(info.get("scale_min") or 0.0)
            hi = float(info.get("scale_max") or 0.0)
            info["has_sensor"] = hi > lo
        except (TypeError, ValueError):
            info["has_sensor"] = False
        return info

    def read_pressure(self) -> Optional[float]:
        value = self.read(PRESSURE_REG["value"])
        return None if value is None else float(value)

    def read_pressure_unit(self) -> Optional[str]:
        value = self.read(PRESSURE_REG["unit"]) or self.read(PRESSURE_REG["unit_alt"])
        return None if value is None else str(value)

    def _write_verified(self, reg: str, value, tol: float = 1e-6):
        """Write a register and read it back. A write that did not take raises."""
        if self.dry_run:
            print(f"[dry-run] {self.cfg.label}: {reg} = {value}")
            return value
        self._device[reg] = value
        time.sleep(SETTLE_TIME)
        back = self.read(reg)
        try:
            if back is None or abs(float(back) - float(value)) > tol:
                raise RuntimeError(
                    f"{self.cfg.label}: {reg} did not take -- wrote {value}, "
                    f"device reports {back}")
        except (TypeError, ValueError):
            raise RuntimeError(f"{self.cfg.label}: {reg} read back as {back!r}")
        return back

    def write_pressure_setpoint(self, value: float) -> float:
        """
        Set the pressure setpoint.

        Refused unless a sensor is fitted and the value lies inside the
        sensor's own range -- a setpoint outside what the sensor can measure
        can never be reached, and the controller would drive its valve to a
        limit trying.
        """
        info = self.pressure_info()
        if not info.get("has_sensor"):
            raise RuntimeError(
                f"{self.cfg.label}: no pressure sensor fitted "
                f"(PRESSURE_SCALE {info.get('scale_min')}-"
                f"{info.get('scale_max')}); a pressure setpoint would "
                f"control nothing")
        lo, hi = float(info["scale_min"]), float(info["scale_max"])
        if not (lo <= value <= hi):
            raise RuntimeError(
                f"{self.cfg.label}: {value} outside the sensor range "
                f"{lo}-{hi} {info.get('unit') or ''}")
        return self._write_verified(PRESSURE_REG["setpoint"], float(value),
                                    tol=1e-4)

    def write_pressure_flow_limit(self, sccm: float) -> float:
        """Maximum flow the controller may use while holding pressure."""
        if not (0.0 <= sccm <= self.full_scale_sccm):
            raise RuntimeError(
                f"{self.cfg.label}: flow limit {sccm} SCCM outside "
                f"0-{self.full_scale_sccm:.0f} SCCM")
        return self._write_verified(PRESSURE_REG["flow_limit"],
                                    sccm_to_ln(sccm), tol=1e-5)

    def write_pressure_mode(self, mode: int) -> int:
        """
        Switch between flow control and pressure control.

        Raw U16, because the register map does not document the meanings.
        Consequence worth stating: a unit in pressure-control mode stops
        regulating flow. On MFC1 or MFC2 that breaks the CO2/N2 blend.
        """
        if not isinstance(mode, int) or not (0 <= mode <= 0xFFFF):
            raise RuntimeError("mode must be an integer 0-65535")
        return int(self._write_verified(PRESSURE_REG["mode"], mode, tol=0.5))

    def read_temperature(self) -> Optional[float]:
        value = self.read_any(TEMP_KEYS)
        return float(value) if value is not None else None

    # -- control -----------------------------------------------------------

    @property
    def setpoint_sccm(self) -> float:
        return self._setpoint_sccm

    def set_flow_sccm(self, true_flow_sccm: float) -> float:
        """Request a true flow of the plumbed gas, in SCCM."""
        if self.cfg.read_only:
            raise PermissionError(
                f"{self.cfg.label} is configured as a downstream monitor. "
                f"Writing a setpoint would put a second regulator in series "
                f"with the upstream MFCs, and a zero setpoint would close the "
                f"line entirely. Set read_only=False in CHANNELS only if you "
                f"have confirmed this device is not regulating the stream."
            )
        true_flow_sccm = max(0.0, true_flow_sccm)
        indicated = true_flow_sccm / self.cfg.k_factor if self.cfg.k_factor else true_flow_sccm
        indicated = self._check_limits(indicated)
        self._setpoint_sccm = indicated * self.cfg.k_factor

        if self.dry_run:
            print(f"[dry-run] {self.cfg.label}: SETPOINT = {indicated:7.2f} SCCM "
                  f"({sccm_to_ln(indicated):.4f} ln/min)")
        else:
            self._device["SETPOINT"] = sccm_to_ln(indicated)
            time.sleep(SETTLE_TIME)
        return self._setpoint_sccm

    def close_valve(self) -> None:
        self.set_flow_sccm(0.0)

    def _check_limits(self, indicated_sccm: float) -> float:
        if indicated_sccm > self.full_scale_sccm:
            print(f"{self.cfg.label}: {indicated_sccm:.1f} SCCM exceeds full scale "
                  f"{self.full_scale_sccm:.0f}, clamped")
            return self.full_scale_sccm
        # min_flow_sccm, not the raw turndown: a floor verified on this bench
        # overrides the grade letter, and the two must not disagree between
        # the plan check and the write path.
        floor = self.min_flow_sccm
        source = ("measured" if self.floor_is_measured
                  else f"{self.turndown:.0%} of FS")
        if 0 < indicated_sccm < floor:
            print(f"{self.cfg.label}: WARNING {indicated_sccm:.2f} SCCM is below the "
                  f"{floor:.0f} SCCM floor ({source}) "
                  f"-- delivered flow is not trustworthy")
        return indicated_sccm


# ==========================================================================
# Bench
# ==========================================================================

@dataclass
class Step:
    """One point in a sequence. Give exactly one of ppm= or percent=."""
    duration: float
    ppm: Optional[float] = None
    percent: Optional[float] = None
    total_sccm: float = DEFAULT_TOTAL_SCCM
    tag: str = ""

    @property
    def fraction(self) -> float:
        if (self.ppm is None) == (self.percent is None):
            raise ValueError("Step needs exactly one of ppm= or percent=")
        return self.ppm * 1e-6 if self.ppm is not None else self.percent / 100.0


class GasBench:
    """
    Owns the MFCs. As a context manager it guarantees every setpoint returns
    to zero and every serial link closes, including on exception or Ctrl-C.
    """

    def __init__(self, dry_run: bool = False, verbose: bool = False,
                 cylinder_fraction: float = CYLINDER_CO2_FRACTION,
                 use_monitor: bool = False):
        self.dry_run = dry_run
        self.cylinder_fraction = cylinder_fraction
        self.use_monitor = use_monitor
        self.n2 = MFC(CHANNELS["n2"], dry_run, verbose)
        self.co2 = MFC(CHANNELS["co2"], dry_run, verbose)
        self.monitor = MFC(CHANNELS["monitor"], dry_run, verbose)
        self._log: List[dict] = []

    @property
    def controlled(self) -> Tuple[MFC, ...]:
        """
        Channels this bench writes setpoints to.

        Derived from each channel's own read_only flag rather than hard-coded.
        When MFC3 was switched from monitor to controllable, a literal
        (n2, co2) here would have left stop() silently skipping it -- the
        class of bug where a config change and a safety path drift apart.
        """
        return tuple(m for m in self.all_channels if not m.cfg.read_only)

    @property
    def all_channels(self) -> Tuple[MFC, ...]:
        return (self.n2, self.co2, self.monitor)

    @property
    def channels(self) -> Tuple[MFC, ...]:
        """Everything the bench connects to."""
        return tuple(m for m in self.all_channels
                     if not m.cfg.read_only or self.use_monitor)

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "GasBench":
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if not self.dry_run and VIAPI_AVAILABLE:
            viapi.Instrument.REPOSITORY = _resolve_repository()
            viapi.Logger.redirect(open(os.path.join(OUTPUT_DIR, "viapi.log"), "a"))
        for mfc in self.channels:
            mfc.connect()
        self._warn_uncorrected_gases()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        print("\nShutting down: zeroing all setpoints.")
        for mfc in self.controlled:
            try:
                mfc.close_valve()
            except Exception as err:                               # noqa: BLE001
                print(f"{mfc.cfg.label}: could not zero -- {err}", file=sys.stderr)
        for mfc in self.channels:
            mfc.disconnect()
        if self._log:
            self.write_log()
        return False

    def _warn_uncorrected_gases(self) -> None:
        for mfc in self.channels:
            cfg = mfc.cfg
            if cfg.gas != cfg.device_gas and cfg.k_factor == 1.0:
                print(f"\n  WARNING {cfg.label}: {cfg.gas} through a head calibrated "
                      f"for {cfg.device_gas}, k_factor still 1.0. Delivered flow "
                      f"will differ from setpoint.\n", file=sys.stderr)

    # -- blending ----------------------------------------------------------

    def split(self, fraction: float, total_sccm: float) -> Dict[str, float]:
        """Flows in SCCM for a target CO2 mole fraction. No hardware access."""
        if fraction > self.cylinder_fraction:
            raise ValueError(f"target {fraction * 100:.3f} % exceeds cylinder "
                             f"({self.cylinder_fraction * 100:.1f} %)")
        q_co2 = total_sccm * fraction / self.cylinder_fraction
        return {"n2": total_sccm - q_co2, "co2": q_co2}

    def concentration_uncertainty(self, fraction: float,
                                  total_sccm: float) -> float:
        """
        Propagated 1-sigma uncertainty on the delivered mole fraction.

        c = q_co2 / (q_co2 + q_n2), so
        u_c = sqrt((q_n2 u_co2)^2 + (q_co2 u_n2)^2) / Q^2
        """
        flows = self.split(fraction, total_sccm)
        q_co2, q_n2 = flows["co2"], flows["n2"]
        total = q_co2 + q_n2
        if total <= 0:
            return 0.0
        u_co2 = self.co2.uncertainty_sccm(q_co2)
        u_n2 = self.n2.uncertainty_sccm(q_n2)
        return math.sqrt((q_n2 * u_co2) ** 2 + (q_co2 * u_n2) ** 2) / total ** 2

    def split3(self, co2_fraction: float, wet_fraction: float,
               total_sccm: float) -> Dict[str, float]:
        """
        Three-line blend: CO2 + humid N2 + dry N2, total held constant.

        co2_fraction is the CO2 mole fraction wanted IN THE CELL, so the CO2
        line carries more flow than that when the cylinder is dilute.
        wet_fraction is the share of total flow taken from the bubbler line,
        which is what sets the water content.

        Dry N2 is the remainder and takes up the slack, exactly as the
        experiment plan has it. A negative remainder means the two demands
        together exceed the total flow.
        """
        if co2_fraction > self.cylinder_fraction:
            raise ValueError(
                f"CO2 target {co2_fraction * 1e6:.0f} ppm exceeds the cylinder "
                f"({self.cylinder_fraction * 1e6:.0f} ppm)")
        if not 0.0 <= wet_fraction <= 1.0:
            raise ValueError(f"wet fraction {wet_fraction} outside 0..1")

        q_co2 = total_sccm * co2_fraction / self.cylinder_fraction
        q_wet = total_sccm * wet_fraction
        q_dry = total_sccm - q_co2 - q_wet
        if q_dry < -1e-9:
            raise ValueError(
                f"CO2 ({q_co2:.1f}) + wet ({q_wet:.1f}) = "
                f"{q_co2 + q_wet:.1f} SCCM exceeds the {total_sccm:.0f} SCCM "
                f"total -- lower the CO2 target or the wet fraction")
        return {"co2": q_co2, "monitor": q_wet, "n2": max(0.0, q_dry)}

    def check3(self, flows: Dict[str, float]) -> List[str]:
        """
        Which lines in a three-gas blend fall below their own floor.

        Each MFC is either fully closed or at/above its minimum -- a set
        point in between is accepted by the device and delivered wrongly,
        which is the failure mode this catches before the run rather than
        after it.
        """
        problems = []
        for key, mfc in (("n2", self.n2), ("co2", self.co2),
                         ("monitor", self.monitor)):
            q = flows.get(key, 0.0)
            floor = mfc.min_flow_sccm
            if 0 < q < floor:
                source = ("measured on this bench" if mfc.floor_is_measured
                          else f"{mfc.turndown:.0%} of "
                               f"{mfc.full_scale_sccm:.0f}")
                problems.append(
                    f"{mfc.cfg.label} at {q:.1f} SCCM is below its "
                    f"{floor:.0f} SCCM floor ({source})")
        return problems

    def set_concentration(self, ppm: Optional[float] = None,
                          percent: Optional[float] = None,
                          total_sccm: float = DEFAULT_TOTAL_SCCM
                          ) -> Dict[str, float]:
        if (ppm is None) == (percent is None):
            raise ValueError("give exactly one of ppm= or percent=")
        fraction = ppm * 1e-6 if ppm is not None else percent / 100.0
        flows = self.split(fraction, total_sccm)
        # Balance gas first, analyte last: the cell never sees a rich slug.
        self.n2.set_flow_sccm(flows["n2"])
        self.co2.set_flow_sccm(flows["co2"])
        return flows

    def flush(self, total_sccm: float = DEFAULT_TOTAL_SCCM) -> None:
        """Pure N2, CO2 closed."""
        self.co2.set_flow_sccm(0.0)
        self.n2.set_flow_sccm(total_sccm)

    def stop(self) -> None:
        for mfc in self.controlled:
            mfc.close_valve()

    # -- feasibility -------------------------------------------------------

    def feasibility(self, levels_percent: Sequence[float],
                    total_sccm: float = DEFAULT_TOTAL_SCCM) -> None:
        """
        For each target concentration, print the required flows, whether both
        heads stay inside their usable band, and the resulting uncertainty.
        No hardware access -- safe to run at your desk.
        """
        floor_co2 = self.co2.min_flow_sccm
        floor_n2 = self.n2.min_flow_sccm

        print(f"\nTotal flow {total_sccm:.0f} SCCM "
              f"({sccm_to_ln(total_sccm):.3f} ln/min), "
              f"cylinder {self.cylinder_fraction * 100:.0f} % CO2")
        print(f"MFC1_N2  FS {self.n2.full_scale_sccm:.0f} SCCM, "
              f"usable above {floor_n2:.0f}")
        print(f"MFC2_CO2 FS {self.co2.full_scale_sccm:.0f} SCCM, "
              f"usable above {floor_co2:.0f}\n")

        header = (f"{'target':>9}  {'CO2 flow':>10}  {'N2 flow':>10}  "
                  f"{'% of FS':>8}  {'uncert.':>10}  status")
        print(header)
        print("-" * len(header))

        for level in levels_percent:
            fraction = level / 100.0
            flows = self.split(fraction, total_sccm)
            q_co2, q_n2 = flows["co2"], flows["n2"]
            pct_fs = q_co2 / self.co2.full_scale_sccm * 100
            uncertainty = self.concentration_uncertainty(fraction, total_sccm) * 100

            if q_co2 == 0:
                status = "zero point, fine"
            elif q_co2 < floor_co2:
                status = f"NOT REACHABLE, below {floor_co2:.0f} SCCM floor"
            elif q_n2 > 0 and q_n2 < floor_n2:
                status = f"N2 below {floor_n2:.0f} SCCM floor"
            elif uncertainty / max(level, 1e-9) > 0.05:
                status = (f"reachable, but +/-{uncertainty:.1f} pp "
                          f"= {uncertainty / level * 100:.0f} % relative")
            else:
                status = "ok"

            print(f"{level:>7.2f} %  {q_co2:>9.2f}   {q_n2:>9.2f}   "
                  f"{pct_fs:>7.2f}   {uncertainty:>8.3f} %  {status}")

        low = floor_co2 / total_sccm * self.cylinder_fraction * 100
        print(f"\nLowest reachable CO2 at this total flow: {low:.2f} %")
        print("Full-scale-referenced accuracy means relative error grows as flow")
        print("drops. If the numbers above are too coarse, see the notes in")
        print("--advice.")

    @staticmethod
    def advice() -> None:
        print("""
Getting honest low-flow performance out of 2 ln/min heads
---------------------------------------------------------
At 200 SCCM total you are at 10 % of full scale. The N2 side is fine there.
The CO2 side is the constraint, because its flow is only a fraction of the
total: at 200 SCCM total and 5 % CO2 you need 10 SCCM, which is 0.5 % of a
2000 SCCM full scale -- below any sensible turndown limit.

Options, roughly in order of how well they solve it:

1. Right-size the CO2 head. A 20-100 SCCM full-scale controller on the CO2
   line makes the whole low-flow range accurate. This is the real fix; the
   others are workarounds.

2. Use a premixed cylinder. Feeding e.g. 10 % CO2 in N2 instead of 100 %
   multiplies the CO2 flow by ten for the same delivered concentration,
   pulling it back into the usable band.

3. Run a higher total flow and split the stream. Blend at 1000-2000 SCCM
   where both heads are accurate, send 200 SCCM to the cell, and vent the
   rest through a tee. Costs gas, needs a back-pressure-stable split, but
   uses hardware you already have.

4. Two-stage dilution. Blend a rich mix in stage one, dilute it in stage two
   with the 10 ln/min head. Doubles the number of things that can drift.

Whichever you pick, the accuracy figure in CONFIG is a placeholder. Put the
real datasheet number for GSC-B4TA-BB23 in before quoting any uncertainty in
a report.
""")

    # -- cell exchange and verification ------------------------------------

    @staticmethod
    def residence_time_s(total_sccm: float,
                         volume_cm3: float = CELL_VOLUME_CM3 + DEAD_VOLUME_CM3
                         ) -> float:
        """
        V/Q for the swept volume, in seconds.

        At 190 SCCM the ADM01's own 7 cm3 gives about 2.2 s, but the tubing
        and manifold between the mixing junction and the cell are swept too,
        and they usually dominate. Ideal plug flow is optimistic; real lines
        mix, so treat this as a lower bound.
        """
        if total_sccm <= 0:
            return float("inf")
        return volume_cm3 / (total_sccm / 60.0)

    @classmethod
    def settle_time_s(cls, total_sccm: float,
                      exchanges: int = VOLUME_EXCHANGES) -> float:
        """How long to wait after a step before the reading means anything."""
        return exchanges * cls.residence_time_s(total_sccm)

    def verify_flows(self, tolerance: float = 0.05,
                     settle_s: Optional[float] = None) -> bool:
        """
        Compare measured flow against commanded flow after settling.

        The operating plan is explicit that you should record actual feedback
        rather than command values. This is that check: it returns False and
        prints the discrepancy if a channel has not reached its setpoint,
        which catches an empty cylinder, a closed valve, or a blocked line
        before you spend three minutes averaging noise.
        """
        if settle_s is None:
            settle_s = self.settle_time_s(
                sum(m.setpoint_sccm for m in self.controlled))
        time.sleep(min(settle_s, 60.0))

        ok = True
        for mfc in self.controlled:
            commanded = mfc.setpoint_sccm
            measured = mfc.read_flow_sccm()
            if measured is None or commanded == 0:
                continue
            if abs(measured - commanded) > tolerance * max(commanded, 1e-9):
                print(f"  !! {mfc.cfg.label}: commanded {commanded:.2f} SCCM, "
                      f"measured {measured:.2f} SCCM", file=sys.stderr)
                ok = False

        if self.use_monitor:
            total_cmd = sum(m.setpoint_sccm for m in self.controlled)
            total_meas = self.monitor.read_flow_sccm()
            if total_meas is not None:
                print(f"  monitor: sum of setpoints {total_cmd:.1f} SCCM, "
                      f"downstream reads {total_meas:.1f} SCCM "
                      f"({total_meas / self.monitor.full_scale_sccm * 100:.1f} % "
                      f"of its FS -- coarse)")
        return ok

    def setpoint_table(self, total_sccm: float = DEFAULT_TOTAL_SCCM,
                       start: int = 1, stop: int = 100) -> None:
        """
        Print the CO2/N2 set-point table at constant total flow, marking the
        rows where both heads stay above their controllable floor.
        """
        floor_co2 = self.co2.min_flow_sccm
        floor_n2 = self.n2.min_flow_sccm
        print(f"\nSet-point table, Q_total = {sccm_to_ln(total_sccm):.3f} ln/min "
              f"({total_sccm:.0f} SCCM), cylinder "
              f"{self.cylinder_fraction * 100:.0f} % CO2")
        print(f"'ok' means both heads are above their floor "
              f"(CO2 {floor_co2:.0f}, N2 {floor_n2:.0f} SCCM)\n")
        print(f"{'CO2 %':>6}  {'CO2 ln/min':>11}  {'N2 ln/min':>10}  "
              f"{'CO2 SCCM':>9}  {'N2 SCCM':>9}   flag")

        usable: List[int] = []
        for level in range(start, stop + 1):
            flows = self.split(level / 100.0, total_sccm)
            q_co2, q_n2 = flows["co2"], flows["n2"]
            good = (q_co2 == 0 or q_co2 >= floor_co2) and (q_n2 == 0 or q_n2 >= floor_n2)
            if good:
                usable.append(level)
            print(f"{level:>6}  {sccm_to_ln(q_co2):>11.4f}  {sccm_to_ln(q_n2):>10.4f}  "
                  f"{q_co2:>9.2f}  {q_n2:>9.2f}   {'ok' if good else 'below floor'}")

        if usable:
            print(f"\nUsable band: {min(usable)} % to {max(usable)} % CO2")
        print(f"Settle before recording: about "
              f"{self.settle_time_s(total_sccm):.0f} s "
              f"({VOLUME_EXCHANGES} exchanges of "
              f"{CELL_VOLUME_CM3 + DEAD_VOLUME_CM3:.0f} cm3 at {total_sccm:.0f} SCCM)")

    # -- sequencing --------------------------------------------------------

    def run_sequence(self, steps: List[Step], flush_between: float = 0.0,
                     flush_sccm: float = DEFAULT_TOTAL_SCCM) -> None:
        t_next = time.monotonic()

        for index, step in enumerate(steps, start=1):
            if flush_between > 0:
                self.flush(flush_sccm)
                self._record(index, "flush", 0.0, flush_sccm, step.tag)
                t_next += flush_between
                self._wait_until(t_next)

            flows = self.set_concentration(ppm=step.ppm, percent=step.percent,
                                           total_sccm=step.total_sccm)
            self.verify_flows()
            uncertainty = self.concentration_uncertainty(
                step.fraction, step.total_sccm) * 100
            print(f"[{index}/{len(steps)}] {step.fraction * 100:>7.3f} % CO2 "
                  f"+/- {uncertainty:.3f}   N2 {flows['n2']:>7.2f} + "
                  f"CO2 {flows['co2']:>7.2f} SCCM   hold {step.duration:.0f} s  "
                  f"{step.tag}")
            self._record(index, "measure", step.fraction, step.total_sccm, step.tag)
            t_next += step.duration
            self._wait_until(t_next)

        self.stop()
        print("Sequence complete.")

    @staticmethod
    def _wait_until(t_target: float) -> None:
        while True:
            remaining = t_target - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.2))

    # -- logging -----------------------------------------------------------

    def _record(self, index, phase, fraction, total_sccm, tag) -> None:
        self._log.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "step": index,
            "phase": phase,
            "target_percent": fraction * 100,
            "target_ppm": fraction * 1e6,
            "uncertainty_percent": self.concentration_uncertainty(
                fraction, total_sccm) * 100,
            "total_sccm": total_sccm,
            "sp_n2_sccm": self.n2.setpoint_sccm,
            "sp_co2_sccm": self.co2.setpoint_sccm,
            "meas_n2_sccm": self.n2.read_flow_sccm(),
            "meas_co2_sccm": self.co2.read_flow_sccm(),
            "temp_co2_c": self.co2.read_temperature(),
            "tag": tag,
        })

    def write_log(self) -> str:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        filename = os.path.join(
            OUTPUT_DIR, "mfc_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
        with open(filename, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self._log[0].keys()))
            writer.writeheader()
            writer.writerows(self._log)
        print(f"Step log written to {filename}")
        return filename


# ==========================================================================
# Commissioning helpers
# ==========================================================================

def scan_bus(addresses=(1, 2, 246, 247), dry_run: bool = False) -> None:
    print(f"Scanning COM{PORT} at {BAUDRATE} baud, parity {PARITY}\n")
    for address in addresses:
        probe = MFC(ChannelConfig(f"addr{address}", address, 0.0), dry_run=dry_run)
        try:
            probe.connect()
            print(f"    totalizer : {probe.read('TOTALIZER_VALUE')}")
            pressure = probe.read_pressure()
            unit = probe.read_pressure_unit()
            print(f"    pressure  : {pressure} {unit or ''}"
                  if pressure is not None else
                  "    pressure  : not exposed by this device")
        except Exception as exc:                                   # noqa: BLE001
            print(f"  addr {address:>3}: no response ({exc})")
        finally:
            probe.disconnect()


def test_channel(address: int, flow_sccm: float = 100.0, seconds: float = 10.0,
                 full_scale_ln: float = 2.0, dry_run: bool = False) -> None:
    """Open one address briefly to confirm which physical line it is."""
    probe = MFC(ChannelConfig(f"addr{address}", address, full_scale_ln),
                dry_run=dry_run)
    try:
        probe.connect()
        print(f"Opening addr {address} to {flow_sccm:.0f} SCCM for {seconds:.0f} s")
        probe.set_flow_sccm(flow_sccm)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            print(f"  measured: {probe.read_flow_sccm()} SCCM   ",
                  end="\r", flush=True)
            time.sleep(1.0)
        print()
    finally:
        try:
            probe.close_valve()
            print(f"addr {address} closed.")
        finally:
            probe.disconnect()


DEFAULT_LEVELS = [0, 2, 5, 10, 20, 30, 40, 50]


def build_sequence(levels_percent: Sequence[float] = DEFAULT_LEVELS,
                   hold: float = 180.0,
                   total_sccm: float = DEFAULT_TOTAL_SCCM) -> List[Step]:
    return [Step(duration=hold, percent=level, total_sccm=total_sccm,
                 tag=f"co2_{level}pct") for level in levels_percent]


def _parse_addresses(spec: str) -> List[int]:
    spec = spec.strip()
    if "-" in spec:
        low, high = spec.split("-", 1)
        return list(range(int(low), int(high) + 1))
    return [int(part) for part in spec.split(",") if part.strip()]


def _parse_levels(spec: str) -> List[float]:
    return [float(part) for part in spec.split(",") if part.strip()]


def _install_sigint_handler() -> None:
    def handler(signum, frame):                                    # noqa: ARG001
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, handler)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Low-flow N2/CO2 MFC control (MFC1=N2 addr1, MFC2=CO2 addr2)")
    parser.add_argument("--run", action="store_true",
                        help="REQUIRED to execute a live sequence on hardware")
    parser.add_argument("--pressure-info", action="store_true",
                        help="read every pressure register on all three units")
    parser.add_argument("--gas-info", action="store_true",
                        help="check whether the heads can be set to N2/CO2")
    parser.add_argument("--registers", type=str, nargs="?", const="",
                        default=None, metavar="FILTER",
                        help="list register names in the local database, "
                             "e.g. --registers SERIAL")
    parser.add_argument("--update-db", action="store_true",
                        help="download the register database for offline use")
    parser.add_argument("--check", action="store_true",
                        help="diagnose the Python environment, no hardware access")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--scan-range", type=str, default="")
    parser.add_argument("--feasibility", action="store_true",
                        help="check target concentrations against the hardware limits")
    parser.add_argument("--advice", action="store_true",
                        help="how to get accurate flows below 200 SCCM")
    parser.add_argument("--test-channel", type=int, default=None)
    parser.add_argument("--test-flow", type=float, default=100.0,
                        help="SCCM used by --test-channel")
    parser.add_argument("--test-seconds", type=float, default=10.0)
    parser.add_argument("--total-sccm", type=float, default=DEFAULT_TOTAL_SCCM)
    parser.add_argument("--levels", type=str, default="",
                        help="CO2 percentages, e.g. 0,5,10,20,40")
    parser.add_argument("--hold", type=float, default=180.0,
                        help="seconds per point, on top of settling")
    parser.add_argument("--flush", type=float, default=30.0)
    parser.add_argument("--monitor", action="store_true",
                        help="also read the downstream 10 ln/min total-flow monitor")
    parser.add_argument("--table", action="store_true",
                        help="print the 1-100 %% CO2 set-point table")
    # Spyder passes no argv; fall back to DEFAULT_ARGS so F5 does something
    # useful. A stray Spyder flag like --wdir is not ours, so ignore extras
    # rather than aborting with a confusing argparse error.
    argv = sys.argv[1:]
    argv = [a for a in argv if a != "--wdir"]
    if not argv:
        argv = DEFAULT_ARGS
        print(f"(no arguments given; using DEFAULT_ARGS = {argv})\n")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"(ignoring unrecognised arguments: {unknown})\n")

    _install_sigint_handler()
    levels = _parse_levels(args.levels) if args.levels else DEFAULT_LEVELS

    if args.check:
        environment_report()
        return 0

    if args.update_db:
        return update_database()

    if args.registers is not None:
        return list_registers(args.registers)

    if args.pressure_info:
        if VIAPI_AVAILABLE and not args.verbose:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            viapi.Logger.redirect(open(os.path.join(OUTPUT_DIR, "viapi.log"), "a"))
        return pressure_report(dry_run=args.dry_run)

    if args.gas_info:
        if VIAPI_AVAILABLE and not args.verbose:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            viapi.Logger.redirect(open(os.path.join(OUTPUT_DIR, "viapi.log"), "a"))
        return gas_info(dry_run=args.dry_run)

    # viapi logs register-database chatter to the terminal by default, which
    # buries the actual output. Send it to a file unless --verbose is asked.
    if VIAPI_AVAILABLE and not args.verbose:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        viapi.Logger.redirect(open(os.path.join(OUTPUT_DIR, "viapi.log"), "a"))

    if args.advice:
        GasBench.advice()
        return 0

    if args.table:
        GasBench(dry_run=True).setpoint_table(args.total_sccm)
        return 0

    if args.feasibility:
        GasBench(dry_run=True).feasibility(
            levels, args.total_sccm)
        return 0

    if args.scan or args.scan_range:
        addresses = (_parse_addresses(args.scan_range) if args.scan_range
                     else (1, 2, 246, 247))
        scan_bus(addresses, dry_run=args.dry_run)
        return 0

    if args.test_channel is not None:
        test_channel(args.test_channel, args.test_flow, args.test_seconds,
                     dry_run=args.dry_run)
        return 0

    # Opening gas valves should never be what happens when someone runs the
    # file with no arguments -- in Spyder, hitting F5 is one keystroke.
    if not (args.run or args.dry_run):
        print(__doc__)
        print(f"mfc_control version {SCRIPT_VERSION}")
        print("No action given. Nothing was sent to the hardware.\n")
        print("  --check          diagnose the Python environment")
        print("  --table          print the set-point table")
        print("  --feasibility    check targets against hardware limits")
        print("  --advice         notes on low-flow accuracy")
        print("  --scan           identify devices on the bus")
        print("  --dry-run        rehearse a sequence, no serial traffic")
        print("  --run            EXECUTE a live sequence on the MFCs\n")
        print("Example:")
        print("  python mfc_control.py --run --total-sccm 190 "
              "--levels 20,30,45,60 --hold 180\n")
        environment_report()
        return 0

    with GasBench(dry_run=args.dry_run, verbose=args.verbose,
                  use_monitor=args.monitor) as bench:
        steps = build_sequence(levels, args.hold, args.total_sccm)
        bench.run_sequence(steps, flush_between=args.flush,
                           flush_sccm=args.total_sccm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
