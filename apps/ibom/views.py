import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel
from apps.products.models import Product

from .models import BOM


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


@csrf_exempt
@require_http_methods(['POST'])
def model_ibom_create(request, model_id):
    m = get_object_or_404(RMCMModel, id=model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    owner = m.owner
    if owner is None or not hasattr(owner, "profile") or not getattr(owner.profile, "organization_id", None):
        return JsonResponse({'error': 'Owner organization not configured for model'}, status=400)

    org = owner.profile.organization

    parent_id = data.get('parent_product_id')
    component_id = data.get('component_product_id')
    units = data.get('units_per_assy', 1)

    parent = get_object_or_404(Product, id=parent_id)
    component = get_object_or_404(Product, id=component_id)

    bid = data.get('id')
    bom_kwargs = {
        'organization': org,
        'parent_product': parent,
        'component_product': component,
        'quantity_per_assembly': units,
    }
    if bid:
        bom_kwargs['id'] = bid

    b = BOM.objects.create(**bom_kwargs)

    return JsonResponse({'id': str(b.id)}, status=201)


@csrf_exempt
@require_http_methods(['PUT'])
def model_ibom_set_for_parent(request, model_id, parent_id):
    """PUT /api/models/:id/ibom/:parentId — set all IBOM entries for a parent product."""
    m = get_object_or_404(RMCMModel, id=model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    owner = m.owner
    if owner is None or not hasattr(owner, "profile") or not getattr(owner.profile, "organization_id", None):
        return JsonResponse({'error': 'Owner organization not configured for model'}, status=400)

    org = owner.profile.organization

    entries = data if isinstance(data, list) else data.get('entries', [])

    parent = get_object_or_404(Product, id=parent_id)

    from django.utils import timezone

    now = timezone.now()
    BOM.objects.filter(parent_product=parent, deleted_at__isnull=True).update(deleted_at=now)

    from django.db import IntegrityError

    for entry in entries:
        component_id = entry.get('component_product_id')
        units = entry.get('units_per_assy', 1)
        component = get_object_or_404(Product, id=component_id)
        try:
            BOM.objects.create(
                organization=org,
                parent_product=parent,
                component_product=component,
                quantity_per_assembly=units,
            )
        except IntegrityError:
            # A row with the same (parent_product, component_product) already exists.
            existing = BOM.objects.filter(
                parent_product=parent,
                component_product=component,
            ).first()
            if not existing:
                # If we cannot find the existing row, re-raise so it surfaces during debugging.
                raise
            # If the row was soft-deleted, revive it and update the quantity.
            if existing.deleted_at is not None:
                existing.deleted_at = None
            existing.quantity_per_assembly = units
            existing.save()

    return JsonResponse({})


@csrf_exempt
@require_http_methods(['PATCH'])
def model_ibom_update(request, model_id, entry_id):
    get_object_or_404(RMCMModel, id=model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    b = get_object_or_404(BOM, id=entry_id)

    if 'units_per_assy' in data:
        b.quantity_per_assembly = data['units_per_assy']

    b.save()

    return JsonResponse({})


@csrf_exempt
@require_http_methods(['DELETE'])
def model_ibom_delete(request, model_id, entry_id):
    get_object_or_404(RMCMModel, id=model_id)
    try:
        b = BOM.objects.get(id=entry_id)
    except BOM.DoesNotExist:
        return JsonResponse({"success": True}, status=200)

    from django.utils import timezone

    b.deleted_at = timezone.now()
    b.save()

    return JsonResponse({"success": True}, status=200)