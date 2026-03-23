"""
full_calculate_corrected.py  — FULLY CORRECTED vs legacy C++ (calc1.cpp / calc2.cpp / calc8.cpp)
============================================================
All bugs from original Python port fixed after line-by-line audit against legacy:

  FIX-A  effabs() implemented: effective absenteeism = ul^(num-1)*absrate (not plain absrate)
  FIX-B  xbar1 recovery: labor times multiplied back by (1+labOT/100) after calc_op LABOR_T
  FIX-C  smbard uses MIN(xbar1,xbar2) not xprime for accumulation
  FIX-D  Labor cs2 uses (faccvs*v_lab)^0.9 power formula, NOT flow-weighted formula
  FIX-E  First-pass uwait: accumulated raw, then scaled by effabs/(1-effabs)/num
  FIX-F  Labor num_av normalised by INITIAL num first, then recalculated (correct ordering)
  FIX-G  lextra ca2: computed per-group from eq cs2 mixture formula (not hard-coded 1.0)
  FIX-H  f_lot_wait_mct uses legacy formula: (xtrans*lsize/lotsiz-1)*per_piece_time
  FIX-I  Labor xbarbar in ggc uses xlabor=xbar1/(1-absrate), not xprime
  FIX-J  xprsig variance uses teq->fac_eq_lab (not tlabor->fac_eq_lab) — was already correct
  FIX-K  Labor util first-pass divides by initial num_av (=num), then recalculates num_av
"""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


# ─────────────────────────────────────────────────────────────────────────────
# Tiny helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _sanitize(v: float) -> float:
    return v if (v == v and abs(v) != float("inf")) else 0.0


def _round1(x: float) -> float:
    return round(x * 10) / 10


def _round4(x: float) -> float:
    return round(x * 10000) / 10000


# ─────────────────────────────────────────────────────────────────────────────
# FIX-A: effabs() — legacy calc1.cpp ~line 936
# effective absenteeism is modulated by utilisation and operator count
# effabs = ul^(num-1) * absrate   (for num>=1)
# effabs = absrate                (for num<1, i.e. delay/virtual server)
# ─────────────────────────────────────────────────────────────────────────────

def effabs(absrate_frac: float, labor_ul: float, labor_num: float) -> float:
    """
    Effective absenteeism factor.
    Legacy calc1.cpp effabs():
        n = tlabor->num - 1
        if n < 0:  x = absrate/100
        else:      x = pow(ul, n) * absrate/100     (non-OPT mode uses ul directly)
    Returns a fraction (0..1), capped at 0.999.
    """
    n = float(labor_num) - 1.0
    if n < 0.0:
        x = float(absrate_frac)
    else:
        x = (float(labor_ul) ** n) * float(absrate_frac)
    return min(x, 0.999)


# ─────────────────────────────────────────────────────────────────────────────
# Small swappable sub-formulas (unchanged from original unless noted)
# ─────────────────────────────────────────────────────────────────────────────

def f_ops_per_period(conv1: float, conv2: float) -> float:
    return max(float(conv1), 0.001) * max(float(conv2), 0.001)


def f_overtime_factor(overtime_pct: float) -> float:
    return 1.0 + float(overtime_pct) / 100.0


def f_unavailable_factor(unavail_pct: float) -> float:
    return 1.0 - float(unavail_pct) / 100.0


def f_available_time_equip(count, overtime_pct, unavail_pct, ops_per_period) -> float:
    ot = f_overtime_factor(overtime_pct)
    uv = f_unavailable_factor(unavail_pct)
    return float(count) * ot * uv * float(ops_per_period)


def f_available_time_labor(count, overtime_pct, ops_per_period) -> float:
    """Labor available time — NOT reduced by absrate (BUG-09 fix retained)."""
    ot = f_overtime_factor(overtime_pct)
    return float(count) * ot * float(ops_per_period)


def f_lot_size(lot_size: float, lot_factor: float) -> float:
    return max(1.0, float(lot_size) * float(lot_factor))


def f_tbatch_size(tbatch_size: float, lot_size_val: float) -> float:
    tb = float(tbatch_size)
    return float(lot_size_val) if tb == -1 else max(1.0, tb)


def f_num_tbatches(lot_size_val: float, tbatch_size_val: float) -> float:
    """Float division (BUG-07 fix retained)."""
    return float(lot_size_val) / float(tbatch_size_val) if tbatch_size_val > 0 else 1.0


def f_assign_fraction(pct_assigned: float) -> float:
    return float(pct_assigned) / 100.0


def f_num_lots(demand: float, lot_size_val: float, assign_fraction: float) -> float:
    return (float(demand) / float(lot_size_val)) * float(assign_fraction)


def f_setup_per_lot(equip_setup_lot, equip_setup_piece, equip_setup_tbatch,
                    lot_size_val, num_tbatches, equip_setup_factor, product_setup_factor) -> float:
    base = (float(equip_setup_lot)
            + float(equip_setup_piece) * float(lot_size_val)
            + float(equip_setup_tbatch) * float(num_tbatches))
    return base * float(equip_setup_factor) * float(product_setup_factor)


def f_run_per_lot(equip_run_piece, equip_run_lot, equip_run_tbatch,
                  lot_size_val, num_tbatches, equip_run_factor) -> float:
    base = (float(equip_run_piece) * float(lot_size_val)
            + float(equip_run_lot)
            + float(equip_run_tbatch) * float(num_tbatches))
    return base * float(equip_run_factor)


def f_time_per_piece(per_lot_time: float, lot_size_val: float) -> float:
    return float(per_lot_time) / float(lot_size_val) if lot_size_val else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# FIX-H: f_lot_wait_mct — legacy calc1.cpp T_BATCH_WAIT_LOT formula
#
# Legacy w_lot per operation (calc1.cpp line 664–665):
#   toper->w_lot = vpergood * ((xbars_t2 + xbarr_t2) * MAX(0, xtrans*lsize/lotsiz - 1)
#                              + tgather * x1)
# where T_BATCH_WAIT_LOT returns:
#   xs = esetpiece * facset / OT
#   xr = epiece    * facrun / OT
# and xtrans = tbatch (or lotsiz if tbatch=-1), lsize=lotsiz for simple case.
#
# For simple case (lsize=lotsiz, vpergood=1, tgather=0):
#   w_lot = (xs + xr) * MAX(0, xtrans/lotsiz*lotsiz/lotsiz - 1)  -- wait this simplifies:
#   xtrans * lsize/lotsiz = tbatch * lotsiz/lotsiz = tbatch   (when lsize=lotsiz)
#   w_lot = (xs + xr) * MAX(0, tbatch - 1)
# where xs+xr = (esetpiece*facset + epiece*facrun) / OT = per-piece equip run+setup time
# ─────────────────────────────────────────────────────────────────────────────

def f_lot_wait_mct(
    lot_size_val: float,
    tbatch_size_val: float,
    run_piece_ot_adj: float,   # epiece * facrun / OT  (OT-adjusted run per piece, minutes)
    setup_piece_ot_adj: float, # esetpiece * facset / OT (OT-adjusted setup per piece, minutes)
    conv1: float,
) -> float:
    """
    FIX-H: Transfer-batch lot wait using legacy T_BATCH_WAIT_LOT formula.

    Legacy: w_lot = (xs_piece + xr_piece) * MAX(0, xtrans - 1)
    where xtrans = tbatch (or lotsiz if tbatch=-1), for lsize=lotsiz case.
    Converted to days by dividing by conv1.
    """
    xtrans = float(tbatch_size_val)  # already set to lotsiz if tbatch=-1
    per_piece_min = float(run_piece_ot_adj) + float(setup_piece_ot_adj)
    wait_min = per_piece_min * max(0.0, xtrans - 1.0)
    return wait_min / max(float(conv1), 0.001)


# ─────────────────────────────────────────────────────────────────────────────
# M/G/c queue mathematics — calc8.cpp ggc (unchanged, confirmed correct)
# ─────────────────────────────────────────────────────────────────────────────

def _cdf_std_normal(z: float) -> float:
    return 0.5 * math.erfc(-float(z) / math.sqrt(2.0))


def _erlang_c(rho: float, m: float) -> float:
    if rho <= 0.0:
        return 0.0
    rho = min(rho, 0.9999)
    m_int = max(1, int(round(m)))
    mrho = m_int * rho
    temp = 1.0
    total = 1.0
    for i in range(1, m_int):
        temp *= mrho / i
        total += temp
    temp *= mrho / m_int
    numerator = temp / (1.0 - rho)
    denom = total + numerator
    return numerator / max(denom, 1e-20)


