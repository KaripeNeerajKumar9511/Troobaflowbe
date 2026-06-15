"""Build member-invite links; PUBLIC_APP_URL overrides client origin in production."""

from django.conf import settings


def public_app_origin() -> str:
    return (getattr(settings, "PUBLIC_APP_URL", "") or "").strip().rstrip("/")


def resolve_invite_url_base(client_base: str = "") -> str:
    """Prefer PUBLIC_APP_URL; otherwise use client-supplied base (e.g. local dev)."""
    origin = public_app_origin()
    if origin:
        return f"{origin}/accept-invite"
    client = (client_base or "").strip().rstrip("/")
    return client


def build_invite_url(invite_url_base: str, token) -> str:
    if not invite_url_base:
        return str(token)
    return f"{invite_url_base.rstrip('/')}?token={token}"
