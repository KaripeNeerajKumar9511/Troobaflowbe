from django.db import models
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from apps.organizations.models import Organization


class UserProfile(models.Model):
    """
    Extra per-user data for the RMCT app.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="users"
    )

    full_name = models.CharField(max_length=255)

    role = models.CharField(max_length=50, db_index=True, default="user")

    user_level = models.PositiveSmallIntegerField(default=1)

    is_active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:

        db_table = "user_profiles"

        indexes = [
            models.Index(fields=["organization", "role"]),
            models.Index(fields=["organization", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.full_name or self.user.get_username()

def create_user_account(*, name: str, email: str, password: str, password_confirm: str, organization):

    email = (email or "").strip().lower()
    name = (name or "").strip()

    if not email:
        return None, "Email is required"
    if not name:
        return None, "Name is required"
    if not password:
        return None, "Password is required"
    if password != password_confirm:
        return None, "Passwords do not match"

    if User.objects.filter(email__iexact=email).exists():
        return None, "An account with this email already exists"

    user = User.objects.create_user(
        username=email,
        email=email,
        password=password,
        first_name=name,
    )

    UserProfile.objects.create(
        user=user,
        full_name=name,
        organization=organization
    )

    return user, None

def authenticate_user(*, email: str, password: str):
    """
    All auth + DB checks for login.

    Returns (user, error_message). If error_message is not None, user will be None.
    """
    email = (email or "").strip().lower()
    password = password or ""

    if not email or not password:
        return None, "Email and password are required"

    user = authenticate(username=email, password=password)
    if user is not None:
        return user, None

    # Username is stored as normalized email; also try DB user whose email matches case-insensitively.
    existing = User.objects.filter(email__iexact=email).first()
    if existing is not None:
        user = authenticate(username=existing.username, password=password)
        if user is not None:
            return user, None

    return None, "Invalid credentials"


def get_profile_payload(user: User):
    if not user.is_authenticated:
        return {"email": None, "name": None, "organization_id": None, "organization_name": None, "role": None, "user_level": 1}

    try:
        profile = user.profile
    except UserProfile.DoesNotExist:
        return {
            "id": user.id,
            "email": user.email,
            "name": user.get_full_name() or user.email,
            "organization_id": None,
            "organization_name": None,
            "role": "user",
            "user_level": 1,
        }

    org_name = ""
    if profile.organization_id:
        try:
            org_name = profile.organization.name
        except Exception:
            org_name = ""

    return {
        "id": user.id,
        "email": user.email,
        "name": profile.full_name or user.get_full_name() or user.email,
        "organization_id": str(profile.organization_id),
        "organization_name": org_name,
        "role": profile.role,
        "user_level": profile.user_level,
    }