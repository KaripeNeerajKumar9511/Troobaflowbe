from django.db import models
from django.contrib.auth.models import User
from apps.organizations.models import Organization, OrganizationMember


class UserProfile(models.Model):
    """
    Extra per-user data for the RMCT app.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    # Current/active organization for the session/UI. Membership is tracked in
    # apps.organizations.models.OrganizationMember (multi-org capable).
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="users"
    )

    full_name = models.CharField(max_length=255)

    role = models.CharField(max_length=50, db_index=True, default="user")

    user_level = models.PositiveSmallIntegerField(default=1)

    is_active = models.BooleanField(default=True, db_index=True)

    must_change_password = models.BooleanField(default=False, db_index=True)

    # Admin-provisioned password (cleared after user changes password).
    admin_stored_password = models.CharField(max_length=128, blank=True, default="")

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

    profile = UserProfile.objects.create(
        user=user,
        full_name=name,
        organization=organization,
        role="org_owner" if organization is not None else "user",
    )
    if organization is not None:
        OrganizationMember.objects.get_or_create(organization=organization, user=user)
        if organization.owner_id is None:
            organization.owner = user
            organization.save(update_fields=["owner", "updated_at"])

    return user, None

def authenticate_user(*, email: str, password: str):
    """
    All auth + DB checks for login.

    Returns (user, error_message). If error_message is not None, user will be None.
    """
    from .access import portal_access_block_reason

    email = (email or "").strip().lower()
    password = password or ""

    if not email or not password:
        return None, "Email and password are required"

    candidates: list[User] = []
    seen_ids: set[int] = set()

    def add_candidate(user: User | None) -> None:
        if user is not None and user.id not in seen_ids:
            seen_ids.add(user.id)
            candidates.append(user)

    add_candidate(User.objects.filter(username=email).first())
    add_candidate(User.objects.filter(email__iexact=email).first())

    for user in candidates:
        if not user.check_password(password):
            continue
        block = portal_access_block_reason(user)
        if block:
            return None, block
        return user, None

    return None, "Invalid credentials"


def get_profile_payload(user: User):
    if not user.is_authenticated:
        return {"email": None, "name": None, "organization_id": None, "organization_name": None, "role": None, "user_level": 1, "must_change_password": False}

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
            "must_change_password": False,
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
        "must_change_password": bool(profile.must_change_password),
    }