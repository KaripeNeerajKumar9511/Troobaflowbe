"""Portal user access checks (active / frozen accounts and organizations)."""

from __future__ import annotations

ACCOUNT_FROZEN_MESSAGE = (
    "Your account has been frozen. Please contact support."
)

ORG_STATUS_ACTIVE = 1
ORG_STATUS_FROZEN = 0


def portal_access_block_reason(user) -> str | None:
    """
    Return a user-facing message when the account must not use the portal,
    or None when access is allowed.
    """
    if user is None:
        return ACCOUNT_FROZEN_MESSAGE

    try:
        profile = user.profile
    except Exception:
        return None

    if profile.deleted_at is not None:
        return ACCOUNT_FROZEN_MESSAGE

    if not profile.is_active:
        return ACCOUNT_FROZEN_MESSAGE

    if not user.is_active:
        return ACCOUNT_FROZEN_MESSAGE

    if profile.organization_id:
        org = profile.organization
        if org is None:
            return None
        if org.deleted_at is not None:
            return ACCOUNT_FROZEN_MESSAGE
        if org.status == ORG_STATUS_FROZEN:
            return ACCOUNT_FROZEN_MESSAGE

    return None
