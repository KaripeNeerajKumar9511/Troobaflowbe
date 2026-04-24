
from __future__ import annotations
import math
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .dll_full_calculate import DllRunDiagnostics, run_full_calculate_via_dll

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants and tiny helpers
# ─────────────────────────────────────────────────────────────────────────────
EPSILON   = 1e-6
SSEPSILON = 1e-20


def _s(v: float) -> float:
    return v if (v == v and abs(v) != float("inf")) else 0.0


def _sanitize(v: float) -> float:
    """Convert NaN/inf to 0.0 for JSON-safe numeric output."""
    return _s(float(v))


def _r1(x: float) -> float:
    return round(float(x) * 10) / 10


def _r8(x: float) -> float:
    return round(float(x) * 10000) / 10000


def _r4(x: float) -> float:
    return round(float(x) * 10000) / 10000


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _persist_full_calculate_output(
    model: Any,
    scenario: Any,
    response_payload: Dict[str, Any],
    status: str = "success",
) -> Optional[str]:
    """
    Persist each full-calculate request/response snapshot to one folder.
    """
    root = Path(__file__).resolve().parents[2] / "tmp" / "full_calculate_outputs"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    out_path = root / f"run_{stamp}_{status}.json"
    payload = {
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "request": {
            "model": model,
            "scenario": scenario,
        },
        "response": response_payload,
    }
    try:
        out_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        return str(out_path)
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# effabs — calc1.cpp lines 936-957
# For num >= 1: effabs = ul^(num-1) * absrate_frac
# For num <  1: effabs = absrate_frac   (delay server)
# ─────────────────────────────────────────────────────────────────────────────
def effabs(absrate_frac: float, labor_ul: float, labor_num: float) -> float:
    n = float(labor_num) - 1.0
    x = float(absrate_frac) if n < 0.0 else (float(labor_ul) ** n) * float(absrate_frac)
    return min(x, 0.999)


# ─────────────────────────────────────────────────────────────────────────────
# Visit probability — lvisit (LVISIT FIX)
# ─────────────────────────────────────────────────────────────────────────────
def compute_visit_probs(product_id: str, operations_list: list, routing_list: list) -> Dict[str, float]:
    routes  = [r for r in routing_list if r.get("product_id") == product_id]
    ops     = [op for op in operations_list if op.get("product_id") == product_id]
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
# FIX-VPG: compute_vpergood — exact match to calc5.cpp do_visits()
# ─────────────────────────────────────────────────────────────────────────────
def compute_vpergood(
    product_id: str,
    operations_list: list,
    routing_list: list,
    lot_size: float,
) -> Dict[str, float]:
    routes = [r for r in routing_list if r.get("product_id") == product_id]
    ops    = [op for op in operations_list if op.get("product_id") == product_id]

    op_names_set = {op.get("op_name", "") for op in ops}
    for sentinel in ("DOCK", "STOCK", "SCRAP"):
        op_names_set.add(sentinel)
    all_names = sorted(op_names_set)

    fixed = ["DOCK", "STOCK", "SCRAP"]
    real  = [n for n in all_names if n not in fixed]
    ordered = fixed + real
    idx = {name: i for i, name in enumerate(ordered)}
    n   = len(ordered)

    if n <= 3:
        return {"DOCK": 1.0, "STOCK": 1.0, "SCRAP": 0.0}

    pij = np.zeros((n, n), dtype=float)
    for r in routes:
        frm = r.get("from_op_name", "")
        to  = r.get("to_op_name",   "")
        pct = float(r.get("pct_routed", 0)) / 100.0
        fi  = idx.get(frm, -1)
        ti  = idx.get(to,  -1)
        if fi >= 0 and ti >= 0:
            pij[ti, fi] += pct

    s_pij = pij.copy()

    scrap_row = idx["SCRAP"]
    pij_p = pij.copy()
    for j in range(n):
        scrap_frac = pij_p[scrap_row, j]
        if scrap_frac > SSEPSILON and (1.0 - scrap_frac) > EPSILON:
            pij_p[:, j] /= (1.0 - scrap_frac)
            pij_p[scrap_row, j] = 0.0

    stock_idx = idx["STOCK"]
    dock_idx  = idx["DOCK"]

    pij_p[dock_idx,  stock_idx] = 1.0
    pij_p[scrap_row, stock_idx] = 1.0

    A1 = pij_p - np.eye(n)
    b1 = np.zeros(n)
    A1[stock_idx, :] = 0.0
    A1[stock_idx, stock_idx] = 1.0
    b1[stock_idx] = 1.0

    try:
        mreturn1 = np.linalg.solve(A1, b1)
    except np.linalg.LinAlgError:
        vp_fallback = compute_visit_probs(product_id, operations_list, routing_list)
        result = {}
        for op in ops:
            op_name = op.get("op_name", "")
            result[op_name] = vp_fallback.get(op_name, 0.0)
        return result

    m1_stock = max(mreturn1[stock_idx], SSEPSILON)

    lvisit_map: Dict[str, float] = {}
    vper100_map: Dict[str, float] = {}

    for op in ops:
        op_name  = op.get("op_name", "")
        pct_asn  = float(op.get("pct_assigned", 100)) / 100.0
        i        = idx.get(op_name, -1)
        if i < 0:
            continue
        lv = float(mreturn1[i]) * pct_asn
        lvisit_map[op_name]  = lv
        vper100_map[op_name] = 100.0 * float(mreturn1[i]) * lv / m1_stock

    pij2 = s_pij.copy()
    pij2 = pij2.T.copy()
    for i in range(n):
        scrap_out_i = s_pij[scrap_row, i]
        scale_i     = float(mreturn1[i]) * (1.0 - scrap_out_i)
        for j in range(n):
            denom = float(mreturn1[j]) if float(mreturn1[j]) > SSEPSILON else 1.0
            pij2[j, i] = pij2[j, i] * scale_i / denom

    A2 = pij2 - np.eye(n)
    b2 = np.zeros(n)
    A2[stock_idx, :] = 0.0
    A2[stock_idx, stock_idx] = 1.0
    b2[stock_idx] = 1.0
    A2[scrap_row, :] = 0.0
    A2[scrap_row, scrap_row] = 1.0
    b2[scrap_row] = 1.0

    try:
        mreturn2 = np.linalg.solve(A2, b2)
    except np.linalg.LinAlgError:
        mreturn2 = np.ones(n)

    vpergood_map: Dict[str, float] = {}
    for op in ops:
        op_name = op.get("op_name", "")
        i       = idx.get(op_name, -1)
        if i < 0:
            vpergood_map[op_name] = 0.0
            continue
        vp100 = vper100_map.get(op_name, 0.0)
        vpg   = float(mreturn2[i]) * vp100 / 100.0
        vpergood_map[op_name] = max(0.0, vpg)

    vpergood_map["DOCK"]  = 1.0
    vpergood_map["STOCK"] = 1.0
    vpergood_map["SCRAP"] = 0.0

    return vpergood_map


# ─────────────────────────────────────────────────────────────────────────────
# Yield from routing
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


def f_avail_equip(count: float, overtime_pct: float, ops_per_period: float) -> float:
    return float(count) * (1.0 + float(overtime_pct) / 100.0) * float(ops_per_period)


def f_num_lots(demand: float, lot_size_val: float, assign_fraction: float) -> float:
    return (float(demand) // float(lot_size_val)) * float(assign_fraction)  


# ─────────────────────────────────────────────────────────────────────────────
# calc_op equivalents — matching calc1.cpp exactly
# ─────────────────────────────────────────────────────────────────────────────

def _eq_EQUIP_T(op, eq, lot_size_v, nb, lsize=None):
    if lsize is None:
        lsize = lot_size_v
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    sf = float(eq.get("setup_factor", 1))
    rf = float(eq.get("run_factor", 1))
    xs = (float(op.get("equip_setup_lot",    0))
          + float(op.get("equip_setup_tbatch", 0)) * nb
          + float(op.get("equip_setup_piece",  0)) * lsize
         ) * sf / ot
    xr_lot = (float(op.get("equip_run_lot",    0))
              + float(op.get("equip_run_tbatch", 0)) * nb
              + float(op.get("equip_run_piece",  0)) * lsize
             ) * rf / ot
    return xs, (xr_lot / lsize if lsize > EPSILON else 0.0)


def _eq_LABOR_T(op, eq, lab, lot_size_v, nb, lsize=None):
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
         ) * esf * lsf / lab_ot
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
         ) * esf * lsf
    xr_lot = (float(op.get("labor_run_lot",    0))
              + float(op.get("labor_run_tbatch", 0)) * nb
              + float(op.get("labor_run_piece",  0)) * lsize
             ) * erf * lrf
    return xs, (xr_lot / lsize if lsize > EPSILON else 0.0)
