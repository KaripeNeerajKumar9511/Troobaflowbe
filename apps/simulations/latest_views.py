"""
full_calculate_corrected_v3.py
==============================
Complete corrected ManuPlan engine — all bugs fixed vs legacy C++ (dll2).

CRITICAL FIXES:
  LVISIT   Visit probability per operation computed from routing via balance
           equations (handles inspection/rework feedback loops). Previously
           assumed lvisit=1 for all ops, causing massive over-utilisation.
  BUG-15   Equipment avail_time removed (1-unavail/100) from denominator.
           Legacy adds unavail as a separate term to total_util.
  BUG-17   IBOM demand propagation now multiplies by (1+parent_scrap).
  BUG-10   T_BATCH_PIECE now returns esetup+esetbatch+esetpiece (full legacy).
  BUG-11   w_labor computed as flowtime residual, not direct approximation.

LATENT FIXES (dormant for zero-OT, lsize=1, tbatch=-1):
  BUG-4    Removed OT factor from _labor_times_no_OT (net cancels in set_xbar_cs).
  BUG-5    xbar1/xbar2 re-scales xbarrl*=MAX(1,lsize) before summing.
  BUG-16   MCT xprime uses T_BATCH_TOTAL modes, not LABOR_T/EQUIP_T.

RETAINED v1/v2 FIXES:
  BUG-3    xlabor uses plain absrate (not effabs).
  BUG-1    Pre-lextra uwait additive with lextra increment.
  BUG-2    f_lot_wait_mct uses xtrans*lsize/lotsiz ratio and vpergood.

NEW OUTPUT FIELDS:
  Equipment: wip_process, wip_queue, wip_total, wait_min, num_av, visits_per_100
  Labor:     wip_total, wip_process, wip_queue, eq_cover, fac_eq_lab
  Product:   w_equip, w_labor, w_setup, w_run, w_lot, wip_lots
  Operation: ueset, uerun, ulset, ulrun, flowtime, n_setups, qpoper,
             w_run, w_setup, w_lot, w_equip, w_labor
"""

from __future__ import annotations
import math
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# ─────────────────────────────────────────────────────────────────────────────
# Constants and tiny helpers
# ─────────────────────────────────────────────────────────────────────────────
EPSILON   = 1e-6
SSEPSILON = 1e-20


def _s(v: float) -> float:
    return v if (v == v and abs(v) != float("inf")) else 0.0


def _r1(x: float) -> float:
    return round(float(x) * 10) / 10


def _r4(x: float) -> float:
    return round(float(x) * 10000) / 10000


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# effabs — calc1.cpp lines 938-952
# For num >= 1: effabs = ul^(num-1) * absrate_frac
# For num <  1: effabs = absrate_frac   (delay server)
# ─────────────────────────────────────────────────────────────────────────────
def effabs(absrate_frac: float, labor_ul: float, labor_num: float) -> float:
    n = float(labor_num) - 1.0
    x = float(absrate_frac) if n < 0.0 else (float(labor_ul) ** n) * float(absrate_frac)
    return min(x, 0.999)


# ─────────────────────────────────────────────────────────────────────────────
# Visit probability — LVISIT FIX
# Solves balance equations v[j] = Σ_i v[i]*R[i,j] with v[DOCK]=1
# via fixed-point iteration. Handles inspection/rework cycles.
# ─────────────────────────────────────────────────────────────────────────────
def compute_visit_probs(product_id: str, operations_list: list, routing_list: list) -> Dict[str, float]:
    routes = [r for r in routing_list if r.get("product_id") == product_id]
    ops    = [op for op in operations_list if op.get("product_id") == product_id]
    op_names = list({op.get("op_name", "") for op in ops})

    adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for r in routes:
        adj[r.get("from_op_name", "")].append(
            (r.get("to_op_name", ""), float(r.get("pct_routed", 0)) / 100.0)
        )

    vp: Dict[str, float] = {n: 0.0 for n in op_names}
    vp["DOCK"] = 1.0

    for _ in range(500):
        vp_new = {n: 0.0 for n in op_names}
        vp_new["DOCK"] = 1.0
        for frm, tos in adj.items():
            fv = vp.get(frm, 0.0)
            if fv <= 0.0:
                continue
            for to, pct in tos:
                if to in ("STOCK", "SCRAP"):
                    continue
                vp_new[to] = vp_new.get(to, 0.0) + fv * pct
        delta = max(abs(vp_new.get(n, 0.0) - vp.get(n, 0.0)) for n in op_names)
        vp = vp_new
        if delta < 1e-9:
            break

    return vp


