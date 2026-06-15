"""Server-authoritative cell updates for org-wide realtime collaboration."""

from __future__ import annotations

from typing import Any

from apps.equipment.models import EquipmentGroup
from apps.generaldata.models import GeneralData
from apps.labor.models import Labor
from django.db.models import Max

from apps.ibom.models import BOM
from apps.operations.models import Operation
from apps.products.models import Product
from apps.routing.models import Routing
from apps.routing.routing_path import apply_routing_cell_update
from apps.rmct.models import RMCMModel
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization

ALLOWED_ENTITIES = frozenset(
    {"operation", "product", "equipment", "labor", "general", "routing", "ibom"}
)

ALLOWED_OPERATION_COLUMNS = frozenset(
    {
        "op_number",
        "name",
        "percent_assign",
        "equipment_setup_per_lot",
        "equipment_run_per_piece",
        "labor_setup_per_lot",
        "labor_run_per_piece",
        "equipment_setup_per_piece",
        "equipment_setup_per_tbatch",
        "equipment_run_per_lot",
        "equipment_run_per_tbatch",
        "labor_setup_per_piece",
        "labor_setup_per_tbatch",
        "labor_run_per_lot",
        "labor_run_per_tbatch",
        "oper1",
        "oper2",
        "oper3",
        "oper4",
        "comments",
        "equipment_group_id",
    }
)

ALLOWED_PRODUCT_FIELDS = frozenset(
    {
        "name",
        "demand",
        "lot_size",
        "tbatch_size",
        "dept_code",
        "demand_factor",
        "lot_factor",
        "var_factor",
        "make_to_stock",
        "gather_tbatches",
        "prod1",
        "prod2",
        "prod3",
        "prod4",
        "comments",
    }
)

ALLOWED_EQUIPMENT_FIELDS = frozenset(
    {
        "name",
        "count",
        "equip_type",
        "mttf",
        "mttr",
        "overtime_pct",
        "labor_group_id",
        "dept_code",
        "out_of_area",
        "setup_factor",
        "run_factor",
        "var_factor",
        "eq1",
        "eq2",
        "eq3",
        "eq4",
        "comments",
    }
)

ALLOWED_LABOR_FIELDS = frozenset(
    {
        "name",
        "count",
        "overtime_pct",
        "unavail_pct",
        "dept_code",
        "prioritize_use",
        "setup_factor",
        "run_factor",
        "var_factor",
        "lab1",
        "lab2",
        "lab3",
        "lab4",
        "comments",
    }
)

ALLOWED_ROUTING_COLUMNS = frozenset({"pct_routed", "to_op_name"})

ALLOWED_IBOM_COLUMNS = frozenset({"units_per_assy"})

ALLOWED_GENERAL_FIELDS = frozenset(
    {
        "model_title",
        "author",
        "comments",
        "ops_time_unit",
        "mct_time_unit",
        "prod_period_unit",
        "conv1",
        "conv2",
        "util_limit",
        "var_equip",
        "var_labor",
        "var_prod",
        "gen1",
        "gen2",
        "gen3",
        "gen4",
    }
)


def _model_in_org(*, model_id: str, org_id: str) -> bool:
    return RMCMModel.objects.filter(id=model_id, organization_id=org_id).exists()


def apply_collab_cell(
    *,
    entity: str,
    model_id: str,
    org_id: str,
    row_id: str,
    column: str,
    value: Any,
) -> dict[str, Any] | None:
    entity = (entity or "operation").strip().lower()
    if entity not in ALLOWED_ENTITIES:
        return None
    if not _model_in_org(model_id=model_id, org_id=org_id):
        return None

    if entity == "operation":
        return _apply_operation(model_id=model_id, org_id=org_id, row_id=row_id, column=column, value=value)
    if entity == "product":
        return _apply_product(model_id=model_id, org_id=org_id, row_id=row_id, column=column, value=value)
    if entity == "equipment":
        return _apply_equipment(model_id=model_id, org_id=org_id, row_id=row_id, column=column, value=value)
    if entity == "labor":
        return _apply_labor(model_id=model_id, org_id=org_id, row_id=row_id, column=column, value=value)
    if entity == "general":
        return _apply_general(model_id=model_id, org_id=org_id, column=column, value=value)
    if entity == "routing":
        return _apply_routing(
            model_id=model_id, org_id=org_id, row_id=row_id, column=column, value=value
        )
    if entity == "ibom":
        return _apply_ibom(
            model_id=model_id, org_id=org_id, row_id=row_id, column=column, value=value
        )
    return None


