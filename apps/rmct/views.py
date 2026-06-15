"""
RMCT REST API: models, versions, scenarios, changes, results.
Matches frontend contract in docs/BACKEND_API.md. No Supabase; all data in Django DB.
"""
import json
import uuid
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.shortcuts import get_object_or_404

from apps.generaldata.models import GeneralData
from apps.labor.models import Labor
from apps.equipment.models import EquipmentGroup
from apps.products.models import Product
from apps.operations.models import Operation
from apps.routing.models import Routing
from apps.ibom.models import BOM

from .models import RMCMModel, ModelVersion, Scenario, ScenarioChange, ScenarioResult
from .snapshot_restore import SnapshotScopeError, apply_snapshot_to_model
from apps.organizations.scoping import get_org_context

PRE_RESTORE_LABEL = "Previous state (auto-saved)"


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _require_authenticated(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    return None


def _owned_models_qs(request):
    ctx, err = get_org_context(request)
    if err:
        # callers must handle None
        return RMCMModel.objects.none()
    return RMCMModel.objects.filter(organization_id=ctx.organization.id)


def _owned_model_or_404(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        raise RMCMModel.DoesNotExist()
    return get_object_or_404(RMCMModel.objects.filter(organization_id=ctx.organization.id), id=model_id)


def _owned_scenario_or_404(request, scenario_id):
    ctx, err = get_org_context(request)
    if err:
        raise Scenario.DoesNotExist()
    return get_object_or_404(Scenario.objects.filter(model__organization_id=ctx.organization.id), id=scenario_id)


def _owned_version_or_404(request, version_id):
    ctx, err = get_org_context(request)
    if err:
        raise ModelVersion.DoesNotExist()
    return get_object_or_404(ModelVersion.objects.filter(model__organization_id=ctx.organization.id), id=version_id)


def _serialize_general(m: RMCMModel) -> dict:
    """
    Build GeneralData payload for a model.

    Prefers the dedicated GeneralData table (apps.generaldata) and falls back to
    the legacy JSON field on RMCMModel if no relational row exists.
    """
    try:
        gd = m.general_settings  # OneToOneField related_name on GeneralData
    except GeneralData.DoesNotExist:
        # No relational row yet; do not fall back to legacy JSON on RMCMModel.
        return {}

    return {
        'model_title': gd.model_title or '',
        'author': gd.author or '',
        'comments': gd.comments or '',
        'ops_time_unit': gd.ops_time_unit,
        'mct_time_unit': gd.mct_time_unit,
        'prod_period_unit': gd.prod_period_unit,
        'conv1': gd.conv1,
        'conv2': gd.conv2,
        'util_limit': gd.util_limit,
        'var_equip': gd.var_equip,
        'var_labor': gd.var_labor,
        'var_prod': gd.var_prod,
        'gen1': gd.gen1,
        'gen2': gd.gen2,
        'gen3': gd.gen3,
        'gen4': gd.gen4,
        'output_view_mode': gd.output_view_mode or 'normal',
    }


def _update_general(m: RMCMModel, data: dict) -> None:
    """
    Create or update the GeneralData row for a model from a partial payload.

    Mirrors the shape of `model.general` used on the frontend. Any fields
    present in `data` are applied; others are left unchanged/defaulted.
    """
    # Get or create the OneToOne GeneralData row for this model.
    gd, _created = GeneralData.objects.get_or_create(model=m)

    # Simple direct fields
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
        "output_view_mode": "output_view_mode",
    }

    for payload_key, field_name in mappings.items():
        if payload_key in data:
            value = data[payload_key]
            if payload_key == "output_view_mode" and value not in ("normal", "premium"):
                continue
            setattr(gd, field_name, value)

    gd.save()


def _labor_for_model(m: RMCMModel):
    """
    Build LaborGroup[] payload for a model from the dedicated Labor table.

    Uses the owner's organization (via UserProfile) when available.
    Falls back to the legacy JSON field if no organization is found.
    """
    labors = Labor.objects.filter(
        model=m,
        deleted_at__isnull=True,
    ).order_by("created_at")

    payload = []
    for labor in labors:
        payload.append({
            'id': str(labor.id),
            'name': labor.name,
            'count': labor.count,
            'overtime_pct': labor.overtime_percent,
            'unavail_pct': labor.unavailability_percent,
            'dept_code': labor.department or '',
            'prioritize_use': labor.prioritize,
            'setup_factor': labor.setup_factor,
            'run_factor': labor.run_factor,
            'var_factor': labor.variable_factor,
            'lab1': labor.lab1 or 0.0,
            'lab2': labor.lab2 or 0.0,
            'lab3': labor.lab3 or 0.0,
            'lab4': labor.lab4 or 0.0,
            'comments': labor.notes or '',
        })
    return payload


def _equipment_for_model(m: RMCMModel):
    """
    Build EquipmentGroup[] payload for a model from the dedicated equipment table.

    Uses the owner's organization when available, otherwise falls back to the
    legacy JSON field on RMCMModel.
    """
    equipment_qs = EquipmentGroup.objects.filter(
        model=m,
        deleted_at__isnull=True,
    ).order_by("created_at")

    payload = []
    for eq in equipment_qs:
        payload.append({
            'id': str(eq.id),
            'name': eq.name,
            'equip_type': (eq.equipment_type or 'Standard').lower() == 'delay' and 'delay' or 'standard',
            'count': eq.count,
            'mttf': eq.mttf_minutes,
            'mttr': eq.mttr_minutes,
            'overtime_pct': eq.overtime_percent,
            'labor_group_id': str(eq.labor_group_id) if eq.labor_group_id else '',
            'dept_code': eq.department_area or '',
            'out_of_area': bool(eq.out_of_area_equipment),
            'unavail_pct': eq.percent_time_unavailable,
            'setup_factor': eq.setup_factor,
            'run_factor': eq.run_factor,
            'var_factor': eq.variability_factor,
            'eq1': eq.eq1,
            'eq2': eq.eq2,
            'eq3': eq.eq3,
            'eq4': eq.eq4,
            'comments': eq.comments or '',
        })
    return payload


def _products_for_model(m: RMCMModel):
    """
    Build Product[] payload for a model from the dedicated products table.
    """
    products_qs = Product.objects.filter(
        model=m,
        deleted_at__isnull=True,
    ).order_by("created_at")

    payload = []
    for p in products_qs:
        payload.append({
            'id': str(p.id),
            'name': p.name,
            'demand': p.end_demand,
            'lot_size': p.lot_size,
            'tbatch_size': p.transfer_batch,
            'demand_factor': p.demand_factor,
            'lot_factor': p.lot_factor,
            'var_factor': p.variability_factor,
            'setup_factor': 1.0,  # not present on Product model; keep default
            'make_to_stock': p.make_to_stock,
            'gather_tbatches': p.gather_transfer_batches,
            'dept_code': p.department_area or '',
            'prod1': p.prod1,
            'prod2': p.prod2,
            'prod3': p.prod3,
            'prod4': p.prod4,
            'comments': p.comments or '',
        })
    return payload


def _operations_for_model(m: RMCMModel):
    """
    Build Operation[] payload for a model from the dedicated operations table.
    """
    from apps.operations.normalize import normalize_operations_payload

    ops_qs = Operation.objects.filter(
        product__model=m,
        deleted_at__isnull=True,
    ).select_related("product", "equipment_group", "labor").order_by("product__created_at", "op_number")

    payload = []
    for op in ops_qs:
        payload.append({
            'id': str(op.id),
            'product_id': str(op.product_id),
            'op_name': op.name,
            'op_number': op.op_number,
            'equip_id': str(op.equipment_group_id) if op.equipment_group_id else '',
            'pct_assigned': op.percent_assign,
            'equip_setup_lot': op.equipment_setup_per_lot,
            'equip_setup_piece': op.equipment_setup_per_piece,
            'equip_setup_tbatch': op.equipment_setup_per_tbatch,
            'equip_run_piece': op.equipment_run_per_piece,
            'equip_run_lot': op.equipment_run_per_lot,
            'equip_run_tbatch': op.equipment_run_per_tbatch,
            'labor_setup_lot': op.labor_setup_per_lot,
            'labor_setup_piece': op.labor_setup_per_piece,
            'labor_setup_tbatch': op.labor_setup_per_tbatch,
            'labor_run_piece': op.labor_run_per_piece,
            'labor_run_lot': op.labor_run_per_lot,
            'labor_run_tbatch': op.labor_run_per_tbatch,
            'oper1': op.oper1,
            'oper2': op.oper2,
            'oper3': op.oper3,
            'oper4': op.oper4,
        })
    return normalize_operations_payload(payload)


def _routing_for_model(m: RMCMModel):
    """
    Build RoutingEntry[] payload for a model from the dedicated routing table.
    """
    routing_qs = Routing.objects.filter(
        product__model=m,
        deleted_at__isnull=True,
    ).select_related("product", "from_operation", "to_operation").order_by("created_at")

    payload = []
    for r in routing_qs:
        payload.append({
            'id': str(r.id),
            'product_id': str(r.product_id),
            'from_op_name': r.from_operation.name,
            'to_op_name': r.to_operation.name,
            'pct_routed': r.probability,
        })
    return payload


def _ibom_for_model(m: RMCMModel):
    """
    Build IBOMEntry[] payload for a model from the dedicated BOM table.
    """
    bom_qs = BOM.objects.filter(
        parent_product__model=m,
        deleted_at__isnull=True,
    ).order_by("created_at")

    payload = []
    for b in bom_qs:
        payload.append({
            'id': str(b.id),
            'parent_product_id': str(b.parent_product_id),
            'component_product_id': str(b.component_product_id),
            'units_per_assy': b.quantity_per_assembly,
        })
    return payload


def _build_snapshot_from_model(m: RMCMModel) -> dict:
    """Server-side snapshot for checkpoints (authoritative DB state)."""
    return {
        "general": _serialize_general(m),
        "labor": _labor_for_model(m),
        "equipment": _equipment_for_model(m),
        "products": _products_for_model(m),
        "operations": _operations_for_model(m),
        "routing": _routing_for_model(m),
        "ibom": _ibom_for_model(m),
        "param_names": m.param_names if isinstance(getattr(m, "param_names", None), dict) else {},
        "dept_codes": m.dept_codes if isinstance(getattr(m, "dept_codes", None), dict) else {},
    }


def _model_to_payload(m: RMCMModel) -> dict:
    """Serialize RMCMModel to frontend Model shape."""
    last_run_value = m.last_run_at
    if last_run_value is None:
        last_run_serialized = None
    elif hasattr(last_run_value, "isoformat"):
        last_run_serialized = last_run_value.isoformat()
    else:
        # Gracefully handle unexpected string/other types persisted in last_run_at
        last_run_serialized = str(last_run_value)

    return {
        'id': str(m.id),
        'name': m.name,
        'description': m.description or '',
        'tags': m.tags or [],
        'created_at': m.created_at.isoformat() if m.created_at else '',
        'updated_at': m.updated_at.isoformat() if m.updated_at else '',
        'last_run_at': last_run_serialized,
        'run_status': m.run_status or 'never_run',
        'is_archived': bool(m.is_archived),
        'is_demo': bool(m.is_demo),
        'is_starred': bool(m.is_starred),
        'general': _serialize_general(m),
        'param_names': m.param_names if isinstance(getattr(m, "param_names", None), dict) else {},
        'labor': _labor_for_model(m),
        'equipment': _equipment_for_model(m),
        'products': _products_for_model(m),
        'operations': _operations_for_model(m),
        'routing': _routing_for_model(m),
        'ibom': _ibom_for_model(m),
    }


# ─── Models ─────────────────────────────────────────────────────────────

@csrf_exempt
def model_list_or_create(request):
    """GET /api/models — list all. POST /api/models — create (body has full model with id)."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    if request.method == 'GET':
        qs = _owned_models_qs(request)
        payload = [_model_to_payload(m) for m in qs]
        return JsonResponse(payload, safe=False)
    if request.method == 'POST':
        return model_save(request, model_id=None)
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@require_http_methods(['GET'])
def model_list(request):
    """GET /api/models — list all models."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    qs = _owned_models_qs(request)
    payload = [_model_to_payload(m) for m in qs]
    return JsonResponse(payload, safe=False)


@require_http_methods(['GET'])
def model_detail(request, model_id):
    """GET /api/models/:id — get one model."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_models_qs(request).filter(id=model_id).first()
    if not m:
        return JsonResponse(None, safe=False)
    return JsonResponse(_model_to_payload(m))


@csrf_exempt
@require_http_methods(['POST', 'PUT'])
def model_save(request, model_id=None):
    """
    POST /api/models — create (body includes id) or PUT /api/models/:id — update full model.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    model_id = model_id or data.get('id')
    if not model_id:
        return JsonResponse({'error': 'id required'}, status=400)
    try:
        uid = uuid.UUID(str(model_id))
    except ValueError:
        return JsonResponse({'error': 'Invalid id'}, status=400)
    from django.utils import timezone
    updated_at = timezone.now()
    defaults = {
        'name': data.get('name', ''),
        'description': data.get('description', ''),
        'tags': data.get('tags', []),
        'last_run_at': data.get('last_run_at'),
        'run_status': data.get('run_status', 'never_run'),
        'is_archived': data.get('is_archived', False),
        'is_demo': data.get('is_demo', False),
        'is_starred': data.get('is_starred', False),
    }
    if isinstance(data.get('param_names'), dict):
        defaults['param_names'] = data['param_names']
    if isinstance(data.get('dept_codes'), dict):
        defaults['dept_codes'] = data['dept_codes']
    ctx, err = get_org_context(request)
    if err:
        return err

    existing = RMCMModel.objects.filter(id=uid).first()
    if existing and existing.organization_id != ctx.organization.id:
        return JsonResponse({'error': 'Model not found'}, status=404)
    defaults['owner'] = request.user
    defaults['organization'] = ctx.organization
    if existing:
        for key, value in defaults.items():
            setattr(existing, key, value)
        existing.save()
        obj, created = existing, False
    else:
        obj = RMCMModel.objects.create(id=uid, **defaults)
        created = True
    return JsonResponse(_model_to_payload(obj), status=201 if created else 200)


@csrf_exempt
@require_http_methods(['PATCH'])
def model_patch(request, model_id):
    """PATCH /api/models/:id — partial update (metadata or nested)."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    for key in ('name', 'description', 'tags', 'run_status', 'last_run_at',
                'is_archived', 'is_demo', 'is_starred'):
        if key in data:
            setattr(m, key, data[key])
    if isinstance(data.get('param_names'), dict):
        m.param_names = data['param_names']
    if isinstance(data.get('dept_codes'), dict):
        m.dept_codes = data['dept_codes']
    m.save()
    return JsonResponse(_model_to_payload(m))


@csrf_exempt
@require_http_methods(['DELETE'])
def model_delete(request, model_id):
    """DELETE /api/models/:id."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_models_qs(request).filter(id=model_id).first()
    if m:
        m.delete()
    return JsonResponse({"success": True}, status=200)


# ─── Param names ────────────────────────────────────────────────────────

@require_http_methods(['GET'])
def model_param_names(request, model_id):
    """GET /api/models/:id/param-names."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    return JsonResponse(m.param_names if isinstance(m.param_names, dict) else {})


@csrf_exempt
@require_http_methods(['PUT'])
def model_param_names_upsert(request, model_id):
    """PUT /api/models/:id/param-names — merge param names."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    cur = m.param_names if isinstance(m.param_names, dict) else {}
    cur = {**cur, **{k: v for k, v in data.items() if k != "model_id"}}
    m.param_names = cur
    m.save(update_fields=["param_names", "updated_at"])
    return JsonResponse(cur)


@require_http_methods(['GET'])
def model_dept_codes(request, model_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    dc = m.dept_codes if isinstance(m.dept_codes, dict) else {}
    return JsonResponse(dc)


@csrf_exempt
@require_http_methods(['PUT'])
def model_dept_codes_put(request, model_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    m.dept_codes = data if isinstance(data, dict) else {}
    m.save(update_fields=["dept_codes", "updated_at"])
    return JsonResponse(m.dept_codes)


# ─── General ─────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['PATCH'])
def model_general(request, model_id):
    """PATCH /api/models/:id/general."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    _update_general(m, data)
    return JsonResponse({})


# ─── Labor ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def model_labor_create(request, model_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    owner = m.owner
    if owner is None or not hasattr(owner, "profile") or not getattr(owner.profile, "organization_id", None):
        return JsonResponse({'error': 'Owner organization not configured for model'}, status=400)

    org = owner.profile.organization

    # Allow frontend to provide a stable UUID id so that modelStore ids
    # match rows in the relational Labor table.
    labor_id = data.get('id')

    labor_kwargs = {
        'organization': org,
        'name': data.get('name', '').upper(),
        'count': data.get('count', 1),
        'overtime_percent': data.get('overtime_pct', 0),
        'unavailability_percent': data.get('unavail_pct', 0),
        'department': data.get('dept_code') or None,
        'setup_factor': data.get('setup_factor', 1),
        'run_factor': data.get('run_factor', 1),
        'variable_factor': data.get('var_factor', 1),
        'prioritize': data.get('prioritize_use', False),
        'lab1': data.get('lab1', 0),
        'lab2': data.get('lab2', 0),
        'lab3': data.get('lab3', 0),
        'lab4': data.get('lab4', 0),
        'notes': data.get('comments', ''),
    }

    if labor_id:
        labor_kwargs['id'] = labor_id

    labor = Labor.objects.create(**labor_kwargs)

    return JsonResponse(
        {
            'id': str(labor.id),
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(['PATCH'])
def model_labor_update(request, model_id, labor_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    labor = get_object_or_404(Labor.objects.filter(model=m), id=labor_id)

    # Map incoming payload fields onto Labor model.
    if 'name' in data:
        labor.name = data['name']
    if 'count' in data:
        labor.count = data['count']
    if 'overtime_pct' in data:
        labor.overtime_percent = data['overtime_pct']
    if 'unavail_pct' in data:
        labor.unavailability_percent = data['unavail_pct']
    if 'dept_code' in data:
        labor.department = data['dept_code'] or None
    if 'setup_factor' in data:
        labor.setup_factor = data['setup_factor']
    if 'run_factor' in data:
        labor.run_factor = data['run_factor']
    if 'var_factor' in data:
        labor.variable_factor = data['var_factor']
    if 'prioritize_use' in data:
        labor.prioritize = data['prioritize_use']
    for key, field in (('lab1', 'lab1'), ('lab2', 'lab2'), ('lab3', 'lab3'), ('lab4', 'lab4')):
        if key in data:
            setattr(labor, field, data[key])
    if 'comments' in data:
        labor.notes = data['comments']

    labor.save()

    return JsonResponse({})


@csrf_exempt
@require_http_methods(['DELETE'])
def model_labor_delete(request, model_id, labor_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    # Soft-delete the Labor row if it exists; keep behaviour consistent with
    # apps.labor.views.delete_labor.
    try:
        labor = Labor.objects.get(id=labor_id, model=m)
    except Labor.DoesNotExist:
        return JsonResponse({}, status=204)

    from django.utils import timezone

    labor.deleted_at = timezone.now()
    labor.save()

    # For now we do not attempt to automatically clear equipment assignments here,
    # because those live in the dedicated equipment tables rather than RMCMModel.equipment.
    return JsonResponse({}, status=204)


#
# Equipment, products, operations, routing, and IBOM CRUD views are implemented
# in their dedicated app view modules (apps.equipment.views, apps.products.views,
# apps.operations.views, apps.routing.views, apps.ibom.views) and are wired to
# the same URLs via apps.rmct.urls.


# ─── Versions ────────────────────────────────────────────────────────────

@require_http_methods(['GET'])
def version_list(request, model_id):
    """GET /api/models/:modelId/versions."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    qs = (
        ModelVersion.objects.filter(model=m)
        .annotate(
            _prio=Case(
                When(version_kind=ModelVersion.KIND_PRE_RESTORE, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("_prio", "-created_at")
    )
    payload = [
        {
            "id": str(v.id),
            "label": v.label,
            "created_at": v.created_at.isoformat(),
            "version_kind": v.version_kind,
        }
        for v in qs
    ]
    return JsonResponse(payload, safe=False)


@require_http_methods(['POST'])
def version_create(request, model_id):
    """POST /api/models/:modelId/versions — body: { label, snapshot }."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    label = data.get('label', '')
    snapshot = data.get('snapshot', {})
    if not isinstance(snapshot, dict) or not snapshot:
        snapshot = _build_snapshot_from_model(m)
    vid = uuid.uuid4()
    ModelVersion.objects.create(
        id=vid,
        model=m,
        label=label,
        snapshot=snapshot,
        version_kind=ModelVersion.KIND_MANUAL,
    )
    return JsonResponse({'id': str(vid)}, status=201)


@require_http_methods(['GET'])
def version_snapshot(request, version_id):
    """GET /api/versions/:versionId — snapshot + created_at."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    v = _owned_version_or_404(request, version_id)
    return JsonResponse({'snapshot': v.snapshot, 'created_at': v.created_at.isoformat()})


@require_http_methods(['PATCH'])
def version_patch(request, version_id):
    """PATCH /api/versions/:versionId — update label."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    v = _owned_version_or_404(request, version_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    if 'label' in data:
        v.label = data['label']
        v.save()
    return JsonResponse({})


@require_http_methods(['DELETE'])
def version_delete(request, version_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    ctx, err = get_org_context(request)
    if err:
        return err
    v = ModelVersion.objects.filter(id=version_id, model__organization_id=ctx.organization.id).first()
    if v:
        v.delete()
    return JsonResponse({}, status=204)


@require_http_methods(['POST'])
@transaction.atomic
def version_restore(request, model_id, version_id):
    """POST /api/models/:modelId/versions/:versionId/restore — apply snapshot to model, return Model."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    v = ModelVersion.objects.filter(id=version_id, model=m).first()
    if not v:
        return JsonResponse({'error': 'Version not found'}, status=404)
    snap = v.snapshot or {}
    consumed_undo = v.version_kind == ModelVersion.KIND_PRE_RESTORE

    rollback_id = None
    if not consumed_undo:
        # One rollback slot per model: current DB state before applying a manual checkpoint.
        ModelVersion.objects.filter(model=m, version_kind=ModelVersion.KIND_PRE_RESTORE).delete()
        rollback_id = uuid.uuid4()
        rollback_snapshot = _build_snapshot_from_model(m)
        ModelVersion.objects.create(
            id=rollback_id,
            model=m,
            label=PRE_RESTORE_LABEL,
            snapshot=rollback_snapshot,
            version_kind=ModelVersion.KIND_PRE_RESTORE,
        )

    try:
        apply_snapshot_to_model(m, snap)
    except SnapshotScopeError as e:
        return JsonResponse({'error': str(e)}, status=403)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    if consumed_undo:
        # Undo checkpoint is single-use: remove it so it cannot be restored again until
        # the user restores another manual checkpoint (which recreates the slot).
        ModelVersion.objects.filter(id=version_id, model=m).delete()

    payload = _model_to_payload(m)
    payload["restore_meta"] = {
        "restored_from_label": v.label,
    }
    if rollback_id is not None:
        payload["restore_meta"]["rollback_version_id"] = str(rollback_id)
        payload["restore_meta"]["rollback_label"] = PRE_RESTORE_LABEL
    if consumed_undo:
        payload["restore_meta"]["consumed_undo"] = True
    return JsonResponse(payload)


# ─── Scenarios ───────────────────────────────────────────────────────────

def _scenario_to_payload(s: Scenario, changes_list: list) -> dict:
    return {
        'id': str(s.id),
        'modelId': str(s.model_id),
        'name': s.name,
        'description': s.description or '',
        'familyId': str(s.family_id) if s.family_id else None,
        'status': s.status or 'needs_recalc',
        'changes': changes_list,
        'createdAt': s.created_at.isoformat(),
        'updatedAt': s.updated_at.isoformat(),
    }


def scenario_list_or_create(request, model_id):
    """GET /api/models/:modelId/scenarios — list. POST — create (body: name, description)."""
    if request.method == 'POST':
        return scenario_create(request, model_id)
    return scenario_list(request, model_id)


@require_http_methods(['GET'])
def scenario_list(request, model_id):
    """GET /api/models/:modelId/scenarios — scenarios + changes + results."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    scenarios_qs = Scenario.objects.filter(model=m, is_basecase=False).order_by('-updated_at')
    scenario_ids = [s.id for s in scenarios_qs]
    changes_qs = ScenarioChange.objects.filter(scenario_id__in=scenario_ids)
    changes_by_scenario = {}
    for c in changes_qs:
        key = str(c.scenario_id)
        if key not in changes_by_scenario:
            changes_by_scenario[key] = []
        changes_by_scenario[key].append({
            'id': str(c.id),
            'dataType': c.data_type,
            'entityId': c.entity_id or '',
            'entityName': c.entity_name or '',
            'field': c.field_name,
            'fieldLabel': c.field_name,
            'basecaseValue': c.basecase_value,
            'whatIfValue': c.whatif_value,
        })
    results_map = {}
    for s in scenarios_qs:
        try:
            r = s.result
            results_map[str(s.id)] = r.results
        except ScenarioResult.DoesNotExist:
            pass
    scenarios_payload = []
    for s in scenarios_qs:
        scenarios_payload.append(_scenario_to_payload(s, changes_by_scenario.get(str(s.id), [])))
    return JsonResponse({'scenarios': scenarios_payload, 'results': results_map})


@csrf_exempt
def scenario_basecase_results(request, model_id):
    """GET /api/models/:modelId/scenarios/basecase/results — return results. PUT — save results."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    if request.method == 'GET':
        base = Scenario.objects.filter(model=m, is_basecase=True).first()
        if not base:
            return JsonResponse(None, safe=False)
        try:
            r = base.result
            return JsonResponse(r.results)
        except ScenarioResult.DoesNotExist:
            return JsonResponse(None, safe=False)
    if request.method == 'PUT':
        return scenario_basecase_save_results(request, model_id)
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@require_http_methods(['POST'])
def scenario_ensure_basecase(request, model_id):
    """POST /api/models/:modelId/scenarios/basecase — idempotent, return { id }."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    base = Scenario.objects.filter(model=m, is_basecase=True).first()
    if base:
        return JsonResponse({'id': str(base.id)})
    sid = uuid.uuid4()
    Scenario.objects.create(id=sid, model=m, name='Basecase', description='', is_basecase=True, status='needs_recalc')
    return JsonResponse({'id': str(sid)}, status=201)


@require_http_methods(['POST'])
def scenario_create(request, model_id):
    """POST /api/models/:modelId/scenarios — body: { name, description }."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    name = data.get('name', '')
    description = data.get('description', '')
    sid = uuid.uuid4()
    Scenario.objects.create(id=sid, model=m, name=name, description=description, is_basecase=False, status='needs_recalc')
    return JsonResponse({'id': str(sid)}, status=201)


@require_http_methods(['PATCH'])
def scenario_patch(request, scenario_id):
    """PATCH /api/scenarios/:id — name, description, status."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    s = _owned_scenario_or_404(request, scenario_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    for key in ('name', 'description', 'status'):
        if key in data:
            setattr(s, key, data[key])
    s.save()
    return JsonResponse({})


@require_http_methods(['DELETE'])
def scenario_delete(request, scenario_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    ctx, err = get_org_context(request)
    if err:
        return err
    s = Scenario.objects.filter(id=scenario_id, model__organization_id=ctx.organization.id).first()
    if s:
        s.delete()
    return JsonResponse({}, status=204)


@require_http_methods(['PUT'])
def scenario_upsert_change(request, scenario_id):
    """PUT /api/scenarios/:id/changes — body: ScenarioChange."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    s = _owned_scenario_or_404(request, scenario_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    change_id = data.get('id')
    if not change_id:
        change_id = uuid.uuid4()
    else:
        try:
            change_id = uuid.UUID(str(change_id))
        except (ValueError, TypeError):
            change_id = uuid.uuid4()
    entity_id = data.get('entityId') or data.get('entity_id', '')
    entity_name = data.get('entityName') or data.get('entity_name', '')
    field = data.get('field') or data.get('field_name', '')
    basecase_value = str(data.get('basecaseValue', data.get('basecase_value', '')))
    whatif_value = str(data.get('whatIfValue', data.get('whatif_value', '')))
    data_type = data.get('dataType') or data.get('data_type', '')
    defaults = {
        'scenario': s,
        'data_type': data_type,
        'entity_id': entity_id,
        'entity_name': entity_name,
        'field_name': field,
        'basecase_value': basecase_value,
        'whatif_value': whatif_value,
    }
    ScenarioChange.objects.update_or_create(id=change_id, defaults=defaults)
    return JsonResponse({})


@require_http_methods(['DELETE'])
def scenario_remove_change(request, scenario_id, change_id):
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    ctx, err = get_org_context(request)
    if err:
        return err
    ScenarioChange.objects.filter(
        scenario_id=scenario_id,
        id=change_id,
        scenario__model__organization_id=ctx.organization.id,
    ).delete()
    return JsonResponse({}, status=204)


@csrf_exempt
@require_http_methods(['PUT'])
def scenario_save_results(request, scenario_id):
    """PUT /api/scenarios/:id/results — body: CalcResults."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    s = _owned_scenario_or_404(request, scenario_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    ScenarioResult.objects.update_or_create(scenario=s, defaults={'results': data})
    return JsonResponse({})


@csrf_exempt
@require_http_methods(['PUT'])
def scenario_basecase_save_results(request, model_id):
    """PUT /api/models/:modelId/scenarios/basecase/results — body: CalcResults."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    m = _owned_model_or_404(request, model_id)
    base = Scenario.objects.filter(model=m, is_basecase=True).first()
    if not base:
        base = Scenario.objects.create(id=uuid.uuid4(), model=m, name='Basecase', description='', is_basecase=True, status='calculated')
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    ScenarioResult.objects.update_or_create(scenario=base, defaults={'results': data})
    return JsonResponse({})


# ─── Seed demo ───────────────────────────────────────────────────────────

@require_http_methods(['POST'])
def seed_demo(request):
    """POST /api/models/seed-demo — create demo model from frontend createDemoModel() payload if needed."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err
    data = _parse_json(request)
    if data is None:
        data = {}
    # If client sends full demo model, use it; else we could generate server-side
    model_id = data.get('id') or uuid.uuid4()
    name = data.get('name', 'Hub Manufacturing Cell — Demo')
    try:
        model_uuid = uuid.UUID(str(model_id))
    except (ValueError, TypeError):
        model_uuid = uuid.uuid4()

    existing = RMCMModel.objects.filter(id=model_uuid).first()
    if existing and existing.owner_id != request.user.id:
        model_uuid = uuid.uuid4()

    RMCMModel.objects.get_or_create(
        id=model_uuid,
        defaults={
            'name': name,
            'description': data.get('description', ''),
            'tags': data.get('tags', ['Demo', 'Tutorial']),
            'run_status': 'never_run',
            'is_demo': True,
            'param_names': data.get('param_names', {}),
            'owner': request.user,
        },
    )
    return JsonResponse({'id': str(model_uuid)}, status=201)