def _eq_TBATCH_TOTAL_EQUIP(op, eq, lot_size_v, tbatch_v, lsize=None):
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


def _eq_TBATCH_TOTAL_LABOR(op, eq, lab, lot_size_v, tbatch_v, lsize=None):
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
          ) * esf * lsf / lab_ot
    xr  = (float(op.get("labor_run_lot",    0))
           + float(op.get("labor_run_tbatch", 0))
           + float(op.get("labor_run_piece",  0)) * tbs
          ) * erf * lrf / lab_ot
    return xs, xr


def _eq_TBATCH_PIECE(op, eq):
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
    ot = 1.0 + float(eq.get("overtime_pct", 0)) / 100.0
    xs = float(op.get("equip_setup_piece", 0)) * float(eq.get("setup_factor", 1)) / ot
    xr = float(op.get("equip_run_piece",   0)) * float(eq.get("run_factor",   1)) / ot
    return xs, xr


# ─────────────────────────────────────────────────────────────────────────────
# calc_xprime — calc1.cpp lines 862-930
# ─────────────────────────────────────────────────────────────────────────────
def _calc_xprime(xbar1, xbar2, mttr, mttf, absrate_frac, labor_ul, labor_num, fac_eq_lab):
    ea    = effabs(absrate_frac, labor_ul, labor_num)
    abs_f = 1.0 / max(1.0 - ea, 1e-6)
    rep   = (mttr / mttf) if mttf > 0.0 else 0.0

    if xbar2 >= xbar1 - EPSILON:
        return max(0.0, xbar2 - xbar1) + xbar2 * rep + xbar1 * abs_f * (1.0 + fac_eq_lab)
    elif xbar2 > EPSILON:
        return xbar2 * rep + xbar2 * abs_f * (1.0 + fac_eq_lab)
    else:
        return xbar1 * abs_f * (1.0 + fac_eq_lab)


# ─────────────────────────────────────────────────────────────────────────────
# _compute_xbar_cs — calc2.cpp set_xbar_cs
#
# FIX-3: smbard_eq is now passed in as a frozen input (computed once in the
# util pass, matching C++ where teq->smbard is set in mpc() and never reset).
# The function no longer recomputes smb internally.
#
# FIX-2: ct2_lab_map is always the ORIGINAL squared values — caller must
# pass (var_factor * var_labor)^2, not GGc-updated values.
# ─────────────────────────────────────────────────────────────────────────────
def _compute_xbar_cs(m, effective_demand, scrap_rates, var_equip, var_labor,
                     fac_eq_lab_map, ct2_lab_map, labor_util_map, labor_num_map,
                     ops_per_period, visit_probs_all,
                     smbard_eq_frozen: Dict[str, float]):
    """
    Computes xbarbar, cs2 for equipment and labor.
    smbard_eq_frozen: pre-computed smb values from util pass (never changes).
    ct2_lab_map: MUST be original squared values (faccvs*v_lab/100)^2.
    """
    equipment_list = m.get("equipment", [])
    labor_by_id    = {x["id"]: x for x in m.get("labor", [])}

    xbb = {eq["id"]: 0.0 for eq in equipment_list}
    xbd = {eq["id"]: 0.0 for eq in equipment_list}
    xsb = {eq["id"]: 0.0 for eq in equipment_list}
    tpm = {eq["id"]: 0.0 for eq in equipment_list}
    lab_xbb: Dict[str, float] = {}
    lab_xbd: Dict[str, float] = {}

    for product in m.get("products", []):
        pid    = product.get("id", "")
        demand = effective_demand.get(pid, 0.0) or 0.0
        if demand <= 0.0:
            continue
        scrap      = scrap_rates.get(pid, 0.0)
        lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
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
            vlam1        = dlam * af * visit_prob * min(1.0, lsize)

            xbarsl, xbarrl_pc = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, lsize)
            xbarrl_lot = xbarrl_pc * max(1.0, lsize)
            lab_ot_fac = 1.0 + float(lab.get("overtime_pct", 0) if lab else 0) / 100.0
            xbar1 = (xbarsl + xbarrl_lot) * lab_ot_fac

            xbars, xbarr_pc = _eq_EQUIP_T(op, eq, lot_size_v, nb, lsize)
            xbarr_lot = xbarr_pc * max(1.0, lsize)
            xbar2 = xbars + xbarr_lot

            xprime  = _calc_xprime(xbar1, xbar2, mttr, mttf, abs_frac, labor_ul, labor_num, fac)
            xm_only = max(0.0, xbar2 - xbar1)
            xl_only = (min(xbar1, xbar2) if xbar2 > SSEPSILON else xbar1) / max(1.0 - abs_frac, 0.01)

            eq_cv   = var_equip * float(eq.get("var_factor", 1))
            # FIX-2: use ct2_lab_map which is always original squared value
            ct2_lab = ct2_lab_map.get(
                lab_id,
                (var_labor * float(lab.get("var_factor", 1) if lab else 1)) ** 2
            )
            xprsig_sq = (2.0 * mttr ** 2 * imttf * xbar2
                         + ((1.0 + imttf * mttr) * eq_cv * xm_only) ** 2
                         + ct2_lab * (xl_only * (1.0 + fac)) ** 2)

            xbb[eq_id] = xbb.get(eq_id, 0.0) + vlam1 * xprime
            xbd[eq_id] = xbd.get(eq_id, 0.0) + vlam1
            xsb[eq_id] = xsb.get(eq_id, 0.0) + vlam1 * (xprsig_sq + xprime ** 2)
            tpm[eq_id] = tpm.get(eq_id, 0.0) + vlam1

            xlabor = xbar1 / max(1.0 - abs_frac, 1e-6)
            lab_xbb[lab_id] = lab_xbb.get(lab_id, 0.0) + vlam1 * xlabor
            lab_xbd[lab_id] = lab_xbd.get(lab_id, 0.0) + vlam1

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

    lab_xbarbar_map: Dict[str, float] = {}
    # FIX-2: recompute cs2 for labor as ^0.9 (Phase-2 of set_xbar_cs in C++)
    lab_cs2_map: Dict[str, float] = {}
    for lab in m.get("labor", []):
        lid = lab.get("id", "")
        xbd_v = lab_xbd.get(lid, 0.0)
        xbb_v = lab_xbb.get(lid, 0.0)
        lab_xbarbar_map[lid] = (xbb_v / xbd_v) if xbd_v > SSEPSILON else 0.0
        # Phase-2: if any flow passed through, use ^0.9; else keep ^2
        if xbb_v > SSEPSILON:
            lab_cs2_map[lid] = min(4.0, (float(lab.get("var_factor", 1)) * var_labor) ** 0.9)
        else:
            lab_cs2_map[lid] = min(4.0, (float(lab.get("var_factor", 1)) * var_labor) ** 2)

    return xbarbar_eq, cs2_eq, tpm, lab_xbarbar_map, lab_cs2_map


# ─────────────────────────────────────────────────────────────────────────────
# Standard normal CDF
# ─────────────────────────────────────────────────────────────────────────────
def _ncdf(x: float) -> float:
    return 1.0 - 0.5 * math.erfc(x / math.sqrt(2.0))


# ─────────────────────────────────────────────────────────────────────────────
# Erlang-C
# ─────────────────────────────────────────────────────────────────────────────
def _erlangC(rho: float, m: float) -> float:
    mrho = m * rho
    mm   = max(1, int(m))
    temp = 1.0
    for i in range(1, mm + 1):
        temp *= mrho / i
    numerator = temp / max(1.0 - rho, 1e-20)
    denom = 1.0
    for k in range(1, mm):
        t = 1.0
        for i in range(1, k + 1):
            t *= mrho / i
        denom += t
    denom += numerator
    return numerator / max(denom, 1e-20)


