"""Normalize operation payloads: one DOCK per product at op_number 0."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.operations.models import Operation
from apps.products.models import Product

ROUTING_META_OP_NUMBERS = {
    "DOCK": 0,
    "STOCK": 10000,
    "SCRAP": 10001,
}


def _is_dock(op: dict[str, Any]) -> bool:
    return str(op.get("op_name", "")).strip().upper() == "DOCK"


def normalize_product_operations(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docks = [o for o in ops if _is_dock(o)]
    if len(docks) <= 1:
        if len(docks) == 1 and docks[0].get("op_number") != 0:
            return [
                {**docks[0], "op_number": 0} if o.get("id") == docks[0].get("id") else o
                for o in ops
            ]
        return ops

    canonical = next((d for d in docks if d.get("op_number") == 0), None)
    if canonical is None:
        canonical = min(docks, key=lambda d: int(d.get("op_number") or 0))
    drop_ids = {d.get("id") for d in docks if d.get("id") != canonical.get("id")}

    out: list[dict[str, Any]] = []
    for op in ops:
        if op.get("id") in drop_ids:
            continue
        if op.get("id") == canonical.get("id"):
            out.append({**op, "op_number": 0})
        else:
            out.append(op)
    return out


def normalize_operations_payload(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_product: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        pid = str(op.get("product_id", ""))
        by_product.setdefault(pid, []).append(op)

    out: list[dict[str, Any]] = []
    for prod_ops in by_product.values():
        out.extend(normalize_product_operations(prod_ops))
    return out


def dedupe_routing_meta_operations_for_product(product: Product) -> None:
    """Soft-delete duplicate DOCK/STOCK/SCRAP rows; keep canonical op_number per meta op."""
    org = product.organization
    now = timezone.now()
    for meta_name, target_num in ROUTING_META_OP_NUMBERS.items():
        rows = list(
            Operation.objects.filter(
                product=product,
                organization=org,
                deleted_at__isnull=True,
                name__iexact=meta_name,
            ).order_by("op_number")
        )
        if not rows:
            continue
        canonical = next((r for r in rows if r.op_number == target_num), rows[0])
        if canonical.op_number != target_num:
            canonical.op_number = target_num
            canonical.save(update_fields=["op_number"])
        for extra in rows:
            if extra.id == canonical.id:
                continue
            extra.deleted_at = now
            extra.save(update_fields=["deleted_at"])