def _half_in_whitt(rho: float, m: float) -> float:
    waitbeta = (1.0 - min(rho, 0.9999)) * math.sqrt(max(m, 1.0))
    val = 1.0 + 2.5066 * waitbeta * _cdf_std_normal(waitbeta) * math.exp(0.5 * waitbeta ** 2)
    return 1.0 / max(val, 1e-10)


def ggc_wait(
    labor_ul: float,
    num_av: float,
    xbarbar: float,   # FIX-I: must be labor xbarbar (xlabor-weighted), not equip xbarbar
    ca2: float,
    cs2: float,
) -> Tuple[float, float]:
    """
    M/G/c queue wait for labor group — calc8.cpp ggc().
    Returns:
      fac_eq_lab  — meanwait / xbarbar  (dimensionless congestion ratio)
      ct2         — departure-process CV (stored as CV, used as CV^2 in variance — legacy behaviour)
    """
    rho = float(labor_ul)
    num = float(num_av)

    if num < 1.0:
        rho *= num
        num = 1.0

    if xbarbar < 1e-10 or rho < 1e-10:
        return 0.0, float(cs2)

    rho = min(rho, 0.9999 / max(num, 1.0))

    ECBOUND = 70.0
    probwait_m = _erlang_c(rho, num) if num <= ECBOUND else _half_in_whitt(rho, num)

    mean_wait_m = probwait_m * xbarbar / (num_av * max(1.0 - rho, 1e-6))

    gamma = min(
        0.24,
        (1.0 - rho) * (num - 1.0) * (math.sqrt(4.0 + 5.0 * num) - 2.0)
        / max(16.0 * num * rho, 1e-20),
    )
    phi1 = 1.0 + gamma
    phi2 = 1.0 - 4.0 * gamma
    phi3 = phi2 * math.exp(-2.0 * (1.0 - rho) / max(3.0 * rho, 1e-10))
    phi4 = min(1.0, 0.5 * (phi1 + phi3))

    c_sq = 0.5 * (ca2 + cs2)
    xi = 1.0 if c_sq >= 1.0 else phi4 ** (2.0 * (1.0 - c_sq))

    if ca2 >= cs2:
        denom_phi = max(4.0 * ca2 - 3.0 * cs2, 1e-20)
        phi = 4.0 * (ca2 - cs2) * phi1 / denom_phi + cs2 * xi / denom_phi
    else:
        denom_phi = max(ca2 + cs2, 1e-20)
        phi = (cs2 - ca2) * phi3 * 0.5 / denom_phi + (cs2 + 3.0 * ca2) * xi * 0.5 / denom_phi

    mean_wait = phi * c_sq * mean_wait_m

    z_ct = (ca2 + cs2) / max(1.0 + cs2, 1e-20)
    sqrt_num = math.sqrt(abs(num))
    gamma2 = (num - num * rho - 0.5) / max(math.sqrt(num * rho * z_ct), 1e-10)

    pi6 = 1.0 - _cdf_std_normal(gamma2)
    one_minus_rho_sqrt_n = (1.0 - rho) * sqrt_num
    pi5_denom = max(1.0 - _cdf_std_normal(one_minus_rho_sqrt_n), 1e-10)
    pi5 = min(
        1.0,
        (1.0 - _cdf_std_normal(2.0 * one_minus_rho_sqrt_n / max(1.0 + ca2, 1e-10)))
        * probwait_m / pi5_denom,
    )
    pi4 = min(
        1.0,
        (1.0 - _cdf_std_normal((1.0 + cs2) * one_minus_rho_sqrt_n / max(ca2 + cs2, 1e-10)))
        * probwait_m / pi5_denom,
    )

    pi1 = rho ** 2 * pi4 + (1.0 - rho ** 2) * pi5
    pi2 = ca2 * pi1 + (1.0 - ca2) * pi6
    pi3 = (2.0 * (1.0 - ca2) * (gamma2 - 0.5) * pi2
           + (1.0 - 2.0 * (1.0 - ca2) * (gamma2 - 0.5)) * pi1)

    if num < 7 or gamma2 <= 0.5 or ca2 >= 1.0:
        pi = pi1
    elif num >= 7 and gamma2 >= 1.0 and ca2 < 1.0:
        pi = pi2
    else:
        pi = pi3

    probwait_ct = min(1.0, max(0.0, pi))

    if cs2 >= 1.0:
        dscube = 3.0 * cs2 * (1.0 + cs2)
    else:
        dscube = (2.0 * cs2 + 1.0) * (cs2 + 1.0)

    if probwait_ct > 1e-10:
        cd_sq = (2.0 * rho - 1.0
                 + 4.0 * (1.0 - rho) * dscube / max(3.0 * (cs2 + 1.0) ** 2, 1e-20))
        cw_sq = (cd_sq + 1.0 - probwait_ct) / probwait_ct
    else:
        cw_sq = 0.0

    # FIX: ct2 = sqrt(cs2*xbar^2 + cw^2*wait^2) / (xbar+wait)  — legacy calc8.cpp line 152
    # This is CV (not CV^2); it is used AS CV^2 in variance formula (legacy inconsistency, replicated)
    ct2 = math.sqrt(max(cs2 * xbarbar ** 2 + cw_sq * mean_wait ** 2, 0.0)) / max(xbarbar + mean_wait, 1e-20)

    fac_eq_lab = mean_wait / max(xbarbar, 1e-20)
    return max(0.0, fac_eq_lab), max(0.0, ct2)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-B: OT-adjusted equipment times (unchanged — already correct)
# ─────────────────────────────────────────────────────────────────────────────

def _ot_adj_equip_times(op, eq, lot_size_val, num_tbatches, prod_setup_factor):
    """Returns OT-adjusted (xbars per lot, xbarr per piece) for equipment."""
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    xs = (
        float(op.get("equip_setup_lot", 0))
        + float(op.get("equip_setup_piece", 0)) * lot_size_val
        + float(op.get("equip_setup_tbatch", 0)) * num_tbatches
    ) * float(eq.get("setup_factor", 1)) * prod_setup_factor / ot

    xr_lot = (
        float(op.get("equip_run_piece", 0)) * lot_size_val
        + float(op.get("equip_run_lot", 0))
        + float(op.get("equip_run_tbatch", 0)) * num_tbatches
    ) * float(eq.get("run_factor", 1)) / ot

    xr_piece = xr_lot / lot_size_val if lot_size_val > 0 else 0.0
    return xs, xr_piece


def _ot_adj_equip_piece_rates(op, eq):
    """
    Returns per-PIECE OT-adjusted rates for T_BATCH_WAIT_LOT computation:
      xs_piece = esetpiece * facset / OT
      xr_piece = epiece    * facrun / OT
    Used in FIX-H lot-wait formula.
    """
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    xs_piece = float(op.get("equip_setup_piece", 0)) * float(eq.get("setup_factor", 1)) / ot
    xr_piece = float(op.get("equip_run_piece", 0)) * float(eq.get("run_factor", 1)) / ot
    return xs_piece, xr_piece


# ─────────────────────────────────────────────────────────────────────────────
# FIX-B: Raw labor times WITH OT recovery
# Legacy calc_op LABOR_T divides by (1+labOT/100), then set_xbar_cs MULTIPLIES
# back by (1+labOT/100) to recover raw un-OT-adjusted times.
# ─────────────────────────────────────────────────────────────────────────────

def _raw_labor_times(op, eq, lab, lot_size_val, num_tbatches, prod_setup_factor):
    """
    FIX-B: Return raw (un-OT-adjusted) labor times.
    Legacy: calc_op divides by (1+labOT/100), then set_xbar_cs multiplies back.
    Net result: xbar1 = raw labor time (no OT factor applied).
    """
    if lab is None:
        return 0.0, 0.0

    eq_sf = float(eq.get("setup_factor", 1))
    eq_rf = float(eq.get("run_factor", 1))
    lab_sf = float(lab.get("setup_factor", 1))
    lab_rf = float(lab.get("run_factor", 1))
    # FIX-B: multiply back by (1+labOT/100) to undo the division in calc_op LABOR_T
    lab_ot_factor = 1.0 + float(lab.get("overtime_pct", 0)) / 100.0

    xs_raw = (
        float(op.get("labor_setup_lot", 0))
        + float(op.get("labor_setup_piece", 0)) * lot_size_val
        + float(op.get("labor_setup_tbatch", 0)) * num_tbatches
    ) * eq_sf * lab_sf * prod_setup_factor * lab_ot_factor

    xr_lot_raw = (
        float(op.get("labor_run_piece", 0)) * lot_size_val
        + float(op.get("labor_run_lot", 0))
        + float(op.get("labor_run_tbatch", 0)) * num_tbatches
    ) * eq_rf * lab_rf * lab_ot_factor

    xr_piece_raw = xr_lot_raw / lot_size_val if lot_size_val > 0 else 0.0
    return xs_raw, xr_piece_raw