# ─────────────────────────────────────────────────────────────────────────────
# G/G/c labour wait — matches calc8.cpp ggc() exactly
# ─────────────────────────────────────────────────────────────────────────────
def _ggc_wait(labor_ul: float, num_av: float, xbarbar: float, ca2: float, cs2: float):
    rho      = float(labor_ul)
    num      = float(num_av)
    orig_nav = num

    if num < 1.0:
        rho = rho * num
        num = 1.0

    if xbarbar < EPSILON or rho < EPSILON:
        return 0.0, float(cs2)

    ECBOUND = 70.0
    if num <= ECBOUND:
        probwait_m = _erlangC(rho, num)
    else:
        wb  = (1.0 - rho) * math.sqrt(num)
        exp_arg = min(700.0, 0.5 * wb * wb)
        probwait_m = 1.0 / max(1.0 + 2.5066 * wb * _ncdf(wb) * math.exp(exp_arg), 1e-20)

    meanwait_m = probwait_m * xbarbar / max(orig_nav * (1.0 - rho), 1e-20)

    gamma = min(0.24,
                (1.0 - rho) * (num - 1.0) * (math.sqrt(4.0 + 5.0 * num) - 2.0)
                / max(16.0 * num * rho, 1e-20))
    phi1 = 1.0 + gamma
    phi2 = 1.0 - 4.0 * gamma
    exp2 = max(-700.0, -2.0 * (1.0 - rho) / max(3.0 * rho, 1e-20))
    phi3 = phi2 * math.exp(exp2)
    phi4 = min(1.0, 0.5 * (phi1 + phi3))

    c_sq = 0.5 * (ca2 + cs2)
    xi   = 1.0 if c_sq >= 1.0 else phi4 ** (2.0 * (1.0 - c_sq))

    if ca2 >= cs2:
        d    = max(4.0 * ca2 - 3.0 * cs2, 1e-20)
        phi  = 4.0 * (ca2 - cs2) * phi1 / d + cs2 * xi / d
    else:
        d    = max(ca2 + cs2, 1e-20)
        phi  = (cs2 - ca2) * phi3 * 0.5 / d + (cs2 + 3.0 * ca2) * xi * 0.5 / d

    meanwait = phi * c_sq * meanwait_m

    z_val   = (ca2 + cs2) / max(1.0 + cs2, 1e-20)
    gamma2  = (num - num * rho - 0.5) / max(math.sqrt(max(num * rho * z_val, 1e-20)), 1e-20)
    sn      = math.sqrt(max(num, 0.0))
    omr     = max(1.0 - rho, 1e-20)
    denom0  = max(1.0 - _ncdf(omr * sn), 1e-20)

    pi6 = 1.0 - _ncdf(gamma2)
    pi5 = min(1.0, (1.0 - _ncdf(2.0 * omr * sn / max(1.0 + ca2, 1e-20)))
              * probwait_m / denom0)
    pi4 = min(1.0, (1.0 - _ncdf((1.0 + cs2) * omr * sn / max(ca2 + cs2, 1e-20)))
              * probwait_m / denom0)

    pi1 = rho * rho * pi4 + (1.0 - rho * rho) * pi5
    pi2 = ca2 * pi1 + (1.0 - ca2) * pi6
    pi3 = (2.0 * (1.0 - ca2) * (gamma2 - 0.5) * pi2
           + (1.0 - 2.0 * (1.0 - ca2) * (gamma2 - 0.5)) * pi1)

    if num < 7 or gamma2 <= 0.5 or ca2 >= 1.0:
        pi = pi1
    elif num >= 7 and gamma2 >= 1.0 and ca2 < 1.0:
        pi = pi2
    elif num >= 7 and ca2 < 1.0 and 0.5 < gamma2 < 1.0:
        pi = pi3
    else:
        pi = pi1

    probwait = min(pi, 1.0)

    if probwait > EPSILON:
        if cs2 >= 1.0:
            dscube = 3.0 * cs2 * (1.0 + cs2)
        else:
            dscube = (2.0 * cs2 + 1.0) * (cs2 + 1.0)
        cd_sq = 2.0 * rho - 1.0 + 4.0 * (1.0 - rho) * dscube / max(3.0 * (cs2 + 1.0) ** 2, 1e-20)
        cw_sq = max(0.0, (cd_sq + 1.0 - probwait) / max(probwait, 1e-20))
    else:
        cw_sq = 0.0

    ct2 = (math.sqrt(max(cs2 * xbarbar ** 2 + cw_sq * meanwait ** 2, 0.0))
           / max(xbarbar + meanwait, 1e-20))

    return max(0.0, meanwait / max(xbarbar, 1e-20)), max(0.0, ct2)


# ─────────────────────────────────────────────────────────────────────────────
# _compute_ca2 — calc3.cpp set_cacalc() exact match
# ─────────────────────────────────────────────────────────────────────────────
def _compute_ca2(m, equipment_list, effective_demand, scrap_rates,
                 visit_probs_all, equip_util_map, cs2_eq, num_av_eq,
                 ops_per_period, var_part):
    operations_list = m.get("operations", [])
    routing_list    = m.get("routing",    [])
    products_list   = m.get("products",   [])

    active_eq = [eq for eq in equipment_list
                 if eq.get("equip_type") != "delay" and int(eq.get("count", 0)) > 0]
    if not active_eq:
        return {}

    n      = len(active_eq)
    eq_idx = {eq["id"]: i for i, eq in enumerate(active_eq)}

    lam    = [0.0] * n
    P0_raw = [0.0] * n
    lam0   = [0.0] * n
    C2lam0 = [0.0] * n
    oV     = [0.0] * n
    lamij  = [[0.0] * n for _ in range(n)]

    for product in products_list:
        pid    = product.get("id", "")
        demand = effective_demand.get(pid, 0.0)
        if demand <= 0.0:
            continue
        scrap      = scrap_rates.get(pid, 0.0)
        lot_size_v = max(1.0, float(product.get("lot_size", 1))
                              * float(product.get("lot_factor", 1)))
        dlam       = demand * (1.0 + scrap) / (lot_size_v * max(ops_per_period, 1e-9))
        vp_map     = visit_probs_all.get(pid, {})
        cv_ext_sq  = (float(product.get("var_factor", 1)) * var_part) ** 2

        ops_pid = [op for op in operations_list if op.get("product_id") == pid]
        op_to_eqs: Dict[str, List] = defaultdict(list)
        for op in ops_pid:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if eq and eq.get("equip_type") != "delay":
                af = float(op.get("pct_assigned", 0)) / 100.0
                if af > 0:
                    op_to_eqs[op.get("op_name", "")].append((eq["id"], af))

        rt_from: Dict[str, List] = defaultdict(list)
        for r in routing_list:
            if r.get("product_id") != pid:
                continue
            pct = float(r.get("pct_routed", 0)) / 100.0
            if pct > 0:
                rt_from[r.get("from_op_name", "")].append(
                    (r.get("to_op_name", ""), pct))

        for op in ops_pid:
            op_name = op.get("op_name", "")
            if op_name in ("DOCK", "STOCK", "SCRAP"):
                continue
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq or eq.get("equip_type") == "delay":
                continue
            eid_i = eq["id"]
            idx_i = eq_idx.get(eid_i, -1)
            if idx_i < 0:
                continue
            af_i = float(op.get("pct_assigned", 0)) / 100.0
            if af_i <= 0:
                continue
            vp_i = vp_map.get(op_name, 0.0)
            lam[idx_i] += dlam * vp_i * af_i
            for to_op_name, pct_rt in rt_from.get(op_name, []):
                if to_op_name in ("STOCK", "SCRAP"):
                    continue
                for eid_j, af_j in op_to_eqs.get(to_op_name, []):
                    idx_j = eq_idx.get(eid_j, -1)
                    if idx_j < 0:
                        continue
                    lamij[idx_j][idx_i] += dlam * vp_i * af_i * pct_rt * af_j

        for to_op_name, pct_rt in rt_from.get("DOCK", []):
            if to_op_name in ("STOCK", "SCRAP"):
                continue
            for eid_j, af_j in op_to_eqs.get(to_op_name, []):
                idx_j = eq_idx.get(eid_j, -1)
                if idx_j < 0:
                    continue
                xtemp = dlam * (1.0 + scrap) * pct_rt * af_j
                P0_raw[idx_j]  += xtemp
                lam0[idx_j]    += xtemp
                C2lam0[idx_j]  += xtemp * cv_ext_sq
                oV[idx_j]      += xtemp * xtemp

    P0    = [0.0] * n
    C0    = [1.0] * n
    nV    = [0.0] * n
    nW    = [0.0] * n
    X     = [1.0] * n

    for i, eq in enumerate(active_eq):
        eid  = eq["id"]
        u1   = min(equip_util_map.get(eid, 0.0), 0.95)
        cnt  = max(1, int(eq.get("count", 1)))
        cs2i = cs2_eq.get(eid, 1.0)

        if lam[i] > SSEPSILON:
            ivlam = 1.0 / lam[i]
            P0[i] = P0_raw[i] / lam[i]

            if oV[i] > SSEPSILON:
                if lam0[i] < SSEPSILON:
                    oV[i] = 0.0
                else:
                    oV[i] = lam0[i] ** 2 / oV[i]
                denom_ow = 1.0 + 4.0 * (1.0 - u1) ** 2 * (oV[i] - 1.0)
                oW_i = 1.0 / denom_ow if denom_ow > SSEPSILON else 1.0
            else:
                oW_i = 0.0

            C0[i] = ((1.0 - oW_i) + oW_i * C2lam0[i] / lam0[i]
                     if lam0[i] > SSEPSILON else 1.0)

            sum_sq = P0[i] ** 2
            for j in range(n):
                sum_sq += (lamij[i][j] * ivlam) ** 2
            if sum_sq > SSEPSILON:
                nV[i] = 1.0 / sum_sq
            if nV[i] > SSEPSILON:
                denom_nw = 1.0 + 4.0 * (1.0 - u1) ** 2 * (nV[i] - 1.0)
                nW[i] = 1.0 / denom_nw if denom_nw > SSEPSILON else 1.0
        else:
            P0[i] = 0.0
            C0[i] = 1.0

        if cnt > 0:
            X[i] = 1.0 + cnt ** (-0.5) * (max(0.2, cs2i) - 1.0)
        else:
            X[i] = cs2i

    mat = np.zeros((n, n))
    rhs = np.zeros(n)

    for i in range(n):
        eid_i   = active_eq[i]["id"]
        lam_i   = lam[i]
        ivlam_i = 1.0 / lam_i if lam_i > SSEPSILON else 0.0
        acc_A   = 0.0

        for j in range(n):
            eid_j = active_eq[j]["id"]
            u1_j  = min(equip_util_map.get(eid_j, 0.0), 0.95)
            lam_j = lam[j]
            pij   = lamij[i][j] * ivlam_i
            qij   = lamij[i][j] / lam_j if lam_j > SSEPSILON else 0.0
            mat[i][j] = nW[i] * pij * qij * (1.0 - u1_j ** 2)
            acc_A += pij * ((1.0 - qij) + qij * u1_j ** 2 * X[j])

        A_i       = 1.0 + nW[i] * ((P0[i] * C0[i] - 1.0) + acc_A)
        mat[i][i] -= 1.0
        rhs[i]     = -A_i

    try:
        ca2_vals = np.linalg.solve(mat, rhs)
        return {active_eq[i]["id"]: float(max(0.0, ca2_vals[i]))
                for i in range(n)}
    except np.linalg.LinAlgError:
        return {eq["id"]: cs2_eq.get(eq["id"], 1.0) for eq in active_eq}


