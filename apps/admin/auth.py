"""TF Admin session helpers (separate from Django user auth)."""
from django.conf import settings

SESSION_KEY = 'tf_admin'


def admin_credentials_valid(email: str, password: str) -> bool:
    email_norm = (email or '').strip().lower()
    expected_email = getattr(settings, 'TF_ADMIN_EMAIL', 'admin@gmail.com').strip().lower()
    expected_password = getattr(settings, 'TF_ADMIN_PASSWORD', '12345678')
    return email_norm == expected_email and password == expected_password


def is_admin_session(request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def set_admin_session(request, email: str) -> None:
    request.session[SESSION_KEY] = True
    request.session['tf_admin_email'] = (email or '').strip().lower()
    request.session.modified = True


def clear_admin_session(request) -> None:
    request.session.pop(SESSION_KEY, None)
    request.session.pop('tf_admin_email', None)
    request.session.modified = True
