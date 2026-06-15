"""
TF Admin API: platform admin login and user oversight (inputs, outputs, errors).
Credentials: settings TF_ADMIN_EMAIL / TF_ADMIN_PASSWORD (defaults admin@gmail.com / 12345678).
"""
import json
import uuid
from django.contrib.auth.models import User
from django.utils.text import slugify
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel, Scenario, ScenarioResult
from apps.rmct.views import _model_to_payload
from apps.simulations.latest_views import verify_model_data
from apps.users.models import UserProfile
from apps.users.access import ORG_STATUS_ACTIVE, ORG_STATUS_FROZEN
from apps.organizations.models import Organization, OrganizationMember
from apps.organizations.emails import send_org_owner_welcome_email

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


def _admin_email(request) -> str:
    return (request.session.get('tf_admin_email') or '').strip().lower()


def _user_summary(profile: UserProfile) -> dict:
    user = profile.user
    model_count = RMCMModel.objects.filter(owner_id=user.id).count()
    is_org_owner = Organization.objects.filter(
        owner_id=user.id,
        deleted_at__isnull=True,
    ).exists()
    return {
        'id': user.id,
        'email': user.email,
        'name': profile.full_name or user.get_full_name() or user.email,
        'organization_id': str(profile.organization_id) if profile.organization_id else '',
        'organization_name': profile.organization.name if profile.organization_id else '',
        'role': profile.role,
        'user_level': profile.user_level,
        'is_active': profile.is_active,
        'is_org_owner': is_org_owner,
        'must_change_password': bool(profile.must_change_password),
        'has_stored_password': bool(profile.admin_stored_password),
        'created_at': profile.created_at.isoformat() if profile.created_at else '',
        'model_count': model_count,
    }


def _org_summary(org: Organization) -> dict:
    member_count = OrganizationMember.objects.filter(organization=org).count()
    owner_email = ''
    if org.owner_id:
        owner = User.objects.filter(id=org.owner_id).first()
        owner_email = owner.email if owner else ''
    return {
        'id': str(org.id),
        'name': org.name,
        'organization_code': org.organization_code,
        'slug': org.slug,
        'status': org.status,
        'is_frozen': org.status == ORG_STATUS_FROZEN,
        'owner_id': org.owner_id,
        'owner_email': owner_email,
        'created_by_admin_email': org.created_by_admin_email or '',
        'member_count': member_count,
        'created_at': org.created_at.isoformat() if org.created_at else '',
        'updated_at': org.updated_at.isoformat() if org.updated_at else '',
    }


def _password_summary(profile: UserProfile) -> dict:
    user = profile.user
    stored = profile.admin_stored_password or None
    return {
        'user_id': user.id,
        'email': user.email,
        'name': profile.full_name or user.get_full_name() or user.email,
        'role': profile.role,
        'password': stored,
        'has_stored_password': bool(stored),
        'must_change_password': bool(profile.must_change_password),
        'password_changed': not stored,
    }


def _get_org_or_404(org_id: str) -> Organization:
    return get_object_or_404(
        Organization.objects.filter(deleted_at__isnull=True),
        id=org_id,
    )


def _org_member_user_ids(org: Organization) -> list[int]:
    return list(
        OrganizationMember.objects.filter(organization=org).values_list('user_id', flat=True)
    )


def _set_org_members_active(org: Organization, *, active: bool) -> None:
    user_ids = _org_member_user_ids(org)
    if not user_ids:
        return
    User.objects.filter(id__in=user_ids).update(is_active=active)
    UserProfile.objects.filter(user_id__in=user_ids).update(is_active=active)


def _hard_delete_users(user_ids: list[int]) -> None:
    if user_ids:
        User.objects.filter(id__in=user_ids).delete()


def _hard_delete_organization(org: Organization) -> None:
    member_user_ids = _org_member_user_ids(org)
    org.owner_id = None
    org.save(update_fields=['owner', 'updated_at'])
    org.delete()
    _hard_delete_users(member_user_ids)


def _hard_delete_org_member(org: Organization, user_id: int) -> None:
    if org.owner_id == user_id:
        raise ValueError('Cannot delete organization owner. Delete the organization instead.')
    if not OrganizationMember.objects.filter(organization=org, user_id=user_id).exists():
        raise LookupError('Member not found')
    OrganizationMember.objects.filter(organization=org, user_id=user_id).delete()
    if not OrganizationMember.objects.filter(user_id=user_id).exists():
        User.objects.filter(id=user_id).delete()