# ─────────────────────────────────────────────────────────────────────────────
# _compute_lextra — calc2.cpp lextra()
#
# FIX-1: cs2_lab in num_v formula now uses lab_cs2_map (^0.9) from
#         _compute_xbar_cs Phase-2, matching C++ tlabor->cs2.
# FIX-3: smbard_eq passed in as frozen input from util pass.
# ─────────────────────────────────────────────────────────────────────────────
def _compute_lextra(m, equipment_list, labor_by_id, xbarbar_eq, cs2_eq,
                    tpm_eq, smbard_eq, lab_xbarbar_map,
                    labor_util_map, labor_num_map, num_av_lab_map, num_av_eq_map,
                    var_labor, utlimit, equip_sru_map,
                    lab_cs2_map: Dict[str, float]):
    """
    lab_cs2_map: tlabor->cs2 after Phase-2 of set_xbar_cs = (faccvs*v_lab)^0.9
                 Used in num_v formula (ca2 computation).  FIX-1.
    smbard_eq:   frozen smb values from util pass.  FIX-3.
    """
    fac_eq_lab_map: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    uwait_lextra:   Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    # ct2_lab_map returned here is NOT used in second xbar_cs — caller uses
    # original squared values.  We return it only for the ggc ct2 output.
    ct2_lab_map:    Dict[str, float] = {}
    uwait_replace_set: set = set()

    for lab in m.get("labor", []):
        lab_id  = lab.get("id", "")
        lab_num = float(lab.get("count", 0))
        lab_vf  = float(lab.get("var_factor", 1))

        # ct2 starts as squared (Phase-1 of set_xbar_cs)
        ct2_initial = min(4.0, (lab_vf * var_labor) ** 2)
        ct2_lab_map[lab_id] = ct2_initial

        # FIX-1: cs2 for num_v uses ^0.9 (Phase-2), matching C++ tlabor->cs2
        cs2_lab_09 = lab_cs2_map.get(lab_id, min(4.0, (lab_vf * var_labor) ** 0.9))

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
        tlab_tpm  = sum(tpm_eq.get(e["id"], 0.0) for e in eq_grp)
        tlab_smb  = sum(smbard_eq.get(e["id"], 0.0) for e in eq_grp)

        if tlab_tpm < SSEPSILON:
            continue

        if lab_num <= 0 or (num_av >= eq_cover + SSEPSILON and eq_cover > 0):
            continue

        elif labor_ul > utlimit / 100.0:
            WAIT = (eq_cover - 1.0) if eq_cover > 0 else 1000.0
            fac_g = WAIT if xbarbar_l > SSEPSILON else 0.0
            for e in eq_grp:
                eid = e["id"]
                nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
                fac_eq_lab_map[eid] = fac_g
                if int(e.get("count", 0)) > 0 and nav > SSEPSILON:
                    uwait_lextra[eid] = (fac_g * smbard_eq.get(eid, 0.0)) / nav
                else:
                    uwait_lextra[eid] = 0.0

        else:
            u1 = min(0.95, labor_ul)
            tlab_nm = 0.0
            tlab_ca = 0.0
            for e in eq_grp:
                eid   = e["id"]
                s1    = num_av_eq_map.get(eid, float(e.get("count", 1)))
                s2    = max(num_av, 1.0)
                smb_v = smbard_eq.get(eid, 0.0)
                cs2_e = min(4.0, cs2_eq.get(eid, 1.0))
                if int(e.get("count", 0)) > 0:
                    r1 = max(0.0, 1.0 - smb_v / max(s1, 1e-20))
                    r2 = u1
                    # FIX-1: use cs2_lab_09 (^0.9) to match C++ tlabor->cs2
                    num_v  = (1.0 + (cs2_e - 1.0) * r1 ** 2 / max(s1 ** 0.5, 1e-10)
                              - (1.0 - r1 ** 2) * (1.0 - r2 ** 2)
                              + (1.0 - r1 ** 2) * (cs2_lab_09 - 1.0) * r2 ** 2 / max(s2 ** 0.5, 1e-10))
                    demon  = 1.0 - (1.0 - r1 ** 2) * (1.0 - r2 ** 2)
                    if demon < SSEPSILON:
                        demon = 1.0
                        num_v = 1.0
                    tlab_nm += smb_v * (1.0 - smb_v / (tlab_smb * max(s1, 1e-10))) if tlab_smb > SSEPSILON else smb_v
                    tlab_ca += (num_v / demon) * tpm_eq.get(eid, 0.0)
                else:
                    tlab_nm += smb_v
                    tlab_ca += tpm_eq.get(eid, 0.0)

            nm_1  = (eq_cover - 1.0) / eq_cover if eq_cover > 0 else 1.0
            ca2_l = min(4.0, tlab_ca / max(tlab_tpm, SSEPSILON))
            # cs2 fed to GGc is also ^0.9 (same as C++ tlabor->cs2 at this point)
            cs2_ggc = min(4.0, (lab_vf * var_labor) ** 0.9)
            fac_raw, ct2_new = _ggc_wait(labor_ul, num_av, xbarbar_l, ca2_l, cs2_ggc)
            WAIT = min(fac_raw * nm_1, eq_cover - 1.0) if eq_cover > 0 else fac_raw * nm_1
            fac_g = WAIT if xbarbar_l > SSEPSILON else 0.0
            # Note: ct2_new is stored but NOT fed back into ct2_lab_map used
            # for the second xbar_cs call (FIX-2).

            lab_balance = int(lab.get("balance", 0))
            if lab_balance == -1 and equip_sru_map is not None:
                _do_balance(eq_grp, fac_eq_lab_map, uwait_lextra,
                            {e["id"]: 0.0 for e in eq_grp},
                            smbard_eq, num_av_eq_map, equip_sru_map)
            else:
                for e in eq_grp:
                    eid = e["id"]
                    nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
                    fac_eq_lab_map[eid] = fac_g
                    if nav > SSEPSILON:
                        if labor_ul > 0.95:
                            uwait_lextra[eid] = 1.0
                            uwait_replace_set.add(eid)
                        else:
                            uwait_lextra[eid] = (fac_g * smbard_eq.get(eid, 0.0)) / nav

    return fac_eq_lab_map, uwait_lextra, ct2_lab_map, uwait_replace_set