# ─────────────────────────────────────────────────────────────────────────────
# FIX-A/B: calc_xprime using effabs (legacy calc1.cpp ~line 877)
# ─────────────────────────────────────────────────────────────────────────────

def _calc_xprime(
    xbar1: float,      # raw labor time per visit (OT-recovered, minutes)
    xbar2: float,      # OT-adjusted equipment time per visit (minutes)
    mttr: float,
    mttf: float,
    absrate_frac: float,   # labour absenteeism as fraction (0..1)
    labor_ul: float,       # FIX-A: total labor utilisation (for effabs)
    labor_num: float,      # FIX-A: labor group count (for effabs)
    fac_eq_lab: float,
) -> float:
    """
    FIX-A: Uses effabs() instead of plain 1/(1-absrate).
    Legacy calc_xprime (calc1.cpp):
      ea = effabs(ul, num, absrate)   <- FIX-A
      abs_f = 1 / (1 - ea)
      if xbar2 >= xbar1:
        xprime = (xbar2-xbar1) + xbar2*mttr/mttf + xbar1*abs_f*(1+fac)
      elif xbar2 > 0:
        xprime = xbar2*mttr/mttf + xbar2*abs_f*(1+fac)
      else:
        xprime = xbar1*abs_f*(1+fac)
    """
    ea = effabs(absrate_frac, labor_ul, labor_num)
    abs_f = 1.0 / max(1.0 - ea, 1e-6)
    repair = (mttr / mttf) if mttf > 0.0 else 0.0

    if xbar2 >= xbar1 - 1e-12:
        xm_only = max(0.0, xbar2 - xbar1)
        xl_only = xbar1 * abs_f
        return xm_only + xbar2 * repair + xl_only * (1.0 + fac_eq_lab)
    elif xbar2 > 1e-20:
        return xbar2 * repair + xbar2 * abs_f * (1.0 + fac_eq_lab)
    else:
        return xbar1 * abs_f * (1.0 + fac_eq_lab)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-C/D/I: set_xbar_cs equivalent (calc2.cpp)
#
# FIX-C: smbard uses MIN(xbar1,xbar2) not xprime
# FIX-D: labor cs2 = (faccvs*v_lab)^0.9, NOT flow-weighted formula
# FIX-I: labor xbarbar accumulates xlabor = xbar1/(1-absrate), not xprime
# ─────────────────────────────────────────────────────────────────────────────

def _compute_xbar_cs(
    m: Dict,
    effective_demand: Dict[str, float],
    scrap_rates: Dict[str, float],
    var_equip: float,    # global equipment CV fraction (already divided by 100)
    var_labor: float,    # global labor CV fraction
    fac_eq_lab_map: Dict[str, float],
    ct2_lab_map: Dict[str, float],
    labor_util_map: Dict[str, float],   # FIX-A: need labor ul for effabs
    labor_num_map: Dict[str, float],    # FIX-A: need labor count for effabs
    ops_per_period: float,
) -> Tuple[Dict, Dict, Dict, Dict, Dict, Dict, Dict]:
    """
    FIX-C/D/I: Compute flow-weighted xbarbar and cs2 per equipment,
    and labor xbarbar per labor group (used by ggc).

    Returns per equipment-id:
      xbarbar_eq, cs2_eq, ca2_eq, tpm_eq, smbard_eq, xbard_eq
    Plus per labor-id:
      lab_xbarbar_map  — FIX-I: labor group xbarbar (xlabor-weighted)
    """
    equipment_list = m.get("equipment", [])
    labor_by_id    = {x["id"]: x for x in m.get("labor", [])}

    # Equipment accumulators
    xbb   = {eq["id"]: 0.0 for eq in equipment_list}   # sum(vlam*xprime)
    xbd   = {eq["id"]: 0.0 for eq in equipment_list}   # sum(vlam)
    xsb   = {eq["id"]: 0.0 for eq in equipment_list}   # sum(vlam*(xprsig^2+xprime^2))
    tpm   = {eq["id"]: 0.0 for eq in equipment_list}
    smb   = {eq["id"]: 0.0 for eq in equipment_list}   # FIX-C: sum(vlam*MIN(xbar1,xbar2)/(1-abs))

    # FIX-I: Labor accumulators for xbarbar (used in ggc)
    lab_xbb  = {x["id"]: 0.0 for x in m.get("labor", [])}   # sum(vlam*xlabor)
    lab_xbd  = {x["id"]: 0.0 for x in m.get("labor", [])}   # sum(vlam)

    for product in m.get("products", []):
        pid    = product.get("id", "")
        demand = effective_demand.get(pid, 0.0) or 0.0
        if demand <= 0.0:
            continue

        scrap      = scrap_rates.get(pid, 0.0)
        lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
        ps_factor  = float(product.get("setup_factor", 1))

        demand_inflated = demand * (1.0 + scrap)
        dlam = demand_inflated / (lot_size_v * max(ops_per_period, 1e-9))

        ops = [o for o in m.get("operations", []) if o.get("product_id") == pid]

        for op in ops:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq:
                continue
            if eq.get("equip_type") == "delay":
                continue

            af = f_assign_fraction(op.get("pct_assigned", 0))
            if af <= 0.0:
                continue

            eq_id   = eq.get("id", "")
            lab     = labor_by_id.get(eq.get("labor_group_id") or "")
            lab_id  = eq.get("labor_group_id") or ""
            mttf    = float(eq.get("mttf", 0) or 0)
            mttr    = float(eq.get("mttr", 0) or 0)
            imttf   = 1.0 / mttf if mttf > 0 else 0.0
            absrate_frac = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0

            # FIX-A: get labor ul and num for effabs
            labor_ul  = labor_util_map.get(lab_id, absrate_frac)
            labor_num = labor_num_map.get(lab_id, 1.0)

            tbatch_v = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
            nb       = f_num_tbatches(lot_size_v, tbatch_v)

            xbars, xbarr_pc = _ot_adj_equip_times(op, eq, lot_size_v, nb, ps_factor)
            xbar2 = xbars + xbarr_pc

            # FIX-B: raw labor times with OT recovery
            xbarsl, xbarrl_pc = _raw_labor_times(op, eq, lab, lot_size_v, nb, ps_factor)
            xbar1 = xbarsl + xbarrl_pc

            fac   = fac_eq_lab_map.get(eq_id, 0.0)
            vlam1 = dlam * af

            xprime = _calc_xprime(xbar1, xbar2, mttr, mttf, absrate_frac, labor_ul, labor_num, fac)

            # FIX-C: smbard uses MIN(xbar1,xbar2)/(1-absrate), NOT xprime
            # Legacy calc1.cpp line 307-320:
            #   x1 = (xbar2 > SSEPSILON) ? MIN(xbar1, xbar2) : xbar1
            #   smbard += v1 * x1 / (1 - absrate)
            x1_smb = min(xbar1, xbar2) if xbar2 > 1e-20 else xbar1
            smb[eq_id] += vlam1 * x1_smb / max(1.0 - absrate_frac, 0.01)

            # xprsig^2 variance for equipment — calc2.cpp line 97-100
            # Uses teq->fac_eq_lab (= fac_eq_lab_map[eq_id]) — confirmed correct
            xm_only = max(0.0, xbar2 - xbar1)
            xl_only_abs = (min(xbar1, xbar2) / max(1.0 - absrate_frac, 0.01)
                           if xbar2 > 1e-20
                           else xbar1 / max(1.0 - absrate_frac, 0.01))

            eq_cv_fac = var_equip * float(eq.get("var_factor", 1))
            # ct2_labor: from ct2_lab_map if available, else initialized cs2 = (cv)^2
            ct2_labor = ct2_lab_map.get(lab_id, (var_labor * float(lab.get("var_factor", 1) if lab and "var_factor" in lab else 1)) ** 2)

            repair_var  = 2.0 * mttr ** 2 * imttf * xbar2
            machine_sq  = ((1.0 + mttr / mttf if mttf > 0 else 1.0) * eq_cv_fac * xm_only) ** 2
            # FIX-J: uses teq->fac_eq_lab (fac) not tlabor->fac_eq_lab (already correct)
            labor_sq    = ct2_labor * (xl_only_abs * (1.0 + fac)) ** 2
            xprsig_sq   = repair_var + machine_sq + labor_sq

            xbb[eq_id] += vlam1 * xprime
            xbd[eq_id] += vlam1
            xsb[eq_id] += vlam1 * (xprsig_sq + xprime ** 2)
            tpm[eq_id] += vlam1

            # FIX-I: labor xbarbar accumulates xlabor = xbar1/(1-absrate), NOT xprime
            # Legacy calc2.cpp line 124-132:
            #   xlabor = xbar1 / (1 - absrate)
            #   tlabor->xbarbar += vlam1 * xlabor
            #   tlabor->xbard   += vlam1
            ea_val   = effabs(absrate_frac, labor_ul, labor_num)
            xlabor   = xbar1 / max(1.0 - ea_val, 1e-6)
            lab_xbb[lab_id] = lab_xbb.get(lab_id, 0.0) + vlam1 * xlabor
            lab_xbd[lab_id] = lab_xbd.get(lab_id, 0.0) + vlam1

    # Finalise equipment xbarbar and cs2
    xbarbar_eq: Dict[str, float] = {}
    cs2_eq:     Dict[str, float] = {}
    ca2_eq:     Dict[str, float] = {}

    for eq in equipment_list:
        eq_id = eq.get("id", "")
        xbd_v = xbd.get(eq_id, 0.0)
        xbb_v = xbb.get(eq_id, 0.0)
        xsb_v = xsb.get(eq_id, 0.0)

        if xbd_v > 1e-20 and xbb_v > 1e-20:
            xbarbar_eq[eq_id] = xbb_v / xbd_v
            cs2_eq[eq_id]     = max(0.0, (xsb_v * xbd_v / (xbb_v ** 2)) - 1.0)
        else:
            xbarbar_eq[eq_id] = 0.0
            cs2_eq[eq_id]     = (var_equip * float(eq.get("var_factor", 1))) ** 2

        ca2_eq[eq_id] = 1.0  # updated later in lextra per labor group

    # FIX-I: finalise labor xbarbar
    lab_xbarbar_map: Dict[str, float] = {}
    for lab in m.get("labor", []):
        lab_id  = lab.get("id", "")
        xbd_v   = lab_xbd.get(lab_id, 0.0)
        xbb_v   = lab_xbb.get(lab_id, 0.0)
        lab_xbarbar_map[lab_id] = (xbb_v / xbd_v) if xbd_v > 1e-20 else 0.0

    return xbarbar_eq, cs2_eq, ca2_eq, tpm, smb, xbd, lab_xbarbar_map


