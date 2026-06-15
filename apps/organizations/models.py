import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class Organization(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255, db_index=True)

    organization_code = models.CharField(max_length=50, unique=True)

    slug = models.SlugField(unique=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
        null=True,
        blank=True,
        db_index=True,
    )

    # TF Admin is session-based (not a Django User). We persist who created the org for audit.
    created_by_admin_email = models.EmailField(null=True, blank=True, db_index=True)

    plan_type = models.CharField(max_length=50, null=True, blank=True)

    contact_email = models.EmailField(null=True, blank=True)
    contact_phone = models.CharField(max_length=20, null=True, blank=True)

    country = models.CharField(max_length=100, null=True, blank=True)
    timezone = models.CharField(max_length=100, null=True, blank=True)

    status = models.SmallIntegerField(default=1, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "organizations"

        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]


class OrganizationMember(models.Model):
    id = models.BigAutoField(primary_key=True)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
        db_index=True,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
        db_index=True,
    )

    joined_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "organization_members"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="unique_org_member",
            )
        ]
        indexes = [
            models.Index(fields=["organization", "joined_at"]),
            models.Index(fields=["user", "joined_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.organization_id}:{self.user_id}"


class OrganizationInvite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invites",
        db_index=True,
    )

    email = models.EmailField(db_index=True)

    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, editable=False)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_organization_invites",
        db_index=True,
    )

    accepted = models.BooleanField(default=False, db_index=True)

    expires_at = models.DateTimeField(db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "organization_invites"
        indexes = [
            models.Index(fields=["organization", "email", "accepted"]),
            models.Index(fields=["expires_at", "accepted"]),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at