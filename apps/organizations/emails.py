import logging
from email import encoders
from email.mime.base import MIMEBase
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

_EMAIL_LOGO_PATH = Path(__file__).resolve().parent / "email_assets" / "trooba-logo-mono-white.svg"
_EMAIL_LOGO_CID = "trooba_logo"


def _from_email() -> str:
    return getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@trooba.local")


def _email_branding_context() -> dict:
    return {
        "logo_cid": _EMAIL_LOGO_CID if _EMAIL_LOGO_PATH.is_file() else "",
    }


def _attach_email_logo(msg: EmailMultiAlternatives) -> None:
    if not _EMAIL_LOGO_PATH.is_file():
        return
    part = MIMEBase("image", "svg+xml")
    part.set_payload(_EMAIL_LOGO_PATH.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-ID", f"<{_EMAIL_LOGO_CID}>")
    part.add_header("Content-Disposition", "inline", filename="trooba-logo.svg")
    msg.attach(part)
    msg.mixed_subtype = "related"


def _build_html_email(*, subject: str, text: str, html: str, to_email: str) -> EmailMultiAlternatives:
    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=_from_email(), to=[to_email])
    msg.attach_alternative(html, "text/html")
    _attach_email_logo(msg)
    return msg


def _send(msg: EmailMultiAlternatives) -> tuple[bool, str | None]:
    """Send email; return (success, error_message). Never swallows SMTP failures silently."""
    try:
        sent = msg.send(fail_silently=False)
        if sent < 1:
            return False, "Mail backend returned 0 messages sent"
        return True, None
    except Exception as exc:
        logger.exception("Email send failed to %s", msg.to)
        return False, str(exc)


def send_org_owner_welcome_email(
    *,
    to_email: str,
    owner_name: str,
    organization_name: str,
    temporary_password: str,
    login_url: str,
) -> tuple[bool, str | None]:
    subject = f"Welcome to {organization_name}"
    ctx = {
        "owner_name": owner_name,
        "organization_name": organization_name,
        "temporary_password": temporary_password,
        "login_url": login_url,
        "recipient_email": to_email,
        **_email_branding_context(),
    }
    html = render_to_string("organizations/emails/org_owner_welcome.html", ctx)
    text = render_to_string("organizations/emails/org_owner_welcome.txt", ctx)
    msg = _build_html_email(subject=subject, text=text, html=html, to_email=to_email)
    return _send(msg)


def send_member_invite_email(
    *,
    to_email: str,
    inviter_name: str,
    organization_name: str,
    invite_url: str,
    expires_hours: int = 24,
) -> tuple[bool, str | None]:
    subject = f"Join {organization_name} on Trooba Flow"
    ctx = {
        "inviter_name": inviter_name,
        "organization_name": organization_name,
        "invitee_email": to_email,
        "invite_url": invite_url,
        "expires_hours": expires_hours,
        **_email_branding_context(),
    }
    html = render_to_string("organizations/emails/member_invite.html", ctx)
    text = render_to_string("organizations/emails/member_invite.txt", ctx)
    msg = _build_html_email(subject=subject, text=text, html=html, to_email=to_email)
    return _send(msg)