def _apply_operation(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    if column not in ALLOWED_OPERATION_COLUMNS:
        return None
    op = (
        Operation.objects.filter(id=row_id, product__model_id=model_id)
        .select_related("product")
        .first()
    )
    if not op or not op.product_id:
        return None
    sync_row_organization(op, org_id)
    revive_soft_deleted(op)
    if column == "equipment_group_id":
        if value:
            op.equipment_group = EquipmentGroup.objects.filter(
                id=value, model_id=model_id
            ).first()
        else:
            op.equipment_group = None
        op.save(update_fields=["equipment_group_id", "updated_at"])
    else:
        setattr(op, column, value)
        op.save(update_fields=[column, "updated_at"])
    return _payload("operation", model_id, row_id, column, value, op.updated_at)


def _apply_product(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    if column not in ALLOWED_PRODUCT_FIELDS:
        return None
    p = Product.objects.filter(id=row_id, model_id=model_id).first()
    if not p:
        return None
    sync_row_organization(p, org_id)
    revive_soft_deleted(p)
    if column == "name":
        p.name = value
    elif column == "demand":
        p.end_demand = value
    elif column == "lot_size":
        p.lot_size = value
    elif column == "tbatch_size":
        p.transfer_batch = value
    elif column == "dept_code":
        p.department_area = value or None
    elif column == "demand_factor":
        p.demand_factor = value
    elif column == "lot_factor":
        p.lot_factor = value
    elif column == "var_factor":
        p.variability_factor = value
    elif column == "make_to_stock":
        p.make_to_stock = bool(value)
    elif column == "gather_tbatches":
        p.gather_transfer_batches = bool(value)
    elif column in ("prod1", "prod2", "prod3", "prod4"):
        setattr(p, column, value)
    elif column == "comments":
        p.comments = value
    p.save()
    return _payload("product", model_id, row_id, column, value, p.updated_at)


def _apply_equipment(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    if column not in ALLOWED_EQUIPMENT_FIELDS:
        return None
    eq = EquipmentGroup.objects.filter(
        id=row_id, model_id=model_id
    ).first()
    if not eq:
        return None
    if str(eq.organization_id) != str(org_id):
        sync_row_organization(eq, org_id)
    revive_soft_deleted(eq)
    if column == "name":
        eq.name = value
    elif column == "count":
        eq.count = value
    elif column == "mttf":
        eq.mttf_minutes = value
    elif column == "mttr":
        eq.mttr_minutes = value
    elif column == "overtime_pct":
        eq.overtime_percent = value
    elif column == "dept_code":
        eq.department_area = value or None
    elif column == "out_of_area":
        eq.out_of_area_equipment = bool(value)
    elif column == "setup_factor":
        eq.setup_factor = value
    elif column == "run_factor":
        eq.run_factor = value
    elif column == "var_factor":
        eq.variability_factor = value
    elif column == "equip_type":
        et = (str(value or "standard")).lower()
        eq.equipment_type = "Delay" if et == "delay" else "Standard"
    elif column == "labor_group_id":
        if value:
            try:
                eq.labor_group = Labor.objects.get(id=value, model_id=model_id)
            except Labor.DoesNotExist:
                eq.labor_group = None
        else:
            eq.labor_group = None
    elif column in ("eq1", "eq2", "eq3", "eq4"):
        setattr(eq, column, value)
    elif column == "comments":
        eq.comments = value
    eq.save()
    return _payload("equipment", model_id, row_id, column, value, eq.updated_at)


def _apply_labor(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    if column not in ALLOWED_LABOR_FIELDS:
        return None
    labor = Labor.objects.filter(id=row_id, model_id=model_id).first()
    if not labor:
        return None
    sync_row_organization(labor, org_id)
    revive_soft_deleted(labor)
    if column == "name":
        labor.name = value
    elif column == "count":
        labor.count = value
    elif column == "overtime_pct":
        labor.overtime_percent = value
    elif column == "unavail_pct":
        labor.unavailability_percent = value
    elif column == "dept_code":
        labor.department = value or None
    elif column == "setup_factor":
        labor.setup_factor = value
    elif column == "run_factor":
        labor.run_factor = value
    elif column == "var_factor":
        labor.variable_factor = value
    elif column == "prioritize_use":
        labor.prioritize = bool(value)
    elif column in ("lab1", "lab2", "lab3", "lab4"):
        setattr(labor, column, value)
    elif column == "comments":
        labor.notes = value
    labor.save()
    return _payload("labor", model_id, row_id, column, value, labor.updated_at)


def _routing_to_operation(*, model: RMCMModel, product: Product, op_name: str, org_id: str) -> Operation | None:
    name = str(op_name or "").strip()
    if not name:
        return None
    existing = Operation.objects.filter(
        product=product,
        name=name,
        organization_id=org_id,
        deleted_at__isnull=True,
    ).first()
    if existing:
        return existing
    max_num = (
        Operation.objects.filter(product=product, organization_id=org_id)
        .aggregate(Max("op_number"))
        .get("op_number__max")
        or 0
    )
    return Operation.objects.create(
        organization_id=org_id,
        product=product,
        name=name,
        op_number=max_num + 10,
        percent_assign=100,
        equipment_setup_per_lot=0,
        equipment_run_per_piece=0,
        labor_setup_per_lot=0,
        labor_run_per_piece=0,
    )


def _apply_routing(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    if column not in ALLOWED_ROUTING_COLUMNS:
        return None
    routing = (
        Routing.objects.filter(
            id=row_id, product__model_id=model_id, deleted_at__isnull=True
        )
        .select_related("product")
        .first()
    )
    if not routing or not routing.product_id:
        return None
    sync_row_organization(routing, org_id)
    m = RMCMModel.objects.filter(id=model_id, organization_id=org_id).first()
    if not m:
        return None
    pct = None
    to_op = None
    if column == "pct_routed":
        pct = float(value)
        if pct < 0 or pct > 100:
            return None
    elif column == "to_op_name":
        to_op = _routing_to_operation(
            model=m, product=routing.product, op_name=str(value), org_id=org_id
        )
        if not to_op:
            return None
    routing, _merged = apply_routing_cell_update(
        routing,
        probability=pct,
        to_operation=to_op,
    )
    return _payload("routing", model_id, str(routing.id), column, value, routing.updated_at)


def _apply_ibom(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    if column not in ALLOWED_IBOM_COLUMNS:
        return None
    bom = (
        BOM.objects.filter(
            id=row_id, parent_product__model_id=model_id, deleted_at__isnull=True
        )
        .select_related("parent_product")
        .first()
    )
    if not bom or not bom.parent_product_id:
        return None
    sync_row_organization(bom, org_id)
    revive_soft_deleted(bom)
    if column == "units_per_assy":
        qty = float(value)
        if qty <= 0:
            return None
        bom.quantity_per_assembly = qty
    bom.save()
    return _payload("ibom", model_id, row_id, column, value, bom.updated_at)


def _apply_general(*, model_id: str, org_id: str, column: str, value: Any) -> dict[str, Any] | None:
    if column not in ALLOWED_GENERAL_FIELDS:
        return None
    m = RMCMModel.objects.filter(id=model_id, organization_id=org_id).first()
    if not m:
        return None
    gd, _ = GeneralData.objects.get_or_create(model=m)
    if column in (
        "model_title",
        "author",
        "comments",
        "ops_time_unit",
        "mct_time_unit",
        "prod_period_unit",
    ):
        setattr(gd, column, value)
    elif column in ("conv1", "conv2", "util_limit", "var_equip", "var_labor", "var_prod", "gen1", "gen2", "gen3", "gen4"):
        setattr(gd, column, value)
    gd.save()
    return _payload("general", model_id, model_id, column, value, None)


def _payload(
    entity: str,
    model_id: str,
    row_id: str,
    column: str,
    value: Any,
    updated_at,
) -> dict[str, Any]:
    return {
        "entity": entity,
        "model_id": str(model_id),
        "row_id": str(row_id),
        "column": column,
        "value": value,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


# Backward compatibility
def apply_operation_cell(
    *, model_id: str, org_id: str, row_id: str, column: str, value: Any
) -> dict[str, Any] | None:
    result = apply_collab_cell(
        entity="operation",
        model_id=model_id,
        org_id=org_id,
        row_id=row_id,
        column=column,
        value=value,
    )
    return result