# ─────────────────────────────────────────────────────────────────────────────
# _do_balance — calc2.cpp do_balance()
#
# FIX-4: t_util now correctly multiplies by num_av, matching C++:
#         t_util += teq->num * (teq->uset + teq->urun + teq->udown)
# ─────────────────────────────────────────────────────────────────────────────
def _do_balance(eq_grp, fac_eq_lab_map, uwait_lextra,
                equip_uwait_pre, smbard_eq, num_av_eq_map, equip_sru_map):
    t_wt_lab = 0.0
    eq_A: Dict[str, float] = {}
    for e in eq_grp:
        eid = e["id"]
        nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
        if nav > 0:
            pre = equip_uwait_pre.get(eid, 0.0)
            fac = fac_eq_lab_map.get(eid, 0.0)
            smb = smbard_eq.get(eid, 0.0)
            t_wt_lab += pre * nav
            t_wt_lab += fac * smb
            eq_A[eid] = pre
            uwait_lextra[eid] = 0.0
            fac_eq_lab_map[eid] = -1.0

    done = False
    net_util = 0.0
    while not done:
        eq_cnt = 0.0
        t_util = 0.0
        for e in eq_grp:
            eid = e["id"]
            nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
            smb = smbard_eq.get(eid, 0.0)
            if fac_eq_lab_map.get(eid, 0.0) == -1.0 and smb >= 0:
                eq_cnt += nav
                # FIX-4: multiply by nav to match C++ teq->num * (uset+urun+udown)
                t_util += nav * equip_sru_map.get(eid, 0.0)
        if eq_cnt <= 0:
            break
        net_util = 1.0001 * (t_wt_lab + t_util) / eq_cnt
        done = True
        for e in eq_grp:
            eid = e["id"]
            smb = smbard_eq.get(eid, 0.0)
            if fac_eq_lab_map.get(eid, 0.0) < 0 and smb >= 0:
                if equip_sru_map.get(eid, 0.0) > net_util:
                    fac_eq_lab_map[eid] = 0.0
                    done = False

    for e in eq_grp:
        eid = e["id"]
        nav = num_av_eq_map.get(eid, float(e.get("count", 1)))
        smb = smbard_eq.get(eid, 0.0)
        if fac_eq_lab_map.get(eid, 0.0) < 0.0 and smb >= 0:
            uwait_new = net_util - equip_sru_map.get(eid, 0.0)
            pre_a = eq_A.get(eid, 0.0)
            uwait_lextra[eid] = max(0.0, uwait_new - pre_a)
            fac_eq_lab_map[eid] = (uwait_lextra[eid] * nav / smb
                                   if smb > SSEPSILON else 0.0)
        if fac_eq_lab_map.get(eid, 0.0) < 0:
            fac_eq_lab_map[eid] = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Demand / IBOM helpers
