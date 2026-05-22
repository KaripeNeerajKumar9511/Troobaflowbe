"""
TF Admin API: platform admin login and user oversight (inputs, outputs, errors).
Credentials: settings TF_ADMIN_EMAIL / TF_ADMIN_PASSWORD (defaults admin@gmail.com / 12345678).
"""
import json
import uuid
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel, Scenario, ScenarioResult
from apps.rmct.views import _model_to_payload
from apps.simulations.latest_views import verify_model_data
from apps.users.models import UserProfile

from .auth import (
    admin_credentials_valid,
    clear_admin_session,
    is_admin_session,
    set_admin_session,
)


def _require_admin(request):
    if not is_admin_session(request):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    return None


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _user_summary(profile: UserProfile) -> dict:
    user = profile.user
    model_count = RMCMModel.objects.filter(owner_id=user.id).count()
    return {
        'id': user.id,
        'email': user.email,
        'name': profile.full_name or user.get_full_name() or user.email,
        'organization_id': str(profile.organization_id),
        'organization_name': profile.organization.name if profile.organization_id else '',
        'role': profile.role,
        'user_level': profile.user_level,
        'is_active': profile.is_active,
        'created_at': profile.created_at.isoformat() if profile.created_at else '',
        'model_count': model_count,
    }


def _model_summary(m: RMCMModel) -> dict:
    last_run = m.last_run_at
    if last_run is not None and hasattr(last_run, 'isoformat'):
        last_run_serialized = last_run.isoformat()
    else:
        last_run_serialized = str(last_run) if last_run else None
    return {
        'id': str(m.id),
        'name': m.name,
        'description': m.description or '',
        'run_status': m.run_status or 'never_run',
        'updated_at': m.updated_at.isoformat() if m.updated_at else '',
        'last_run_at': last_run_serialized,
        'is_archived': bool(m.is_archived),
        'is_demo': bool(m.is_demo),
        'is_starred': bool(m.is_starred),
    }


def _scenario_outputs(model: RMCMModel) -> list:
    outputs = []
    for scenario in model.scenarios.all():
        try:
            result = scenario.result
        except ScenarioResult.DoesNotExist:
            continue
        results_data = result.results or {}
        outputs.append({
            'scenario_id': str(scenario.id),
            'scenario_name': scenario.name,
            'is_basecase': scenario.is_basecase,
            'calculated_at': results_data.get('calculatedAt'),
            'results': results_data,
        })
    return outputs


def _issue_rows_from_results(results_data: dict, model_id: str, model_name: str, scenario_id: str, scenario_name: str) -> list:
    """Flatten DLL/calc messages for admin error panel (raw + kind)."""
    rows = []
    errors_raw = results_data.get('errorsRaw') or []
    errors = results_data.get('errors') or []
    warnings = results_data.get('warnings') or []
    over_limit = results_data.get('overLimitResources') or results_data.get('over_limit_resources') or []

    def append(kind: str, dll_message: str, raw_line: str | None = None):
        if not dll_message or not str(dll_message).strip():
            return
        rows.append({
            'kind': kind,
            'dll_message': str(dll_message).strip(),
            'raw_line': raw_line,
            'model_id': model_id,
            'model_name': model_name,
            'scenario_id': scenario_id,
            'scenario_name': scenario_name,
        })

    if isinstance(errors_raw, list) and errors_raw:
        for i, raw in enumerate(errors_raw):
            raw_s = str(raw).strip()
            if not raw_s:
                continue
            parsed = errors[i] if i < len(errors) else raw_s
            kind = 'warning' if 'warning' in raw_s.lower()[:40] else 'error'
            append(kind, str(parsed), raw_s)
    else:
        if isinstance(errors, list):
            for err in errors:
                append('error', str(err))
        if isinstance(warnings, list):
            for warn in warnings:
                append('warning', str(warn))
        if isinstance(over_limit, list):
            for msg in over_limit:
                append('warning', str(msg))

    return rows


@csrf_exempt
@require_http_methods(['POST'])
def admin_login(request):
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    email = data.get('email') or ''
    password = data.get('password') or ''

    if not admin_credentials_valid(email, password):
        return JsonResponse({'error': 'Invalid admin credentials'}, status=400)

    logout(request)
    set_admin_session(request, email)
    return JsonResponse({
        'message': 'Admin login successful',
        'email': email.strip().lower(),
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_logout(request):
    clear_admin_session(request)
    return JsonResponse({'message': 'Logged out'})


@require_http_methods(['GET'])
def admin_me(request):
    if not is_admin_session(request):
        return JsonResponse({'authenticated': False})
    return JsonResponse({
        'authenticated': True,
        'email': request.session.get('tf_admin_email', 'admin@gmail.com'),
        'role': 'admin',
    })


@require_http_methods(['GET'])
def admin_stats(request):
    err = _require_admin(request)
    if err:
        return err
    total_users = UserProfile.objects.filter(deleted_at__isnull=True).count()
    total_models = RMCMModel.objects.count()
    return JsonResponse({
        'total_users': total_users,
        'total_models': total_models,
    })


@require_http_methods(['GET'])
def admin_users(request):
    err = _require_admin(request)
    if err:
        return err
    profiles = (
        UserProfile.objects.filter(deleted_at__isnull=True)
        .select_related('user', 'organization')
        .order_by('-created_at')
    )
    return JsonResponse([_user_summary(p) for p in profiles], safe=False)


@require_http_methods(['GET'])
def admin_user_detail(request, user_id: int):
    """User profile + model cards (no full payloads)."""
    err = _require_admin(request)
    if err:
        return err

    try:
        profile = UserProfile.objects.select_related('user', 'organization').get(
            user_id=user_id,
            deleted_at__isnull=True,
        )
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    models = RMCMModel.objects.filter(owner_id=profile.user_id).order_by('-updated_at')
    return JsonResponse({
        'user': _user_summary(profile),
        'models': [_model_summary(m) for m in models],
    })


@require_http_methods(['GET'])
def admin_model_detail(request, user_id: int, model_id: uuid.UUID):
    """Single model: inputs, outputs per scenario, issue rows, verify validations."""
    err = _require_admin(request)
    if err:
        return err

    try:
        profile = UserProfile.objects.get(user_id=user_id, deleted_at__isnull=True)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    model = get_object_or_404(
        RMCMModel.objects.prefetch_related('scenarios'),
        id=model_id,
        owner_id=profile.user_id,
    )
    payload = _model_to_payload(model)
    outputs = _scenario_outputs(model)

    issue_rows = []
    for scenario in model.scenarios.all():
        try:
            result = scenario.result
        except ScenarioResult.DoesNotExist:
            continue
        results_data = result.results or {}
        issue_rows.extend(
            _issue_rows_from_results(
                results_data,
                str(model.id),
                model.name,
                str(scenario.id),
                scenario.name,
            )
        )

    validation = verify_model_data(payload)

    return JsonResponse({
        'user': _user_summary(profile),
        'model': _model_summary(model),
        'input': payload,
        'outputs': outputs,
        'issue_rows': issue_rows,
        'validation': validation,
    })
