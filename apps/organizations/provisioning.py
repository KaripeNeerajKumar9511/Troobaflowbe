"""
Ensure every portal user belongs to an organization as owner when they have none.

Designed for production:
- Login calls `ensure_user_organization_if_needed` (cheap EXISTS checks; no writes when healthy).
- One-time / ops backfill: `python manage.py provision_orphan_users` (and optional --repair-model-links).
"""
from __future__ import annotations

import logging
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from apps.organizations.models import Organization, OrganizationMember
from apps.rmct.models import RMCMModel
from apps.users.models import UserProfile

logger = logging.getLogger(__name__)

# Legacy migration default org — models often remained here after organization FK was added.
DEFAULT_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def user_display_name(user: User, profile: UserProfile | None = None) -> str:
    if profile is None:
        profile = UserProfile.objects.filter(user=user).only("full_name").first()
    if profile and (profile.full_name or "").strip():
        return profile.full_name.strip()
    full = (user.get_full_name() or "").strip()
    if full:
        return full
    if (user.first_name or "").strip():
        return user.first_name.strip()
    local = (user.email or "").split("@")[0].strip()
    return local or f"User {user.id}"


def _unique_organization_code(base: str) -> str:
    stem = (slugify(base) or "user").replace("-", "")[:24].upper() or "USER"
    for _ in range(8):
        suf = uuid.uuid4().hex[:8].upper()
        code = f"ORG-{stem}-{suf}"[:50]
        if not Organization.objects.filter(organization_code=code).exists():
            return code
    return f"ORG-{uuid.uuid4().hex[:12].upper()}"[:50]


def _unique_organization_slug(base: str) -> str:
    for _ in range(8):
        suf = uuid.uuid4().hex[:8]
        slug = (slugify(f"{base}-{suf}")[:90] or f"org-{suf}")
        if not Organization.objects.filter(slug=slug).exists():
            return slug
    return f"org-{uuid.uuid4().hex[:12]}"


def _mislinked_models_q(user_id: int, active_org_id: uuid.UUID) -> Q:
    """Models that should be moved into the user's active organization."""
    org_conditions = Q(organization__isnull=True) | Q(
        organization_id=DEFAULT_ORGANIZATION_ID
    ) | Q(organization__owner_id=user_id)
    return (
        Q(owner_id=user_id) & org_conditions & ~Q(organization_id=active_org_id)
    ) | (
        Q(owner__isnull=True)
        & (Q(organization__isnull=True) | Q(organization_id=DEFAULT_ORGANIZATION_ID))
        & ~Q(organization_id=active_org_id)
    )


def user_has_mislinked_models(user_id: int, active_org_id: uuid.UUID) -> bool:
    return RMCMModel.objects.filter(_mislinked_models_q(user_id, active_org_id)).exists()


def user_needs_provisioning(user: User) -> bool:
    """
    Fast check (few indexed EXISTS queries, no writes).
    Used on login to avoid running the full provisioning path every request.
    """
    user_id = user.id
    if not OrganizationMember.objects.filter(user_id=user_id).exists():
        return True

    profile = UserProfile.objects.filter(user_id=user_id).only("organization_id").first()
    if profile is None or not profile.organization_id:
        return True

    active_org_id = profile.organization_id
    if not OrganizationMember.objects.filter(
        user_id=user_id, organization_id=active_org_id
    ).exists():
        return True

    return user_has_mislinked_models(user_id, active_org_id)


def attach_user_models_to_organization(
    user: User,
    org: Organization,
    *,
    previous_org_id: uuid.UUID | None = None,
) -> int:
    """
    Move RMCT models into the user's active organization (single UPDATE per queryset).
    """
    if previous_org_id == org.id:
        previous_org_id = None

    org_conditions = Q(organization__isnull=True) | Q(
        organization_id=DEFAULT_ORGANIZATION_ID
    )
    if previous_org_id is not None:
        org_conditions |= Q(organization_id=previous_org_id)
    org_conditions |= Q(organization__owner_id=user.id)

    owned_qs = (
        RMCMModel.objects.filter(owner=user)
        .filter(org_conditions)
        .exclude(organization_id=org.id)
    )
    updated = owned_qs.update(organization=org)

    orphan_conditions = Q(owner__isnull=True) & (
        Q(organization__isnull=True) | Q(organization_id=DEFAULT_ORGANIZATION_ID)
    )
    if previous_org_id is not None:
        orphan_conditions |= Q(owner__isnull=True, organization_id=previous_org_id)

    updated += (
        RMCMModel.objects.filter(orphan_conditions)
        .exclude(organization_id=org.id)
        .update(organization=org, owner=user)
    )
    return updated


