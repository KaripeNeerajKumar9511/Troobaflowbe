"""
Apply a frontend-shaped model snapshot to relational tables (post-migration from JSON blobs).
All writes are scoped to the target model and organization to prevent cross-tenant data bleed.
"""
from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.generaldata.models import GeneralData
from apps.ibom.models import BOM
from apps.labor.models import Labor
from apps.equipment.models import EquipmentGroup
from apps.products.models import Product
from apps.operations.models import Operation
from apps.routing.models import Routing

from .models import RMCMModel


class SnapshotScopeError(ValueError):
    """Snapshot references entity IDs that do not belong to this model."""


def _model_organization(m: RMCMModel):
    owner = m.owner
    if owner is None or not hasattr(owner, "profile") or not getattr(owner.profile, "organization_id", None):
        return None
    return owner.profile.organization


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _assert_id_owned_by_model(model_cls, entity_label: str, row_id: uuid.UUID, m: RMCMModel) -> None:
    """Reject snapshot IDs that already belong to another model (multi-tenant safety)."""
    existing = model_cls.objects.filter(id=row_id).first()
    if existing is not None and getattr(existing, "model_id", None) != m.id:
        raise SnapshotScopeError(
            f"{entity_label} id {row_id} does not belong to this model"
        )


def validate_snapshot_scope(m: RMCMModel, snap: dict) -> None:
    """Pre-flight: ensure snapshot does not reference foreign model rows."""
    snap = snap or {}
    for item in snap.get("labor") or []:
        lid = _parse_uuid(item.get("id"))
        if lid:
            _assert_id_owned_by_model(Labor, "Labor", lid, m)
    for item in snap.get("equipment") or []:
        eid = _parse_uuid(item.get("id"))
        if eid:
            _assert_id_owned_by_model(EquipmentGroup, "Equipment", eid, m)
    for item in snap.get("products") or []:
        pid = _parse_uuid(item.get("id"))
        if pid:
            _assert_id_owned_by_model(Product, "Product", pid, m)
    snapshot_product_ids = {
        str(_parse_uuid(item.get("id")))
        for item in (snap.get("products") or [])
        if _parse_uuid(item.get("id"))
    }
    for item in snap.get("operations") or []:
        oid = _parse_uuid(item.get("id"))
        if oid:
            op = Operation.objects.filter(id=oid).select_related("product").first()
            if op is not None and op.product.model_id != m.id:
                raise SnapshotScopeError(f"Operation id {oid} does not belong to this model")
        pid = _parse_uuid(item.get("product_id"))
        if pid:
            prod = Product.objects.filter(id=pid).first()
            if prod is not None and prod.model_id != m.id:
                raise SnapshotScopeError(f"Product id {pid} does not belong to this model")
            if prod is None and str(pid) not in snapshot_product_ids:
                raise SnapshotScopeError(f"Operation references unknown product id {pid}")


def _restore_general(m: RMCMModel, general: dict) -> None:
    if not isinstance(general, dict):
        return
    gd, _created = GeneralData.objects.get_or_create(model=m)
    mappings = {
        "model_title": "model_title",
        "author": "author",
        "comments": "comments",
        "ops_time_unit": "ops_time_unit",
        "mct_time_unit": "mct_time_unit",
        "prod_period_unit": "prod_period_unit",
        "conv1": "conv1",
        "conv2": "conv2",
        "util_limit": "util_limit",
        "var_equip": "var_equip",
        "var_labor": "var_labor",
        "var_prod": "var_prod",
        "gen1": "gen1",
        "gen2": "gen2",
        "gen3": "gen3",
        "gen4": "gen4",
    }
    for payload_key, field_name in mappings.items():
        if payload_key in general:
            setattr(gd, field_name, general[payload_key])
    gd.save()


def _soft_delete_extras(qs, keep_ids: set) -> None:
    now = timezone.now()
    if keep_ids:
        qs.exclude(id__in=keep_ids).update(deleted_at=now)
    else:
        qs.update(deleted_at=now)


