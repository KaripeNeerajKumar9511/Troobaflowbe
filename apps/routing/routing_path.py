"""Helpers for routing rows under unique_operation_path (product, from, to)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.operations.models import Operation
    from apps.routing.models import Routing


def apply_routing_cell_update(
    routing: Routing,
    *,
    probability: float | None = None,
    to_operation: Operation | None = None,
) -> tuple[Routing, bool]:
    """
    Update probability and/or destination without violating unique_operation_path.

    Returns (canonical_row, merged) where merged is True if the row id changed
    because the edit was folded into another live route with the same path.
    """
    from apps.routing.models import Routing

    merged = False
    prob = probability if probability is not None else routing.probability

    if to_operation is not None:
        conflict = (
            Routing.objects.filter(
                product_id=routing.product_id,
                from_operation_id=routing.from_operation_id,
                to_operation_id=to_operation.id,
            )
            .exclude(pk=routing.pk)
            .order_by("-deleted_at")
            .first()
        )

        if conflict:
            if conflict.deleted_at is None:
                # Another live route already uses this path — merge into it.
                conflict.probability = prob
                conflict.deleted_at = None
                conflict.save()
                routing.deleted_at = timezone.now()
                routing.save(update_fields=["deleted_at", "updated_at"])
                return conflict, True

            # Soft-deleted ghost blocks the unique index — remove it, keep this row.
            conflict.delete()

        routing.to_operation = to_operation

    routing.probability = prob
    routing.deleted_at = None
    routing.save()
    return routing, merged