def ensure_user_organization(
    user: User,
    *,
    org_name: str | None = None,
    force_model_relink: bool = False,
) -> tuple[Organization, bool, int]:
    """
    Guarantee org membership and active profile.organization.

    Returns (organization, created_new_org, models_linked_count).
    Idempotent: safe to run multiple times (management command / repair jobs).
    """
    with transaction.atomic():
        profile, _ = UserProfile.objects.select_for_update().get_or_create(
            user=user,
            defaults={"full_name": user_display_name(user)},
        )
        previous_org_id = profile.organization_id

        owned_org = (
            Organization.objects.filter(owner=user, deleted_at__isnull=True)
            .order_by("created_at")
            .only("id", "name", "owner_id")
            .first()
        )
        member_org_ids = list(
            OrganizationMember.objects.filter(user=user)
            .order_by("joined_at")
            .values_list("organization_id", flat=True)
        )

        created = False
        profile_dirty: list[str] = []

        if owned_org is None and not member_org_ids:
            display = (org_name or user_display_name(user, profile)).strip()
            owned_org = Organization.objects.create(
                name=display,
                organization_code=_unique_organization_code(display),
                slug=_unique_organization_slug(display),
                owner=user,
                contact_email=(user.email or "").strip() or None,
                status=1,
            )
            OrganizationMember.objects.create(organization=owned_org, user=user)
            profile.organization = owned_org
            profile.role = "org_owner"
            profile_dirty = ["organization", "role", "full_name", "updated_at"]
            if not (profile.full_name or "").strip():
                profile.full_name = display
            created = True
        else:
            org = owned_org
            if org is None:
                org = (
                    Organization.objects.filter(
                        id__in=member_org_ids, deleted_at__isnull=True
                    )
                    .order_by("created_at")
                    .only("id", "owner_id")
                    .first()
                )
            if org is None:
                raise ValueError(
                    f"User {user.id} has memberships but no valid organization"
                )

            OrganizationMember.objects.get_or_create(organization=org, user=user)

            if org.owner_id == user.id and profile.role != "org_owner":
                profile.role = "org_owner"
                profile_dirty.append("role")
            if (
                not profile.organization_id
                or profile.organization_id not in member_org_ids
            ):
                profile.organization = org
                profile_dirty.append("organization")
            owned_org = org

        if profile_dirty:
            profile_dirty.append("updated_at")
            profile.save(update_fields=list(dict.fromkeys(profile_dirty)))

        models_linked = 0
        need_relink = (
            force_model_relink
            or created
            or previous_org_id != owned_org.id
            or user_has_mislinked_models(user.id, owned_org.id)
        )
        if need_relink:
            models_linked = attach_user_models_to_organization(
                user,
                owned_org,
                previous_org_id=previous_org_id,
            )

        return owned_org, created, models_linked


def ensure_user_organization_if_needed(user: User) -> bool:
    """
    Run full provisioning only when required. Returns True if provisioning ran.
    Intended for the login path — steady-state users cost ~3 EXISTS queries only.
    """
    if not user_needs_provisioning(user):
        return False
    try:
        org, created, linked = ensure_user_organization(user)
        if created or linked:
            logger.info(
                "provisioned user_id=%s org_id=%s created_org=%s models_linked=%s",
                user.id,
                org.id,
                created,
                linked,
            )
        return True
    except Exception:
        logger.exception("ensure_user_organization failed for user_id=%s", user.id)
        return False


def users_needing_organization():
    """Users with no org membership (for one-time backfill)."""
    member_user_ids = OrganizationMember.objects.values_list("user_id", flat=True)
    return (
        User.objects.filter(is_active=True)
        .exclude(id__in=member_user_ids)
        .order_by("id")
        .iterator(chunk_size=200)
    )


def user_ids_with_mislinked_models():
    """Active users with RMCT models not scoped to their active organization."""
    from django.db.models import Exists, OuterRef

    mislinked = RMCMModel.objects.filter(owner_id=OuterRef("pk")).exclude(
        organization_id=OuterRef("profile__organization_id")
    ).filter(
        Q(organization__isnull=True)
        | Q(organization_id=DEFAULT_ORGANIZATION_ID)
        | Q(organization__owner_id=OuterRef("pk"))
    )

    return (
        User.objects.filter(is_active=True, profile__organization_id__isnull=False)
        .annotate(_mislinked=Exists(mislinked))
        .filter(_mislinked=True)
        .order_by("id")
    )