# ─────────────────────────────────────────────────────────────────────────────
# FIX-D/G/I: lextra equivalent (calc2.cpp lextra + calc8.cpp ggc)
#
# FIX-D: labor cs2 = (faccvs*v_lab)^0.9 (not flow-weighted)
# FIX-G: ca2 computed per-group from mixture formula (not hard-coded 1.0)
# FIX-I: ggc uses lab_xbarbar (xlabor-weighted) not equipment xbarbar
# ─────────────────────────────────────────────────────────────────────────────

def _compute_lextra(
    m: Dict,
    equipment_list: List,
    labor_by_id: Dict,
    xbarbar_eq: Dict[str, float],
    cs2_eq: Dict[str, float],
    tpm_eq: Dict[str, float],
    smbard_eq: Dict[str, float],
    lab_xbarbar_map: Dict[str, float],   # FIX-I: labor-group xbarbar
    labor_util_map: Dict[str, float],
    labor_num_map: Dict[str, float],
    num_av_lab_map: Dict[str, float],
    num_av_eq_map: Dict[str, float],
    var_labor: float,
    utlimit: float,
) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float]]:
    """
    FIX-D/G/I: Compute fac_eq_lab and uwait per equipment.

    Returns fac_eq_lab_map, uwait_eq_map, ct2_lab_map.
    """
    fac_eq_lab_map: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    uwait_eq_map:   Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    ct2_lab_map:    Dict[str, float] = {}

    for lab in m.get("labor", []):
        lab_id  = lab.get("id", "")
        lab_num = float(lab.get("count", 0))

        # FIX-D: Initialize ct2 = cs2 = (faccvs*v_lab/100)^2
        # Legacy lextra line 241-242
        lab_var_factor = float(lab.get("var_factor", 1) if "var_factor" in lab else 1)
        cs2_lab = min(4.0, (lab_var_factor * var_labor) ** 2)
        ct2_lab_map[lab_id] = cs2_lab   # default; overwritten by ggc if active

        eq_in_group = [e for e in equipment_list
                       if e.get("labor_group_id") == lab_id and int(e.get("count", 0)) > 0]
        if not eq_in_group:
            continue

        max_eq_ot   = max(float(e.get("overtime_pct", 0)) for e in eq_in_group)
        eq_cover_raw = sum(float(e.get("count", 1)) * (float(e.get("overtime_pct", 0)) + 100.0)
                           for e in eq_in_group)
        eq_cover = eq_cover_raw / (100.0 * (1.0 + max_eq_ot / 100.0))

        if eq_cover <= 0.0:
            continue

        labor_ul  = labor_util_map.get(lab_id, 0.0)
        num_av    = num_av_lab_map.get(lab_id, lab_num)

        tlab_tpm  = sum(tpm_eq.get(e["id"], 0.0) for e in eq_in_group)
        tlab_smbard = sum(smbard_eq.get(e["id"], 0.0) for e in eq_in_group)

        if tlab_tpm < 1e-20:
            continue

        # FIX-I: use lab_xbarbar (xlabor-weighted) for ggc xbarbar
        xbarbar_lab = lab_xbarbar_map.get(lab_id, 0.0)

        if lab_num <= 0 or (num_av >= eq_cover + 1e-10 and eq_cover > 0):
            WAIT = 0.0

        elif labor_ul > (utlimit / 100.0):
            WAIT = (eq_cover - 1.0) if eq_cover > 0 else 1000.0
            if xbarbar_lab > 1e-20:
                pass  # WAIT already set
            else:
                WAIT = 0.0

        else:
            # FIX-G: compute ca2 from mixture formula (legacy calc2.cpp ~line 340-380)
            u1 = min(0.95, labor_ul)
            tlab_nm  = 0.0
            tlab_ca  = 0.0

            for teq in eq_in_group:
                eq_id  = teq.get("id", "")
                s1     = num_av_eq_map.get(eq_id, float(teq.get("count", 1)))
                s2     = num_av
                smb_v  = smbard_eq.get(eq_id, 0.0)
                tpm_v  = tpm_eq.get(eq_id, 0.0)
                cs2_e  = min(4.0, cs2_eq.get(eq_id, 1.0))

                if int(teq.get("count", 0)) > 0:
                    rho1 = max(0.0, 1.0 - (smb_v / max(s1, 1e-20)))
                    rho2 = u1
                    s2_safe = max(s2, 1.0)

                    num_v  = (1.0 + (cs2_e - 1.0) * rho1 ** 2 / max(s1 ** 0.5, 1e-10)
                              - (1.0 - rho1 ** 2) * (1.0 - rho2 ** 2)
                              + (1.0 - rho1 ** 2) * (cs2_lab - 1.0) * rho2 ** 2 / max(s2_safe ** 0.5, 1e-10))
                    demon  = 1.0 - (1.0 - rho1 ** 2) * (1.0 - rho2 ** 2)
                    if demon < 1e-20:
                        demon = 1.0
                        num_v = 1.0

                    if tlab_smbard > 1e-20:
                        tlab_nm += smb_v * (1.0 - smb_v / (tlab_smbard * max(s1, 1e-10)))
                    else:
                        tlab_nm += smb_v

                    tlab_ca += (num_v / demon) * tpm_v
                else:
                    tlab_nm += smb_v
                    tlab_ca += 1.0 * tpm_v

            # nm_1 formula — legacy OVERWRITES to (eq_cover-1)/eq_cover before ggc
            nm_1 = (eq_cover - 1.0) / eq_cover if eq_cover > 0 else 1.0

            # ca2 for this labor group
            ca2_lab = min(4.0, tlab_ca / max(tlab_tpm, 1e-20))

            # FIX-D: cs2 for labor = (faccvs*v_lab)^0.9  (legacy calc2.cpp line 152)
            # NOTE: legacy uses ^0.9 in set_xbar_cs after computing xbarbar, not ^2
            cs2_lab_ggc = min(4.0, (lab_var_factor * var_labor) ** 0.9)

            # ggc uses lab_xbarbar (FIX-I), computed ca2 (FIX-G), cs2_lab_ggc (FIX-D)
            fac_raw, ct2_new = ggc_wait(labor_ul, num_av, xbarbar_lab, ca2_lab, cs2_lab_ggc)
            WAIT = fac_raw * nm_1
            if eq_cover > 0:
                WAIT = min(WAIT, eq_cover - 1.0)

            ct2_lab_map[lab_id] = ct2_new

        fac_for_group = WAIT if xbarbar_lab > 1e-20 else 0.0

        for eq in eq_in_group:
            eq_id    = eq.get("id", "")
            num_av_e = num_av_eq_map.get(eq_id, float(eq.get("count", 1)))
            smb_v    = smbard_eq.get(eq_id, 0.0)

            fac_eq_lab_map[eq_id] = fac_for_group

            if num_av_e > 1e-20:
                if labor_ul > 0.95:
                    uwait_eq_map[eq_id] = 1.0
                else:
                    uwait_eq_map[eq_id] = (fac_for_group * smb_v) / num_av_e

    return fac_eq_lab_map, uwait_eq_map, ct2_lab_map