# ─────────────────────────────────────────────────────────────────────────────
# Yield from routing (P(reach STOCK | start DOCK))
# ─────────────────────────────────────────────────────────────────────────────
def f_yield_from_routing(routing_rows: list, product_id: str) -> float:
    routes = [r for r in routing_rows if r.get("product_id") == product_id]
    adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    nodes = {"DOCK", "STOCK", "SCRAP"}
    for r in routes:
        frm = str(r.get("from_op_name", ""))
        to  = str(r.get("to_op_name",   ""))
        nodes.add(frm); nodes.add(to)
        adj[frm].append((to, float(r.get("pct_routed", 0)) / 100.0))

    if not adj:
        return 1.0

    p_stock: Dict[str, float] = {n: 0.0 for n in nodes}
    p_stock["STOCK"] = 1.0

    for _ in range(500):
        delta = 0.0
        for n in nodes:
            if n in ("STOCK", "SCRAP"):
                continue
            outs  = adj.get(n)
            new_v = 1.0 if not outs else sum(p * p_stock.get(t, 0.0) for t, p in outs)
            delta = max(delta, abs(new_v - p_stock[n]))
            p_stock[n] = new_v
        if delta < 1e-10:
            break

    return min(max(float(p_stock.get("DOCK", 1.0)), 0.0), 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Basic dimension helpers
# ─────────────────────────────────────────────────────────────────────────────
def f_ops_per_period(conv1: float, conv2: float) -> float:
    return max(float(conv1), 0.001) * max(float(conv2), 0.001)


def f_lot_size(lot_size: float, lot_factor: float) -> float:
    return max(1.0, float(lot_size) * float(lot_factor))


def f_tbatch_size(tbatch_size: float, lot_size_val: float) -> float:
    tb = float(tbatch_size)
    return float(lot_size_val) if tb <= 0 else max(1.0, tb)


def f_num_tbatches(lot_size_val: float, tbatch_size_val: float) -> float:
    return float(lot_size_val) / float(tbatch_size_val) if tbatch_size_val > 0 else 1.0


def f_assign_fraction(pct_assigned: float) -> float:
    return float(pct_assigned) / 100.0


def f_num_lots(demand: float, lot_size_val: float, assign_fraction: float) -> float:
    return (float(demand) / float(lot_size_val)) * float(assign_fraction)


# ─────────────────────────────────────────────────────────────────────────────
# BUG-15 FIX: avail_time does NOT include unavail_factor.
# Legacy uset/urun are pure-work fractions; unavail added separately to total_util.
# ─────────────────────────────────────────────────────────────────────────────
def f_avail_equip(count: float, overtime_pct: float, ops_per_period: float) -> float:
    return float(count) * (1.0 + float(overtime_pct) / 100.0) * float(ops_per_period)


def f_avail_labor(count: float, overtime_pct: float, ops_per_period: float) -> float:
    return float(count) * (1.0 + float(overtime_pct) / 100.0) * float(ops_per_period)


# ─────────────────────────────────────────────────────────────────────────────
# calc_op equivalents — matching calc1.cpp exactly
#
# All return (xs_per_lot, xr_per_piece) unless noted.
# Legacy calc_op returns xs (per-lot setup) and xr (per-piece run after /lsize).
# ─────────────────────────────────────────────────────────────────────────────

def _eq_EQUIP_T(op, eq, lot_size_v, nb, ps_factor, lsize=None):
    """calc_op(EQUIP_T): xs=per-lot setup, xr=per-piece run, both OT-adjusted."""
    if lsize is None:
        lsize = lot_size_v
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    sf = float(eq.get("setup_factor", 1))
    rf = float(eq.get("run_factor", 1))
    xs = (float(op.get("equip_setup_lot",    0))
          + float(op.get("equip_setup_tbatch", 0)) * nb
          + float(op.get("equip_setup_piece",  0)) * lsize
         ) * sf * ps_factor / ot
    xr_lot = (float(op.get("equip_run_lot",    0))
              + float(op.get("equip_run_tbatch", 0)) * nb
              + float(op.get("equip_run_piece",  0)) * lsize
             ) * rf / ot
    return xs, (xr_lot / lsize if lsize > EPSILON else 0.0)


def _eq_LABOR_T(op, eq, lab, lot_size_v, nb, ps_factor, lsize=None):
    """calc_op(LABOR_T): xs=per-lot setup, xr=per-piece run, both OT-adjusted."""
    if lsize is None:
        lsize = lot_size_v
    lab_ot = 1.0 + float(lab.get("overtime_pct", 0) if lab else 0) / 100.0
    esf = float(eq.get("setup_factor",  1))
    erf = float(eq.get("run_factor",    1))
    lsf = float(lab.get("setup_factor", 1) if lab else 1)
    lrf = float(lab.get("run_factor",   1) if lab else 1)
    xs = (float(op.get("labor_setup_lot",    0))
          + float(op.get("labor_setup_tbatch", 0)) * nb
          + float(op.get("labor_setup_piece",  0)) * lsize
         ) * esf * lsf * ps_factor / lab_ot
    xr_lot = (float(op.get("labor_run_lot",    0))
              + float(op.get("labor_run_tbatch", 0)) * nb
              + float(op.get("labor_run_piece",  0)) * lsize
             ) * erf * lrf / lab_ot
    return xs, (xr_lot / lsize if lsize > EPSILON else 0.0)


def _labor_no_OT(op, eq, lab, lot_size_v, nb, ps_factor, lsize=None):
    """
    BUG-4 FIX: Labor times WITHOUT OT for use in set_xbar_cs.
    Legacy: calc_op(LABOR_T) /= OT, then set_xbar_cs *= OT — net cancels.
    Returns (xs_per_lot, xr_per_piece).
    """
    if lsize is None:
        lsize = lot_size_v
    esf = float(eq.get("setup_factor",  1))
    erf = float(eq.get("run_factor",    1))
    lsf = float(lab.get("setup_factor", 1) if lab else 1)
    lrf = float(lab.get("run_factor",   1) if lab else 1)
    xs = (float(op.get("labor_setup_lot",    0))
          + float(op.get("labor_setup_tbatch", 0)) * nb
          + float(op.get("labor_setup_piece",  0)) * lsize
         ) * esf * lsf * ps_factor
    xr_lot = (float(op.get("labor_run_lot",    0))
              + float(op.get("labor_run_tbatch", 0)) * nb
              + float(op.get("labor_run_piece",  0)) * lsize
             ) * erf * lrf
    return xs, (xr_lot / lsize if lsize > EPSILON else 0.0)


def _eq_TBATCH_TOTAL_EQUIP(op, eq, lot_size_v, tbatch_v, lsize=None):
    """
    BUG-16 FIX: calc_op(T_BATCH_TOTAL_EQUIP).
    Returns (xs, xr) per transfer-batch (NOT per-piece).
    """
    if lsize is None:
        lsize = lot_size_v
    ot  = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    sf  = float(eq.get("setup_factor", 1))
    rf  = float(eq.get("run_factor",   1))
    tbs = max(1.0, tbatch_v * lsize / lot_size_v) if lot_size_v > 0 else 1.0
    xs  = (float(op.get("equip_setup_lot",    0))
           + float(op.get("equip_setup_tbatch", 0))
           + float(op.get("equip_setup_piece",  0)) * tbs
          ) * sf / ot
    xr  = (float(op.get("equip_run_lot",    0))
           + float(op.get("equip_run_tbatch", 0))
           + float(op.get("equip_run_piece",  0)) * tbs
          ) * rf / ot
    return xs, xr


def _eq_TBATCH_TOTAL_LABOR(op, eq, lab, lot_size_v, tbatch_v, ps_factor, lsize=None):
    """
    BUG-16 FIX: calc_op(T_BATCH_TOTAL_LABOR).
    Returns (xs, xr) per transfer-batch (NOT per-piece).
    """
    if lsize is None:
        lsize = lot_size_v
    lab_ot = 1.0 + float(lab.get("overtime_pct", 0) if lab else 0) / 100.0
    esf = float(eq.get("setup_factor",  1))
    erf = float(eq.get("run_factor",    1))
    lsf = float(lab.get("setup_factor", 1) if lab else 1)
    lrf = float(lab.get("run_factor",   1) if lab else 1)
    tbs = max(1.0, tbatch_v * lsize / lot_size_v) if lot_size_v > 0 else 1.0
    xs  = (float(op.get("labor_setup_lot",    0))
           + float(op.get("labor_setup_tbatch", 0))
           + float(op.get("labor_setup_piece",  0)) * tbs
          ) * esf * lsf * ps_factor / lab_ot
    xr  = (float(op.get("labor_run_lot",    0))
           + float(op.get("labor_run_tbatch", 0))
           + float(op.get("labor_run_piece",  0)) * tbs
          ) * erf * lrf / lab_ot
    return xs, xr


def _eq_TBATCH_PIECE(op, eq):
    """
    BUG-10 FIX: calc_op(T_BATCH_PIECE) — calc1.cpp lines 1055-1062.
    xs = (esetup + esetbatch*1 + esetpiece*1) * facset / OT
    xr = (erlot  + erbatch*1  + epiece*1)    * facrun  / OT
    Returns times for 1-piece + 1-tbatch + 1-lot (all combined).
    """
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    sf = float(eq.get("setup_factor", 1))
    rf = float(eq.get("run_factor",   1))
    xs = (float(op.get("equip_setup_lot",    0))
          + float(op.get("equip_setup_tbatch", 0))
          + float(op.get("equip_setup_piece",  0))
         ) * sf / ot
    xr = (float(op.get("equip_run_lot",    0))
          + float(op.get("equip_run_tbatch", 0))
          + float(op.get("equip_run_piece",  0))
         ) * rf / ot
    return xs, xr


def _eq_TBATCH_WAIT_LOT(op, eq):
    """
    calc_op(T_BATCH_WAIT_LOT) — per-piece components only.
    xs = esetpiece * facset / OT
    xr = epiece    * facrun  / OT
    """
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    xs = float(op.get("equip_setup_piece", 0)) * float(eq.get("setup_factor", 1)) / ot
    xr = float(op.get("equip_run_piece",   0)) * float(eq.get("run_factor",   1)) / ot
    return xs, xr


# ─────────────────────────────────────────────────────────────────────────────
# calc_xprime — calc1.cpp lines 893-924 (verified correct)
# ─────────────────────────────────────────────────────────────────────────────
def _calc_xprime(xbar1, xbar2, mttr, mttf, absrate_frac, labor_ul, labor_num, fac_eq_lab):
    ea    = effabs(absrate_frac, labor_ul, labor_num)
    abs_f = 1.0 / max(1.0 - ea, 1e-6)
    rep   = (mttr / mttf) if mttf > 0.0 else 0.0

    if xbar2 >= xbar1 - EPSILON:
        return max(0.0, xbar2 - xbar1) + xbar2 * rep + xbar1 * abs_f * (1.0 + fac_eq_lab)
    elif xbar2 > SSEPSILON:
        return xbar2 * rep + xbar2 * abs_f * (1.0 + fac_eq_lab)
    else:
        return xbar1 * abs_f * (1.0 + fac_eq_lab)


# ─────────────────────────────────────────────────────────────────────────────
# _compute_xbar_cs — calc2.cpp set_xbar_cs
# BUG-3: xlabor uses plain absrate
# BUG-4: _labor_no_OT (OT cancels in set_xbar_cs)
# BUG-5: re-scale xbarrl *= MAX(1,lsize) before summing xbar1
# ─────────────────────────────────────────────────────────────────────────────
def _compute_xbar_cs(m, effective_demand, scrap_rates, var_equip, var_labor,
                     fac_eq_lab_map, ct2_lab_map, labor_util_map, labor_num_map,
                     ops_per_period, visit_probs_all):
    equipment_list = m.get("equipment", [])
    labor_by_id    = {x["id"]: x for x in m.get("labor", [])}

    xbb = {eq["id"]: 0.0 for eq in equipment_list}
    xbd = {eq["id"]: 0.0 for eq in equipment_list}
    xsb = {eq["id"]: 0.0 for eq in equipment_list}
    tpm = {eq["id"]: 0.0 for eq in equipment_list}
    smb = {eq["id"]: 0.0 for eq in equipment_list}
    lab_xbb: Dict[str, float] = {}
    lab_xbd: Dict[str, float] = {}

    for product in m.get("products", []):
        pid    = product.get("id", "")
        demand = effective_demand.get(pid, 0.0) or 0.0
        if demand <= 0.0:
            continue
        scrap      = scrap_rates.get(pid, 0.0)
        lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
        ps_factor  = float(product.get("setup_factor", 1))
        tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
        nb         = f_num_tbatches(lot_size_v, tbatch_v)
        dlam       = demand * (1.0 + scrap) / (lot_size_v * max(ops_per_period, 1e-9))
        vp_map     = visit_probs_all.get(pid, {})

        for op in m.get("operations", []):
            if op.get("product_id") != pid:
                continue
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq or eq.get("equip_type") == "delay":
                continue
            af = f_assign_fraction(op.get("pct_assigned", 0))
            if af <= 0.0:
                continue

            eq_id        = eq.get("id", "")
            lab_id       = eq.get("labor_group_id") or ""
            lab          = labor_by_id.get(lab_id)
            lsize        = float(op.get("lsize", lot_size_v))
            mttf         = float(eq.get("mttf", 0) or 0)
            mttr         = float(eq.get("mttr", 0) or 0)
            imttf        = 1.0 / mttf if mttf > 0 else 0.0
            abs_frac     = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
            labor_ul     = labor_util_map.get(lab_id, abs_frac)
            labor_num    = labor_num_map.get(lab_id, 1.0)
            fac          = fac_eq_lab_map.get(eq_id, 0.0)
            visit_prob   = vp_map.get(op.get("op_name", ""), 1.0)
            vlam1_full   = dlam * af * visit_prob
            vlam1        = vlam1_full * min(1.0, lsize)  # v1 *= MIN(1, lsize)

            # BUG-4 FIX: no OT in labor times for xbar_cs context
            xbarsl, xbarrl_pc = _labor_no_OT(op, eq, lab, lot_size_v, nb, ps_factor, lsize)
            # BUG-5 FIX: re-scale xbarrl back to per-lot, then apply OT factor
            xbarrl_lot = xbarrl_pc * max(1.0, lsize)
            lab_ot_fac = 1.0 + float(lab.get("overtime_pct", 0) if lab else 0) / 100.0
            xbar1 = (xbarsl + xbarrl_lot) * lab_ot_fac  # calc2.cpp line 88

            xbars, xbarr_pc = _eq_EQUIP_T(op, eq, lot_size_v, nb, ps_factor, lsize)
            xbarr_lot = xbarr_pc * max(1.0, lsize)  # BUG-5 FIX: re-scale
            xbar2 = xbars + xbarr_lot  # calc2.cpp line 89

            xprime  = _calc_xprime(xbar1, xbar2, mttr, mttf, abs_frac, labor_ul, labor_num, fac)
            xm_only = max(0.0, xbar2 - xbar1)
            xl_only = (min(xbar1, xbar2) if xbar2 > SSEPSILON else xbar1) / max(1.0 - abs_frac, 0.01)

            # smbar — uses MIN(xbar1, xbar2) / (1 - plain_absrate)
            x1_smb = min(xbar1, xbar2) if xbar2 > SSEPSILON else xbar1
            smb[eq_id] = smb.get(eq_id, 0.0) + vlam1 * x1_smb / max(1.0 - abs_frac, 0.01)

            eq_cv   = var_equip * float(eq.get("var_factor", 1))
            ct2_lab = ct2_lab_map.get(
                lab_id,
                (var_labor * float(lab.get("var_factor", 1) if lab else 1)) ** 2
            )
            # xprsig^2 — calc2.cpp line 96-99 (uses ct2 and teq->fac_eq_lab)
            xprsig_sq = (2.0 * mttr ** 2 * imttf * xbar2
                         + ((1.0 + imttf * mttr) * eq_cv * xm_only) ** 2
                         + ct2_lab * (xl_only * (1.0 + fac)) ** 2)

            xbb[eq_id] = xbb.get(eq_id, 0.0) + vlam1 * xprime
            xbd[eq_id] = xbd.get(eq_id, 0.0) + vlam1
            xsb[eq_id] = xsb.get(eq_id, 0.0) + vlam1 * (xprsig_sq + xprime ** 2)
            tpm[eq_id] = tpm.get(eq_id, 0.0) + vlam1

            # BUG-3 FIX: xlabor uses plain absrate — calc2.cpp line 124
            xlabor = xbar1 / max(1.0 - abs_frac, 1e-6)
            lab_xbb[lab_id] = lab_xbb.get(lab_id, 0.0) + vlam1 * xlabor
            lab_xbd[lab_id] = lab_xbd.get(lab_id, 0.0) + vlam1

    # Finalise equipment cs2 — calc2.cpp lines 175-195
    xbarbar_eq: Dict[str, float] = {}
    cs2_eq:     Dict[str, float] = {}
    for eq in equipment_list:
        eq_id = eq.get("id", "")
        xbd_v = xbd.get(eq_id, 0.0)
        xbb_v = xbb.get(eq_id, 0.0)
        xsb_v = xsb.get(eq_id, 0.0)
        if int(eq.get("count", 0)) > 0 and xbd_v > SSEPSILON and xbb_v > SSEPSILON:
            xbarbar_eq[eq_id] = xbb_v / xbd_v
            cs2_eq[eq_id]     = max(0.0, (xsb_v * xbd_v / xbb_v ** 2) - 1.0)
        else:
            xbarbar_eq[eq_id] = 0.0
            cs2_eq[eq_id]     = (float(eq.get("var_factor", 1)) * var_equip) ** 2

    # Finalise labor xbarbar — calc2.cpp lines 148-170
    lab_xbarbar_map: Dict[str, float] = {}
    for lab in m.get("labor", []):
        lid = lab.get("id", "")
        xbd_v = lab_xbd.get(lid, 0.0)
        xbb_v = lab_xbb.get(lid, 0.0)
        lab_xbarbar_map[lid] = (xbb_v / xbd_v) if xbd_v > SSEPSILON else 0.0

    return xbarbar_eq, cs2_eq, tpm, smb, lab_xbarbar_map


# ─────────────────────────────────────────────────────────────────────────────
# G/G/c labour wait — simplified Erlang-C based approximation for ggc()
# ─────────────────────────────────────────────────────────────────────────────
def _ggc_wait(labor_ul, num_av, xbarbar, ca2, cs2):
    rho = min(float(labor_ul), 0.9999)
    num = max(float(num_av), 1.0)
    if xbarbar < 1e-10 or rho < 1e-10:
        return 0.0, float(cs2)

    rho_adj = min(rho, 0.9999 / num)

    # Erlang-C
    m_int = max(1, round(num))
    tmp = 1.0; tot = 1.0; mr = m_int * rho_adj
    for i in range(1, m_int):
        tmp *= mr / i; tot += tmp
    tmp *= mr / m_int
    pw_m = tmp / max(1.0 - rho_adj, 1e-20) / max(tot + tmp / max(1.0 - rho_adj, 1e-20), 1e-20)

    mean_wait_m = pw_m * xbarbar / (num * max(1.0 - rho_adj, 1e-6))
    c_sq = 0.5 * (ca2 + cs2)
    xi   = 1.0 if c_sq >= 1.0 else (1.0 - rho_adj) ** (2.0 * (1.0 - c_sq))
    phi  = ((0.5 * cs2 * xi + 0.5 * ca2) / max(ca2 + cs2, 1e-20)
            if ca2 < cs2 else
            (4.0 * (ca2 - cs2) / max(4.0 * ca2 - 3.0 * cs2, 1e-20)
             + cs2 * xi / max(4.0 * ca2 - 3.0 * cs2, 1e-20)))
    mean_wait = phi * c_sq * mean_wait_m
    ct2 = (math.sqrt(max(cs2 * xbarbar ** 2 + mean_wait ** 2, 0.0))
           / max(xbarbar + mean_wait, 1e-20))
    return max(0.0, mean_wait / max(xbarbar, 1e-20)), max(0.0, ct2)


# ─────────────────────────────────────────────────────────────────────────────
# _compute_lextra — calc2.cpp lextra() (verified correct in v2)
# ─────────────────────────────────────────────────────────────────────────────
def _compute_lextra(m, equipment_list, labor_by_id, xbarbar_eq, cs2_eq,
                    tpm_eq, smbard_eq, lab_xbarbar_map,
                    labor_util_map, labor_num_map, num_av_lab_map, num_av_eq_map,
                    var_labor, utlimit):
    fac_eq_lab_map: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    uwait_lextra:   Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    ct2_lab_map:    Dict[str, float] = {}

    for lab in m.get("labor", []):
        lab_id  = lab.get("id", "")
        lab_num = float(lab.get("count", 0))
        lab_vf  = float(lab.get("var_factor", 1))
        cs2_lab = min(4.0, (lab_vf * var_labor) ** 2)
        ct2_lab_map[lab_id] = cs2_lab

        eq_grp = [e for e in equipment_list
                  if e.get("labor_group_id") == lab_id and int(e.get("count", 0)) > 0]
        if not eq_grp:
            continue

        max_ot   = max(float(e.get("overtime_pct", 0)) for e in eq_grp)
        eq_cover = (sum(float(e.get("count", 1)) * (float(e.get("overtime_pct", 0)) + 100.0)
                        for e in eq_grp)
                    / (100.0 * (1.0 + max_ot / 100.0)))
        if eq_cover <= 0.0:
            continue

        labor_ul  = labor_util_map.get(lab_id, 0.0)
        num_av    = num_av_lab_map.get(lab_id, lab_num)
        xbarbar_l = lab_xbarbar_map.get(lab_id, 0.0)
        tlab_tpm  = sum(tpm_eq.get(e["id"], 0.0)    for e in eq_grp)
        tlab_smb  = sum(smbard_eq.get(e["id"], 0.0) for e in eq_grp)

        if tlab_tpm < SSEPSILON:
            continue

        # Branch 1: enough labor or delay
        if lab_num <= 0 or (num_av >= eq_cover + SSEPSILON and eq_cover > 0):
            continue

        # Branch 2: over-utilised
        elif labor_ul > utlimit / 100.0:
            WAIT = (eq_cover - 1.0) if eq_cover > 0 else 1000.0
            fac_g = WAIT if xbarbar_l > SSEPSILON else 0.0
            for e in eq_grp:
                eid = e["id"]; nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
                fac_eq_lab_map[eid] = fac_g
                if nav > SSEPSILON:
                    uwait_lextra[eid] = (fac_g * smbard_eq.get(eid, 0.0)) / nav

        # Branch 3: normal G/G/c
        else:
            u1 = min(0.95, labor_ul)
            tlab_nm = 0.0; tlab_ca = 0.0
            for e in eq_grp:
                eid  = e["id"]
                s1   = num_av_eq_map.get(eid, float(e.get("count", 1)))
                s2   = max(num_av, 1.0)
                smb_v = smbard_eq.get(eid, 0.0)
                cs2_e = min(4.0, cs2_eq.get(eid, 1.0))
                if int(e.get("count", 0)) > 0:
                    r1 = max(0.0, 1.0 - smb_v / max(s1, 1e-20))
                    r2 = u1
                    num_v  = (1.0 + (cs2_e - 1.0) * r1 ** 2 / max(s1 ** 0.5, 1e-10)
                              - (1.0 - r1 ** 2) * (1.0 - r2 ** 2)
                              + (1.0 - r1 ** 2) * (cs2_lab - 1.0) * r2 ** 2 / max(s2 ** 0.5, 1e-10))
                    demon  = 1.0 - (1.0 - r1 ** 2) * (1.0 - r2 ** 2)
                    if demon < SSEPSILON:
                        demon = 1.0; num_v = 1.0
                    tlab_nm += smb_v * (1.0 - smb_v / (tlab_smb * max(s1, 1e-10))) if tlab_smb > SSEPSILON else smb_v
                    tlab_ca += (num_v / demon) * tpm_eq.get(eid, 0.0)
                else:
                    tlab_nm += smb_v
                    tlab_ca += tpm_eq.get(eid, 0.0)

            nm_1  = (eq_cover - 1.0) / eq_cover if eq_cover > 0 else 1.0
            ca2_l = min(4.0, tlab_ca / max(tlab_tpm, SSEPSILON))
            cs2_ggc = min(4.0, (lab_vf * var_labor) ** 0.9)
            fac_raw, ct2_new = _ggc_wait(labor_ul, num_av, xbarbar_l, ca2_l, cs2_ggc)
            WAIT = min(fac_raw * nm_1, eq_cover - 1.0) if eq_cover > 0 else fac_raw * nm_1
            fac_g = WAIT if xbarbar_l > SSEPSILON else 0.0
            ct2_lab_map[lab_id] = ct2_new
            for e in eq_grp:
                eid = e["id"]; nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
                fac_eq_lab_map[eid] = fac_g
                if nav > SSEPSILON:
                    uwait_lextra[eid] = (1.0 if labor_ul > 0.95
                                         else (fac_g * smbard_eq.get(eid, 0.0)) / nav)

    return fac_eq_lab_map, uwait_lextra, ct2_lab_map


# ─────────────────────────────────────────────────────────────────────────────
# Demand / IBOM helpers
# ─────────────────────────────────────────────────────────────────────────────
def compute_effective_demand(products, ibom, scrap_rates) -> Dict[str, float]:
    """
    BUG-17 FIX: comp_demand += parent_demand * units_per_assy * (1 + parent_scrap).
    """
    children: Dict[str, List] = {}
    for entry in ibom:
        children.setdefault(entry.get("parent_product_id"), []).append({
            "componentId":  entry.get("component_product_id"),
            "unitsPerAssy": float(entry.get("units_per_assy", 1)),
        })

    demand: Dict[str, float] = {}
    for p in products:
        demand[p["id"]] = float(p.get("demand", 0)) * float(p.get("demand_factor", 1))

    visited: set = set()
    order: List[str] = []

    def visit(pid):
        if pid in visited: return
        visited.add(pid)
        for k in children.get(pid, []):
            visit(k["componentId"])
        order.append(pid)

    for p in products:
        visit(p["id"])
    order.reverse()

    for parent_id in order:
        parent_scrap = scrap_rates.get(parent_id, 0.0)
        for k in children.get(parent_id, []):
            cid = k["componentId"]
            demand[cid] = (demand.get(cid, 0.0)
                           + demand.get(parent_id, 0.0) * float(k["unitsPerAssy"]) * (1.0 + parent_scrap))

    return demand


def apply_scenario(model, scenario):
    import copy
    if not scenario or not scenario.get("changes"):
        return copy.deepcopy(model)
    m = copy.deepcopy(model)
    for c in scenario.get("changes", []):
        dt  = c.get("dataType"); eid = c.get("entityId")
        fld = c.get("field");    val = c.get("whatIfValue")
        tl  = {"Labor": "labor", "Equipment": "equipment",
                "Product": "products", "Routing": "routing"}.get(dt)
        if tl:
            for item in m.get(tl, []):
                if item.get("id") == eid:
                    if dt == "Product" and fld == "included" and str(val) == "false":
                        item["demand"] = 0
                    elif dt == "Routing":
                        item[fld] = float(val) if val is not None else 0
                    else:
                        item[fld] = val
                    break
        elif dt == "Product Inclusion" and val == "No":
            for p in m.get("products", []):
                if p.get("id") == eid:
                    p["demand"] = 0; break
    return m


def f_capacity_limited_flow(product, ops_for_product, equipment_list, ops_per_period, visit_probs):
    lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
    tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
    nb         = f_num_tbatches(lot_size_v, tbatch_v)
    ps_factor  = float(product.get("setup_factor", 1))
    limits     = []
    for op in ops_for_product:
        eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
        if not eq: continue
        af = f_assign_fraction(op.get("pct_assigned", 0))
        count = int(eq.get("count", 0))
        if af <= 0 or count <= 0: continue
        avail = f_avail_equip(count, eq.get("overtime_pct", 0), ops_per_period)
        lsize = float(op.get("lsize", lot_size_v))
        vp    = visit_probs.get(op.get("op_name", ""), 1.0)
        xs, xr_pc = _eq_EQUIP_T(op, eq, lot_size_v, nb, ps_factor, lsize)
        pp = (xs + xr_pc * lsize) / lot_size_v  # per-piece processing time
        if pp <= 0 or vp <= 0: continue
        limits.append(avail / (af * pp * vp))
    return min(limits) if limits else float("inf")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CALCULATION
# ─────────────────────────────────────────────────────────────────────────────
def full_calculate_corrected(model, scenario=None):
    m = apply_scenario(model, scenario)
    g = m.get("general", {})
    warnings: List[str] = []; errors: List[str] = []

    conv1          = float(g.get("conv1", 480))
    conv2          = float(g.get("conv2", 5))
    ops_per_period = f_ops_per_period(conv1, conv2)
    util_limit     = float(g.get("util_limit", 85))
    var_equip      = float(g.get("var_equip", 0)) / 100.0
    var_labor      = float(g.get("var_labor", 0)) / 100.0

    equipment_list  = m.get("equipment", [])
    labor_by_id     = {x["id"]: x for x in m.get("labor", [])}
    operations_list = m.get("operations", [])
    routing_list    = m.get("routing", [])
    products_list   = m.get("products", [])

    if not operations_list:
        errors.append("No operations defined.")

    # ── Scrap rates and effective demand ─────────────────────────────────────
    scrap_rates: Dict[str, float] = {
        p.get("id", ""): 1.0 - f_yield_from_routing(routing_list, p.get("id", ""))
        for p in products_list
    }
    effective_demand = compute_effective_demand(products_list, m.get("ibom", []), scrap_rates)

    # ── Visit probabilities per product per operation ─────────────────────────
    visit_probs_all: Dict[str, Dict[str, float]] = {
        p.get("id", ""): compute_visit_probs(p.get("id", ""), operations_list, routing_list)
        for p in products_list
    }

    # ── Max equipment OT per labor group ──────────────────────────────────────
    max_lab_ot: Dict[str, float] = {}
    for eq in equipment_list:
        lid = eq.get("labor_group_id") or ""
        max_lab_ot[lid] = max(max_lab_ot.get(lid, -100.0), float(eq.get("overtime_pct", 0)))

    # ── num_av per equipment — calc1.cpp line 405 ─────────────────────────────
    num_av_eq: Dict[str, float] = {}
    for eq in equipment_list:
        eid   = eq.get("id", "")
        lid   = eq.get("labor_group_id") or ""
        cnt   = int(eq.get("count", 0))
        eq_ot = float(eq.get("overtime_pct", 0))
        m_ot  = max_lab_ot.get(lid, 0.0)
        num_av_eq[eid] = max(float(cnt) * (eq_ot + 100.0) / (100.0 + m_ot), float(cnt))

    # ═══════════════════════════════════════════════════════════════════════════
    # EQUIPMENT UTILISATION PASS
    # ═══════════════════════════════════════════════════════════════════════════
    equip_rows:      List[Dict] = []
    equip_util_map:  Dict[str, float] = {}  # total util fraction (for feedback)
    equip_uwait_raw: Dict[str, float] = {}  # raw sum(v1*x1) before scaling

    for eq in equipment_list:
        eq_id    = eq.get("id", "")
        eq_name  = eq.get("name", "")
        count    = int(eq.get("count", 0))
        is_delay = eq.get("equip_type") == "delay"
        lid      = eq.get("labor_group_id") or ""
        lab      = labor_by_id.get(lid)

        base_row = {
            "id": eq_id, "name": eq_name, "count": count,
            "setupUtil": 0.0, "runUtil": 0.0, "repairUtil": 0.0,
            "waitLaborUtil": 0.0, "totalUtil": 0.0, "idle": 100.0,
            "laborGroup": lab.get("name", "") if lab else "",
            "wip_process": 0.0, "wip_queue": 0.0, "wip_total": 0.0,
            "wait_min": 0.0, "num_av": num_av_eq.get(eq_id, float(count)),
            "visits_per_100": 0.0,
        }

        if count <= 0 and not is_delay:
            equip_rows.append(base_row)
            equip_util_map[eq_id] = 0.0
            equip_uwait_raw[eq_id] = 0.0
            continue

        eff_cnt    = 1 if is_delay else count
        avail_time = f_avail_equip(eff_cnt, eq.get("overtime_pct", 0), ops_per_period)
        mttf = float(eq.get("mttf", 0) or 0)
        mttr = float(eq.get("mttr", 0) or 0)

        total_setup = 0.0; total_run = 0.0; total_uwait = 0.0

        for op in operations_list:
            if op.get("equip_id") != eq_id: continue
            product = next((p for p in products_list if p.get("id") == op.get("product_id")), None)
            if not product: continue
            pid      = product.get("id", "")
            demand_i = effective_demand.get(pid, 0.0) * (1.0 + scrap_rates.get(pid, 0.0))
            if demand_i <= 0: continue
            lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
            tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
            nb         = f_num_tbatches(lot_size_v, tbatch_v)
            af         = f_assign_fraction(op.get("pct_assigned", 0))
            num_lots   = f_num_lots(demand_i, lot_size_v, af)
            ps_factor  = float(product.get("setup_factor", 1))
            lsize      = float(op.get("lsize", lot_size_v))
            vp         = visit_probs_all.get(pid, {}).get(op.get("op_name", ""), 1.0)

            xs_e, xr_e = _eq_EQUIP_T(op, eq, lot_size_v, nb, ps_factor, lsize)
            total_setup += num_lots * xs_e       * vp
            total_run   += num_lots * xr_e * lsize * vp  # xr_lot = xr_piece * lsize

            # uwait: v1 * min(xbar1, xbar2) — calc1.cpp line 311
            dlam   = demand_i / (lot_size_v * max(ops_per_period, 1e-9))
            vlam1  = dlam * af * vp
            xbar2  = xs_e + xr_e  # per-piece equip
            xs_l, xr_l = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, ps_factor, lsize)
            xbar1  = xs_l + xr_l  # per-piece labor
            x1_uw  = min(xbar1, xbar2) if xbar2 > SSEPSILON else xbar1
            total_uwait += vlam1 * x1_uw

        setup_frac  = total_setup / avail_time if avail_time > 0 else 0.0
        run_frac    = total_run   / avail_time if avail_time > 0 else 0.0
        repair_frac = (setup_frac + run_frac) * (mttr / mttf) if mttf > 0 else 0.0

        base_row.update({
            "setupUtil":  _r1(setup_frac  * 100),
            "runUtil":    _r1(run_frac    * 100),
            "repairUtil": _r1(repair_frac * 100),
        })
        equip_rows.append(base_row)
        equip_util_map[eq_id]  = setup_frac + run_frac + repair_frac
        equip_uwait_raw[eq_id] = total_uwait

    # ═══════════════════════════════════════════════════════════════════════════
    # LABOR UTILISATION PASS
    # ═══════════════════════════════════════════════════════════════════════════
    labor_rows:     List[Dict] = []
    labor_util_map: Dict[str, float] = {}
    labor_uset_map: Dict[str, float] = {}
    labor_urun_map: Dict[str, float] = {}
    num_av_lab:     Dict[str, float] = {}
    labor_num_map:  Dict[str, float] = {}

    for lab in m.get("labor", []):
        lid       = lab.get("id", "")
        lab_name  = lab.get("name", "")
        lab_count = int(lab.get("count", 0))
        abs_pct   = float(lab.get("unavail_pct", 0))
        abs_frac  = abs_pct / 100.0
        lab_ot    = float(lab.get("overtime_pct", 0))
        labor_num_map[lid] = float(lab_count)

        base_lab = {
            "id": lid, "name": lab_name, "count": lab_count,
            "setupUtil": 0.0, "runUtil": 0.0, "unavailPct": abs_pct,
            "totalUtil": abs_pct, "idle": max(0.0, 100.0 - abs_pct),
            "wip_total": 0.0, "wip_process": 0.0, "wip_queue": 0.0,
            "eq_cover": 0.0, "fac_eq_lab": 0.0,
        }

        if lab_count <= 0:
            labor_rows.append(base_lab)
            labor_util_map[lid] = abs_frac
            labor_uset_map[lid] = 0.0; labor_urun_map[lid] = 0.0
            num_av_lab[lid] = 0.0; continue

        avail_lab   = f_avail_labor(lab_count, lab_ot, ops_per_period)
        total_setup = 0.0; total_run = 0.0

        for op in operations_list:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq or eq.get("labor_group_id") != lid: continue
            product = next((p for p in products_list if p.get("id") == op.get("product_id")), None)
            if not product: continue
            pid      = product.get("id", "")
            demand_i = effective_demand.get(pid, 0.0) * (1.0 + scrap_rates.get(pid, 0.0))
            if demand_i <= 0: continue
            lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
            tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
            nb         = f_num_tbatches(lot_size_v, tbatch_v)
            af         = f_assign_fraction(op.get("pct_assigned", 0))
            num_lots   = f_num_lots(demand_i, lot_size_v, af)
            ps_factor  = float(product.get("setup_factor", 1))
            lsize      = float(op.get("lsize", lot_size_v))
            vp         = visit_probs_all.get(pid, {}).get(op.get("op_name", ""), 1.0)

            xs_l, xr_l = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, ps_factor, lsize)
            total_setup += num_lots * xs_l       * vp
            total_run   += num_lots * xr_l * lsize * vp

        setup_frac = total_setup / avail_lab if avail_lab > 0 else 0.0
        run_frac   = total_run   / avail_lab if avail_lab > 0 else 0.0
        total_frac = setup_frac + run_frac + abs_frac  # absrate added separately
        idle_frac  = max(0.0, 1.0 - total_frac)

        m_ot  = max_lab_ot.get(lid, 0.0)
        nav_l = float(lab_count) * (1.0 + lab_ot / 100.0) / (1.0 + m_ot / 100.0)
        num_av_lab[lid]    = max(nav_l, float(lab_count))
        labor_util_map[lid] = total_frac
        labor_uset_map[lid] = setup_frac
        labor_urun_map[lid] = run_frac

        base_lab.update({
            "setupUtil": _r1(setup_frac * 100),
            "runUtil":   _r1(run_frac   * 100),
            "totalUtil": _r1(total_frac * 100),
            "idle":      _r1(idle_frac  * 100),
        })
        labor_rows.append(base_lab)

    # ── FIX-E: Pre-lextra uwait scaling — calc1.cpp line 397 ─────────────────
    equip_uwait_pre: Dict[str, float] = {}
    for eq in equipment_list:
        eid   = eq.get("id", "")
        cnt   = int(eq.get("count", 0))
        if eq.get("equip_type") == "delay" or cnt <= 0:
            equip_uwait_pre[eid] = 0.0; continue
        avail = f_avail_equip(cnt, eq.get("overtime_pct", 0), ops_per_period)
        lid   = eq.get("labor_group_id") or ""
        lab   = labor_by_id.get(lid)
        af_l  = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
        ln    = float(lab.get("count", 0)) if lab else 1.0
        ea    = effabs(af_l, labor_util_map.get(lid, af_l), ln)
        pre   = equip_uwait_raw.get(eid, 0.0) * (ea / max(1.0 - ea, 1e-6)) / max(float(cnt), 1.0)
        equip_uwait_pre[eid] = pre / max(avail, 1e-9)
        equip_util_map[eid]  = min(equip_util_map.get(eid, 0.0) + equip_uwait_pre[eid], 0.9999)

    # ── XBAR_CS / LEXTRA — TWO-PASS ──────────────────────────────────────────
    fac_eq_lab_map: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    ct2_lab_map:    Dict[str, float] = {}

    def _run_xbar_cs():
        return _compute_xbar_cs(m, effective_demand, scrap_rates, var_equip, var_labor,
                                 fac_eq_lab_map, ct2_lab_map, labor_util_map, labor_num_map,
                                 ops_per_period, visit_probs_all)

    xbarbar_eq, cs2_eq, tpm_eq, smbard_eq, lab_xbar_map = _run_xbar_cs()

    fac_eq_lab_map, uwait_lextra, ct2_lab_map = _compute_lextra(
        m, equipment_list, labor_by_id,
        xbarbar_eq, cs2_eq, tpm_eq, smbard_eq, lab_xbar_map,
        labor_util_map, labor_num_map, num_av_lab, num_av_eq,
        var_labor, util_limit,
    )

    xbarbar_eq, cs2_eq, tpm_eq, smbard_eq, lab_xbar_map = _run_xbar_cs()

    # ── FINAL EQUIP UTIL + CTq WAIT ──────────────────────────────────────────
    eq_wait_map: Dict[str, float] = {}  # queue wait in minutes

    for er in equip_rows:
        eq = next((e for e in equipment_list if e.get("id") == er["id"]), None)
        if not eq:
            eq_wait_map[er["id"]] = 0.0; continue

        eq_id    = er["id"]
        is_delay = eq.get("equip_type") == "delay"
        mttf     = float(eq.get("mttf", 0) or 0)
        mttr     = float(eq.get("mttr", 0) or 0)
        cnt      = max(1, int(eq.get("count", 1))) if not is_delay else 1

        if is_delay:
            eq_wait_map[eq_id] = mttr ** 2 / mttf if mttf > 0 else 0.0
            udown = mttf / (mttf + mttr) if (mttf + mttr) > 0 else 0.0
            er.update({"waitLaborUtil": 0.0, "totalUtil": _r1((1.0 - udown) * 100),
                       "idle": _r1(udown * 100),
                       "visits_per_100": _r1(tpm_eq.get(eq_id, 0.0) * 100)})
            equip_util_map[eq_id] = 1.0 - udown
            continue

        # BUG-1 FIX: total uwait = pre-lextra + lextra
        uwait_total = equip_uwait_pre.get(eq_id, 0.0) + uwait_lextra.get(eq_id, 0.0)
        unavail_f   = float(eq.get("unavail_pct", 0)) / 100.0  # BUG-15 FIX: added separately
        total_f     = min(float(er["setupUtil"] + er["runUtil"] + er["repairUtil"]) / 100.0
                          + uwait_total + unavail_f, 0.9999)

        er["waitLaborUtil"]  = _r1(uwait_total * 100)
        er["totalUtil"]      = _r1(total_f * 100)
        er["idle"]           = _r1(max(0.0, 100.0 - total_f * 100))
        er["visits_per_100"] = _r1(tpm_eq.get(eq_id, 0.0) * 100)
        equip_util_map[eq_id] = total_f

        # CTq — calc1.cpp lines 515-528 (M/G/c P-K formula)
        xbb = xbarbar_eq.get(eq_id, 0.0)
        cs2 = min(max(cs2_eq.get(eq_id, 1.0), 0.0), 4.0)
        ca2 = 1.0  # external arrival CV2 ≈ 1
        u1  = total_f
        if xbb > SSEPSILON and u1 > EPSILON:
            exp_ = math.sqrt(2.0 * (cnt + 1.0)) - 1.0
            wm   = xbb * ((ca2 + cs2) / 2.0) * (min(u1, 0.9999) ** exp_) / (cnt * max(1.0 - u1, 1e-6))
            eq_wait_map[eq_id] = max(0.0, wm)
        else:
            eq_wait_map[eq_id] = 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # PRODUCT MCT, WIP, OPERATION DETAILS
    # ═══════════════════════════════════════════════════════════════════════════
    product_results:   List[Dict] = []
    operation_results: List[Dict] = []

    # Accumulate q per equipment
    eq_q_acc: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}

    for product in products_list:
        pid          = product.get("id", "")
        pname        = product.get("name", "")
        demand_total = effective_demand.get(pid, 0.0) or 0.0
        demand_end   = float(product.get("demand", 0)) * float(product.get("demand_factor", 1))
        lot_size_v   = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
        ps_factor    = float(product.get("setup_factor", 1))
        tbatch_v     = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
        nb           = f_num_tbatches(lot_size_v, tbatch_v)
        ops          = [o for o in operations_list if o.get("product_id") == pid]
        vp_map       = visit_probs_all.get(pid, {})
        yield_frac   = 1.0 - scrap_rates.get(pid, 0.0)

        if not ops or demand_total <= 0:
            product_results.append({
                "id": pid, "name": pname, "demand": demand_total, "lotSize": lot_size_v,
                "goodMade": round(demand_total * yield_frac), "goodShipped": round(demand_end),
                "started": round(demand_total), "scrap": 0, "wip": 0, "wip_lots": 0.0,
                "mct": 0.0, "mctLotWait": 0.0, "mctQueue": 0.0, "mctWaitLabor": 0.0,
                "mctSetup": 0.0, "mctRun": 0.0, "w_equip": 0.0, "w_labor": 0.0,
                "w_setup": 0.0, "w_run": 0.0, "w_lot": 0.0,
            })
            continue

        # Accumulators — all in MINUTES, divided by conv1 at end
        ft_tot = 0.0  # total flowtime (= tsgood * conv1)
        wip_lots = 0.0
        sum_w_setup = sum_w_run = sum_w_lot = sum_w_equip = sum_w_labor = 0.0

        demand_inflated = demand_total * (1.0 + scrap_rates.get(pid, 0.0))
        dlam_base = demand_inflated / (lot_size_v * max(ops_per_period, 1e-9))

        for op in ops:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq: continue
            af = f_assign_fraction(op.get("pct_assigned", 0))
            if af <= 0: continue

            eq_id    = eq.get("id", "")
            is_delay = eq.get("equip_type") == "delay"
            lid      = eq.get("labor_group_id") or ""
            lab      = labor_by_id.get(lid)
            abs_frac = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
            lab_num  = float(lab.get("count", 0)) if lab else 1.0
            lab_ul   = labor_util_map.get(lid, abs_frac)
            mttf     = float(eq.get("mttf", 0) or 0)
            mttr     = float(eq.get("mttr", 0) or 0)
            fac      = fac_eq_lab_map.get(eq_id, 0.0)
            lsize    = float(op.get("lsize", lot_size_v))
            vp       = vp_map.get(op.get("op_name", ""), 1.0)
            # vpergood ≈ visit_prob * yield_frac (fraction of visits that produce good parts)
            # For main-path ops vpergood ~ yield_frac; for rework ops it's scaled down
            # We use vp (visit probability) for scaling flowtime, matching legacy lvisit
            vpergood = vp  # legacy toper->vpergood == lvisit in standard single-product routing
            wait_min = eq_wait_map.get(eq_id, 0.0)
            eq_u     = equip_util_map.get(eq_id, 0.0)
            eq_uwait = equip_uwait_pre.get(eq_id, 0.0) + uwait_lextra.get(eq_id, 0.0)
            v1       = dlam_base * af * vp * min(1.0, lsize)

            # Per-operation util accumulation (for ulset/ulrun/ueset/uerun)
            xs_l_u, xr_l_u = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, ps_factor, lsize)
            xs_e_u, xr_e_u = _eq_EQUIP_T(op, eq, lot_size_v, nb, ps_factor, lsize)
            ulset   = v1 * xs_l_u * 100.0
            ulrun   = v1 * (xr_l_u * max(1.0, lsize)) * 100.0
            ueset   = v1 * xs_e_u * 100.0
            uerun   = v1 * (xr_e_u * max(1.0, lsize)) * 100.0
            n_setup = v1 * ops_per_period

            if is_delay:
                ft_tot += vpergood * wait_min
                operation_results.append({
                    "product": pname, "operation": op.get("op_name", ""),
                    "equipment": eq.get("name", ""), "labor": "",
                    "assign_pct": op.get("pct_assigned", 100),
                    "visit_prob": _r4(vp),
                    "ueset": 0.0, "uerun": 0.0, "ulset": 0.0, "ulrun": 0.0,
                    "flowtime": 0.0, "n_setups": 0.0, "qpoper": 0.0,
                    "w_run": 0.0, "w_setup": 0.0, "w_lot": 0.0, "w_equip": 0.0, "w_labor": 0.0,
                })
                continue

            # BUG-16 FIX: T_BATCH_TOTAL modes for xprime
            xs_tl, xr_tl = _eq_TBATCH_TOTAL_LABOR(op, eq, lab, lot_size_v, tbatch_v, ps_factor, lsize)
            xs_te, xr_te = _eq_TBATCH_TOTAL_EQUIP(op, eq, lot_size_v, tbatch_v, lsize)
            lab_ot_f = 1.0 + float(lab.get("overtime_pct", 0) if lab else 0) / 100.0
            xbar1    = (xs_tl + xr_tl) * lab_ot_f
            xbar2    = xs_te + xr_te

            xprime_min = _calc_xprime(xbar1, xbar2, mttr, mttf, abs_frac, lab_ul, lab_num, fac)
            rpv_min    = xprime_min + wait_min
            flowtime_m = vpergood * rpv_min

            # BUG-10 FIX: T_BATCH_PIECE for w_run and w_setup
            xs_bp, xr_bp = _eq_TBATCH_PIECE(op, eq)
            w_run_m   = vpergood * xr_bp
            w_setup_m = vpergood * xs_bp

            # BUG-2: lot-wait using T_BATCH_WAIT_LOT
            xs_wl, xr_wl = _eq_TBATCH_WAIT_LOT(op, eq)
            ratio     = max(0.0, tbatch_v * lsize / lot_size_v - 1.0) if lot_size_v > 0 else 0.0
            w_lot_m   = vpergood * (xs_wl + xr_wl) * ratio

            # w_equip: queue portion + repair portion — calc1.cpp line 669-672
            w_eq_q  = vpergood * wait_min * (eq_u - eq_uwait) / eq_u if eq_u > SSEPSILON else 0.0
            w_eq_r  = vpergood * (xr_bp + xs_bp + (xs_wl + xr_wl) * ratio) * (mttr / mttf if mttf > 0 else 0)
            w_equip_m = w_eq_q + w_eq_r

            # BUG-11 FIX: w_labor as residual — calc1.cpp line 667
            w_labor_m = max(0.0, flowtime_m - w_run_m - w_setup_m - w_lot_m - w_equip_m)
            if w_labor_m < 0.0001 * max(flowtime_m, 1e-20):
                w_labor_m = 0.0

            qpoper = v1 * rpv_min * max(1.0, lsize)
            eq_q_acc[eq_id] = eq_q_acc.get(eq_id, 0.0) + qpoper

            ft_tot      += flowtime_m
            wip_lots    += v1 * rpv_min
            sum_w_setup += w_setup_m
            sum_w_run   += w_run_m
            sum_w_lot   += w_lot_m
            sum_w_equip += w_equip_m
            sum_w_labor += w_labor_m

            operation_results.append({
                "product":   pname,
                "operation": op.get("op_name", ""),
                "equipment": eq.get("name", ""),
                "labor":     lab.get("name", "") if lab else "",
                "assign_pct": op.get("pct_assigned", 100),
                "visit_prob": _r4(vp),
                "ueset":    _r4(ueset),
                "uerun":    _r4(uerun),
                "ulset":    _r4(ulset),
                "ulrun":    _r4(ulrun),
                "flowtime": _r4(flowtime_m / conv1),
                "n_setups": _r4(n_setup),
                "qpoper":   _r4(qpoper),
                "w_run":    _r4(w_run_m   / conv1),
                "w_setup":  _r4(w_setup_m / conv1),
                "w_lot":    _r4(w_lot_m   / conv1),
                "w_equip":  _r4(w_equip_m / conv1),
                "w_labor":  _r4(w_labor_m / conv1),
            })

        # Convert to shifts (÷ conv1)
        def to_s(m_):
            return m_ / max(conv1, 0.001)

        mct_shifts = to_s(ft_tot)
        cap_lim    = f_capacity_limited_flow(product, ops, equipment_list, ops_per_period, vp_map)
        needed     = demand_total / yield_frac if yield_frac > 0 else float("inf")
        started    = round(min(needed, cap_lim)) if cap_lim != float("inf") else round(needed)
        good_made  = round(started * yield_frac)
        scrap_cnt  = max(0, started - good_made)
        shipped    = round(min(good_made, demand_end))
        wip        = max(0, round(started / max(conv2, 1.0) * mct_shifts))

        product_results.append({
            "id":           pid,
            "name":         pname,
            "demand":       demand_total,
            "lotSize":      lot_size_v,
            "goodMade":     good_made,
            "goodShipped":  shipped,
            "started":      started,
            "scrap":        scrap_cnt,
            "wip":          wip,
            "wip_lots":     _r4(wip_lots),
            "mct":          _r4(mct_shifts),
            "mctSetup":     _r4(to_s(sum_w_setup)),
            "mctRun":       _r4(to_s(sum_w_run)),
            "mctLotWait":   _r4(to_s(sum_w_lot)),
            "mctQueue":     _r4(to_s(sum_w_equip)),   # queue+repair portion of equip wait
            "mctWaitLabor": _r4(to_s(sum_w_labor)),
            "w_equip":      _r4(to_s(sum_w_equip)),
            "w_labor":      _r4(to_s(sum_w_labor)),
            "w_setup":      _r4(to_s(sum_w_setup)),
            "w_run":        _r4(to_s(sum_w_run)),
            "w_lot":        _r4(to_s(sum_w_lot)),
        })

    # ── Equipment WIP (qp, qw, q) — calc1.cpp lines 822-837 ─────────────────
    for er in equip_rows:
        eq = next((e for e in equipment_list if e.get("id") == er["id"]), None)
        if not eq: continue
        eq_id  = er["id"]
        cnt    = int(eq.get("count", 0))
        sf     = float(er.get("setupUtil",  0)) / 100.0
        rf     = float(er.get("runUtil",    0)) / 100.0
        q_tot  = eq_q_acc.get(eq_id, 0.0)
        qp     = (sf + rf) * cnt if cnt > 0 else (sf + rf)  # (uset+urun)*num
        qw     = max(0.0, q_tot - qp)
        er["wip_process"] = _r4(max(0.0, qp))
        er["wip_queue"]   = _r4(qw)
        er["wip_total"]   = _r4(max(0.0, q_tot))
        er["wait_min"]    = _r4(eq_wait_map.get(eq_id, 0.0))

    # ── Labor WIP — calc1.cpp lines 839-848 ──────────────────────────────────
    for lr in labor_rows:
        lid    = lr["id"]
        lab    = labor_by_id.get(lid)
        cnt    = int(lab.get("count", 0)) if lab else 0
        sf     = labor_uset_map.get(lid, 0.0)
        rf     = labor_urun_map.get(lid, 0.0)
        qpl    = (sf + rf) * cnt if cnt > 0 else 0.0

        # ql = sum(smbard * (1 + fac_eq_lab)) over equipment in group
        eq_grp = [e for e in equipment_list if e.get("labor_group_id") == lid]
        ql     = sum(smbard_eq.get(e["id"], 0.0) * (1.0 + fac_eq_lab_map.get(e["id"], 0.0))
                     for e in eq_grp)
        qwl    = max(0.0, ql - qpl)

        # eq_cover for display
        eq_act = [e for e in eq_grp if int(e.get("count", 0)) > 0]
        m_ot   = max((float(e.get("overtime_pct", 0)) for e in eq_act), default=0.0)
        eq_cov = (sum(float(e.get("count", 1)) * (float(e.get("overtime_pct", 0)) + 100.0)
                      for e in eq_act) / (100.0 * (1.0 + m_ot / 100.0))
                  if eq_act else 0.0)
        max_fac = max((fac_eq_lab_map.get(e["id"], 0.0) for e in eq_act), default=0.0)

        lr["wip_total"]   = _r4(max(0.0, ql))
        lr["wip_process"] = _r4(max(0.0, qpl))
        lr["wip_queue"]   = _r4(qwl)
        lr["eq_cover"]    = _r4(eq_cov)
        lr["fac_eq_lab"]  = _r4(max_fac)

    # ── Warnings ─────────────────────────────────────────────────────────────
    over_limit = []
    for er in equip_rows:
        if float(er["totalUtil"]) > util_limit:
            over_limit.append(f"Equipment: {er['name']} ({er['totalUtil']}%)")
            warnings.append(f'Equipment "{er["name"]}" util ({er["totalUtil"]}%) > limit ({util_limit}%)')
    for lr in labor_rows:
        if float(lr["totalUtil"]) > util_limit:
            over_limit.append(f"Labor: {lr['name']} ({lr['totalUtil']}%)")
            warnings.append(f'Labor "{lr["name"]}" util ({lr["totalUtil"]}%) > limit ({util_limit}%)')

    # ── Sanitize ─────────────────────────────────────────────────────────────
    for er in equip_rows:
        for k in ["setupUtil","runUtil","repairUtil","waitLaborUtil","totalUtil","idle",
                  "wip_process","wip_queue","wip_total","wait_min","visits_per_100"]:
            er[k] = _s(float(er.get(k, 0)))
    for lr in labor_rows:
        for k in ["setupUtil","runUtil","totalUtil","idle",
                  "wip_total","wip_process","wip_queue","eq_cover","fac_eq_lab"]:
            lr[k] = _s(float(lr.get(k, 0)))
    for pr in product_results:
        for k in ["wip","wip_lots","mct","mctLotWait","mctQueue","mctWaitLabor",
                  "mctSetup","mctRun","w_equip","w_labor","w_setup","w_run","w_lot"]:
            pr[k] = _s(float(pr.get(k, 0)))
    for opr in operation_results:
        for k in ["ueset","uerun","ulset","ulrun","flowtime","n_setups","qpoper",
                  "w_run","w_setup","w_lot","w_equip","w_labor","visit_prob"]:
            opr[k] = _s(float(opr.get(k, 0)))

    return {
        "equipment":          equip_rows,
        "labor":              labor_rows,
        "products":           product_results,
        "operations":         operation_results,
        "warnings":           warnings,
        "errors":             errors,
        "overLimitResources": over_limit,
        "calculatedAt":       datetime.now(timezone.utc).isoformat(),
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
        results = full_calculate_corrected(model, scenario)
        return JsonResponse({"results": results})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)