def _restore_labor(m: RMCMModel, org, items: list) -> None:
    keep: set[uuid.UUID] = set()
    for item in items:
        lid = _parse_uuid(item.get("id"))
        if lid:
            keep.add(lid)
    _soft_delete_extras(Labor.objects.filter(model=m), keep)

    for item in items:
        lid = _parse_uuid(item.get("id"))
        if not lid:
            continue
        fields = {
            "organization": org,
            "model": m,
            "name": (item.get("name") or "").upper(),
            "count": item.get("count", 1),
            "overtime_percent": item.get("overtime_pct", 0),
            "unavailability_percent": item.get("unavail_pct", 0),
            "department": item.get("dept_code") or None,
            "setup_factor": item.get("setup_factor", 1),
            "run_factor": item.get("run_factor", 1),
            "variable_factor": item.get("var_factor", 1),
            "prioritize": item.get("prioritize_use", False),
            "lab1": item.get("lab1", 0),
            "lab2": item.get("lab2", 0),
            "lab3": item.get("lab3", 0),
            "lab4": item.get("lab4", 0),
            "notes": item.get("comments", ""),
            "deleted_at": None,
        }
        row = Labor.objects.filter(id=lid, model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            _assert_id_owned_by_model(Labor, "Labor", lid, m)
            Labor.objects.create(id=lid, **fields)


def _restore_equipment(m: RMCMModel, org, items: list) -> None:
    keep: set[uuid.UUID] = set()
    for item in items:
        eid = _parse_uuid(item.get("id"))
        if eid:
            keep.add(eid)
    _soft_delete_extras(EquipmentGroup.objects.filter(model=m), keep)

    for item in items:
        eid = _parse_uuid(item.get("id"))
        if not eid:
            continue
        equip_type = (item.get("equip_type") or "standard").lower()
        equipment_type = "Delay" if equip_type == "delay" else "Standard"

        labor_group = None
        lg_id = _parse_uuid(item.get("labor_group_id"))
        if lg_id:
            labor_group = Labor.objects.filter(id=lg_id, model=m).first()

        fields = {
            "organization": org,
            "model": m,
            "name": (item.get("name") or "").upper(),
            "count": item.get("count", 1),
            "mttf_minutes": item.get("mttf", 0),
            "mttr_minutes": item.get("mttr", 0),
            "overtime_percent": item.get("overtime_pct", 0),
            "labor_group": labor_group,
            "department_area": item.get("dept_code") or None,
            "out_of_area_equipment": item.get("out_of_area", False),
            "percent_time_unavailable": item.get("unavail_pct", 0),
            "setup_factor": item.get("setup_factor", 1),
            "run_factor": item.get("run_factor", 1),
            "variability_factor": item.get("var_factor", 1),
            "eq1": item.get("eq1", 0),
            "eq2": item.get("eq2", 0),
            "eq3": item.get("eq3", 0),
            "eq4": item.get("eq4", 0),
            "comments": item.get("comments", ""),
            "equipment_type": equipment_type,
            "deleted_at": None,
        }
        row = EquipmentGroup.objects.filter(id=eid, model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            _assert_id_owned_by_model(EquipmentGroup, "Equipment", eid, m)
            EquipmentGroup.objects.create(id=eid, **fields)


def _restore_products(m: RMCMModel, org, items: list) -> None:
    keep: set[uuid.UUID] = set()
    for item in items:
        pid = _parse_uuid(item.get("id"))
        if pid:
            keep.add(pid)
    _soft_delete_extras(Product.objects.filter(model=m), keep)

    for item in items:
        pid = _parse_uuid(item.get("id"))
        if not pid:
            continue
        fields = {
            "organization": org,
            "model": m,
            "name": (item.get("name") or "").upper(),
            "end_demand": item.get("demand", 0),
            "lot_size": item.get("lot_size", 1),
            "transfer_batch": item.get("tbatch_size", -1),
            "department_area": item.get("dept_code") or None,
            "demand_factor": item.get("demand_factor", 1),
            "lot_factor": item.get("lot_factor", 1),
            "variability_factor": item.get("var_factor", 1),
            "make_to_stock": item.get("make_to_stock", False),
            "gather_transfer_batches": item.get("gather_tbatches", False),
            "prod1": item.get("prod1", 0),
            "prod2": item.get("prod2", 0),
            "prod3": item.get("prod3", 0),
            "prod4": item.get("prod4", 0),
            "comments": item.get("comments", ""),
            "deleted_at": None,
        }
        row = Product.objects.filter(id=pid, model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            _assert_id_owned_by_model(Product, "Product", pid, m)
            Product.objects.create(id=pid, **fields)


def _restore_operations(m: RMCMModel, org, items: list) -> None:
    product_ids = Product.objects.filter(model=m).values_list("id", flat=True)
    keep: set[uuid.UUID] = set()
    for item in items:
        oid = _parse_uuid(item.get("id"))
        if oid:
            keep.add(oid)
    _soft_delete_extras(Operation.objects.filter(product_id__in=product_ids), keep)

    for item in items:
        oid = _parse_uuid(item.get("id"))
        pid = _parse_uuid(item.get("product_id"))
        if not oid or not pid:
            continue
        product = Product.objects.filter(id=pid, model=m, deleted_at__isnull=True).first()
        if not product:
            continue
        equipment_group = None
        eid = _parse_uuid(item.get("equip_id"))
        if eid:
            equipment_group = EquipmentGroup.objects.filter(id=eid, model=m).first()

        fields = {
            "organization": org,
            "product": product,
            "op_number": item.get("op_number", 1),
            "name": item.get("op_name", ""),
            "equipment_group": equipment_group,
            "percent_assign": item.get("pct_assigned", 100),
            "equipment_setup_per_lot": item.get("equip_setup_lot", 0),
            "equipment_setup_per_piece": item.get("equip_setup_piece", 0),
            "equipment_setup_per_tbatch": item.get("equip_setup_tbatch", 0),
            "equipment_run_per_piece": item.get("equip_run_piece", 0),
            "equipment_run_per_lot": item.get("equip_run_lot", 0),
            "equipment_run_per_tbatch": item.get("equip_run_tbatch", 0),
            "labor_setup_per_lot": item.get("labor_setup_lot", 0),
            "labor_setup_per_piece": item.get("labor_setup_piece", 0),
            "labor_setup_per_tbatch": item.get("labor_setup_tbatch", 0),
            "labor_run_per_piece": item.get("labor_run_piece", 0),
            "labor_run_per_lot": item.get("labor_run_lot", 0),
            "labor_run_per_tbatch": item.get("labor_run_tbatch", 0),
            "oper1": item.get("oper1", 0),
            "oper2": item.get("oper2", 0),
            "oper3": item.get("oper3", 0),
            "oper4": item.get("oper4", 0),
            "comments": "",
            "deleted_at": None,
        }
        row = Operation.objects.filter(id=oid, product__model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            op_existing = Operation.objects.filter(id=oid).first()
            if op_existing is not None:
                raise SnapshotScopeError(f"Operation id {oid} does not belong to this model")
            Operation.objects.create(id=oid, **fields)


def _restore_routing(m: RMCMModel, org, items: list) -> None:
    product_ids = list(Product.objects.filter(model=m).values_list("id", flat=True))
    keep: set[uuid.UUID] = set()
    for item in items:
        rid = _parse_uuid(item.get("id"))
        if rid:
            keep.add(rid)
    _soft_delete_extras(Routing.objects.filter(product_id__in=product_ids), keep)

    for item in items:
        rid = _parse_uuid(item.get("id"))
        pid = _parse_uuid(item.get("product_id"))
        if not rid or not pid:
            continue
        product = Product.objects.filter(id=pid, model=m, deleted_at__isnull=True).first()
        if not product:
            continue
        from_name = item.get("from_op_name") or ""
        to_name = item.get("to_op_name") or ""
        from_op = Operation.objects.filter(
            product=product, name=from_name, deleted_at__isnull=True
        ).first()
        to_op = Operation.objects.filter(
            product=product, name=to_name, deleted_at__isnull=True
        ).first()
        if not from_op or not to_op:
            continue
        fields = {
            "organization": org,
            "product": product,
            "from_operation": from_op,
            "to_operation": to_op,
            "probability": item.get("pct_routed", 1),
            "deleted_at": None,
        }
        row = Routing.objects.filter(id=rid, product__model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            existing = Routing.objects.filter(id=rid).first()
            if existing is not None and existing.product.model_id != m.id:
                raise SnapshotScopeError(f"Routing id {rid} does not belong to this model")
            Routing.objects.create(id=rid, **fields)


def _restore_ibom(m: RMCMModel, org, items: list) -> None:
    parent_ids = list(Product.objects.filter(model=m).values_list("id", flat=True))
    keep: set[uuid.UUID] = set()
    for item in items:
        bid = _parse_uuid(item.get("id"))
        if bid:
            keep.add(bid)
    _soft_delete_extras(BOM.objects.filter(parent_product_id__in=parent_ids), keep)

    for item in items:
        bid = _parse_uuid(item.get("id"))
        parent_id = _parse_uuid(item.get("parent_product_id"))
        component_id = _parse_uuid(item.get("component_product_id"))
        if not bid or not parent_id or not component_id:
            continue
        parent = Product.objects.filter(id=parent_id, model=m, deleted_at__isnull=True).first()
        component = Product.objects.filter(id=component_id, model=m, deleted_at__isnull=True).first()
        if not parent or not component:
            continue
        fields = {
            "organization": org,
            "parent_product": parent,
            "component_product": component,
            "quantity_per_assembly": item.get("units_per_assy", 1),
            "deleted_at": None,
        }
        row = BOM.objects.filter(id=bid, parent_product__model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            existing = BOM.objects.filter(id=bid).first()
            if existing is not None and existing.parent_product.model_id != m.id:
                raise SnapshotScopeError(f"BOM id {bid} does not belong to this model")
            BOM.objects.create(id=bid, **fields)


@transaction.atomic
def apply_snapshot_to_model(m: RMCMModel, snap: dict) -> None:
    """Replace model nested data from a version snapshot dict."""
    snap = snap or {}
    org = _model_organization(m)
    if org is None:
        raise ValueError("Owner organization not configured for model")

    validate_snapshot_scope(m, snap)

    if isinstance(snap.get("param_names"), dict):
        m.param_names = snap["param_names"]
    if isinstance(snap.get("dept_codes"), dict):
        m.dept_codes = snap["dept_codes"]

    _restore_general(m, snap.get("general") or {})

    labor = snap.get("labor") if isinstance(snap.get("labor"), list) else []
    equipment = snap.get("equipment") if isinstance(snap.get("equipment"), list) else []
    products = snap.get("products") if isinstance(snap.get("products"), list) else []
    operations = snap.get("operations") if isinstance(snap.get("operations"), list) else []
    routing = snap.get("routing") if isinstance(snap.get("routing"), list) else []
    ibom = snap.get("ibom") if isinstance(snap.get("ibom"), list) else []

    _restore_labor(m, org, labor)
    _restore_equipment(m, org, equipment)
    _restore_products(m, org, products)
    _restore_operations(m, org, operations)
    _restore_routing(m, org, routing)
    _restore_ibom(m, org, ibom)

    m.run_status = "needs_recalc"
    m.save(update_fields=["param_names", "dept_codes", "run_status", "updated_at"])