# ─────────────────────────────────────────────────────────────────────────────
def compute_effective_demand(products, ibom, scrap_rates) -> Dict[str, float]:
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
        xs, xr_pc = _eq_EQUIP_T(op, eq, lot_size_v, nb, lsize)
        pp = (xs + xr_pc * lsize) / lot_size_v
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
    var_part       = float(g.get("var_part",  0)) / 100.0

    equipment_list  = m.get("equipment", [])
    labor_by_id     = {x["id"]: x for x in m.get("labor", [])}
    operations_list = m.get("operations", [])
    routing_list    = m.get("routing", [])
    products_list   = m.get("products", [])

    if not operations_list:
        errors.append("No operations defined.")

    # ── Scrap rates and effective demand ──────────────────────────────────────
    scrap_rates: Dict[str, float] = {
        p.get("id", ""): 1.0 - f_yield_from_routing(routing_list, p.get("id", ""))
        for p in products_list
    }
    effective_demand = compute_effective_demand(products_list, m.get("ibom", []), scrap_rates)

    # ── Visit probabilities (lvisit) — used in UTIL pass ─────────────────────
    visit_probs_all: Dict[str, Dict[str, float]] = {
        p.get("id", ""): compute_visit_probs(p.get("id", ""), operations_list, routing_list)
        for p in products_list
    }

    # ── FIX-VPG: vpergood per operation — used in MCT pass ───────────────────
    vpergood_all: Dict[str, Dict[str, float]] = {}
    for p in products_list:
        pid = p.get("id", "")
        lot_size_v = f_lot_size(p.get("lot_size", 1), p.get("lot_factor", 1))
        vpergood_all[pid] = compute_vpergood(pid, operations_list, routing_list, lot_size_v)

    # ── Max equipment OT per labor group ─────────────────────────────────────
    max_lab_ot: Dict[str, float] = {}
    for eq in equipment_list:
        lid = eq.get("labor_group_id") or ""
        max_lab_ot[lid] = max(max_lab_ot.get(lid, -100.0), float(eq.get("overtime_pct", 0)))

    # ── num_av per equipment ──────────────────────────────────────────────────
    num_av_eq: Dict[str, float] = {}
    for eq in equipment_list:
        eid   = eq.get("id", "")
        lid   = eq.get("labor_group_id") or ""
        cnt   = int(eq.get("count", 0))
        eq_ot = float(eq.get("overtime_pct", 0))
        m_ot  = max_lab_ot.get(lid, 0.0)
        num_av_eq[eid] = float(cnt) * (eq_ot + 100.0) / (100.0 + m_ot)

    # ═══════════════════════════════════════════════════════════════════════════
    # EQUIPMENT UTILISATION PASS  (uses lvisit = visit_probs_all)
    # FIX-3: smbard_eq accumulated here and frozen for all subsequent passes
    # ═══════════════════════════════════════════════════════════════════════════
    equip_rows:      List[Dict] = []
    equip_util_map:  Dict[str, float] = {}
    equip_uwait_raw: Dict[str, float] = {}
    eq_qp_raw:       Dict[str, float] = {}

    # FIX-3: smbard computed here once, matching C++ mpc() teq->smbard
    smbard_eq_frozen: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}
    tpm_eq_frozen:    Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}

    for eq in equipment_list:
        eq_id = eq.get("id", "")
        equip_uwait_raw[eq_id] = 0.0
        eq_qp_raw[eq_id] = 0.0

    for product in products_list:
        pid        = product.get("id", "")
        demand_i   = effective_demand.get(pid, 0.0) * (1.0 + scrap_rates.get(pid, 0.0))
        if demand_i <= 0:
            continue
        lot_size_v = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
        tbatch_v   = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
        nb         = f_num_tbatches(lot_size_v, tbatch_v)
        vp_map     = visit_probs_all.get(pid, {})

        for op in operations_list:
            if op.get("product_id") != pid:
                continue
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq:
                continue
            eq_id    = eq.get("id", "")
            is_delay = eq.get("equip_type") == "delay"
            lid      = eq.get("labor_group_id") or ""
            lab      = labor_by_id.get(lid)
            af       = f_assign_fraction(op.get("pct_assigned", 0))
            if af <= 0:
                continue
            lsize    = float(op.get("lsize", lot_size_v))
            vp       = vp_map.get(op.get("op_name", ""), 1.0)
            abs_frac = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
            dlam     = demand_i / (lot_size_v * max(ops_per_period, 1e-9))
            v1       = dlam * af * vp * min(1.0, lsize)

            xs_e, xr_e_pc = _eq_EQUIP_T(op, eq, lot_size_v, nb, lsize)
            xbarr_lot_e   = xr_e_pc * max(1.0, lsize)
            xs_l, xr_l_pc = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, lsize)
            xbarrl_lot_l  = xr_l_pc * max(1.0, lsize)
            lab_ot_fac    = 1.0 + float(lab.get("overtime_pct", 0) if lab else 0) / 100.0
            xbar1         = (xs_l + xbarrl_lot_l) * lab_ot_fac
            xbar2         = xs_e + xbarr_lot_e
            x1_uw         = min(xbar1, xbar2) if xbar2 > SSEPSILON else xbar1

            if not is_delay:
                equip_uwait_raw[eq_id] = equip_uwait_raw.get(eq_id, 0.0) + v1 * x1_uw
                eq_qp_raw[eq_id]       = eq_qp_raw.get(eq_id, 0.0) + v1 * xbar2
                # FIX-3: accumulate smbard and tpm here (util pass), frozen afterward
                smb_val = v1 * x1_uw / max(1.0 - abs_frac, 0.01)
                smbard_eq_frozen[eq_id] = smbard_eq_frozen.get(eq_id, 0.0) + smb_val
                tpm_eq_frozen[eq_id]    = tpm_eq_frozen.get(eq_id, 0.0) + v1

    # ── Normalise equip util ──────────────────────────────────────────────────
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
            continue

        eff_cnt    = 1 if is_delay else count
        avail_time = float(eff_cnt) * float(ops_per_period)
        mttf = float(eq.get("mttf", 0) or 0)
        mttr = float(eq.get("mttr", 0) or 0)

        total_setup = 0.0; total_run = 0.0

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
            if af <= 0: continue
            num_lots   = f_num_lots(demand_i, lot_size_v, af)
            lsize      = float(op.get("lsize", lot_size_v))
            vp         = visit_probs_all.get(pid, {}).get(op.get("op_name", ""), 1.0)
            xs_e, xr_e = _eq_EQUIP_T(op, eq, lot_size_v, nb, lsize)
            v1_eq = num_lots * min(1.0, lsize) * vp
            total_setup += v1_eq * xs_e
            total_run   += v1_eq * xr_e * max(1.0, lsize)

        setup_frac  = total_setup / avail_time if avail_time > 0 else 0.0
        run_frac    = total_run   / avail_time if avail_time > 0 else 0.0
        repair_frac = (setup_frac + run_frac) * (mttr / mttf) if mttf > 0 else 0.0

        base_row.update({
            "setupUtil":  _r1(setup_frac  * 100),
            "runUtil":    _r1(run_frac    * 100),
            "repairUtil": _r1(repair_frac * 100),
        })
        equip_rows.append(base_row)
        equip_util_map[eq_id] = setup_frac + run_frac + repair_frac

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

        avail_lab   = float(lab_count) * float(ops_per_period)
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
            lsize      = float(op.get("lsize", lot_size_v))
            vp         = visit_probs_all.get(pid, {}).get(op.get("op_name", ""), 1.0)
            xs_l, xr_l = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, lsize)
            v1_lab = num_lots * min(1.0, lsize) * vp
            total_setup += v1_lab * xs_l
            total_run   += v1_lab * xr_l * max(1.0, lsize)

        setup_frac = total_setup / avail_lab if avail_lab > 0 else 0.0
        run_frac   = total_run   / avail_lab if avail_lab > 0 else 0.0
        total_frac = setup_frac + run_frac + abs_frac
        idle_frac  = max(0.0, 1.0 - total_frac)

        m_ot  = max_lab_ot.get(lid, 0.0)
        nav_l = float(lab_count) * (1.0 + lab_ot / 100.0) / (1.0 + m_ot / 100.0)
        num_av_lab[lid]    = nav_l
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

    # ── Pre-lextra uwait scaling ──────────────────────────────────────────────
    equip_uwait_pre: Dict[str, float] = {}
    for eq in equipment_list:
        eid   = eq.get("id", "")
        cnt   = int(eq.get("count", 0))
        if eq.get("equip_type") == "delay" or cnt <= 0:
            equip_uwait_pre[eid] = 0.0; continue
        lid   = eq.get("labor_group_id") or ""
        lab   = labor_by_id.get(lid)
        af_l  = float(lab.get("unavail_pct", 0)) / 100.0 if lab else 0.0
        ln    = float(lab.get("count", 0)) if lab else 1.0
        ea    = effabs(af_l, labor_util_map.get(lid, af_l), ln)
        equip_uwait_pre[eid] = (equip_uwait_raw.get(eid, 0.0)
                                * (ea / max(1.0 - ea, 1e-6))
                                / max(float(cnt), 1.0))
        equip_util_map[eid]  = min(equip_util_map.get(eid, 0.0) + equip_uwait_pre[eid], 0.9999)

    # ── Build original squared ct2_lab_map (Phase-1 of set_xbar_cs) ──────────
    # FIX-2: this map must use ^2, never GGc-updated values.
    ct2_lab_map_orig: Dict[str, float] = {}
    for lab in m.get("labor", []):
        lid = lab.get("id", "")
        lab_vf = float(lab.get("var_factor", 1))
        ct2_lab_map_orig[lid] = min(4.0, (lab_vf * var_labor) ** 2)

    # ── XBAR_CS pass 1 ───────────────────────────────────────────────────────
    fac_eq_lab_map: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}

    xbarbar_eq, cs2_eq, tpm_eq, lab_xbar_map, lab_cs2_map = _compute_xbar_cs(
        m, effective_demand, scrap_rates, var_equip, var_labor,
        fac_eq_lab_map, ct2_lab_map_orig,
        labor_util_map, labor_num_map,
        ops_per_period, visit_probs_all,
        smbard_eq_frozen,
    )

    # ── equip_sru_map for do_balance ─────────────────────────────────────────
    equip_sru_map: Dict[str, float] = {}
    for er in equip_rows:
        eq_id_s = er["id"]
        equip_sru_map[eq_id_s] = (er.get("setupUtil", 0.0) + er.get("runUtil", 0.0)
                                  + er.get("repairUtil", 0.0)) / 100.0

    # ── LEXTRA ───────────────────────────────────────────────────────────────
    fac_eq_lab_map, uwait_lextra, _ct2_ggc, uwait_replace_set = _compute_lextra(
        m, equipment_list, labor_by_id,
        xbarbar_eq, cs2_eq, tpm_eq_frozen, smbard_eq_frozen, lab_xbar_map,
        labor_util_map, labor_num_map, num_av_lab, num_av_eq,
        var_labor, util_limit, equip_sru_map,
        lab_cs2_map,      # FIX-1: ^0.9 values for num_v formula
    )

    # ── XBAR_CS pass 2 ───────────────────────────────────────────────────────
    # FIX-2: pass ct2_lab_map_orig (squared), NOT GGc-updated values
    # FIX-3: pass smbard_eq_frozen (frozen from util pass)
    xbarbar_eq, cs2_eq, tpm_eq, lab_xbar_map, lab_cs2_map = _compute_xbar_cs(
        m, effective_demand, scrap_rates, var_equip, var_labor,
        fac_eq_lab_map, ct2_lab_map_orig,
        labor_util_map, labor_num_map,
        ops_per_period, visit_probs_all,
        smbard_eq_frozen,
    )

    # ── ca2 ──────────────────────────────────────────────────────────────────
    ca2_map: Dict[str, float] = _compute_ca2(
        m, equipment_list, effective_demand, scrap_rates,
        visit_probs_all, equip_util_map, cs2_eq, num_av_eq,
        ops_per_period, var_part,
    )

    # ── Final equip util + CTq wait ───────────────────────────────────────────
    eq_wait_map: Dict[str, float] = {}

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

        if eq_id in uwait_replace_set:
            uwait_total = uwait_lextra.get(eq_id, 0.0)
        else:
            uwait_total = equip_uwait_pre.get(eq_id, 0.0) + uwait_lextra.get(eq_id, 0.0)
        unavail_f   = float(eq.get("unavail_pct", 0)) / 100.0
        total_f     = min(float(er["setupUtil"] + er["runUtil"] + er["repairUtil"]) / 100.0
                          + uwait_total + unavail_f, 0.9999)

        er["waitLaborUtil"]  = _r1(uwait_total * 100)
        er["totalUtil"]      = _r1(total_f * 100)
        er["idle"]           = _r1(max(0.0, 100.0 - total_f * 100))
        er["visits_per_100"] = _r1(tpm_eq.get(eq_id, 0.0) * 100)
        equip_util_map[eq_id] = total_f

        xbb = xbarbar_eq.get(eq_id, 0.0)
        cs2 = min(max(cs2_eq.get(eq_id, 1.0), 0.0), 4.0)
        ca2 = min(max(ca2_map.get(eq_id, cs2_eq.get(eq_id, 1.0)), 0.0), 4.0)
        u1  = total_f
        if xbb > SSEPSILON and u1 > EPSILON:
            exp_ = math.sqrt(2.0 * (cnt + 1.0)) - 1.0
            wm   = xbb * ((ca2 + cs2) / 2.0) * (min(u1, 0.9999) ** exp_) / (cnt * max(1.0 - u1, 1e-6))
            eq_wait_map[eq_id] = max(0.0, wm)
        else:
            eq_wait_map[eq_id] = 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # PRODUCT MCT, WIP, OPERATION DETAILS
    # FIX-VPG: MCT pass uses vpergood_all[pid][op_name]
    # ═══════════════════════════════════════════════════════════════════════════
    product_results:   List[Dict] = []
    operation_results: List[Dict] = []

    eq_q_acc: Dict[str, float] = {eq["id"]: 0.0 for eq in equipment_list}

    for product in products_list:
        pid          = product.get("id", "")
        pname        = product.get("name", "")
        demand_total = effective_demand.get(pid, 0.0) or 0.0
        demand_end   = float(product.get("demand", 0)) * float(product.get("demand_factor", 1))
        lot_size_v   = f_lot_size(product.get("lot_size", 1), product.get("lot_factor", 1))
        tbatch_v     = f_tbatch_size(product.get("tbatch_size", -1), lot_size_v)
        nb           = f_num_tbatches(lot_size_v, tbatch_v)
        ops          = [o for o in operations_list if o.get("product_id") == pid]
        vp_map       = visit_probs_all.get(pid, {})
        vpg_map      = vpergood_all.get(pid, {})
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

        ft_tot = 0.0
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
            vpergood = vpg_map.get(op.get("op_name", ""), vp)

            wait_min = eq_wait_map.get(eq_id, 0.0)
            eq_u     = equip_util_map.get(eq_id, 0.0)
            eq_uwait = (uwait_lextra.get(eq_id, 0.0) if eq_id in uwait_replace_set
                        else equip_uwait_pre.get(eq_id, 0.0) + uwait_lextra.get(eq_id, 0.0))

            v1 = dlam_base * af * vp * min(1.0, lsize)

            xs_l_u, xr_l_u = _eq_LABOR_T(op, eq, lab, lot_size_v, nb, lsize)
            xs_e_u, xr_e_u = _eq_EQUIP_T(op, eq, lot_size_v, nb, lsize)
            ulset   = v1 * xs_l_u * 100.0
            ulrun   = v1 * (xr_l_u * max(1.0, lsize)) * 100.0
            ueset   = v1 * xs_e_u * 100.0
            uerun   = v1 * (xr_e_u * max(1.0, lsize)) * 100.0
            n_setup = v1 * ops_per_period
            print("verp: ", vpergood )
            if is_delay:
                ft_tot += vpergood * wait_min
                operation_results.append({
                    "product": pname, "product_id": pid,
                    "operation": op.get("op_name", ""),
                    "equipment": eq.get("name", ""), "equip_id": eq_id,
                    "labor": "", "labor_id": lid,
                    "assign_pct": op.get("pct_assigned", 100),
                    "visit_prob": _r8(vp), "vpergood": _r8(vpergood),
                    "ueset": 0.0, "uerun": 0.0, "ulset": 0.0, "ulrun": 0.0,
                    "flowtime": 0.0, "n_setups": 0.0, "qpoper": 0.0,
                    "w_run": 0.0, "w_setup": 0.0, "w_lot": 0.0, "w_equip": 0.0, "w_labor": 0.0,
                    "avg_lot_size": _r4(lsize),
                    "visits_per_good": _r4(vpergood),
                })
                continue

            xs_tl, xr_tl = _eq_TBATCH_TOTAL_LABOR(op, eq, lab, lot_size_v, tbatch_v, lsize)
            xs_te, xr_te = _eq_TBATCH_TOTAL_EQUIP(op, eq, lot_size_v, tbatch_v, lsize)
            xbar1    = xs_tl + xr_tl
            xbar2    = xs_te + xr_te

            xprime_min = _calc_xprime(xbar1, xbar2, mttr, mttf, abs_frac, lab_ul, lab_num, fac)
            tgather    = 0.0
            x1_gather  = 0.0
            rpv        = xprime_min + wait_min + tgather * x1_gather

            flowtime_m = vpergood * rpv

            xs_bp, xr_bp = _eq_TBATCH_PIECE(op, eq)
            w_run_m   = vpergood * xr_bp
            w_setup_m = vpergood * xs_bp

            xs_wl, xr_wl = _eq_TBATCH_WAIT_LOT(op, eq)
            ratio     = max(0.0, tbatch_v * lsize / lot_size_v - 1.0) if lot_size_v > 0 else 0.0
            w_lot_m   = vpergood * ((xs_wl + xr_wl) * ratio + tgather * x1_gather)

            w_eq_q    = vpergood * wait_min * (eq_u - eq_uwait) / eq_u if eq_u > SSEPSILON else 0.0
            w_eq_r    = vpergood * (xr_bp + xs_bp + (xs_wl + xr_wl) * ratio) * (mttr / mttf if mttf > 0 else 0)
            w_equip_m = w_eq_q + w_eq_r

            w_labor_m = max(0.0, flowtime_m - w_run_m - w_setup_m - w_lot_m - w_equip_m)
            if w_labor_m < 0.0001 * max(flowtime_m, 1e-20):
                w_labor_m = 0.0

            qpoper = v1 * rpv * max(1.0, lsize)
            eq_q_acc[eq_id] = eq_q_acc.get(eq_id, 0.0) + qpoper

            ft_tot      += flowtime_m
            wip_lots    += v1 * rpv
            sum_w_setup += w_setup_m
            sum_w_run   += w_run_m
            sum_w_lot   += w_lot_m
            sum_w_equip += w_equip_m
            sum_w_labor += w_labor_m

            operation_results.append({
                "product":    pname,
                "product_id": pid,
                "operation":  op.get("op_name", ""),
                "equipment":  eq.get("name", ""),
                "equip_id":   eq_id,
                "labor":      lab.get("name", "") if lab else "",
                "labor_id":   lid,
                "assign_pct": op.get("pct_assigned", 100),
                "visit_prob": _r8(vp),
                "vpergood":   _r8(vpergood),
                "ueset":    _r8(ueset),
                "uerun":    _r8(uerun),
                "ulset":    _r8(ulset),
                "ulrun":    _r8(ulrun),
                "flowtime": _r8(flowtime_m / conv1),
                "n_setups": _r8(n_setup),
                "qpoper":   _r8(qpoper),
                "w_run":    _r8(w_run_m   / conv1),
                "w_setup":  _r8(w_setup_m / conv1),
                "w_lot":    _r8(w_lot_m   / conv1),
                "w_equip":  _r8(w_equip_m / conv1),
                "w_labor":  _r8(w_labor_m / conv1),
                "avg_lot_size": _r8(lsize),
                "visits_per_good": _r8(vpergood),
            })

        def to_s(m_):
            return m_ / max(conv1, 0.001)

        mct_shifts = to_s(ft_tot)
        cap_lim    = f_capacity_limited_flow(product, ops, equipment_list, ops_per_period, vp_map)
        needed     = demand_total / yield_frac if yield_frac > 0 else float("inf")
        started    = round(min(needed, cap_lim)) if cap_lim != float("inf") else round(needed)
        print(f"started: {started}, needed: {needed}, cap_lim: {cap_lim}, yield_frac: {yield_frac}")
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
            "wip_lots":     _r8(wip_lots),
            "mct":          _r8(mct_shifts),
            "mctSetup":     _r8(to_s(sum_w_setup)),
            "mctRun":       _r8(to_s(sum_w_run)),
            "mctLotWait":   _r8(to_s(sum_w_lot)),
            "mctQueue":     _r8(to_s(sum_w_equip)),
            "mctWaitLabor": _r8(to_s(sum_w_labor)),
            "w_equip":      _r8(to_s(sum_w_equip)),
            "w_labor":      _r8(to_s(sum_w_labor)),
            "w_setup":      _r8(to_s(sum_w_setup)),
            "w_run":        _r8(to_s(sum_w_run)),
            "w_lot":        _r8(to_s(sum_w_lot)),
        })

    # ── Equipment WIP ─────────────────────────────────────────────────────────
    for er in equip_rows:
        eq = next((e for e in equipment_list if e.get("id") == er["id"]), None)
        if not eq: continue
        eq_id  = er["id"]
        q_tot  = eq_q_acc.get(eq_id, 0.0)
        machine_count = int(eq.get("count", 0))
        utilization   = equip_util_map.get(eq_id, 0.0)

        qp = utilization * machine_count
        qw = max(0.0, q_tot - qp)
        # print(f"qp: {qp}, qw: {qw}, q_tot: {q_tot}")
        er["wip_process"] = _r8(max(0.0, qp))
        er["wip_queue"]   = _r8(qw)
        er["wip_total"]   = _r8(max(0.0, q_tot))
        er["wait_min"]    = _r8(eq_wait_map.get(eq_id, 0.0))


    equip_by_id: Dict[str, Dict[str, Any]] = {e.get("id", ""): e for e in m.get("equipment", [])}
    for er in equip_rows:
        eq = equip_by_id.get(er.get("id", ""), {})
        eq_count = int(eq.get("count", er.get("count", 0)) or 0)
        base_util = float(er.get("setupUtil", 0)) + float(er.get("runUtil", 0))
        tended = min(1.0, base_util / 100.0) * max(0, eq_count)
        waiting = (float(er.get("waitLaborUtil", 0)) / 100.0) * max(0, eq_count)
        er["machinesTended"] = _r8(_sanitize(tended))
        er["machinesWaiting"] = _r8(_sanitize(waiting))

    # Aggregate to labor groups (avg machines tended / waiting, and avg wait-labor util across equipment groups)
    equip_rows_by_id: Dict[str, Dict[str, Any]] = {e.get("id", ""): e for e in equip_rows}
    for lr in labor_rows:
        lab_id = lr.get("id", "")
        eq_ids = [e.get("id", "") for e in m.get("equipment", []) if (e.get("labor_group_id") or "") == lab_id]
        if not eq_ids:
            lr["machinesTended"] = 0.0
            lr["machinesWaiting"] = 0.0
            lr["avgWaitLaborUtil"] = 0.0
            continue

        tended_sum = 0.0
        waiting_sum = 0.0
        wait_util_sum = 0.0
        n = 0
        for eq_id in eq_ids:
            er = equip_rows_by_id.get(eq_id)
            if not er:
                continue
            tended_sum += float(er.get("machinesTended", 0) or 0)
            waiting_sum += float(er.get("machinesWaiting", 0) or 0)
            wait_util_sum += float(er.get("waitLaborUtil", 0) or 0)
            n += 1

        lr["machinesTended"] = _r8(_sanitize(tended_sum))
        lr["machinesWaiting"] = _r8(_sanitize(waiting_sum))
        lr["avgWaitLaborUtil"] = _r8(_sanitize(wait_util_sum / max(1, n)))
        print(f"lr: {lr}")

    # ── Labor WIP ─────────────────────────────────────────────────────────────
    for lr in labor_rows:
        lid    = lr["id"]
        lab    = labor_by_id.get(lid)
        cnt    = int(lab.get("count", 0)) if lab else 0
        sf     = labor_uset_map.get(lid, 0.0)
        rf     = labor_urun_map.get(lid, 0.0)
        qpl    = (sf + rf) * cnt if cnt > 0 else 0.0

        eq_grp = [e for e in equipment_list if e.get("labor_group_id") == lid]
        ql     = sum(smbard_eq_frozen.get(e["id"], 0.0) * (1.0 + fac_eq_lab_map.get(e["id"], 0.0))
                     for e in eq_grp)
        qwl    = max(0.0, ql - qpl)

        eq_act = [e for e in eq_grp if int(e.get("count", 0)) > 0]
        m_ot   = max((float(e.get("overtime_pct", 0)) for e in eq_act), default=0.0)
        eq_cov = (sum(float(e.get("count", 1)) * (float(e.get("overtime_pct", 0)) + 100.0)
                      for e in eq_act) / (100.0 * (1.0 + m_ot / 100.0))
                  if eq_act else 0.0)
        max_fac = max((fac_eq_lab_map.get(e["id"], 0.0) for e in eq_act), default=0.0)

        lr["wip_total"]   = _r8(max(0.0, ql))
        lr["wip_process"] = _r8(max(0.0, qpl))
        lr["wip_queue"]   = _r8(qwl)
        lr["eq_cover"]    = _r8(eq_cov)
        lr["fac_eq_lab"]  = _r8(max_fac)

    # ── Warnings ──────────────────────────────────────────────────────────────
    over_limit = []
    for er in equip_rows:
        if float(er["totalUtil"]) > util_limit:
            over_limit.append(f"Equipment: {er['name']} ({er['totalUtil']}%)")
            warnings.append(f'Equipment "{er["name"]}" util ({er["totalUtil"]}%) > limit ({util_limit}%)')
    for lr in labor_rows:
        if float(lr["totalUtil"]) > util_limit:
            over_limit.append(f"Labor: {lr['name']} ({lr['totalUtil']}%)")
            warnings.append(f'Labor "{lr["name"]}" util ({lr["totalUtil"]}%) > limit ({util_limit}%)')

    # ── Sanitize ──────────────────────────────────────────────────────────────
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
                  "w_run","w_setup","w_lot","w_equip","w_labor","visit_prob",
                  "avg_lot_size","visits_per_good","vpergood"]:
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