def _hard_delete_platform_user(user_id: int) -> None:
    if Organization.objects.filter(owner_id=user_id, deleted_at__isnull=True).exists():
        raise ValueError(
            'Cannot delete organization owner. Delete the organization or remove ownership first.',
        )
    if not UserProfile.objects.filter(user_id=user_id, deleted_at__isnull=True).exists():
        raise LookupError('User not found')
    OrganizationMember.objects.filter(user_id=user_id).delete()
    _hard_delete_users([user_id])


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


@csrf_exempt
@require_http_methods(['DELETE'])
def admin_delete_user(request, user_id: int):
    """Permanently delete a user account and related data."""
    err = _require_admin(request)
    if err:
        return err
    try:
        _hard_delete_platform_user(user_id)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except LookupError:
        return JsonResponse({'error': 'User not found'}, status=404)
    return JsonResponse({'message': 'User deleted permanently'})


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


@require_http_methods(['GET'])
def admin_organizations(request):
    err = _require_admin(request)
    if err:
        return err
    orgs = (
        Organization.objects.filter(deleted_at__isnull=True)
        .select_related('owner')
        .order_by('-created_at')
    )
    return JsonResponse([_org_summary(o) for o in orgs], safe=False)


@require_http_methods(['GET'])
def admin_organization_detail(request, org_id: uuid.UUID):
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    return JsonResponse(_org_summary(org))


