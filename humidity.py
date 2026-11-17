# -*- coding: utf-8 -*-
"""
humidity.py -- water-vapour maths for the three-line gas bench.

Formulas follow the experiment plan, which follows
Christensen et al., Molecules 26, 609 (2021), Eq. 4:

    e_s(t) = 611.6 Pa * exp(17.48 * t / (t + 240.73))          t in degC
    x_H2O(cell) = (wet flow / total flow) * eta * e_s(T_bubbler) / p
    RH(cell)    = x_H2O * p / e_s(T_cell)
    c(H2O)      = x_H2O * p / (R * T_cell)                     mol/m3

eta is the bubbler saturation efficiency: the fraction of saturation the
wet stream actually reaches. A single bubbler is typically 0.85-0.95, and
the plan assumes 0.9 -- but it is the one number here that is guessed
rather than measured. Measure it once (wet MFC only, read outlet RH) and
put the real value in, because every H2O figure scales linearly with it.

These are PLANNED values. The BME688 at the cell outlet measures what
actually arrived; analyse with the measured RH/T/p, not with these.

Self-check against the plan's own spreadsheet:
    python humidity.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

R_GAS = 8.314462618          # J/(mol K)
KELVIN = 273.15

# Condensation on the quartz fork would wreck a measurement and possibly the
# fork. The plan caps cell RH well below saturation.
RH_LIMIT = 0.70


def saturation_pressure_pa(t_celsius: float) -> float:
    """Magnus-type saturation vapour pressure, Pa."""
    import math
    return 611.6 * math.exp(17.48 * t_celsius / (t_celsius + 240.73))


@dataclass
class HumidityConditions:
    """Everything the water calculation needs that is not a flow."""
    bubbler_c: float = 20.0        # bubbler / room temperature
    cell_c: float = 20.0           # ADM01 cell temperature
    pressure_pa: float = 101325.0  # 1013.25 hPa

    # Bubbler saturation efficiency. 0.748 is a least-squares fit to the
    # bench's own RH-vs-wet-flow curve at 20 degC (9 points, residual sd
    # 2.0 pp), NOT the 0.85-0.95 a single bubbler is usually quoted at.
    # When T_bubbler = T_cell the wet line's RH is the efficiency, and the
    # measured RH at 100 % wet flow was 70.5 %.
    efficiency: float = 0.748

    # The "dry" line is not dry: 190 SCCM of it alone read 4.45 % RH. That
    # offset is a real part of the water reaching the cell and it does not
    # vanish when the bubbler is closed, so the blank is not a zero-water
    # blank.
    dry_line_rh: float = 0.0445

    # Measured RH against wet fraction, as (wet_fraction, rh_fraction)
    # pairs. When present this REPLACES the model above: an interpolated
    # measurement beats a two-parameter fit, and the bubbler's efficiency
    # is not constant with flow anyway -- it rises from about 0.70 at
    # 38 SCCM to 0.79 at 152 SCCM, then falls back to 0.705 at full flow
    # as the residence time in the bubbler shortens.
    calibration: Optional[List[Tuple[float, float]]] = None

    def wet_stream_fraction(self) -> float:
        """H2O mole fraction in the wet line itself, before dilution."""
        return (self.efficiency
                * saturation_pressure_pa(self.bubbler_c) / self.pressure_pa)

    def dry_stream_fraction(self) -> float:
        """H2O mole fraction carried by the nominally dry line."""
        return (self.dry_line_rh
                * saturation_pressure_pa(self.cell_c) / self.pressure_pa)

    def rh_from_calibration(self, wet_fraction: float) -> Optional[float]:
        """Interpolated measured RH, or None when no calibration is set."""
        if not self.calibration:
            return None
        pts = sorted(self.calibration)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if wet_fraction <= xs[0]:
            return ys[0]
        if wet_fraction >= xs[-1]:
            return ys[-1]
        for i in range(1, len(xs)):
            if wet_fraction <= xs[i]:
                span = xs[i] - xs[i - 1]
                t = 0.0 if span <= 0 else (wet_fraction - xs[i - 1]) / span
                return ys[i - 1] + t * (ys[i] - ys[i - 1])
        return ys[-1]


@dataclass
class WaterState:
    """Water content of the blended stream reaching the cell."""
    wet_fraction: float        # wet flow / total flow
    mole_fraction: float       # x_H2O in the cell
    ppmv: float
    rh: float                  # 0..1 at cell temperature
    conc_mol_m3: float

    @property
    def rh_percent(self) -> float:
        return self.rh * 100.0

    @property
    def over_limit(self) -> bool:
        return self.rh > RH_LIMIT


def water_state(wet_sccm: float, total_sccm: float,
                cond: HumidityConditions,
                dry_sccm: Optional[float] = None) -> WaterState:
    """
    Water content of the mixture for a given wet-line flow.

    dry_sccm is the DRY N2 flow. Give it in a three-line blend so the CO2
    line is treated as the dry gas it is; left out, everything that is not
    the wet line is assumed to be dry N2.
    """
    if total_sccm <= 0:
        return WaterState(0.0, 0.0, 0.0, 0.0, 0.0)

    wet_fraction = max(0.0, wet_sccm) / total_sccm
    e_cell = saturation_pressure_pa(cond.cell_c)

    measured_rh = cond.rh_from_calibration(wet_fraction)
    if measured_rh is not None:
        rh = measured_rh
        x = rh * e_cell / cond.pressure_pa
    else:
        dry_fraction = (max(0.0, dry_sccm) / total_sccm
                        if dry_sccm is not None else 1.0 - wet_fraction)
        x = (wet_fraction * cond.wet_stream_fraction()
             + dry_fraction * cond.dry_stream_fraction())
        rh = x * cond.pressure_pa / e_cell

    conc = x * cond.pressure_pa / (R_GAS * (cond.cell_c + KELVIN))
    return WaterState(wet_fraction, x, x * 1e6, rh, conc)


# The bench's own measurement, 20 degC, 190 SCCM total: (wet fraction, RH).
# Used as the default calibration because it is a measurement of this
# bubbler at this flow, which no formula can improve on.
BENCH_RH_CALIBRATION = [
    (0.000, 0.0445), (0.200, 0.1750), (0.300, 0.2500), (0.400, 0.3250),
    (0.500, 0.4000), (0.600, 0.4800), (0.700, 0.5520), (0.800, 0.6440),
    (1.000, 0.7050),
]


def wet_flow_for_rh(rh_target: float, total_sccm: float,
                    cond: HumidityConditions) -> float:
    """
    Wet-line flow needed for a target cell RH. Inverse of water_state.

    Useful because RH is what the BME688 reports and what the 70 % limit is
    expressed in, while the MFC takes a flow.
    """
    x_needed = (rh_target * saturation_pressure_pa(cond.cell_c)
                / cond.pressure_pa)
    stream = cond.wet_stream_fraction()
    if stream <= 0:
        return 0.0
    return total_sccm * x_needed / stream


def _self_check() -> int:
    """Reproduce the numbers in the plan's spreadsheet."""
    # The plan's own assumptions, stated explicitly: the module defaults
    # have since moved to what this bench actually measured.
    cond = HumidityConditions(bubbler_c=23.0, cell_c=23.0,
                              pressure_pa=101325.0, efficiency=0.90,
                              dry_line_rh=0.0, calibration=None)
    checks = [
        ("e_s(23 C) Pa", saturation_pressure_pa(23.0), 2808.81045231367),
        ("x_H2O wet stream", cond.wet_stream_fraction(), 0.0249487234846514),
    ]
    for frac, expected_x, expected_c in (
            (0.13, 0.00324333405300469, 0.133463457751241),
            (0.27, 0.00673615534085588, 0.277193335329501),
            (0.40, 0.00997948939386057, 0.410656793080742),
            (0.60, 0.0149692340907909, 0.615985189621113)):
        st = water_state(190.0 * frac, 190.0, cond)
        checks.append((f"x_H2O at wet {frac:.2f}", st.mole_fraction, expected_x))
        checks.append((f"c(H2O) at wet {frac:.2f}", st.conc_mol_m3, expected_c))

    print(f"{'quantity':<24}{'computed':>22}{'spreadsheet':>22}  ")
    worst = 0.0
    for name, got, want in checks:
        rel = abs(got - want) / abs(want) if want else 0.0
        worst = max(worst, rel)
        print(f"{name:<24}{got:>22.12g}{want:>22.12g}  "
              f"{'ok' if rel < 1e-9 else f'DIFFERS {rel:.2e}'}")
    print(f"\nworst relative difference: {worst:.2e}")

    # Second check: the bench's measured curve, against the module defaults
    print("\nmeasured curve at 20 degC, 190 SCCM total "
          "(defaults, calibration on):")
    bench = HumidityConditions()
    bench.calibration = BENCH_RH_CALIBRATION
    rows = [(190, 0, 4.45), (152, 38, 17.5), (133, 57, 25.0), (114, 76, 32.5),
            (95, 95, 40.0), (76, 114, 48.0), (57, 133, 55.2),
            (38, 152, 64.4), (0, 190, 70.5)]
    worst_pp = 0.0
    print(f"{'dry':>6}{'wet':>6}{'computed RH':>14}{'measured':>11}{'diff':>8}")
    for dry, wet, rh in rows:
        st = water_state(wet, dry + wet, bench, dry_sccm=dry)
        worst_pp = max(worst_pp, abs(st.rh_percent - rh))
        print(f"{dry:>6}{wet:>6}{st.rh_percent:>14.2f}{rh:>11.2f}"
              f"{st.rh_percent - rh:>8.2f}")
    print(f"worst difference: {worst_pp:.3f} pp")

    st = water_state(190.0 * 0.60, 190.0, bench)
    print(f"\nRH at wet 0.60: {st.rh_percent:.1f} % "
          f"-- limit is {RH_LIMIT * 100:.0f} %")
    print(f"wet flow for {RH_LIMIT * 100:.0f} % RH at 190 SCCM: "
          f"{wet_flow_for_rh(RH_LIMIT, 190.0, bench):.1f} SCCM")
    return 0 if (worst < 1e-9 and worst_pp < 0.01) else 1


if __name__ == "__main__":
    raise SystemExit(_self_check())