def verify_model_data(model: dict) -> Dict[str, List[str]]:
    """Structural checks matching frontend verifyData (no numeric simulation)."""
    errors: List[str] = []
    warnings: List[str] = []
    labor = model.get("labor") or []
    equipment = model.get("equipment") or []
    products = model.get("products") or []
    operations = model.get("operations") or []
    routing = model.get("routing") or []

    if len(labor) == 0:
        warnings.append("No labor groups defined.")
    if len(equipment) == 0:
        warnings.append("No equipment groups defined.")
    if len(products) == 0:
        errors.append("No products defined.")
    if len(operations) == 0:
        errors.append("No operations defined for any product.")

    labor_ids = {str(x.get("id", "")) for x in labor}
    equip_ids = {str(x.get("id", "")) for x in equipment}

    for eq in equipment:
        lid = str(eq.get("labor_group_id") or "")
        if lid and lid not in labor_ids:
            errors.append(f'Equipment "{eq.get("name", "")}" references non-existent labor group.')

    for op in operations:
        eid = str(op.get("equip_id") or "")
        if eid and eid not in equip_ids:
            errors.append(f'Operation "{op.get("op_name", "")}" references non-existent equipment.')

    prod_by_id = {str(p.get("id")): p for p in products}
    for p in products:
        pid = str(p.get("id", ""))
        if float(p.get("demand", 0) or 0) > 0:
            if not any(str(o.get("product_id")) == pid for o in operations):
                warnings.append(f'Product "{p.get("name", "")}" has demand but no operations.')

    from_ops_set = set()
    for r in routing:
        from_ops_set.add((str(r.get("product_id", "")), str(r.get("from_op_name", ""))))
    for pid, from_op in from_ops_set:
        outgoing = [r for r in routing if str(r.get("product_id", "")) == pid and str(r.get("from_op_name", "")) == from_op]
        total = sum(float(r.get("pct_routed", 0) or 0) for r in outgoing)
        if outgoing and abs(total - 100.0) > 0.1:
            pname = prod_by_id.get(pid, {}).get("name", pid)
            warnings.append(f'Product "{pname}": routing from "{from_op}" sums to {total}%, not 100%.')

    return {"errors": errors, "warnings": warnings}


