import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel
from apps.labor.models import Labor

from .models import EquipmentGroup
from apps.organizations.scoping import get_org_context
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _resolve_equipment(model: RMCMModel, equip_id: str, org):
    eq = EquipmentGroup.objects.filter(id=equip_id, model=model).first()
    if eq is None:
        return None
    sync_row_organization(eq, org)
    revive_soft_deleted(eq)
    return eq


@csrf_exempt
@require_http_methods(['POST'])
def model_equipment_create(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    org = ctx.organization

    equip_id = data.get('id')
    equip_kwargs = {
        'organization': org,
        'model': m,
        'name': data.get('name', '').upper(),
        'count': data.get('count', 1),
        'mttf_minutes': data.get('mttf', 0),
        'mttr_minutes': data.get('mttr', 0),
        'overtime_percent': data.get('overtime_pct', 0),
        'department_area': data.get('dept_code') or None,
        'out_of_area_equipment': data.get('out_of_area', False),
        'percent_time_unavailable': data.get('unavail_pct', 0),
        'setup_factor': data.get('setup_factor', 1),
        'run_factor': data.get('run_factor', 1),
        'variability_factor': data.get('var_factor', 1),
        'eq1': data.get('eq1', 0),
        'eq2': data.get('eq2', 0),
        'eq3': data.get('eq3', 0),
        'eq4': data.get('eq4', 0),
        'comments': data.get('comments', ''),
    }

    labor_group_id = data.get('labor_group_id')
    if labor_group_id:
        equip_kwargs['labor_group_id'] = labor_group_id

    equip_type = (data.get('equip_type') or 'standard').lower()
    equip_kwargs['equipment_type'] = 'Delay' if equip_type == 'delay' else 'Standard'

    if equip_id:
        equip_kwargs['id'] = equip_id

    from django.db import IntegrityError

    try:
        eq = EquipmentGroup.objects.create(**equip_kwargs)
    except IntegrityError:
        # A row with the same (organization, model, name) already exists.
        # Look it up ignoring deleted_at so we can either return or "undelete" it.
        existing = EquipmentGroup.objects.filter(
            organization=org,
            model=m,
            name=equip_kwargs["name"],
        ).first()
        if not existing:
            # If we cannot find the existing row, re-raise so it surfaces during debugging.
            raise

        # If the row was soft-deleted, revive it and update fields from the payload.
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.count = equip_kwargs["count"]
            existing.mttf_minutes = equip_kwargs["mttf_minutes"]
            existing.mttr_minutes = equip_kwargs["mttr_minutes"]
            existing.overtime_percent = equip_kwargs["overtime_percent"]
            existing.department_area = equip_kwargs["department_area"]
            existing.out_of_area_equipment = equip_kwargs["out_of_area_equipment"]
            existing.percent_time_unavailable = equip_kwargs["percent_time_unavailable"]
            existing.setup_factor = equip_kwargs["setup_factor"]
            existing.run_factor = equip_kwargs["run_factor"]
            existing.variability_factor = equip_kwargs["variability_factor"]
            existing.eq1 = equip_kwargs["eq1"]
            existing.eq2 = equip_kwargs["eq2"]
            existing.eq3 = equip_kwargs["eq3"]
            existing.eq4 = equip_kwargs["eq4"]
            existing.comments = equip_kwargs["comments"]
            if "labor_group_id" in equip_kwargs:
                existing.labor_group_id = equip_kwargs["labor_group_id"]
            existing.equipment_type = equip_kwargs["equipment_type"]
            existing.save()

        return JsonResponse(
            {
                "id": str(existing.id),
                "detail": "Equipment group with this name already exists for this model and organization.",
            },
            status=200,
        )

    return JsonResponse({'id': str(eq.id)}, status=201)


@csrf_exempt
@require_http_methods(['PATCH'])
def model_equipment_update(request, model_id, equip_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    eq = _resolve_equipment(m, equip_id, ctx.organization)
    if eq is None:
        return JsonResponse({'error': 'Equipment not found for this model'}, status=404)

    if 'name' in data:
        eq.name = data['name']
    if 'count' in data:
        eq.count = data['count']
    if 'mttf' in data:
        eq.mttf_minutes = data['mttf']
    if 'mttr' in data:
        eq.mttr_minutes = data['mttr']
    if 'overtime_pct' in data:
        eq.overtime_percent = data['overtime_pct']
    if 'dept_code' in data:
        eq.department_area = data['dept_code'] or None
    if 'out_of_area' in data:
        eq.out_of_area_equipment = data['out_of_area']
    if 'unavail_pct' in data:
        eq.percent_time_unavailable = data['unavail_pct']
    if 'setup_factor' in data:
        eq.setup_factor = data['setup_factor']
    if 'run_factor' in data:
        eq.run_factor = data['run_factor']
    if 'var_factor' in data:
        eq.variability_factor = data['var_factor']
    for key in ('eq1', 'eq2', 'eq3', 'eq4'):
        if key in data:
            setattr(eq, key, data[key])
    if 'comments' in data:
        eq.comments = data['comments']
    if 'labor_group_id' in data:
        labor_group_id = data.get('labor_group_id') or None
        if labor_group_id:
            try:
                eq.labor_group = Labor.objects.get(id=labor_group_id)
            except Labor.DoesNotExist:
                eq.labor_group = None
        else:
            eq.labor_group = None
    if 'equip_type' in data:
        equip_type = (data.get('equip_type') or 'standard').lower()
        eq.equipment_type = 'Delay' if equip_type == 'delay' else 'Standard'

    eq.save()

    return JsonResponse({})


@csrf_exempt
@require_http_methods(['DELETE'])
def model_equipment_delete(request, model_id, equip_id):
    print('model_equipment_delete', model_id, equip_id)
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    eq = _resolve_equipment(m, equip_id, ctx.organization)
    if eq is None:
        return JsonResponse({"success": True}, status=200)

    from django.utils import timezone

    eq.deleted_at = timezone.now()
    eq.save()

    return JsonResponse({"success": True}, status=200)