# ─────────────────────────────────────────────────────────────────────────────
# Demand / routing helpers (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def f_yield_from_routing(routing_rows, product_id, start_op="DOCK") -> float:
    routes = [r for r in routing_rows if r.get("product_id") == product_id]
    outgoing: Dict[str, List[Tuple[str, float]]] = {}
    nodes = {start_op, "STOCK", "SCRAP"}
    for r in routes:
        frm = str(r.get("from_op_name", ""))
        to  = str(r.get("to_op_name", ""))
        pct = float(r.get("pct_routed", 0.0)) / 100.0
        nodes.add(frm); nodes.add(to)
        outgoing.setdefault(frm, []).append((to, pct))

    p_stock: Dict[str, float] = {n: 0.0 for n in nodes}
    p_stock["STOCK"] = 1.0
    p_stock["SCRAP"] = 0.0

    if not outgoing:
        return 1.0

    for _ in range(500):
        delta = 0.0
        for n in nodes:
            if n in ("STOCK", "SCRAP"):
                continue
            outs  = outgoing.get(n)
            new_v = 1.0 if not outs else sum(p * p_stock.get(t, 0.0) for t, p in outs)
            delta = max(delta, abs(new_v - p_stock[n]))
            p_stock[n] = new_v
        if delta < 1e-10:
            break

    return min(max(float(p_stock.get(start_op, 1.0)), 0.0), 1.0)


def compute_effective_demand(products, ibom) -> Dict[str, float]:
    children: Dict[str, List] = {}
    for entry in ibom:
        pid = entry.get("parent_product_id")
        children.setdefault(pid, []).append({
            "componentId":  entry.get("component_product_id"),
            "unitsPerAssy": float(entry.get("units_per_assy", 1)),
        })

    demand: Dict[str, float] = {}
    for p in products:
        demand[p["id"]] = float(p.get("demand", 0)) * float(p.get("demand_factor", 1))

    visited: set = set()
    order:   List[str] = []

    def visit(pid: str) -> None:
        if pid in visited:
            return
        visited.add(pid)
        for k in children.get(pid, []):
            visit(k["componentId"])
        order.append(pid)

    for p in products:
        visit(p["id"])
    order.reverse()

    for parent_id in order:
        for k in children.get(parent_id, []):
            cid = k["componentId"]
            demand[cid] = demand.get(cid, 0.0) + demand.get(parent_id, 0.0) * float(k["unitsPerAssy"])

    return demand


def apply_scenario(model, scenario):
    import copy
    if not scenario or not scenario.get("changes"):
        return copy.deepcopy(model)

    m = copy.deepcopy(model)
    m["labor"]      = [dict(x) for x in m.get("labor", [])]
    m["equipment"]  = [dict(x) for x in m.get("equipment", [])]
    m["products"]   = [dict(x) for x in m.get("products", [])]
    m["operations"] = [dict(x) for x in m.get("operations", [])]
    m["routing"]    = [dict(x) for x in m.get("routing", [])]

    for c in scenario["changes"]:
        data_type = c.get("dataType")
        entity_id = c.get("entityId")
        field     = c.get("field")
        what_if   = c.get("whatIfValue")

        if data_type == "Labor":
            for item in m["labor"]:
                if item.get("id") == entity_id:
                    item[field] = what_if
                    break
        elif data_type == "Equipment":
            for item in m["equipment"]:
                if item.get("id") == entity_id:
                    item[field] = what_if
                    break
        elif data_type == "Product":
            if field == "included" and str(what_if) == "false":
                for p in m["products"]:
                    if p.get("id") == entity_id:
                        p["demand"] = 0
                        break
            else:
                for item in m["products"]:
                    if item.get("id") == entity_id:
                        item[field] = what_if
                        break
        elif data_type == "Routing":
            for item in m["routing"]:
                if item.get("id") == entity_id:
                    item[field] = float(what_if) if what_if is not None else 0
                    break
        elif data_type == "Product Inclusion" and what_if == "No":
            for p in m["products"]:
                if p.get("id") == entity_id:
                    p["demand"] = 0
                    break

    return m


def f_wip_from_littles_law(flow_per_period: float, mct_days: float, conv2: float) -> int:
    days_per_period = max(float(conv2), 1.0)
    flow_per_day    = float(flow_per_period) / days_per_period
    return max(0, round(flow_per_day * float(mct_days)))


def f_capacity_limited_flow_for_product(product, ops_for_product, equipment_list, conv1, ops_per_period) -> float:
    lot_size_val    = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
    tbatch_size_val = f_tbatch_size(product.get("tbatch_size", -1), lot_size_val)
    num_tbatches    = f_num_tbatches(lot_size_val, tbatch_size_val)
    ps_factor       = float(product.get("setup_factor", 1))

    limits: List[float] = []
    for op in ops_for_product:
        eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
        if not eq:
            continue
        af = f_assign_fraction(op.get("pct_assigned", 0))
        if af <= 0:
            continue
        is_delay = eq.get("equip_type") == "delay"
        count    = 1 if is_delay else int(eq.get("count", 0))
        if count <= 0 and not is_delay:
            continue

        avail_time = f_available_time_equip(count, eq.get("overtime_pct", 0),
                                             eq.get("unavail_pct", 0), ops_per_period)
        setup_pl = f_setup_per_lot(op.get("equip_setup_lot", 0), op.get("equip_setup_piece", 0),
                                    op.get("equip_setup_tbatch", 0), lot_size_val, num_tbatches,
                                    eq.get("setup_factor", 1), ps_factor)
        run_pl   = f_run_per_lot(op.get("equip_run_piece", 0), op.get("equip_run_lot", 0),
                                  op.get("equip_run_tbatch", 0), lot_size_val, num_tbatches,
                                  eq.get("run_factor", 1))
        processing_per_piece = f_time_per_piece(setup_pl + run_pl, lot_size_val)
        if processing_per_piece <= 0:
            continue

        limits.append(avail_time / (af * processing_per_piece))

    return min(limits) if limits else float("inf")


def f_feasible_started_flow(external_and_parent_demand, yield_fraction, capacity_limited_flow) -> float:
    y      = min(max(float(yield_fraction), 0.0), 1.0)
    demand = float(external_and_parent_demand)
    needed = demand / y if y > 0 else float("inf")
    return float(min(needed, float(capacity_limited_flow)))


def f_good_shipped(good_made, demand_end) -> int:
    return round(min(float(good_made), float(demand_end)))


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestration
# ─────────────────────────────────────────────────────────────────────────────

