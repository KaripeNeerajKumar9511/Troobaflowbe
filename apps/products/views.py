import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel
from apps.operations.models import Operation
from apps.routing.models import Routing
from apps.ibom.models import BOM

from .models import Product
from apps.organizations.scoping import get_org_context
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


@csrf_exempt
@require_http_methods(['POST'])
def model_products_create(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    org = ctx.organization

    pid = data.get('id')
    prod_kwargs = {
        'organization': org,
        'model': m,
        'name': data.get('name', '').upper(),
        'end_demand': data.get('demand', 0),
        'lot_size': data.get('lot_size', 1),
        'transfer_batch': data.get('tbatch_size', -1),
        'department_area': data.get('dept_code') or None,
        'demand_factor': data.get('demand_factor', 1),
        'lot_factor': data.get('lot_factor', 1),
        'variability_factor': data.get('var_factor', 1),
        'make_to_stock': data.get('make_to_stock', False),
        'gather_transfer_batches': data.get('gather_tbatches', False),
        'prod1': data.get('prod1', 0),
        'prod2': data.get('prod2', 0),
        'prod3': data.get('prod3', 0),
        'prod4': data.get('prod4', 0),
        'comments': data.get('comments', ''),
    }

    if pid:
        prod_kwargs['id'] = pid

    from django.db import IntegrityError

    try:
        p = Product.objects.create(**prod_kwargs)
    except IntegrityError:
        # A row with the same (organization, model, name) already exists.
        # Look it up ignoring deleted_at so we can either return or "undelete" it.
        existing = Product.objects.filter(
            organization=org,
            model=m,
            name=prod_kwargs["name"],
        ).first()
        if not existing:
            # If we cannot find the existing row, re-raise so it surfaces during debugging.
            raise

        # If the row was soft-deleted, revive it and update fields from the payload.
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.end_demand = prod_kwargs["end_demand"]
            existing.lot_size = prod_kwargs["lot_size"]
            existing.transfer_batch = prod_kwargs["transfer_batch"]
            existing.department_area = prod_kwargs["department_area"]
            existing.demand_factor = prod_kwargs["demand_factor"]
            existing.lot_factor = prod_kwargs["lot_factor"]
            existing.variability_factor = prod_kwargs["variability_factor"]
            existing.make_to_stock = prod_kwargs["make_to_stock"]
            existing.gather_transfer_batches = prod_kwargs["gather_transfer_batches"]
            existing.prod1 = prod_kwargs["prod1"]
            existing.prod2 = prod_kwargs["prod2"]
            existing.prod3 = prod_kwargs["prod3"]
            existing.prod4 = prod_kwargs["prod4"]
            existing.comments = prod_kwargs["comments"]
            existing.save()

        return JsonResponse(
            {
                "id": str(existing.id),
                "detail": "Product with this name already exists for this model and organization.",
            },
            status=200,
        )

    return JsonResponse({'id': str(p.id)}, status=201)


@csrf_exempt
@require_http_methods(['PATCH'])
def model_products_update(request, model_id, product_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    p = Product.objects.filter(id=product_id, model=m).first()
    if p is None:
        return JsonResponse({'error': 'Product not found for this model'}, status=404)
    sync_row_organization(p, ctx.organization)
    revive_soft_deleted(p)

    if 'name' in data:
        p.name = data['name']
    if 'demand' in data:
        p.end_demand = data['demand']
    if 'lot_size' in data:
        p.lot_size = data['lot_size']
    if 'tbatch_size' in data:
        p.transfer_batch = data['tbatch_size']
    if 'dept_code' in data:
        p.department_area = data['dept_code'] or None
    if 'demand_factor' in data:
        p.demand_factor = data['demand_factor']
    if 'lot_factor' in data:
        p.lot_factor = data['lot_factor']
    if 'var_factor' in data:
        p.variability_factor = data['var_factor']
    if 'make_to_stock' in data:
        p.make_to_stock = data['make_to_stock']
    if 'gather_tbatches' in data:
        p.gather_transfer_batches = data['gather_tbatches']
    for key in ('prod1', 'prod2', 'prod3', 'prod4'):
        if key in data:
            setattr(p, key, data[key])
    if 'comments' in data:
        p.comments = data['comments']

    p.save()

    return JsonResponse({})


@csrf_exempt
@require_http_methods(['DELETE'])
def model_products_delete(request, model_id, product_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    p = Product.objects.filter(id=product_id, model=m).first()
    if p is None:
        return JsonResponse({"success": True}, status=200)
    sync_row_organization(p, ctx.organization)

    from django.utils import timezone

    now = timezone.now()
    p.deleted_at = now
    p.save()

    Operation.objects.filter(product_id=product_id, deleted_at__isnull=True).update(deleted_at=now)
    Routing.objects.filter(product_id=product_id, deleted_at__isnull=True).update(deleted_at=now)
    BOM.objects.filter(parent_product_id=product_id, deleted_at__isnull=True).update(deleted_at=now)

    return JsonResponse({"success": True}, status=200)


@csrf_exempt
@require_http_methods(['DELETE'])
def model_products_clear_ops_routing(request, model_id, product_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)

    from django.utils import timezone

    now = timezone.now()
    Operation.objects.filter(product_id=product_id, organization_id=ctx.organization.id, deleted_at__isnull=True).update(deleted_at=now)
    Routing.objects.filter(product_id=product_id, organization_id=ctx.organization.id, deleted_at__isnull=True).update(deleted_at=now)

    m.run_status = 'needs_recalc'
    m.save()

    return JsonResponse({"success": True}, status=200)
