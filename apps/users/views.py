from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
from django.conf import settings
import json
import uuid
from django.contrib.auth import login, logout
from django.utils.text import slugify
from apps.organizations.models import Organization
from apps.organizations.models import OrganizationMember
from apps.organizations.provisioning import ensure_user_organization_if_needed
from .models import create_user_account, authenticate_user, get_profile_payload, UserProfile


@ensure_csrf_cookie
@require_http_methods(["GET"])
def csrf_cookie(request):
    """Return 200 so the client receives the CSRF cookie (for X-CSRFToken header)."""
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def signup(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    email = data.get("email") or ""
    password = data.get("password") or ""
    password_confirm = data.get("password_confirm") or ""
    name = (data.get("name") or "").strip()

    display_name = name or email.split("@")[0]
    suf = uuid.uuid4().hex[:10]
    org = Organization.objects.create(
        name=display_name,
        organization_code=f"ORG-{suf.upper()}",
        slug=(slugify(f"{name or email}-{suf}")[:90] or f"org-{suf}"),
        contact_email=email.strip().lower() or None,
        status=1,
    )

    user, error = create_user_account(
        name=display_name,
        email=email,
        password=password,
        password_confirm=password_confirm,
        organization=org,
    )
    if error:
        org.delete()
        return JsonResponse({"error": error}, status=400)

    org.owner = user
    org.save(update_fields=["owner", "updated_at"])
    profile = user.profile
    profile.role = "org_owner"
    profile.save(update_fields=["role", "updated_at"])

    return JsonResponse({"message": "Account created"})


@csrf_exempt
@require_http_methods(["POST"])
def login_view(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    email = data.get("email") or ""
    password = data.get("password") or ""

    user, error = authenticate_user(email=email, password=password)
    if error:
        from apps.users.access import ACCOUNT_FROZEN_MESSAGE
        payload = {"error": error}
        if error == ACCOUNT_FROZEN_MESSAGE:
            payload["code"] = "account_frozen"
        return JsonResponse(payload, status=400)

    login(request, user)

    # Cheap EXISTS checks only when user is already fully provisioned (no writes).
    ensure_user_organization_if_needed(user)

    payload = get_profile_payload(user)
    return JsonResponse({"message": "Login successful", "profile": payload})


@require_http_methods(["GET"])
def profile(request):
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    payload = get_profile_payload(request.user)
    payload["authenticated"] = True
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["PATCH"])
def profile_patch(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    profile = UserProfile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({"error": "Profile not found"}, status=404)
    if "full_name" in data or "name" in data:
        profile.full_name = (data.get("full_name") or data.get("name") or "").strip() or profile.full_name
    # Interface level disabled — ignore user_level updates from the portal.
    # if "user_level" in data:
    #     try:
    #         profile.user_level = max(1, min(5, int(data["user_level"])))
    #     except (TypeError, ValueError):
    #         pass
    profile.save()
    return JsonResponse(get_profile_payload(request.user))


@csrf_exempt
@require_http_methods(["POST"])
def change_password(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    current = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""
    if not request.user.check_password(current):
        return JsonResponse({"error": "Current password is incorrect"}, status=400)
    if len(new_pw) < 8:
        return JsonResponse({"error": "New password must be at least 8 characters"}, status=400)
    request.user.set_password(new_pw)
    request.user.save()
    try:
        profile = request.user.profile
        update_fields = []
        if profile.must_change_password:
            profile.must_change_password = False
            update_fields.append("must_change_password")
        if profile.admin_stored_password:
            profile.admin_stored_password = ""
            update_fields.append("admin_stored_password")
        if update_fields:
            profile.save(update_fields=update_fields)
    except UserProfile.DoesNotExist:
        pass
    login(request, request.user)
    return JsonResponse({"message": "Password updated", "profile": get_profile_payload(request.user)})


@require_http_methods(["GET"])
def org_members(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        return JsonResponse([], safe=False)
    users = UserProfile.objects.filter(organization_id=profile.organization_id, deleted_at__isnull=True).select_related("user")
    out = []
    for p in users:
        out.append({
            "id": str(p.user_id),
            "email": p.user.email,
            "full_name": p.full_name,
            "role": p.role,
            "user_level": p.user_level,
        })
    return JsonResponse(out, safe=False)


@require_http_methods(["GET"])
def my_organizations(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)
    orgs = (
        Organization.objects.filter(memberships__user=request.user, deleted_at__isnull=True)
        .distinct()
        .order_by("name")
        .values("id", "name", "organization_code", "slug")
    )
    out = []
    for o in orgs:
        out.append({**o, "id": str(o["id"])})
    return JsonResponse(out, safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def set_active_organization(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    org_id = (data.get("organization_id") or "").strip()
    if not org_id:
        return JsonResponse({"error": "organization_id is required"}, status=400)
    if not OrganizationMember.objects.filter(organization_id=org_id, user=request.user).exists():
        return JsonResponse({"error": "Forbidden"}, status=403)
    org = Organization.objects.filter(id=org_id, deleted_at__isnull=True).first()
    if not org:
        return JsonResponse({"error": "Organization not found"}, status=404)
    profile = UserProfile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({"error": "Profile not found"}, status=404)
    profile.organization = org
    profile.save(update_fields=["organization", "updated_at"])
    return JsonResponse(get_profile_payload(request.user))


@csrf_exempt
@require_http_methods(["POST"])
def logout_view(request):
    logout(request)

    response = JsonResponse({"message": "Logged out"})
    # Explicitly clear session and CSRF cookies on the client.
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    response.delete_cookie("csrftoken")
    return response