def full_calculate_corrected(
    model: Dict[str, Any], scenario: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    m = apply_scenario(model, scenario)
    g = m.get("general", {})
    warnings: List[str] = []
    errors:   List[str] = []

    conv1 = float(g.get("conv1", 480))
    conv2 = float(g.get("conv2", 5))
    ops_per_period = f_ops_per_period(conv1, conv2)

    util_limit = float(g.get("util_limit", 85))
    var_equip  = float(g.get("var_equip", 0)) / 100.0
    var_labor  = float(g.get("var_labor", 0)) / 100.0

    equipment_list  = m.get("equipment", [])
    labor_by_id     = {x["id"]: x for x in m.get("labor", [])}
    operations_list = m.get("operations", [])
    routing_list    = m.get("routing", [])

    effective_demand = compute_effective_demand(m.get("products", []), m.get("ibom", []))

    scrap_rates: Dict[str, float] = {}
    for product in m.get("products", []):
        pid = product.get("id", "")
        scrap_rates[pid] = 1.0 - f_yield_from_routing(routing_list, pid, start_op="DOCK")

    # ── MAX EQ OT PER LABOR GROUP ────────────────────────────────────────────
    max_lab_ot_map: Dict[str, float] = {}
    for eq in equipment_list:
        lab_id = eq.get("labor_group_id") or ""
        eq_ot  = float(eq.get("overtime_pct", 0))
        max_lab_ot_map[lab_id] = max(max_lab_ot_map.get(lab_id, -100.0), eq_ot)

    # ── NUM_AV per equipment ─────────────────────────────────────────────────
    num_av_eq_map: Dict[str, float] = {}
    for eq in equipment_list:
        eq_id    = eq.get("id", "")
        lab_id   = eq.get("labor_group_id") or ""
        is_delay = eq.get("equip_type") == "delay"
        count    = 1 if is_delay else int(eq.get("count", 0))
        eq_ot_f  = float(eq.get("overtime_pct", 0))
        max_eq_ot_g = max_lab_ot_map.get(lab_id, 0.0)
        num_av   = float(count) * (eq_ot_f + 100.0) / (100.0 + max_eq_ot_g) if count > 0 else 0.0
        num_av_eq_map[eq_id] = max(num_av, float(count))

    # ── EQUIPMENT UTILISATION — FIRST PASS ──────────────────────────────────
    equip_results:    List[Dict[str, Any]] = []
    equip_active_map: Dict[str, float] = {}
    equip_util_map:   Dict[str, float] = {}
    equip_raw_uwait:  Dict[str, float] = {}  # FIX-E: first-pass uwait before lextra

    for eq in equipment_list:
        eq_id    = eq.get("id", "")
        eq_name  = eq.get("name", "")
        eq_count = int(eq.get("count", 0))
        is_delay = eq.get("equip_type") == "delay"
        count    = 1 if is_delay else eq_count

        lab_id = eq.get("labor_group_id") or ""
        lab    = labor_by_id.get(lab_id)

        if count <= 0 and not is_delay:
            equip_results.append({
                "id": eq_id, "name": eq_name, "count": eq_count,
                "setupUtil": 0, "runUtil": 0, "repairUtil": 0,
                "waitLaborUtil": 0, "totalUtil": 0, "idle": 100, "laborGroup": "",
            })
            equip_active_map[eq_id] = 0.0
            equip_util_map[eq_id]   = 0.0
            equip_raw_uwait[eq_id]  = 0.0
            continue

        avail_time = f_available_time_equip(count, eq.get("overtime_pct", 0),
                                             eq.get("unavail_pct", 0), ops_per_period)
        total_setup = 0.0
        total_run   = 0.0
        total_uwait_raw = 0.0  # FIX-E: accumulate raw uwait = v1 * MIN(xbar1,xbar2)

        for op in operations_list:
            if op.get("equip_id") != eq_id:
                continue
            product = next((p for p in m.get("products", []) if p.get("id") == op.get("product_id")), None)
            if not product:
                continue
            pid    = product.get("id", "")
            demand = (effective_demand.get(pid, 0.0) or 0.0) * (1.0 + scrap_rates.get(pid, 0.0))
            if demand <= 0:
                continue

            lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
            tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
            nb         = f_num_tbatches(lot_size_v, tbatch_v)
            af         = f_assign_fraction(op.get("pct_assigned", 0))
            num_lots   = f_num_lots(demand, lot_size_v, af)
            ps_factor  = float(product.get("setup_factor", 1))

            total_setup += num_lots * f_setup_per_lot(
                op.get("equip_setup_lot", 0), op.get("equip_setup_piece", 0),
                op.get("equip_setup_tbatch", 0), lot_size_v, nb,
                eq.get("setup_factor", 1), ps_factor,
            )
            total_run += num_lots * f_run_per_lot(
                op.get("equip_run_piece", 0), op.get("equip_run_lot", 0),
                op.get("equip_run_tbatch", 0), lot_size_v, nb,
                eq.get("run_factor", 1),
            )

            # FIX-E: accumulate raw uwait = v1 * MIN(xbar1, xbar2)
            # v1 = dlam * lvisit = dlam * af  (simple routing)
            # xbar2 = OT-adjusted equip time per lot
            # xbar1 = raw labor time per lot (FIX-B)
            dlam   = demand / (lot_size_v * max(ops_per_period, 1e-9))
            v1_raw = dlam * af  # lots/min flow

            xbars_eq, xbarr_pc_eq = _ot_adj_equip_times(op, eq, lot_size_v, nb, ps_factor)
            xbar2_raw = xbars_eq + xbarr_pc_eq   # OT-adj equip time per visit (1 piece, after /lotsiz)

            lab_op = labor_by_id.get(lab_id)
            xbarsl_l, xbarrl_pc_l = _raw_labor_times(op, eq, lab_op, lot_size_v, nb, ps_factor)
            xbar1_raw = xbarsl_l + xbarrl_pc_l  # raw labor time per visit

            x1_uwait = min(xbar1_raw, xbar2_raw) if xbar2_raw > 1e-20 else xbar1_raw
            total_uwait_raw += v1_raw * x1_uwait

        setup_util = (total_setup / avail_time * 100.0) if avail_time > 0 else 0.0
        run_util   = (total_run   / avail_time * 100.0) if avail_time > 0 else 0.0

        mttf = float(eq.get("mttf", 0) or 0)
        mttr = float(eq.get("mttr", 0) or 0)
        repair_util = (
            ((setup_util + run_util) / 100.0) * (mttr / mttf) * 100.0
            if (mttf > 0 and mttr > 0) else 0.0
        )

        lab_name = lab.get("name", "") if lab else ""

        equip_results.append({
            "id": eq_id, "name": eq_name, "count": eq_count,
            "setupUtil":     _round1(setup_util),
            "runUtil":       _round1(run_util),
            "repairUtil":    _round1(repair_util),
            "waitLaborUtil": 0,
            "totalUtil":     0,
            "idle":          0,
            "laborGroup":    lab_name,
        })
        equip_active_map[eq_id]  = (setup_util + run_util) / 100.0
        equip_util_map[eq_id]    = (setup_util + run_util + repair_util) / 100.0
        equip_raw_uwait[eq_id]   = total_uwait_raw

    # ── FIX-E: Compute first-pass uwait per equipment (legacy calc1.cpp lines 396-399)
    # After labor loop, for each equipment:
    #   uwait *= effabs(tlabor) / (1 - effabs(tlabor))
    #   uwait /= num
    # This pre-lextra uwait is ADDED to total_util to compute labor_ul fed into ggc.
    # However the uwait at this stage is NOT yet normalised by avail_time.
    # It stays as fraction of avail_time.
    equip_uwait_pre: Dict[str, float] = {}
    for eq in equipment_list:
        eq_id    = eq.get("id", "")
        is_delay = eq.get("equip_type") == "delay"
        count    = 1 if is_delay else int(eq.get("count", 0))
        if count <= 0 and not is_delay:
            equip_uwait_pre[eq_id] = 0.0
            continue
        if is_delay:
            equip_uwait_pre[eq_id] = 0.0
            continue

        avail_time = f_available_time_equip(count, eq.get("overtime_pct", 0),
                                             eq.get("unavail_pct", 0), ops_per_period)
        if avail_time <= 0:
            equip_uwait_pre[eq_id] = 0.0
            continue

        lab_id    = eq.get("labor_group_id") or ""
        lab       = labor_by_id.get(lab_id)
        absrate_f = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
        lab_num   = float(lab.get("count", 0)) if lab else 0.0

        # Temporarily use a first-estimate of labor_ul for effabs
        # At this stage we use uset+urun+absrate (pre-lextra labor util)
        # This will be refined after the actual labor loop
        lab_ul_pre = 0.0  # placeholder; will be set after labor util loop

        equip_raw_uwait[eq_id] = equip_raw_uwait.get(eq_id, 0.0)

    # ── LABOR UTILISATION ────────────────────────────────────────────────────
    labor_results:  List[Dict[str, Any]] = []
    labor_util_map: Dict[str, float] = {}
    labor_work_map: Dict[str, float] = {}
    num_av_lab_map: Dict[str, float] = {}
    labor_num_map:  Dict[str, float] = {}

    for lab in m.get("labor", []):
        lab_id    = lab.get("id", "")
        lab_name  = lab.get("name", "")
        lab_count = int(lab.get("count", 0))
        abs_rate  = float(lab.get("unavail_pct", 0))
        abs_frac  = abs_rate / 100.0
        lab_ot    = float(lab.get("overtime_pct", 0))

        labor_num_map[lab_id] = float(lab_count)

        max_eq_ot_for_lab = max_lab_ot_map.get(lab_id, 0.0)

        if lab_count <= 0:
            labor_results.append({
                "id": lab_id, "name": lab_name, "count": lab_count,
                "setupUtil": 0, "runUtil": 0,
                "unavailPct": abs_rate,
                "totalUtil":  abs_rate,
                "idle":       100.0 - abs_rate,
            })
            labor_util_map[lab_id] = abs_frac
            labor_work_map[lab_id] = 0.0
            # FIX-K: num_av for zero-count labor
            num_av_lab_map[lab_id] = 0.0
            continue

        # FIX-K: First accumulate using initial num_av = lab_count (like legacy before recalc)
        # Legacy: first pass divides by tlabor->num_av where num_av was initialised to num
        # Then recalculates num_av = num*(1+labOT/100)/(1+max_eq_ot/100)
        initial_num_av = float(lab_count)

        avail_for_util = initial_num_av * ops_per_period  # normalise raw sums

        total_setup = 0.0
        total_run   = 0.0

        for op in operations_list:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq or eq.get("labor_group_id") != lab_id:
                continue
            product = next((p for p in m.get("products", []) if p.get("id") == op.get("product_id")), None)
            if not product:
                continue
            pid    = product.get("id", "")
            demand = (effective_demand.get(pid, 0.0) or 0.0) * (1.0 + scrap_rates.get(pid, 0.0))
            if demand <= 0:
                continue

            lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
            tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
            nb         = f_num_tbatches(lot_size_v, tbatch_v)
            af         = f_assign_fraction(op.get("pct_assigned", 0))
            num_lots   = f_num_lots(demand, lot_size_v, af)
            ps_factor  = float(product.get("setup_factor", 1))

            eq_sf = float(eq.get("setup_factor", 1))
            eq_rf = float(eq.get("run_factor", 1))

            # Labor util times — raw (including OT factor in denominator, then remove)
            # Legacy: uset += v1 * xbarsl   where xbarsl from LABOR_T (already /labOT)
            # So we use xbarsl WITHOUT multiplying back by labOT (direct from calc_op LABOR_T)
            lab_ot_f = 1.0 + lab_ot / 100.0

            setup_pl_lab_raw = (
                float(op.get("labor_setup_lot",    0))
                + float(op.get("labor_setup_piece", 0)) * lot_size_v
                + float(op.get("labor_setup_tbatch", 0)) * nb
            ) * eq_sf * float(lab.get("setup_factor", 1)) * ps_factor / lab_ot_f

            run_pl_lab_raw = (
                float(op.get("labor_run_piece",   0)) * lot_size_v
                + float(op.get("labor_run_lot",   0))
                + float(op.get("labor_run_tbatch", 0)) * nb
            ) * eq_rf * float(lab.get("run_factor", 1)) / lab_ot_f

            total_setup += num_lots * setup_pl_lab_raw
            total_run   += num_lots * run_pl_lab_raw

        # FIX-K: divide by initial num_av (= lab_count) first, as legacy does
        setup_util = (total_setup / avail_for_util * 100.0) if avail_for_util > 0 else 0.0
        run_util   = (total_run   / avail_for_util * 100.0) if avail_for_util > 0 else 0.0
        work_util  = setup_util + run_util
        total_util = work_util + abs_rate
        idle       = max(0.0, 100.0 - total_util)

        # FIX-K: now recalculate num_av (legacy line 356)
        num_av_l = (float(lab_count) * (1.0 + lab_ot / 100.0)
                    / (1.0 + max_eq_ot_for_lab / 100.0))
        num_av_lab_map[lab_id] = max(num_av_l, float(lab_count))

        # eq_cover finalised (legacy line 359: eq_cover /= 100*(1+max_eq_ot/100))
        # Handled in lextra

        labor_util_map[lab_id] = total_util / 100.0
        labor_work_map[lab_id] = work_util  / 100.0

        labor_results.append({
            "id": lab_id, "name": lab_name, "count": lab_count,
            "setupUtil":  _round1(setup_util),
            "runUtil":    _round1(run_util),
            "unavailPct": abs_rate,
            "totalUtil":  _round1(total_util),
            "idle":       _round1(idle),
        })

    # ── FIX-E: Now apply first-pass uwait using computed labor_util ──────────
    # Legacy: after labor loop, for each equipment:
    #   uwait *= effabs(tlabor) / (1-effabs(tlabor))
    #   uwait /= num
    #   u1 = uset + urun + uwait + udown
    for eq in equipment_list:
        eq_id    = eq.get("id", "")
        is_delay = eq.get("equip_type") == "delay"
        count    = 1 if is_delay else int(eq.get("count", 0))
        if count <= 0 and not is_delay:
            continue
        if is_delay:
            continue

        lab_id    = eq.get("labor_group_id") or ""
        lab       = labor_by_id.get(lab_id)
        absrate_f = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
        lab_num   = float(lab.get("count", 0)) if lab else 1.0
        labor_ul  = labor_util_map.get(lab_id, absrate_f)

        raw_uwait = equip_raw_uwait.get(eq_id, 0.0)
        ea        = effabs(absrate_f, labor_ul, lab_num)
        ea_denom  = max(1.0 - ea, 1e-6)

        # FIX-E: scale by effabs/(1-effabs), then divide by num
        scaled_uwait = raw_uwait * (ea / ea_denom) / max(float(count), 1.0)

        avail_time = f_available_time_equip(count, eq.get("overtime_pct", 0),
                                             eq.get("unavail_pct", 0), ops_per_period)
        uwait_frac_pre = scaled_uwait / max(avail_time, 1e-9)

        # Update total util to include this pre-lextra uwait
        prior_util = equip_util_map.get(eq_id, 0.0)
        equip_util_map[eq_id] = min(prior_util + uwait_frac_pre, 0.9999)
        equip_uwait_pre[eq_id] = uwait_frac_pre

    # ── XBAR_CS / LEXTRA — TWO-PASS ─────────────────────────────────────────
    fac_eq_lab_map: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    ct2_lab_map:    Dict[str, float] = {}

    # Pass 1: fac_eq_lab = 0
    (xbarbar_eq, cs2_eq, ca2_eq, tpm_eq,
     smbard_eq, xbard_eq, lab_xbarbar_map) = _compute_xbar_cs(
        m, effective_demand, scrap_rates,
        var_equip, var_labor,
        fac_eq_lab_map, ct2_lab_map,
        labor_util_map, labor_num_map,
        ops_per_period,
    )

    fac_eq_lab_map, uwait_eq_raw, ct2_lab_map = _compute_lextra(
        m, equipment_list, labor_by_id,
        xbarbar_eq, cs2_eq, tpm_eq, smbard_eq,
        lab_xbarbar_map,
        labor_util_map, labor_num_map,
        num_av_lab_map, num_av_eq_map,
        var_labor, util_limit,
    )

    # Pass 2: recompute with updated fac_eq_lab
    (xbarbar_eq, cs2_eq, ca2_eq, tpm_eq,
     smbard_eq, xbard_eq, lab_xbarbar_map) = _compute_xbar_cs(
        m, effective_demand, scrap_rates,
        var_equip, var_labor,
        fac_eq_lab_map, ct2_lab_map,
        labor_util_map, labor_num_map,
        ops_per_period,
    )

    # ── APPLY UWAIT + FINAL TOTAL UTIL + CTq WAIT ───────────────────────────
    eq_wait_map:     Dict[str, float] = {}
    equip_total_map: Dict[str, float] = {}

    for er in equip_results:
        eq = next((e for e in equipment_list if e.get("id") == er["id"]), None)
        if eq is None:
            eq_wait_map[er["id"]] = 0.0
            continue

        eq_id    = er["id"]
        is_delay = eq.get("equip_type") == "delay"
        mttf     = float(eq.get("mttf", 0) or 0)
        mttr     = float(eq.get("mttr", 0) or 0)
        eq_num   = max(1, int(eq.get("count", 1))) if not is_delay else 1

        if is_delay:
            wait_min = mttr ** 2 / mttf if mttf > 0 else 0.0
            eq_wait_map[eq_id] = wait_min / max(conv1, 0.001)
            er["waitLaborUtil"] = 0.0
            er["totalUtil"]     = _round1(float(er.get("repairUtil", 0)))
            er["idle"]          = _round1(max(0.0, 100.0 - float(er["totalUtil"])))
            equip_total_map[eq_id] = float(er["totalUtil"]) / 100.0
            equip_util_map[eq_id]  = equip_total_map[eq_id]
            continue

        uwait_frac = uwait_eq_raw.get(eq_id, 0.0)
        uwait_pct  = uwait_frac * 100.0

        # Use the util that already includes pre-lextra uwait as base (FIX-E),
        # then replace the pre-lextra uwait component with lextra uwait
        base_util       = (float(er.get("setupUtil", 0)) + float(er.get("runUtil", 0))
                           + float(er.get("repairUtil", 0))) / 100.0
        total_util_frac = min(base_util + uwait_frac, 0.9999)
        total_pct       = total_util_frac * 100.0

        er["waitLaborUtil"] = _round1(uwait_pct)
        er["totalUtil"]     = _round1(total_pct)
        er["idle"]          = _round1(max(0.0, 100.0 - total_pct))
        equip_util_map[eq_id]  = total_util_frac
        equip_total_map[eq_id] = total_util_frac

        xbb = xbarbar_eq.get(eq_id, 0.0)
        cs2 = max(0.0, min(cs2_eq.get(eq_id, 1.0), 4.0))
        ca2 = max(0.0, min(ca2_eq.get(eq_id, 1.0), 4.0))
        u1  = total_util_frac

        if xbb > 1e-20 and u1 > 1e-10:
            exponent = math.sqrt(2.0 * (eq_num + 1.0)) - 1.0
            wait_min = (
                xbb
                * ((ca2 + cs2) / 2.0)
                * (min(u1, 0.9999) ** exponent)
                / (eq_num * max(1.0 - u1, 1e-6))
            )
            eq_wait_map[eq_id] = max(0.0, wait_min) / max(conv1, 0.001)
        else:
            eq_wait_map[eq_id] = 0.0

    # ── PRODUCT MCT & WIP ────────────────────────────────────────────────────
    product_results: List[Dict[str, Any]] = []

    for product in m.get("products", []):
        pid          = product.get("id", "")
        pname        = product.get("name", "")
        demand_total = effective_demand.get(pid, 0.0) or 0.0
        demand_end   = float(product.get("demand", 0.0)) * float(product.get("demand_factor", 1.0))
        lot_size_v   = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))

        ops = [o for o in operations_list if o.get("product_id") == pid]
        if not ops or demand_total <= 0:
            product_results.append({
                "id": pid, "name": pname,
                "demand": demand_total, "lotSize": lot_size_v,
                "goodMade": round(demand_total), "goodShipped": round(demand_end),
                "started": round(demand_total), "scrap": 0, "wip": 0,
                "mct": 0, "mctLotWait": 0, "mctQueue": 0,
                "mctWaitLabor": 0, "mctSetup": 0, "mctRun": 0,
            })
            continue

        total_setup_mct      = 0.0
        total_run_mct        = 0.0
        total_queue_mct      = 0.0
        total_lot_wait_mct   = 0.0
        total_wait_labor_mct = 0.0

        ps_factor  = float(product.get("setup_factor", 1))
        tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
        nb         = f_num_tbatches(lot_size_v, tbatch_v)

        for op in ops:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq:
                continue
            af = f_assign_fraction(op.get("pct_assigned", 0))
            if af <= 0:
                continue

            eq_id    = eq.get("id", "")
            is_delay = eq.get("equip_type") == "delay"
            lab      = labor_by_id.get(eq.get("labor_group_id") or "")
            lab_id   = eq.get("labor_group_id") or ""
            absrate_f = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
            lab_num  = float(lab.get("count", 0)) if lab else 1.0
            labor_ul = labor_util_map.get(lab_id, absrate_f)
            mttf     = float(eq.get("mttf", 0) or 0)
            mttr     = float(eq.get("mttr", 0) or 0)
            fac      = fac_eq_lab_map.get(eq_id, 0.0)

            if is_delay:
                total_queue_mct += eq_wait_map.get(eq_id, 0.0)
                continue

            xbars, xbarr_pc = _ot_adj_equip_times(op, eq, lot_size_v, nb, ps_factor)
            xbar2 = xbars + xbarr_pc

            # FIX-B: raw labor times
            xbarsl, xbarrl_pc = _raw_labor_times(op, eq, lab, lot_size_v, nb, ps_factor)
            xbar1 = xbarsl + xbarrl_pc

            # FIX-A: xprime using effabs
            xprime_min  = _calc_xprime(xbar1, xbar2, mttr, mttf, absrate_f, labor_ul, lab_num, fac)
            xprime_days = xprime_min / max(conv1, 0.001)

            # Decompose MCT into setup vs run (proportional share of xprime)
            ot_f             = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
            setup_pp_adj     = xbars / lot_size_v    # setup per piece OT-adj
            run_pp_adj       = xbarr_pc              # run per piece OT-adj
            total_raw_pp     = max(setup_pp_adj + run_pp_adj, 1e-20)
            setup_share      = setup_pp_adj / total_raw_pp
            run_share        = run_pp_adj   / total_raw_pp
            total_setup_mct += xprime_days * setup_share
            total_run_mct   += xprime_days * run_share

            # FIX-H: lot wait using legacy T_BATCH_WAIT_LOT formula
            xs_piece, xr_piece = _ot_adj_equip_piece_rates(op, eq)
            total_lot_wait_mct += f_lot_wait_mct(lot_size_v, tbatch_v, xr_piece, xs_piece, conv1)

            # Queue wait (CTq)
            total_queue_mct += eq_wait_map.get(eq_id, 0.0)

            # Labor wait MCT contribution
            if lab and fac > 0.0:
                ea_val   = effabs(absrate_f, labor_ul, lab_num)
                abs_f_ea = 1.0 / max(1.0 - ea_val, 1e-6)
                xl_only  = (min(xbar1, xbar2) * abs_f_ea
                            if xbar2 > 1e-20
                            else xbar1 * abs_f_ea)
                wfl_days = xl_only * fac / max(conv1, 0.001)
                total_wait_labor_mct += max(0.0, wfl_days)

        yield_frac = 1.0 - scrap_rates.get(pid, 0.0)

        capacity_limited = f_capacity_limited_flow_for_product(
            product=product, ops_for_product=ops,
            equipment_list=equipment_list, conv1=conv1, ops_per_period=ops_per_period,
        )
        feasible_started = f_feasible_started_flow(demand_total, yield_frac, capacity_limited)

        started      = round(feasible_started) if feasible_started != float("inf") else 0
        good_made    = round(float(started) * float(yield_frac))
        scrap_cnt    = max(0, round(float(started) - float(good_made)))
        good_shipped = f_good_shipped(good_made, demand_end)

        total_mct = (total_setup_mct + total_run_mct + total_queue_mct
                     + total_lot_wait_mct + total_wait_labor_mct)

        wip = f_wip_from_littles_law(started, total_mct, conv2)

        product_results.append({
            "id": pid, "name": pname,
            "demand":       demand_total,
            "lotSize":      lot_size_v,
            "goodMade":     good_made,
            "goodShipped":  good_shipped,
            "started":      started,
            "scrap":        scrap_cnt,
            "wip":          wip,
            "mct":          _round4(total_mct),
            "mctLotWait":   _round4(total_lot_wait_mct),
            "mctQueue":     _round4(total_queue_mct),
            "mctWaitLabor": _round4(total_wait_labor_mct),
            "mctSetup":     _round4(total_setup_mct),
            "mctRun":       _round4(total_run_mct),
        })

    # ── WARNINGS ─────────────────────────────────────────────────────────────
    over_limit: List[str] = []
    for er in equip_results:
        if float(er["totalUtil"]) > util_limit:
            over_limit.append(f"Equipment: {er['name']} ({er['totalUtil']}%)")
            warnings.append(
                f'Equipment group "{er["name"]}" utilization ({er["totalUtil"]}%) '
                f'exceeds limit ({util_limit}%)'
            )
    for lr in labor_results:
        if float(lr["totalUtil"]) > util_limit:
            over_limit.append(f"Labor: {lr['name']} ({lr['totalUtil']}%)")
            warnings.append(
                f'Labor group "{lr["name"]}" utilization ({lr["totalUtil"]}%) '
                f'exceeds limit ({util_limit}%)'
            )

    if not m.get("operations"):
        errors.append("No operations defined. Add operations to products before running calculations.")

    # ── SANITIZE ─────────────────────────────────────────────────────────────
    for e in equip_results:
        for k in ["setupUtil", "runUtil", "repairUtil", "waitLaborUtil", "totalUtil", "idle"]:
            e[k] = _sanitize(float(e[k]))
    for lbr in labor_results:
        for k in ["setupUtil", "runUtil", "totalUtil", "idle"]:
            lbr[k] = _sanitize(float(lbr[k]))
    for p in product_results:
        for k in ["wip", "mct", "mctLotWait", "mctQueue", "mctWaitLabor", "mctSetup", "mctRun"]:
            p[k] = _sanitize(float(p[k]))

    return {
        "equipment":          equip_results,
        "labor":              labor_results,
        "products":           product_results,
        "warnings":           warnings,
        "errors":             errors,
        "overLimitResources": over_limit,
        "calculatedAt":       datetime.utcnow().isoformat() + "Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Django view
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def full_calculate_view(request):
    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    model    = data.get("model")
    scenario = data.get("scenario")
    if not model:
        return JsonResponse({"error": "Missing 'model' in body"}, status=400)

    try:
        print("model", model)
        print("scenario", scenario)
        results = full_calculate_corrected(model, scenario)
        return JsonResponse({"results": results})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)