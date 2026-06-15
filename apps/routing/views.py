import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import transaction
from django.utils import timezone

from apps.rmct.models import RMCMModel
from apps.products.models import Product
from apps.operations.models import Operation
from .models import Routing
from .routing_path import apply_routing_cell_update
from apps.organizations.scoping import get_org_context
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _get_org_from_model(model):
    return getattr(model, "organization", None)


def _get_or_create_product_from_model(model, product_id):
    existing = Product.objects.filter(
        id=product_id,
        organization=model.organization,
        deleted_at__isnull=True
    ).first()
    if existing:
        return existing

    # Do not fall back to legacy JSON snapshot on RMCMModel; require a real Product row.
    raise Product.DoesNotExist(f"Product {product_id} not found")


def _get_or_create_operation_from_model(model, product, op_name):
    from apps.operations.normalize import ROUTING_META_OP_NUMBERS

    org = _get_org_from_model(model)
    name = str(op_name or "").strip()
    name_upper = name.upper()

    if name_upper in ROUTING_META_OP_NUMBERS:
        target_num = ROUTING_META_OP_NUMBERS[name_upper]
        existing = (
            Operation.objects.filter(
                product=product,
                organization=org,
                deleted_at__isnull=True,
                name__iexact=name_upper,
            )
            .order_by("op_number")
            .first()
        )
        if existing:
            if existing.op_number != target_num:
                existing.op_number = target_num
                existing.save(update_fields=["op_number"])
            return existing
        return Operation.objects.create(
            organization=org,
            product=product,
            name=name_upper,
            op_number=target_num,
            percent_assign=100,
            equipment_setup_per_lot=0,
            equipment_run_per_piece=0,
            labor_setup_per_lot=0,
            labor_run_per_piece=0,
        )

    existing = Operation.objects.filter(
        product=product,
        name=name,
        organization=org,
        deleted_at__isnull=True,
    ).first()

    if existing:
        return existing

    from django.db.models import Max

    max_num = (
        Operation.objects.filter(product=product, organization=org)
        .aggregate(Max("op_number"))
        .get("op_number__max")
        or 0
    )

    return Operation.objects.create(
        organization=org,
        product=product,
        name=name,
        op_number=max_num + 10,
        percent_assign=100,
        equipment_setup_per_lot=0,
        equipment_run_per_piece=0,
        labor_setup_per_lot=0,
        labor_run_per_piece=0,
    )


@csrf_exempt
@require_http_methods(["POST"])
def model_routing_create(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    org = _get_org_from_model(m)
    if not org:
        return JsonResponse(
            {"error": "Owner organization not configured"}, status=400
        )
    print(data)
    product_id = data.get("product_id")
    from_op_name = data.get("from_op_name")
    to_op_name = data.get("to_op_name")
    pct_routed = data.get("pct_routed", 100)

    if pct_routed < 0 or pct_routed > 100:
        return JsonResponse({"error": "pct_routed must be 0-100"}, status=400)

    if not product_id or not from_op_name or not to_op_name:
        return JsonResponse(
            {"error": "product_id, from_op_name and to_op_name required"},
            status=400,
        )

    try:
        product = _get_or_create_product_from_model(m, product_id)
        from_op = _get_or_create_operation_from_model(m, product, from_op_name)
        to_op = _get_or_create_operation_from_model(m, product, to_op_name)
    except (Product.DoesNotExist, Operation.DoesNotExist) as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    # Reuse any existing route (including previously soft-deleted ones) to
    # avoid violating the unique_operation_path DB constraint.
    routing = (
        Routing.objects.filter(
            product=product,
            from_operation=from_op,
            to_operation=to_op,
        )
        .order_by("-deleted_at")
        .first()
    )

    if routing:
        # "Undelete" if it was soft-deleted and update probability
        routing.probability = pct_routed
        routing.deleted_at = None
        routing.save()
        created = False
    else:
        routing = Routing.objects.create(
            organization=org,
            product=product,
            from_operation=from_op,
            to_operation=to_op,
            probability=pct_routed,
        )
        created = True

    return JsonResponse({"id": str(routing.id)}, status=201 if created else 200)


@csrf_exempt
@require_http_methods(["PUT"])
@transaction.atomic
def model_routing_set(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    org = _get_org_from_model(m)
    if not org:
        return JsonResponse(
            {"error": "Owner organization not configured"}, status=400
        )

    product_id = data.get("productId") or data.get("product_id")
    entries = data.get("entries", [])

    if product_id is None:
        return JsonResponse({"error": "productId required"}, status=400)

    try:
        product = _get_or_create_product_from_model(m, product_id)
    except Product.DoesNotExist as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    # Soft-delete current live routes for this product. New rows cannot use
    # Routing.objects.create() for the same (product, from, to) because
    # unique_operation_path applies to soft-deleted rows too — reuse/undelete
    # like model_routing_create (see migrations constraint on routing table).
    Routing.objects.filter(
        product=product,
        organization=org,
        deleted_at__isnull=True,
    ).update(deleted_at=timezone.now())

    for entry in entries:

        from_op = _get_or_create_operation_from_model(
            m, product, entry.get("from_op_name")
        )

        to_op = _get_or_create_operation_from_model(
            m, product, entry.get("to_op_name")
        )

        pct_routed = entry.get("pct_routed", 100)

        if pct_routed < 0 or pct_routed > 100:
            return JsonResponse(
                {"error": "pct_routed must be 0-100"}, status=400
            )

        routing = (
            Routing.objects.filter(
                product=product,
                from_operation=from_op,
                to_operation=to_op,
            )
            .order_by("-deleted_at")
            .first()
        )

        if routing:
            routing.probability = pct_routed
            routing.deleted_at = None
            routing.organization = org
            routing.save()
        else:
            Routing.objects.create(
                organization=org,
                product=product,
                from_operation=from_op,
                to_operation=to_op,
                probability=pct_routed,
            )

    from apps.operations.normalize import dedupe_routing_meta_operations_for_product

    dedupe_routing_meta_operations_for_product(product)

    m.updated_at = timezone.now()
    m.save(update_fields=["updated_at"])

    return JsonResponse({})


@csrf_exempt
@require_http_methods(["PATCH"])
@transaction.atomic
def model_routing_update(request, model_id, route_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    org = ctx.organization

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    routing = Routing.objects.filter(
        id=route_id,
        product__model=m,
        deleted_at__isnull=True,
    ).first()
    if routing is None:
        return JsonResponse({"error": "Routing not found for this model"}, status=404)
    sync_row_organization(routing, org)

    pct = None
    if "pct_routed" in data:
        pct = data["pct_routed"]
        if pct < 0 or pct > 100:
            return JsonResponse(
                {"error": "pct_routed must be 0-100"}, status=400
            )

    to_op = None
    if "to_op_name" in data:
        try:
            to_op = _get_or_create_operation_from_model(
                m, routing.product, data.get("to_op_name")
            )
        except Operation.DoesNotExist as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    routing, merged = apply_routing_cell_update(
        routing,
        probability=pct,
        to_operation=to_op,
    )

    return JsonResponse({"id": str(routing.id), "merged": merged})


@csrf_exempt
@require_http_methods(["DELETE"])
def model_routing_delete(request, model_id, route_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)

    routing = Routing.objects.filter(
        id=route_id,
        product__model=m,
        deleted_at__isnull=True,
    ).first()

    if not routing:
        return JsonResponse({"success": True}, status=200)

    routing.deleted_at = timezone.now()
    routing.save()

    return JsonResponse({"success": True}, status=200)