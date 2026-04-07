from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
from django.conf import settings
import json
import uuid
from django.contrib.auth import login, logout
from django.utils.text import slugify
from apps.organizations.models import Organization
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

    suf = uuid.uuid4().hex[:10]
    org = Organization.objects.create(
        name=(name + "'s Workspace") if name else (email.split("@")[0] + " Workspace"),
        organization_code=f"ORG-{suf.upper()}",
        slug=(slugify(f"{name or email}-{suf}")[:90] or f"org-{suf}"),
    )

    user, error = create_user_account(
        name=name or email.split("@")[0],
        email=email,
        password=password,
        password_confirm=password_confirm,
        organization=org,
    )
    if error:
        org.delete()
        return JsonResponse({"error": error}, status=400)
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
        # 400 avoids browser "Unauthorized" noise; same JSON { error } for the client.
        return JsonResponse({"error": error}, status=400)

    login(request, user)

    return JsonResponse({"message": "Login successful"})


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
    if "user_level" in data:
        try:
            profile.user_level = max(1, min(5, int(data["user_level"])))
        except (TypeError, ValueError):
            pass
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
    login(request, request.user)
    return JsonResponse({"message": "Password updated"})


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


@csrf_exempt
@require_http_methods(["POST"])
def logout_view(request):
    logout(request)

    response = JsonResponse({"message": "Logged out"})
    # Explicitly clear session and CSRF cookies on the client.
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    response.delete_cookie("csrftoken")
    return response