@require_http_methods(['GET'])
def admin_organization_members(request, org_id: uuid.UUID):
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    member_user_ids = OrganizationMember.objects.filter(
        organization=org,
    ).values_list('user_id', flat=True)
    profiles = (
        UserProfile.objects.filter(user_id__in=member_user_ids, deleted_at__isnull=True)
        .select_related('user', 'organization')
        .order_by('-created_at')
    )
    return JsonResponse({
        'organization': _org_summary(org),
        'members': [_user_summary(p) for p in profiles],
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_create_organization_member(request, org_id: uuid.UUID):
    """Create a portal user and add them as a member of the given organization."""
    err = _require_admin(request)
    if err:
        return err

    org = _get_org_or_404(str(org_id))

    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    member_name = (data.get('member_name') or data.get('name') or '').strip()
    member_email = (data.get('member_email') or data.get('email') or '').strip().lower()
    temp_password = (data.get('temporary_password') or data.get('temp_password') or '').strip()

    if not member_name:
        return JsonResponse({'error': 'member_name is required'}, status=400)
    if not member_email:
        return JsonResponse({'error': 'member_email is required'}, status=400)
    if not temp_password or len(temp_password) < 8:
        return JsonResponse({'error': 'temporary_password must be at least 8 characters'}, status=400)

    if User.objects.filter(email__iexact=member_email).exists():
        return JsonResponse({'error': 'A user with this email already exists'}, status=400)

    user = User.objects.create_user(
        username=member_email,
        email=member_email,
        password=temp_password,
        first_name=member_name,
    )

    profile = UserProfile.objects.create(
        user=user,
        full_name=member_name,
        organization=org,
        role='member',
        must_change_password=True,
        admin_stored_password=temp_password,
    )

    OrganizationMember.objects.get_or_create(organization=org, user=user)

    login_url = (data.get('login_url') or '').strip() or '/login'
    send_org_owner_welcome_email(
        to_email=member_email,
        owner_name=member_name,
        organization_name=org.name,
        temporary_password=temp_password,
        login_url=login_url,
    )

    return JsonResponse({
        'message': 'Member created',
        'organization_id': str(org.id),
        'user_id': user.id,
        'member_email': member_email,
    }, status=201)


@require_http_methods(['GET'])
def admin_passwords_organizations(request):
    """All organizations for the admin passwords browser."""
    err = _require_admin(request)
    if err:
        return err
    orgs = (
        Organization.objects.filter(deleted_at__isnull=True)
        .select_related('owner')
        .order_by('-created_at')
    )
    out = []
    for org in orgs:
        summary = _org_summary(org)
        member_user_ids = OrganizationMember.objects.filter(
            organization=org,
        ).values_list('user_id', flat=True)
        stored_count = UserProfile.objects.filter(
            user_id__in=member_user_ids,
            deleted_at__isnull=True,
        ).exclude(admin_stored_password='').count()
        summary['stored_password_count'] = stored_count
        out.append(summary)
    return JsonResponse(out, safe=False)


@require_http_methods(['GET'])
def admin_organization_passwords(request, org_id: uuid.UUID):
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    member_user_ids = OrganizationMember.objects.filter(
        organization=org,
    ).values_list('user_id', flat=True)
    profiles = (
        UserProfile.objects.filter(user_id__in=member_user_ids, deleted_at__isnull=True)
        .select_related('user')
        .order_by('full_name', 'user__email')
    )
    return JsonResponse({
        'organization': _org_summary(org),
        'passwords': [_password_summary(p) for p in profiles],
    })


@require_http_methods(['GET'])
def admin_user_credential(request, user_id: int):
    """Return admin-stored password for a user (revealed on eye click)."""
    err = _require_admin(request)
    if err:
        return err
    try:
        profile = UserProfile.objects.select_related('user').get(
            user_id=user_id,
            deleted_at__isnull=True,
        )
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    stored = profile.admin_stored_password or None
    return JsonResponse({
        'user_id': user_id,
        'email': profile.user.email,
        'password': stored,
        'has_stored_password': bool(stored),
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_change_user_password(request, user_id: int):
    """Admin sets a new password for a portal user."""
    err = _require_admin(request)
    if err:
        return err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    new_password = (data.get('new_password') or data.get('password') or '').strip()
    if len(new_password) < 8:
        return JsonResponse({'error': 'new_password must be at least 8 characters'}, status=400)

    try:
        profile = UserProfile.objects.select_related('user').get(
            user_id=user_id,
            deleted_at__isnull=True,
        )
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    user = profile.user
    user.set_password(new_password)
    user.save(update_fields=['password'])

    must_change = data.get('must_change_password')
    if must_change is None:
        must_change = True
    else:
        must_change = bool(must_change)

    profile.admin_stored_password = new_password
    profile.must_change_password = must_change
    profile.save(update_fields=['admin_stored_password', 'must_change_password', 'updated_at'])

    return JsonResponse({
        'message': 'Password updated',
        'user_id': user_id,
        'email': user.email,
        'must_change_password': must_change,
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_create_organization(request):
    """
    Portal admin flow:
    - create organization
    - create owner user (if not exists)
    - assign org_owner role
    - add owner as OrganizationMember
    - mark must_change_password=True (force change after temp password login)
    """
    err = _require_admin(request)
    if err:
        return err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    org_name = (data.get('organization_name') or data.get('name') or '').strip()
    owner_name = (data.get('owner_name') or '').strip()
    owner_email = (data.get('owner_email') or '').strip().lower()
    temp_password = (data.get('temporary_password') or data.get('temp_password') or '').strip()

    if not org_name:
        return JsonResponse({'error': 'organization_name is required'}, status=400)
    if not owner_name:
        return JsonResponse({'error': 'owner_name is required'}, status=400)
    if not owner_email:
        return JsonResponse({'error': 'owner_email is required'}, status=400)
    if not temp_password or len(temp_password) < 8:
        return JsonResponse({'error': 'temporary_password must be at least 8 characters'}, status=400)

    suf = uuid.uuid4().hex[:10]
    org_code = (data.get('organization_code') or f"ORG-{suf.upper()}").strip()
    slug = (data.get('slug') or slugify(f"{org_name}-{suf}")[:90] or f"org-{suf}").strip()

    if Organization.objects.filter(organization_code=org_code).exists():
        return JsonResponse({'error': 'organization_code already exists'}, status=400)
    if Organization.objects.filter(slug=slug).exists():
        return JsonResponse({'error': 'slug already exists'}, status=400)

    user = User.objects.filter(email__iexact=owner_email).first()
    if user is None:
        user = User.objects.create_user(
            username=owner_email,
            email=owner_email,
            password=temp_password,
            first_name=owner_name,
        )
    else:
        # If user exists, we don't reset their password silently.
        # Portal admin should use invite flow for existing users.
        return JsonResponse({'error': 'A user with this email already exists'}, status=400)

    org = Organization.objects.create(
        name=org_name,
        organization_code=org_code,
        slug=slug,
        owner=user,
        created_by_admin_email=_admin_email(request),
        status=int(data.get('status', 1) or 1),
        plan_type=data.get('plan_type'),
        contact_email=data.get('contact_email'),
        contact_phone=data.get('contact_phone'),
        country=data.get('country'),
        timezone=data.get('timezone'),
    )

    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            'full_name': owner_name,
            'organization': org,
        }
    )
    profile.organization = org
    profile.full_name = profile.full_name or owner_name
    profile.role = 'org_owner'
    profile.must_change_password = True
    profile.admin_stored_password = temp_password
    profile.save()

    OrganizationMember.objects.get_or_create(organization=org, user=user)

    login_url = (data.get('login_url') or '').strip()
    if not login_url:
        # frontend can override; default to the standard login route
        login_url = "/login"

    send_org_owner_welcome_email(
        to_email=owner_email,
        owner_name=owner_name,
        organization_name=org_name,
        temporary_password=temp_password,
        login_url=login_url,
    )

    return JsonResponse({
        'message': 'Organization created',
        'organization_id': str(org.id),
        'owner_user_id': user.id,
        'owner_email': owner_email,
        'login_url': login_url,
    }, status=201)


@csrf_exempt
@require_http_methods(['DELETE'])
def admin_delete_organization(request, org_id: uuid.UUID):
    """Permanently delete organization and all member accounts (hard delete)."""
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    org_name = org.name
    _hard_delete_organization(org)
    return JsonResponse({'message': f'Organization "{org_name}" deleted permanently'})


@csrf_exempt
@require_http_methods(['POST'])
def admin_deactivate_organization(request, org_id: uuid.UUID):
    """Freeze organization: block login and API access; data is retained."""
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    org.status = ORG_STATUS_FROZEN
    org.save(update_fields=['status', 'updated_at'])
    _set_org_members_active(org, active=False)
    return JsonResponse({
        'message': 'Organization deactivated',
        'organization': _org_summary(org),
    })


@csrf_exempt
@require_http_methods(['POST'])
def admin_activate_organization(request, org_id: uuid.UUID):
    """Restore organization and member access."""
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    org.status = ORG_STATUS_ACTIVE
    org.save(update_fields=['status', 'updated_at'])
    _set_org_members_active(org, active=True)
    return JsonResponse({
        'message': 'Organization activated',
        'organization': _org_summary(org),
    })


@csrf_exempt
@require_http_methods(['DELETE'])
def admin_delete_organization_member(request, org_id: uuid.UUID, user_id: int):
    """Permanently delete a member account (hard delete when not in other orgs)."""
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    try:
        _hard_delete_org_member(org, user_id)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except LookupError:
        return JsonResponse({'error': 'Member not found'}, status=404)
    return JsonResponse({'message': 'Member deleted permanently'})


@csrf_exempt
@require_http_methods(['POST'])
def admin_deactivate_organization_member(request, org_id: uuid.UUID, user_id: int):
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    if not OrganizationMember.objects.filter(organization=org, user_id=user_id).exists():
        return JsonResponse({'error': 'Member not found'}, status=404)
    user = get_object_or_404(User, id=user_id)
    user.is_active = False
    user.save(update_fields=['is_active'])
    profile = UserProfile.objects.filter(user_id=user_id).first()
    if profile:
        profile.is_active = False
        profile.save(update_fields=['is_active', 'updated_at'])
    return JsonResponse({'message': 'Member deactivated', 'user': _user_summary(profile) if profile else {}})


@csrf_exempt
@require_http_methods(['POST'])
def admin_activate_organization_member(request, org_id: uuid.UUID, user_id: int):
    err = _require_admin(request)
    if err:
        return err
    org = _get_org_or_404(str(org_id))
    if org.status == ORG_STATUS_FROZEN:
        return JsonResponse(
            {'error': 'Activate the organization before activating individual members'},
            status=400,
        )
    if not OrganizationMember.objects.filter(organization=org, user_id=user_id).exists():
        return JsonResponse({'error': 'Member not found'}, status=404)
    user = get_object_or_404(User, id=user_id)
    user.is_active = True
    user.save(update_fields=['is_active'])
    profile = UserProfile.objects.filter(user_id=user_id).first()
    if profile:
        profile.is_active = True
        profile.save(update_fields=['is_active', 'updated_at'])
    return JsonResponse({'message': 'Member activated', 'user': _user_summary(profile) if profile else {}})
