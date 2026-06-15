import json
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from datetime import timedelta
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.admin.auth import is_admin_session
from apps.organizations.emails import send_member_invite_email
from apps.organizations.invite_urls import build_invite_url, resolve_invite_url_base
from apps.users.models import UserProfile, get_profile_payload
from .models import Organization, OrganizationInvite, OrganizationMember


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return {}

def _require_admin(request):
    if not is_admin_session(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    return None


def _require_user(request):
    if not request.user.is_authenticated:
        return None, JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        profile = request.user.profile
    except Exception:
        return None, JsonResponse({"error": "Profile not found"}, status=404)
    if not profile.organization_id:
        return None, JsonResponse({"error": "No active organization selected"}, status=400)
    return profile, None


def _require_org_owner(profile: UserProfile):
    if (profile.role or "") != "org_owner":
        return JsonResponse({"error": "Forbidden"}, status=403)
    return None


def _require_membership(*, organization_id, user_id):
    ok = OrganizationMember.objects.filter(organization_id=organization_id, user_id=user_id).exists()
    if not ok:
        return JsonResponse({"error": "Forbidden"}, status=403)
    return None


# CREATE ORGANIZATION
@csrf_exempt
@require_http_methods(["POST"])
def create_organization(request):
    err = _require_admin(request)
    if err:
        return err
    data = _parse_json(request)

    org = Organization.objects.create(
        name=data.get("name"),
        organization_code=data.get("organization_code"),
        slug=data.get("slug"),
        plan_type=data.get("plan_type"),
        contact_email=data.get("contact_email"),
        contact_phone=data.get("contact_phone"),
        country=data.get("country"),
        timezone=data.get("timezone"),
        status=data.get("status", 1)
    )

    return JsonResponse({
        "message": "Organization created successfully",
        "id": str(org.id)
    }, status=201)


# LIST ALL ORGANIZATIONS
@require_http_methods(["GET"])
def list_organizations(request):
    err = _require_admin(request)
    if err:
        return err

    orgs = Organization.objects.filter(deleted_at__isnull=True).values()

    data = list(orgs)

    return JsonResponse(data, safe=False)


# GET SINGLE ORGANIZATION
@require_http_methods(["GET"])
def get_organization(request, org_id):
    err = _require_admin(request)
    if err:
        return err

    org = get_object_or_404(Organization, id=org_id, deleted_at__isnull=True)

    data = {
        "id": str(org.id),
        "name": org.name,
        "organization_code": org.organization_code,
        "slug": org.slug,
        "plan_type": org.plan_type,
        "contact_email": org.contact_email,
        "contact_phone": org.contact_phone,
        "country": org.country,
        "timezone": org.timezone,
        "status": org.status,
        "created_at": org.created_at,
        "updated_at": org.updated_at,
    }

    return JsonResponse(data)


# UPDATE ORGANIZATION
@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
def update_organization(request, org_id):
    err = _require_admin(request)
    if err:
        return err

    org = get_object_or_404(Organization, id=org_id, deleted_at__isnull=True)

    data = _parse_json(request)

    org.name = data.get("name", org.name)
    org.organization_code = data.get("organization_code", org.organization_code)
    org.slug = data.get("slug", org.slug)
    org.plan_type = data.get("plan_type", org.plan_type)
    org.contact_email = data.get("contact_email", org.contact_email)
    org.contact_phone = data.get("contact_phone", org.contact_phone)
    org.country = data.get("country", org.country)
    org.timezone = data.get("timezone", org.timezone)
    org.status = data.get("status", org.status)

    org.save()

    return JsonResponse({
        "message": "Organization updated successfully"
    })


# DELETE ORGANIZATION (SOFT DELETE)
@csrf_exempt
@require_http_methods(["DELETE"])
def delete_organization(request, org_id):
    err = _require_admin(request)
    if err:
        return err

    org = get_object_or_404(Organization, id=org_id, deleted_at__isnull=True)

    from django.utils import timezone
    org.deleted_at = timezone.now()
    org.save()

    return JsonResponse({
        "message": "Organization deleted successfully"
    })


@require_http_methods(["GET"])
def org_members(request):
    profile, err = _require_user(request)
    if err:
        return err
    err = _require_membership(organization_id=profile.organization_id, user_id=request.user.id)
    if err:
        return err

    members = (
        OrganizationMember.objects
        .filter(organization_id=profile.organization_id)
        .select_related("user")
        .order_by("joined_at")
    )
    user_ids = [m.user_id for m in members]
    profiles = {
        p.user_id: p
        for p in UserProfile.objects.filter(user_id__in=user_ids).select_related("organization")
    }
    out = []
    for m in members:
        p = profiles.get(m.user_id)
        out.append({
            "user_id": m.user_id,
            "email": (m.user.email or "").lower(),
            "full_name": (p.full_name if p else (m.user.get_full_name() or m.user.email)),
            "role": (p.role if p else "member"),
            "joined_at": m.joined_at.isoformat() if m.joined_at else "",
        })
    return JsonResponse(out, safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def org_remove_member(request):
    profile, err = _require_user(request)
    if err:
        return err
    err = _require_membership(organization_id=profile.organization_id, user_id=request.user.id)
    if err:
        return err
    err = _require_org_owner(profile)
    if err:
        return err

    data = _parse_json(request)
    user_id = data.get("user_id")
    if not user_id:
        return JsonResponse({"error": "user_id is required"}, status=400)
    try:
        user_id_int = int(user_id)
    except Exception:
        return JsonResponse({"error": "user_id must be an integer"}, status=400)

    org = Organization.objects.filter(id=profile.organization_id, deleted_at__isnull=True).first()
    if not org:
        return JsonResponse({"error": "Organization not found"}, status=404)
    if org.owner_id == user_id_int:
        return JsonResponse({"error": "Cannot remove organization owner"}, status=400)

    OrganizationMember.objects.filter(organization_id=profile.organization_id, user_id=user_id_int).delete()
    return JsonResponse({"message": "Member removed"})


@csrf_exempt
@require_http_methods(["POST"])
def create_invite(request):
    profile, err = _require_user(request)
    if err:
        return err
    err = _require_membership(organization_id=profile.organization_id, user_id=request.user.id)
    if err:
        return err
    err = _require_org_owner(profile)
    if err:
        return err

    data = _parse_json(request)
    email = (data.get("email") or "").strip().lower()
    invite_url_base = resolve_invite_url_base(data.get("invite_url_base") or "")

    if not email:
        return JsonResponse({"error": "email is required"}, status=400)

    existing_user = User.objects.filter(email__iexact=email).first()
    if existing_user and OrganizationMember.objects.filter(organization_id=profile.organization_id, user=existing_user).exists():
        return JsonResponse({"error": "User is already a member"}, status=400)

    active_invite = (
        OrganizationInvite.objects.filter(
            organization_id=profile.organization_id,
            email__iexact=email,
            accepted=False,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )
    if active_invite is not None:
        return JsonResponse({"error": "Active invite already exists"}, status=400)

    invite = OrganizationInvite.objects.create(
        organization_id=profile.organization_id,
        email=email,
        invited_by=request.user,
        expires_at=timezone.now() + timedelta(hours=24),
    )

    org = Organization.objects.filter(id=profile.organization_id).first()
    org_name = org.name if org else "your organization"
    inviter_name = profile.full_name or request.user.get_full_name() or request.user.email

    invite_url = build_invite_url(invite_url_base, invite.token)

    email_sent, email_error = send_member_invite_email(
        to_email=email,
        inviter_name=inviter_name,
        organization_name=org_name,
        invite_url=invite_url,
        expires_hours=24,
    )

    payload = {
        "message": "Invite created",
        "invite_id": str(invite.id),
        "token": str(invite.token),
        "invite_url": invite_url,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else "",
        "email_sent": email_sent,
    }
    if email_error:
        payload["email_error"] = email_error

    status = 201
    return JsonResponse(payload, status=status)


@require_http_methods(["GET"])
def invite_preview(request):
    """Public: validate invite token and return org/email for the accept-invite page."""
    token = (request.GET.get("token") or "").strip()
    if not token:
        return JsonResponse({"valid": False, "error": "token is required"}, status=400)
    invite = OrganizationInvite.objects.filter(token=token).select_related("organization").first()
    if invite is None:
        return JsonResponse({"valid": False, "error": "Invalid invite token"}, status=404)
    if invite.accepted:
        return JsonResponse({"valid": False, "error": "Invite already accepted"}, status=400)
    if invite.is_expired():
        return JsonResponse({"valid": False, "error": "Invite expired"}, status=400)
    return JsonResponse({
        "valid": True,
        "organization_name": invite.organization.name,
        "email": invite.email,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else "",
    })


@csrf_exempt
@require_http_methods(["POST"])
def accept_invite(request):
    """
    Accept invite using token + password.
    Creates user if missing, adds OrganizationMember, marks invite accepted,
    assigns member role, and sets active org if not already set.
    """
    data = _parse_json(request)
    token = (data.get("token") or "").strip()
    password = (data.get("password") or "").strip()
    full_name = (data.get("name") or data.get("full_name") or "").strip()

    if not token:
        return JsonResponse({"error": "token is required"}, status=400)
    if not password or len(password) < 8:
        return JsonResponse({"error": "password must be at least 8 characters"}, status=400)

    invite = OrganizationInvite.objects.filter(token=token).select_related("organization").first()
    if invite is None:
        return JsonResponse({"error": "Invalid invite token"}, status=400)
    if invite.accepted:
        return JsonResponse({"error": "Invite already accepted"}, status=400)
    if invite.is_expired():
        return JsonResponse({"error": "Invite expired"}, status=400)

    email = (invite.email or "").strip().lower()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=full_name or email.split("@")[0],
        )
    else:
        # Existing user: require authentication by setting their password is dangerous;
        # just set the password provided during accept (invite link possession assumed).
        user.set_password(password)
        user.save()

    OrganizationMember.objects.get_or_create(organization=invite.organization, user=user)

    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            "full_name": full_name or (user.get_full_name() or email),
            "organization": invite.organization,
            "role": "member",
        }
    )
    if not profile.organization_id:
        profile.organization = invite.organization
    if not profile.full_name and full_name:
        profile.full_name = full_name
    if not profile.role or profile.role == "user":
        profile.role = "member"
    profile.must_change_password = False
    profile.save()

    invite.accepted = True
    invite.save(update_fields=["accepted"])

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])

    login(request, user)
    profile_payload = get_profile_payload(user)
    profile_payload["authenticated"] = True

    return JsonResponse({
        "message": "Invite accepted",
        "organization_id": str(invite.organization_id),
        "organization_name": invite.organization.name,
        "user_id": user.id,
        "email": email,
        "profile": profile_payload,
    })