# ─────────────────────────────────────────────────────────────────────────────
# Django view
# ─────────────────────────────────────────────────────────────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def verify_model_view(request):
    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    model = data.get("model")
    if not model:
        return JsonResponse({"error": "Missing 'model' in body"}, status=400)
    payload = verify_model_data(model)
    return JsonResponse(payload)


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
        results = run_full_calculate_via_dll(model, scenario)
        response_payload = {"results": results}
        saved_path = _persist_full_calculate_output(model, scenario, response_payload, status="success")
        if saved_path:
            logger.info("Saved full-calculate output: %s", saved_path)
        return JsonResponse(response_payload)
    except DllRunDiagnostics as e:
        logger.warning("full_calculate DLL execution failed: %s", e)
        error_payload = {
            "error": str(e),
            "errorType": type(e).__name__,
            "diagnostics": e.diagnostics,
        }
        saved_path = _persist_full_calculate_output(model, scenario, error_payload, status="error_422")
        if saved_path:
            logger.info("Saved full-calculate error output: %s", saved_path)
        return JsonResponse(error_payload, status=422)
    except Exception as e:
        logger.exception("full_calculate failed")
        error_payload = {"error": str(e), "errorType": type(e).__name__}
        saved_path = _persist_full_calculate_output(model, scenario, error_payload, status="error_500")
        if saved_path:
            logger.info("Saved full-calculate exception output: %s", saved_path)
        return JsonResponse(error_payload, status=500)
