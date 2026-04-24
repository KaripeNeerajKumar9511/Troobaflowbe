import argparse
import ctypes
import json
import os
import struct
import sys
from collections import defaultdict


def _load_dotenv_if_present():
    """
    Load .env from the backend folder for standalone runs.
    Existing process env vars are preserved.
    """
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _resolve_dll_path(value):
    """
    Accept either a DLL file path or a directory containing known DLL names.
    """
    if not value:
        return None
    p = os.path.abspath(value)

    if os.path.isfile(p) and p.lower().endswith(".dll"):
        return p

    if os.path.isdir(p):
        for name in ("mpx0.dll", "mpx95i.dll", "dll2.dll"):
            candidate = os.path.join(p, name)
            if os.path.isfile(candidate):
                return candidate
        return os.path.join(p, "mpx0.dll")

    return p


def default_mpx_dll_path():
    _load_dotenv_if_present()
    default_hint = os.path.join(os.path.dirname(__file__), "dll2.dll")
    install_or_file = os.environ.get("MPX44_DIR", default_hint)
    return _resolve_dll_path(install_or_file)


def _python_arch():
    return "x64" if sys.maxsize > 2**32 else "x86"


def _pe_machine_type(dll_path):
    with open(dll_path, "rb") as f:
        dos = f.read(64)
        if len(dos) < 64 or dos[:2] != b"MZ":
            return "unknown"
        pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
        f.seek(pe_offset + 4)
        machine_raw = f.read(2)
        if len(machine_raw) != 2:
            return "unknown"
        machine = struct.unpack("<H", machine_raw)[0]
    mapping = {0x014C: "x86", 0x8664: "x64", 0x01C4: "arm", 0xAA64: "arm64"}
    return mapping.get(machine, hex(machine))


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _iarr(values):
    vals = [_to_int(v) for v in values]
    return (ctypes.c_int * len(vals))(*vals)


def _farr(values):
    vals = [_to_float(v) for v in values]
    return (ctypes.c_float * len(vals))(*vals)


def _require_len(name, count, arr_name, arr):
    if len(arr) != count:
        raise ValueError(f"{name}.{arr_name} must have {count} items, got {len(arr)}")


def _add_dll_dir_to_search_path(dll_path):
    dll_dir = os.path.dirname(os.path.abspath(dll_path))
    if not dll_dir:
        return
    add = getattr(os, "add_dll_directory", None)
    if add and os.path.isdir(dll_dir):
        add(dll_dir)
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if dll_dir not in parts:
        os.environ["PATH"] = dll_dir + os.pathsep + path


def _resolve_run_entry(dll):
    c_int_p = ctypes.POINTER(ctypes.c_int)
    c_float_p = ctypes.POINTER(ctypes.c_float)

    names = ("irun_it", "iRun_model", "irun_model", "iRunModel")
    for name in names:
        try:
            fn = getattr(dll, name)
        except AttributeError:
            continue

        fn.restype = ctypes.c_int
        fn.argtypes = [
            ctypes.c_char_p,
            ctypes.c_float, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_float, ctypes.c_float,
            ctypes.c_int, c_int_p, c_float_p, c_float_p, c_float_p, c_float_p, c_float_p, c_float_p, c_int_p,
            ctypes.c_int, c_int_p, c_int_p, c_float_p, c_float_p, c_float_p, c_int_p, c_float_p, c_float_p, c_float_p, c_int_p,
            ctypes.c_int, c_int_p, c_float_p, c_float_p, c_float_p, c_float_p, c_float_p, c_int_p,
            ctypes.c_int, c_int_p, c_int_p, c_int_p, c_int_p, c_float_p,
            c_float_p, c_float_p, c_float_p, c_float_p, c_float_p, c_float_p, c_float_p, c_float_p,
            c_float_p, c_float_p, c_float_p, c_float_p,
            ctypes.c_int, c_int_p, c_int_p, c_int_p, c_float_p,
            ctypes.c_int, c_int_p, c_int_p, c_float_p,
            ctypes.c_int,
        ]
        return fn, name

    raise RuntimeError("No run entry found. Tried irun_it, iRun_model, irun_model, iRunModel")


