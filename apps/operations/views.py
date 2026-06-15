import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import IntegrityError, transaction

from apps.rmct.models import RMCMModel
from apps.equipment.models import EquipmentGroup
from apps.labor.models import Labor
from apps.routing.models import Routing
from apps.products.models import Product

from .models import Operation
from apps.organizations.scoping import get_org_context
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


@csrf_exempt
@require_http_methods(['POST'])
def model_operations_create(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    org = ctx.organization

    op_id = data.get('id')
    product_id = data.get('product_id')
    product = get_object_or_404(Product, id=product_id, organization_id=ctx.organization.id)
    equip_id = data.get('equip_id')
    equipment_group = None
    if equip_id:
        equipment_group = EquipmentGroup.objects.filter(id=equip_id).first()

    op_name = str(data.get('op_name', '') or '').strip()
    op_name_upper = op_name.upper()

    if op_name_upper == 'DOCK':
        existing_dock = Operation.objects.filter(
            organization=org,
            product=product,
            deleted_at__isnull=True,
            name__iexact='DOCK',
        ).first()
        if existing_dock:
            if existing_dock.op_number != 0:
                existing_dock.op_number = 0
                existing_dock.save(update_fields=['op_number'])
            from apps.operations.normalize import dedupe_routing_meta_operations_for_product

            dedupe_routing_meta_operations_for_product(product)
            return JsonResponse({'id': str(existing_dock.id)}, status=200)

    operation_kwargs = {
        'organization': org,
        'product': product,
        'op_number': 0 if op_name_upper == 'DOCK' else data.get('op_number', 1),
        'name': op_name,
        'equipment_group': equipment_group,
        'percent_assign': data.get('pct_assigned', 100),
        'equipment_setup_per_lot': data.get('equip_setup_lot', 0),
        'equipment_run_per_piece': data.get('equip_run_piece', 0),
        'labor_setup_per_lot': data.get('labor_setup_lot', 0),
        'labor_run_per_piece': data.get('labor_run_piece', 0),
        'equipment_setup_per_piece': data.get('equip_setup_piece', 0),
        'equipment_setup_per_tbatch': data.get('equip_setup_tbatch', 0),
        'equipment_run_per_lot': data.get('equip_run_lot', 0),
        'equipment_run_per_tbatch': data.get('equip_run_tbatch', 0),
        'labor_setup_per_piece': data.get('labor_setup_piece', 0),
        'labor_setup_per_tbatch': data.get('labor_setup_tbatch', 0),
        'labor_run_per_lot': data.get('labor_run_lot', 0),
        'labor_run_per_tbatch': data.get('labor_run_tbatch', 0),
        'oper1': data.get('oper1', 0),
        'oper2': data.get('oper2', 0),
        'oper3': data.get('oper3', 0),
        'oper4': data.get('oper4', 0),
        'comments': '',
    }

    labor_group_id = data.get('labor_group_id')
    if labor_group_id:
        operation_kwargs['labor'] = Labor.objects.filter(id=labor_group_id).first()

    if op_id:
        operation_kwargs['id'] = op_id

    try:
        op = Operation.objects.create(**operation_kwargs)
    except IntegrityError:
        # A row with the same (product, op_number) already exists.
        # Look it up (including soft-deleted rows) so we can either return or "undelete" it.
        existing = Operation.objects.filter(
            organization=org,
            product=product,
            op_number=operation_kwargs["op_number"],
        ).first()
        if not existing:
            # If we cannot find the existing row, re-raise so it surfaces during debugging.
            raise

        # If the row was soft-deleted, revive it and update fields from the payload.
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.name = operation_kwargs["name"]
            existing.equipment_group = operation_kwargs.get("equipment_group")
            existing.labor = operation_kwargs.get("labor")
            existing.percent_assign = operation_kwargs["percent_assign"]
            existing.equipment_setup_per_lot = operation_kwargs["equipment_setup_per_lot"]
            existing.equipment_run_per_piece = operation_kwargs["equipment_run_per_piece"]
            existing.labor_setup_per_lot = operation_kwargs["labor_setup_per_lot"]
            existing.labor_run_per_piece = operation_kwargs["labor_run_per_piece"]
            existing.equipment_setup_per_piece = operation_kwargs["equipment_setup_per_piece"]
            existing.equipment_setup_per_tbatch = operation_kwargs["equipment_setup_per_tbatch"]
            existing.equipment_run_per_lot = operation_kwargs["equipment_run_per_lot"]
            existing.equipment_run_per_tbatch = operation_kwargs["equipment_run_per_tbatch"]
            existing.labor_setup_per_piece = operation_kwargs["labor_setup_per_piece"]
            existing.labor_setup_per_tbatch = operation_kwargs["labor_setup_per_tbatch"]
            existing.labor_run_per_lot = operation_kwargs["labor_run_per_lot"]
            existing.labor_run_per_tbatch = operation_kwargs["labor_run_per_tbatch"]
            existing.oper1 = operation_kwargs["oper1"]
            existing.oper2 = operation_kwargs["oper2"]
            existing.oper3 = operation_kwargs["oper3"]
            existing.oper4 = operation_kwargs["oper4"]
            existing.comments = operation_kwargs["comments"]
            existing.save()

        from apps.operations.normalize import dedupe_routing_meta_operations_for_product

        dedupe_routing_meta_operations_for_product(product)
        return JsonResponse(
            {
                "id": str(existing.id),
                "detail": "Operation with this number already exists for this product.",
            },
            status=200,
        )

    from apps.operations.normalize import dedupe_routing_meta_operations_for_product

    dedupe_routing_meta_operations_for_product(product)

    return JsonResponse({'id': str(op.id)}, status=201)


@csrf_exempt
@require_http_methods(['PATCH'])
def model_operations_update(request, model_id, op_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    op = Operation.objects.filter(id=op_id, product__model=m).first()
    if op is None:
        product_id = data.get('product_id')
        op_number = data.get('op_number')
        if product_id is not None and op_number is not None:
            op = Operation.objects.filter(
                product_id=product_id,
                product__model=m,
                op_number=op_number,
            ).first()
    if op is None:
        return JsonResponse({'error': 'Operation not found'}, status=404)
    sync_row_organization(op, ctx.organization)
    revive_soft_deleted(op)

    if 'op_name' in data:
        op.name = data['op_name']
    if 'op_number' in data:
        op.op_number = data['op_number']
    if 'pct_assigned' in data:
        op.percent_assign = data['pct_assigned']
    if 'equip_setup_lot' in data:
        op.equipment_setup_per_lot = data['equip_setup_lot']
    if 'equip_run_piece' in data:
        op.equipment_run_per_piece = data['equip_run_piece']
    if 'labor_setup_lot' in data:
        op.labor_setup_per_lot = data['labor_setup_lot']
    if 'labor_run_piece' in data:
        op.labor_run_per_piece = data['labor_run_piece']
    if 'equip_setup_piece' in data:
        op.equipment_setup_per_piece = data['equip_setup_piece']
    if 'equip_setup_tbatch' in data:
        op.equipment_setup_per_tbatch = data['equip_setup_tbatch']
    if 'equip_run_lot' in data:
        op.equipment_run_per_lot = data['equip_run_lot']
    if 'equip_run_tbatch' in data:
        op.equipment_run_per_tbatch = data['equip_run_tbatch']
    if 'labor_setup_piece' in data:
        op.labor_setup_per_piece = data['labor_setup_piece']
    if 'labor_setup_tbatch' in data:
        op.labor_setup_per_tbatch = data['labor_setup_tbatch']
    if 'labor_run_lot' in data:
        op.labor_run_per_lot = data['labor_run_lot']
    if 'labor_run_tbatch' in data:
        op.labor_run_per_tbatch = data['labor_run_tbatch']
    if 'oper1' in data:
        op.oper1 = data['oper1']
    if 'oper2' in data:
        op.oper2 = data['oper2']
    if 'oper3' in data:
        op.oper3 = data['oper3']
    if 'oper4' in data:
        op.oper4 = data['oper4']
    if 'equip_id' in data:
        equip_id = data.get('equip_id') or None
        if equip_id:
            op.equipment_group = EquipmentGroup.objects.filter(id=equip_id).first()
        else:
            op.equipment_group = None
    if 'labor_group_id' in data:
        labor_group_id = data.get('labor_group_id') or None
        if labor_group_id:
            op.labor = Labor.objects.filter(id=labor_group_id).first()
        else:
            op.labor = None

    new_op_number = data.get('op_number')
    if new_op_number is not None:
        conflicting = Operation.objects.filter(
            product=op.product,
            op_number=new_op_number,
            organization_id=ctx.organization.id,
            deleted_at__isnull=True,
        ).exclude(id=op.id).first()
        if conflicting:
            old_op_number = Operation.objects.filter(
                id=op.id
            ).values_list('op_number', flat=True).first()
            try:
                with transaction.atomic():
                    # Move conflicting to a temp value to avoid constraint violation
                    Operation.objects.filter(id=conflicting.id).update(op_number=-1)
                    op.save()
                    Operation.objects.filter(id=conflicting.id).update(op_number=old_op_number)
            except IntegrityError as e:
                return JsonResponse(
                    {"error": "integrity_error", "detail": str(e)},
                    status=400,
                )
            return JsonResponse({"swapped_with": str(conflicting.id)})

    try:
        op.save()
    except IntegrityError as e:
        return JsonResponse(
            {"error": "integrity_error", "detail": str(e)},
            status=400,
        )

    return JsonResponse({})


@csrf_exempt
@require_http_methods(['DELETE'])
def model_operations_delete(request, model_id, op_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    op = Operation.objects.filter(id=op_id, product__model=m).first()
    if op is None:
        return JsonResponse({"success": True}, status=200)

    from django.utils import timezone

    now = timezone.now()
    op.deleted_at = now
    op.save()

    Routing.objects.filter(from_operation=op, deleted_at__isnull=True).update(deleted_at=now)

    return JsonResponse({"success": True}, status=200)
