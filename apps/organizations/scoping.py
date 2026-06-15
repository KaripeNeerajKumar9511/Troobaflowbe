from __future__ import annotations

from dataclasses import dataclass

from django.http import JsonResponse

from apps.organizations.models import Organization, OrganizationMember
from apps.users.access import ORG_STATUS_FROZEN, portal_access_block_reason
from apps.users.models import UserProfile


@dataclass(frozen=True)
class OrgContext:
    organization: Organization
    profile: UserProfile


def get_org_context(request) -> tuple[OrgContext | None, JsonResponse | None]:
    """
    Resolve the active org for the current Django-authenticated user.
    Enforces:
    - authenticated user
    - has UserProfile
    - profile.organization set (active org)
    - OrganizationMember row exists (membership)
    """
    if not request.user.is_authenticated:
        return None, JsonResponse({"error": "Unauthorized"}, status=401)

    block = portal_access_block_reason(request.user)
    if block:
        return None, JsonResponse({"error": block, "code": "account_frozen"}, status=403)

    try:
        profile = request.user.profile
    except Exception:
        return None, JsonResponse({"error": "Profile not found"}, status=404)
    if not profile.organization_id:
        return None, JsonResponse({"error": "No active organization selected"}, status=400)
    org = profile.organization
    if org is None:
        return None, JsonResponse({"error": "Organization not found"}, status=404)
    if org.deleted_at is not None or org.status == ORG_STATUS_FROZEN:
        block = portal_access_block_reason(request.user)
        return None, JsonResponse(
            {"error": block or "Organization is not available", "code": "account_frozen"},
            status=403,
        )
    if not OrganizationMember.objects.filter(organization_id=org.id, user_id=request.user.id).exists():
        return None, JsonResponse({"error": "Forbidden"}, status=403)
    return OrgContext(organization=org, profile=profile), None


def require_org_owner(ctx: OrgContext) -> JsonResponse | None:
    if (ctx.profile.role or "") != "org_owner":
        return JsonResponse({"error": "Forbidden"}, status=403)
    return None