def _normalize_tbatch(value):
    v = _to_float(value, 1.0)
    if v == -1.0:
        return -1.0
    return v if v > 0 else -1.0


def _has_fatal_results_error(output_dir):
    err_path = os.path.join(output_dir, "results.err")
    if not os.path.exists(err_path):
        return False
    try:
        with open(err_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return False
    for ln in lines[1:]:
        if ln.startswith("0,"):
            return True
    return False


def _build_payload_from_model_lists(
    data,
    force_safe_routing=False,
    strict_json_routing=False,
    normalize_json_routing=False,
):
    general = data.get("general", {})
    labor = data.get("labor", [])
    equipment = data.get("equipment", [])
    products = data.get("products", [])
    operations = data.get("operations", [])
    routing = data.get("routing", [])
    ibom = data.get("ibom", [])

    product_map = {p.get("id"): i + 1 for i, p in enumerate(products)}
    labor_map = {l.get("id"): i + 1 for i, l in enumerate(labor)}
    equip_map = {e.get("id"): i + 1 for i, e in enumerate(equipment)}

    general_payload = [
        ctypes.c_float(_to_float(general.get("conv1", 1.0), 1.0)),
        ctypes.c_float(_to_float(general.get("conv2", 1.0), 1.0)),
        ctypes.c_float(_to_float(general.get("util_limit", 95.0), 95.0)),
        ctypes.c_float(_to_float(general.get("var_labor", 30.0), 30.0)),
        ctypes.c_float(_to_float(general.get("var_equip", 30.0), 30.0)),
        ctypes.c_float(_to_float(general.get("var_prod", 30.0), 30.0)),
    ]

    l_count = len(labor)
    labor_payload = [
        ctypes.c_int(l_count),
        _iarr([i + 1 for i in range(l_count)]),
        _farr([l.get("count", 1) for l in labor]),
        _farr([l.get("overtime_pct", 0) for l in labor]),
        _farr([l.get("unavail_pct", 0) for l in labor]),
        _farr([l.get("setup_factor", 1) for l in labor]),
        _farr([l.get("run_factor", 1) for l in labor]),
        _farr([l.get("var_factor", 1) for l in labor]),
        _iarr([_to_int(l.get("prioritize_use", l.get("use", l.get("balance", 0))), 0) for l in labor]),
    ]

    eq_count = len(equipment)
    equipment_payload = [
        ctypes.c_int(eq_count),
        _iarr([i + 1 for i in range(eq_count)]),
        _iarr([e.get("count", 1) for e in equipment]),
        _farr([e.get("mttf", 1) for e in equipment]),
        _farr([e.get("mttr", 1) for e in equipment]),
        _farr([e.get("overtime_pct", 0) for e in equipment]),
        _iarr([labor_map.get(e.get("labor_group_id"), 1) for e in equipment]),
        _farr([e.get("setup_factor", 1) for e in equipment]),
        _farr([e.get("run_factor", 1) for e in equipment]),
        _farr([e.get("var_factor", 1) for e in equipment]),
        _iarr(
            [
                _to_int(
                    e.get(
                        "cell_id",
                        e.get(
                            "cellid",
                            e.get("area_id", e.get("area", e.get("flag", 0))),
                        ),
                    ),
                    0,
                )
                for e in equipment
            ]
        ),
    ]

    p_count = len(products)
    parts_payload = [
        ctypes.c_int(p_count),
        _iarr([i + 1 for i in range(p_count)]),
        _farr([max(_to_float(p.get("demand", 0), 0), 0.0) for p in products]),
        _farr([max(_to_float(p.get("lot_size", 1), 1), 1.0) for p in products]),
        _farr([_normalize_tbatch(p.get("tbatch_size", 1)) for p in products]),
        _farr([_to_float(p.get("demand_factor", p.get("facdem", 1)), 1) for p in products]),
        _farr([_to_float(p.get("var_factor", 1), 1) for p in products]),
        _iarr([_to_int(p.get("tbatch_gather", p.get("gather_tbatches", p.get("flag", 0))), 0) for p in products]),
    ]

    if strict_json_routing:
        normalized_ops = []
        name_to_ids = defaultdict(list)
        real_op_ids_by_part = {}
        for idx, op in enumerate(operations):
            part_idx = product_map.get(op.get("product_id"))
            if not part_idx:
                continue
            op_id = len(normalized_ops) + 1
            op_name = str(op.get("op_name", "")).strip().upper()
            row = {
                "part_idx": part_idx,
                "op_id": op_id,
                "opnum": max(_to_int(op.get("op_number", idx + 1), idx + 1), 0),
                "op_name": op_name,
                "equip_id": op.get("equip_id", ""),
                "pct_assigned": _to_float(op.get("pct_assigned", 100), 100),
                "equip_setup_lot": _to_float(op.get("equip_setup_lot", 0), 0),
                "equip_setup_tbatch": _to_float(op.get("equip_setup_tbatch", 0), 0),
                "equip_setup_piece": _to_float(op.get("equip_setup_piece", 0), 0),
                "equip_run_lot": _to_float(op.get("equip_run_lot", 0), 0),
                "equip_run_tbatch": _to_float(op.get("equip_run_tbatch", 0), 0),
                "equip_run_piece": _to_float(op.get("equip_run_piece", 0), 0),
                "labor_setup_lot": _to_float(op.get("labor_setup_lot", 0), 0),
                "labor_setup_tbatch": _to_float(op.get("labor_setup_tbatch", 0), 0),
                "labor_setup_piece": _to_float(op.get("labor_setup_piece", 0), 0),
                "labor_run_lot": _to_float(op.get("labor_run_lot", 0), 0),
                "labor_run_tbatch": _to_float(op.get("labor_run_tbatch", 0), 0),
                "labor_run_piece": _to_float(op.get("labor_run_piece", 0), 0),
            }
            normalized_ops.append(row)
            if op_name:
                name_to_ids[(part_idx, op_name)].append(op_id)
            real_op_ids_by_part.setdefault(part_idx, []).append(op_id)
    else:
        ops_by_part = defaultdict(list)
        for op in operations:
            part_idx = product_map.get(op.get("product_id"))
            if part_idx:
                ops_by_part[part_idx].append(op)
        normalized_ops = []
        name_to_ids = defaultdict(list)
        real_op_ids_by_part = {}

        def append_op(part_idx, op_name, src=None, force_blank_equip=False):
            src = src or {}
            equip_id = "" if force_blank_equip else src.get("equip_id", "")
            op_id = len(normalized_ops) + 1
            opnum_from_json = max(_to_int(src.get("op_number", op_id), op_id), 0)
            row = {
                "part_idx": part_idx,
                "op_id": op_id,
                "opnum": op_id if force_safe_routing else opnum_from_json,
                "op_name": op_name,
                "equip_id": equip_id,
                "pct_assigned": _to_float(src.get("pct_assigned", 100), 100),
                "equip_setup_lot": _to_float(src.get("equip_setup_lot", 0), 0),
                "equip_setup_tbatch": _to_float(src.get("equip_setup_tbatch", 0), 0),
                "equip_setup_piece": _to_float(src.get("equip_setup_piece", 0), 0),
                "equip_run_lot": _to_float(src.get("equip_run_lot", 0), 0),
                "equip_run_tbatch": _to_float(src.get("equip_run_tbatch", 0), 0),
                "equip_run_piece": _to_float(src.get("equip_run_piece", 0), 0),
                "labor_setup_lot": _to_float(src.get("labor_setup_lot", 0), 0),
                "labor_setup_tbatch": _to_float(src.get("labor_setup_tbatch", 0), 0),
                "labor_setup_piece": _to_float(src.get("labor_setup_piece", 0), 0),
                "labor_run_lot": _to_float(src.get("labor_run_lot", 0), 0),
                "labor_run_tbatch": _to_float(src.get("labor_run_tbatch", 0), 0),
                "labor_run_piece": _to_float(src.get("labor_run_piece", 0), 0),
            }
            normalized_ops.append(row)
            name_to_ids[(part_idx, op_name)].append(op_id)
            return op_id

        for part_idx in range(1, p_count + 1):
            part_ops = ops_by_part.get(part_idx, [])
            dock_src = next((o for o in part_ops if str(o.get("op_name", "")).strip().upper() == "DOCK"), {})
            append_op(part_idx, "DOCK", dock_src, force_blank_equip=True)
            stock_id = append_op(part_idx, "STOCK", {"pct_assigned": 100}, force_blank_equip=True)
            scrap_id = append_op(part_idx, "SCRAP", {"pct_assigned": 100}, force_blank_equip=True)
            real_ops = [o for o in part_ops if str(o.get("op_name", "")).strip().upper() not in {"DOCK", "STOCK", "SCRAP"}]
            real_ops.sort(key=lambda o: _to_int(o.get("op_number", 0), 0))
            real_ids = []
            for op in real_ops:
                real_ids.append(append_op(part_idx, str(op.get("op_name", "")).strip().upper(), op, force_blank_equip=False))
            if force_safe_routing and len(real_ids) == 1:
                base = real_ops[0]
                fake_src = dict(base)
                fake_src["op_number"] = _to_int(base.get("op_number", 0), 0) + 1
                for fld in (
                    "equip_setup_lot", "equip_setup_tbatch", "equip_setup_piece",
                    "equip_run_lot", "equip_run_tbatch", "equip_run_piece",
                    "labor_setup_lot", "labor_setup_tbatch", "labor_setup_piece",
                    "labor_run_lot", "labor_run_tbatch", "labor_run_piece",
                ):
                    fake_src[fld] = 0
                for n in range(5):
                    fake_src["op_number"] = _to_int(fake_src.get("op_number", 0), 0) + 1
                    real_ids.append(append_op(part_idx, f"BUFFER{n+1}", fake_src, force_blank_equip=False))
            real_op_ids_by_part[part_idx] = real_ids
            if not real_ops:
                name_to_ids[(part_idx, "_AUTO_STOCK")] = [stock_id]
                name_to_ids[(part_idx, "_AUTO_SCRAP")] = [scrap_id]

    op_count = len(normalized_ops)
    operations_payload = [
        ctypes.c_int(op_count),
        _iarr([r["op_id"] for r in normalized_ops]),
        _iarr([r["opnum"] for r in normalized_ops]),
        _iarr([r["part_idx"] for r in normalized_ops]),
        _iarr([equip_map.get(r["equip_id"], 1) if r["equip_id"] else 1 for r in normalized_ops]),
        _farr([r["pct_assigned"] for r in normalized_ops]),
        _farr([r["equip_setup_lot"] for r in normalized_ops]),
        _farr([r["equip_setup_tbatch"] for r in normalized_ops]),
        _farr([r["equip_setup_piece"] for r in normalized_ops]),
        _farr([r["equip_run_lot"] for r in normalized_ops]),
        _farr([r["equip_run_tbatch"] for r in normalized_ops]),
        _farr([r["equip_run_piece"] for r in normalized_ops]),
        _farr([r["labor_setup_lot"] for r in normalized_ops]),
        _farr([r["labor_setup_tbatch"] for r in normalized_ops]),
        _farr([r["labor_setup_piece"] for r in normalized_ops]),
        _farr([r["labor_run_lot"] for r in normalized_ops]),
        _farr([r["labor_run_tbatch"] for r in normalized_ops]),
        _farr([r["labor_run_piece"] for r in normalized_ops]),
    ]

    route_rows = []
    stock_for_part = {}
    scrap_for_part = {}
    for part_idx in range(1, p_count + 1):
        if name_to_ids.get((part_idx, "STOCK")):
            stock_for_part[part_idx] = name_to_ids[(part_idx, "STOCK")][0]
        if name_to_ids.get((part_idx, "SCRAP")):
            scrap_for_part[part_idx] = name_to_ids[(part_idx, "SCRAP")][0]

    if force_safe_routing and (not strict_json_routing):
        for part_idx in range(1, p_count + 1):
            dock = name_to_ids[(part_idx, "DOCK")][0]
            stock = stock_for_part[part_idx]
            scrap = scrap_for_part[part_idx]
            real_ids = real_op_ids_by_part.get(part_idx, [])
            if real_ids:
                route_rows.append((part_idx, dock, real_ids[0], 99.9999))
                route_rows.append((part_idx, dock, dock, 0.0001))
                for i in range(len(real_ids) - 1):
                    route_rows.append((part_idx, real_ids[i], real_ids[i + 1], 100.0))
                route_rows.append((part_idx, real_ids[-1], stock, 100.0))
            else:
                route_rows.append((part_idx, dock, stock, 99.9999))
                route_rows.append((part_idx, dock, scrap, 0.0001))
            route_rows.append((part_idx, stock, stock, 100.0))
            # DLL rejects any operation that routes 100% to SCRAP.
            route_rows.append((part_idx, scrap, stock, 0.0001))
            route_rows.append((part_idx, scrap, scrap, 99.9999))
    else:
        for rt in routing:
            part_idx = product_map.get(rt.get("product_id"))
            if not part_idx:
                continue
            from_key = (part_idx, str(rt.get("from_op_name", "")).strip().upper())
            to_key = (part_idx, str(rt.get("to_op_name", "")).strip().upper())
            from_list = name_to_ids.get(from_key)
            to_list = name_to_ids.get(to_key)
            from_op = from_list[0] if from_list else None
            to_op = to_list[0] if to_list else None
            if from_op is None or to_op is None:
                continue
            route_rows.append((part_idx, from_op, to_op, _to_float(rt.get("pct_routed", 0), 0)))

    if (not force_safe_routing) and normalize_json_routing:
        grouped = defaultdict(list)
        for pidx, from_op, to_op, pct in route_rows:
            grouped[(pidx, from_op)].append((to_op, pct))
        normalized = []
        for (pidx, from_op), targets in grouped.items():
            total = sum(p for _, p in targets)
            if total <= 0:
                continue
            scale = 100.0 / total
            scaled = [(to_op, pct * scale) for to_op, pct in targets]
            scrap_id = scrap_for_part.get(pidx)
            stock_id = stock_for_part.get(pidx)
            only_scrap = scrap_id is not None and all(to_op == scrap_id for to_op, _ in scaled) and stock_id is not None
            if only_scrap:
                normalized.append((pidx, from_op, stock_id, 0.0001))
                normalized.append((pidx, from_op, scrap_id, 99.9999))
            else:
                for to_op, pct in scaled:
                    normalized.append((pidx, from_op, to_op, pct))
        route_rows = normalized

    if force_safe_routing and (not strict_json_routing):
        if not route_rows:
            for part_idx in range(1, p_count + 1):
                dock = name_to_ids[(part_idx, "DOCK")][0]
                route_rows.append((part_idx, dock, stock_for_part[part_idx], 99.9999))
                route_rows.append((part_idx, dock, scrap_for_part[part_idx], 0.0001))

        grouped_targets = defaultdict(list)
        for pidx, from_op, to_op, pct in route_rows:
            grouped_targets[(pidx, from_op)].append((to_op, pct))
        for part_idx in range(1, p_count + 1):
            stock_op = stock_for_part[part_idx]
            scrap_op = scrap_for_part[part_idx]
            if (part_idx, stock_op) not in grouped_targets:
                grouped_targets[(part_idx, stock_op)] = [(stock_op, 100.0)]
            if (part_idx, scrap_op) not in grouped_targets:
                grouped_targets[(part_idx, scrap_op)] = [(scrap_op, 100.0)]

        normalized_routes = []
        for (pidx, from_op), targets in grouped_targets.items():
            total = sum(p for _, p in targets)
            stock_op = stock_for_part[pidx]
            scrap_op = scrap_for_part[pidx]
            if total <= 0:
                normalized_routes.append((pidx, from_op, stock_op, 99.9999))
                normalized_routes.append((pidx, from_op, scrap_op, 0.0001))
                continue
            if total > 100.0 + 1e-3:
                raise ValueError(f"Routing total >100 for part={pidx}, from_op={from_op}, total={total}")
            scale = 100.0 / total if abs(total - 100.0) > 1e-6 else 1.0
            scaled = [(to_op, pct * scale) for to_op, pct in targets]
            only_scrap = all(to_op == scrap_op for to_op, _ in scaled)
            if only_scrap:
                # Keep a tiny non-scrap branch; required by DLL constraints.
                normalized_routes.append((pidx, from_op, stock_op, 0.0001))
                normalized_routes.append((pidx, from_op, scrap_op, 99.9999))
            else:
                for to_op, pct in scaled:
                    normalized_routes.append((pidx, from_op, to_op, pct))
        route_rows = normalized_routes
    else:
        if not route_rows:
            raise ValueError("No valid routing rows could be mapped from JSON.")

    routes_payload = [
        ctypes.c_int(len(route_rows)),
        _iarr([r[0] for r in route_rows]),
        _iarr([r[1] for r in route_rows]),
        _iarr([r[2] for r in route_rows]),
        _farr([r[3] for r in route_rows]),
    ]

    ib_rows = []
    for row in ibom:
        parent = product_map.get(row.get("parent_product_id"))
        comp = product_map.get(row.get("component_product_id"))
        if parent and comp:
            ib_rows.append((parent, comp, _to_float(row.get("units_per_assy", 0), 0)))

    ibom_payload = [
        ctypes.c_int(len(ib_rows)),
        _iarr([r[0] for r in ib_rows]) if ib_rows else ctypes.POINTER(ctypes.c_int)(),
        _iarr([r[1] for r in ib_rows]) if ib_rows else ctypes.POINTER(ctypes.c_int)(),
        _farr([r[2] for r in ib_rows]) if ib_rows else ctypes.POINTER(ctypes.c_float)(),
    ]
    return {
        "general": tuple(general_payload),
        "labor": tuple(labor_payload),
        "equipment": tuple(equipment_payload),
        "parts": tuple(parts_payload),
        "operations": tuple(operations_payload),
        "routes": tuple(routes_payload),
        "ibom": tuple(ibom_payload),
    }


def _payload_from_json(data):
    general = data.get("general", {})
    labor_raw = data.get("labor", [])

    # Model JSON format used by test1/test2/... (list-based sections).
    if isinstance(labor_raw, list):
        return _build_payload_from_model_lists(
            data,
            force_safe_routing=False,
            strict_json_routing=False,
            normalize_json_routing=False,
        )

    labor = data.get("labor", {})
    labor_count = _to_int(labor.get("count", 0))
    l_x1 = labor.get("x1", [])
    l_x2 = labor.get("x2", [])
    l_x3 = labor.get("x3", [])
    l_x4 = labor.get("x4", [])
    l_x5 = labor.get("x5", [])
    l_x6 = labor.get("x6", [])
    l_x7 = labor.get("x7", [])
    l_x8 = labor.get("x8", [])
    for n, a in (("x1", l_x1), ("x2", l_x2), ("x3", l_x3), ("x4", l_x4), ("x5", l_x5), ("x6", l_x6), ("x7", l_x7), ("x8", l_x8)):
        _require_len("labor", labor_count, n, a)

    equipment = data.get("equipment", {})
    eq_count = _to_int(equipment.get("count", 0))
    e_x1 = equipment.get("x1", [])
    e_x2 = equipment.get("x2", [])
    e_x3 = equipment.get("x3", [])
    e_x4 = equipment.get("x4", [])
    e_x5 = equipment.get("x5", [])
    e_x6 = equipment.get("x6", [])
    e_x7 = equipment.get("x7", [])
    e_x8 = equipment.get("x8", [])
    e_x9 = equipment.get("x9", [])
    e_x10 = equipment.get("x10", [])
    e_x11 = equipment.get("x11", [])
    for n, a in (
        ("x1", e_x1), ("x2", e_x2), ("x3", e_x3), ("x4", e_x4), ("x5", e_x5), ("x6", e_x6),
        ("x7", e_x7), ("x8", e_x8), ("x9", e_x9), ("x10", e_x10), ("x11", e_x11)
    ):
        _require_len("equipment", eq_count, n, a)

    parts = data.get("parts", {})
    part_count = _to_int(parts.get("count", 0))
    p_x1 = parts.get("x1", [])
    p_x2 = parts.get("x2", [])
    p_x3 = parts.get("x3", [])
    p_x4 = parts.get("x4", [])
    p_x5 = parts.get("x5", [])
    p_x6 = parts.get("x6", [])
    p_x7 = parts.get("x7", [])
    for n, a in (("x1", p_x1), ("x2", p_x2), ("x3", p_x3), ("x4", p_x4), ("x5", p_x5), ("x6", p_x6), ("x7", p_x7)):
        _require_len("parts", part_count, n, a)

    operations = data.get("operations", {})
    op_count = _to_int(operations.get("count", 0))
    o_arrays = [operations.get(f"x{i}", []) for i in range(1, 19)]
    for i, arr in enumerate(o_arrays, start=1):
        _require_len("operations", op_count, f"x{i}", arr)

    routes = data.get("routes", {})
    route_count = _to_int(routes.get("count", 0))
    r_x1 = routes.get("x1", [])
    r_x2 = routes.get("x2", [])
    r_x3 = routes.get("x3", [])
    r_x4 = routes.get("x4", [])
    for n, a in (("x1", r_x1), ("x2", r_x2), ("x3", r_x3), ("x4", r_x4)):
        _require_len("routes", route_count, n, a)

    ibom = data.get("ibom", {})
    ibom_count = _to_int(ibom.get("count", 0))
    b_x1 = ibom.get("x1", [])
    b_x2 = ibom.get("x2", [])
    b_x3 = ibom.get("x3", [])
    b_x4 = ibom.get("x4", [])
    for n, a in (("x1", b_x1), ("x2", b_x2), ("x3", b_x3), ("x4", b_x4)):
        _require_len("ibom", ibom_count, n, a)

    payload = {
        "general": (
        ctypes.c_float(_to_float(general.get("time1", 1.0), 1.0)),
        ctypes.c_float(_to_float(general.get("time2", 1.0), 1.0)),
        ctypes.c_float(_to_float(general.get("u_limit", 95.0), 95.0)),
        ctypes.c_float(_to_float(general.get("lab_var", 30.0), 30.0)),
        ctypes.c_float(_to_float(general.get("eq_var", 30.0), 30.0)),
        ctypes.c_float(_to_float(general.get("part_var", 30.0), 30.0)),
        ),
        "labor": (
        ctypes.c_int(labor_count),
        _iarr(l_x1), _farr(l_x2), _farr(l_x3), _farr(l_x4), _farr(l_x5), _farr(l_x6), _farr(l_x7), _iarr(l_x8),
        ),
        "equipment": (
        ctypes.c_int(eq_count),
        _iarr(e_x1), _iarr(e_x2), _farr(e_x3), _farr(e_x4), _farr(e_x5), _iarr(e_x6), _farr(e_x7), _farr(e_x8), _farr(e_x9), _iarr(e_x10),
        ),
        "parts": (
        ctypes.c_int(part_count),
        _iarr(p_x1), _farr(p_x2), _farr(p_x3), _farr(p_x4), _farr(p_x5), _farr(p_x6), _iarr(p_x7),
        ),
        "operations": (
        ctypes.c_int(op_count),
        _iarr(o_arrays[0]), _iarr(o_arrays[1]), _iarr(o_arrays[2]), _iarr(o_arrays[3]), _farr(o_arrays[4]),
        _farr(o_arrays[5]), _farr(o_arrays[6]), _farr(o_arrays[7]), _farr(o_arrays[8]), _farr(o_arrays[9]),
        _farr(o_arrays[10]), _farr(o_arrays[11]), _farr(o_arrays[12]), _farr(o_arrays[13]), _farr(o_arrays[14]),
        _farr(o_arrays[15]), _farr(o_arrays[16]), _farr(o_arrays[17]),
        ),
        "routes": (
        ctypes.c_int(route_count),
        _iarr(r_x1), _iarr(r_x2), _iarr(r_x3), _farr(r_x4),
        ),
        "ibom": (
        ctypes.c_int(ibom_count),
        _iarr(b_x1), _iarr(b_x2), _farr(b_x3),
        ),
    }
    return payload


def run_model_from_json(json_path, dll_path=None, output_dir=None, wid=1, routing_mode="auto"):
    _load_dotenv_if_present()
    json_path = os.path.abspath(json_path)
    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"JSON not found: {json_path}")

    if dll_path is None:
        dll_path = os.environ.get("RMCT_DLL") or default_mpx_dll_path()
    dll_path = _resolve_dll_path(dll_path)
    if not os.path.isfile(dll_path):
        raise FileNotFoundError(f"DLL not found: {dll_path}")

    if output_dir is None:
        output_dir = os.path.dirname(json_path)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    py_arch = _python_arch()
    dll_arch = _pe_machine_type(dll_path)
    print(f"Python arch: {py_arch}")
    print(f"DLL arch: {dll_arch}")
    print(f"DLL path: {dll_path}")
    print(f"JSON path: {json_path}")
    print(f"Output dir: {output_dir}")

    if dll_arch in ("x86", "x64") and dll_arch != py_arch:
        raise RuntimeError(
            f"Architecture mismatch. Python is {py_arch}, DLL is {dll_arch}. "
            "Use matching Python bitness."
        )

    _add_dll_dir_to_search_path(dll_path)
    dll = ctypes.WinDLL(dll_path)
    run_fn, run_name = _resolve_run_entry(dll)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def _run_once(payload_dict):
        rc_val = run_fn(
            output_dir.encode("utf-8"),
            *payload_dict["general"],
            *payload_dict["labor"],
            *payload_dict["equipment"],
            *payload_dict["parts"],
            *payload_dict["operations"],
            *payload_dict["routes"],
            *payload_dict["ibom"],
            ctypes.c_int(wid),
        )
        print(f"{run_name} -> {rc_val}")
        return rc_val

    packed_mode = isinstance(data.get("labor", []), dict)
    if packed_mode:
        payload = _payload_from_json(data)
        return _run_once(payload), "packed"

    if routing_mode == "json":
        payload = _build_payload_from_model_lists(
            data,
            force_safe_routing=False,
            strict_json_routing=False,
            normalize_json_routing=True,
        )
        return _run_once(payload), "json"
    if routing_mode == "safe":
        payload = _build_payload_from_model_lists(
            data,
            force_safe_routing=True,
            strict_json_routing=False,
            normalize_json_routing=False,
        )
        return _run_once(payload), "safe"

    # Match list-style JSON used by test4.json: do not re-scale branch % here (that path can
    # drop groups when totals are zero and confuse the DLL). Auto still retries in safe mode on fatal results.err.
    payload = _build_payload_from_model_lists(
        data,
        force_safe_routing=False,
        strict_json_routing=False,
        normalize_json_routing=False,
    )
    rc = _run_once(payload)
    if not _has_fatal_results_error(output_dir):
        return rc, "json"

    print("Fatal error found in results.err with JSON routing. Retrying in safe routing mode...")
    payload = _build_payload_from_model_lists(
        data,
        force_safe_routing=True,
        strict_json_routing=False,
        normalize_json_routing=False,
    )
    rc2 = _run_once(payload)
    return rc2, "safe"


def main():
    parser = argparse.ArgumentParser(
        description="Standalone DLL runner from JSON input (no local project imports)."
    )
    parser.add_argument("--json", required=True, help="Input JSON file.")
    parser.add_argument(
        "--dll",
        default=None,
        help="DLL path. If omitted: RMCT_DLL env var, then MPX44_DIR\\mpx0.dll/mpx95i.dll",
    )
    parser.add_argument("--out", default=None, help="Output directory for results files.")
    parser.add_argument("--wid", type=int, default=1, help="Run model WID.")
    parser.add_argument(
        "--routing-mode",
        choices=["auto", "json", "safe"],
        default="auto",
        help="Routing strategy for list-style JSON: auto/json/safe.",
    )
    args = parser.parse_args()

    rc, used_mode = run_model_from_json(
        args.json,
        dll_path=args.dll,
        output_dir=args.out,
        wid=args.wid,
        routing_mode=args.routing_mode,
    )
    print(f"Routing mode used: {used_mode}")
    raise SystemExit(0 if rc == 0 else rc)


if __name__ == "__main__":
    main()