from django.contrib.auth.models import User
from django.test import TestCase

from apps.organizations.models import Organization, OrganizationMember
from apps.organizations.provisioning import (
    DEFAULT_ORGANIZATION_ID,
    ensure_user_organization,
    ensure_user_organization_if_needed,
    user_needs_provisioning,
    users_needing_organization,
)
from apps.rmct.models import RMCMModel
from apps.users.models import UserProfile
import uuid


class ProvisionOrphanUsersTests(TestCase):
    def test_creates_personal_org_for_user_without_membership(self):
        user = User.objects.create_user(
            username="orphan@example.com",
            email="orphan@example.com",
            password="testpass123",
            first_name="Ada",
        )
        UserProfile.objects.create(user=user, full_name="Ada Lovelace", organization=None)

        org, created, _ = ensure_user_organization(user)

        self.assertTrue(created)
        self.assertEqual(org.name, "Ada Lovelace")
        self.assertEqual(org.owner_id, user.id)
        profile = UserProfile.objects.get(user=user)
        self.assertEqual(profile.organization_id, org.id)
        self.assertEqual(profile.role, "org_owner")
        self.assertTrue(
            OrganizationMember.objects.filter(organization=org, user=user).exists()
        )

    def test_links_orphan_models_to_new_org(self):
        user = User.objects.create_user(
            username="modelowner@example.com",
            email="modelowner@example.com",
            password="testpass123",
        )
        UserProfile.objects.create(user=user, full_name="Model Owner", organization=None)
        model_id = uuid.uuid4()
        RMCMModel.objects.create(
            id=model_id,
            owner=user,
            organization=None,
            name="Test Model",
        )

        org, created, linked = ensure_user_organization(user)

        self.assertTrue(created)
        self.assertEqual(linked, 1)
        m = RMCMModel.objects.get(id=model_id)
        self.assertEqual(m.organization_id, org.id)

    def test_migrates_models_from_default_organization(self):
        user = User.objects.create_user(
            username="defaultorg@example.com",
            email="defaultorg@example.com",
            password="testpass123",
        )
        default_org, _ = Organization.objects.get_or_create(
            id=DEFAULT_ORGANIZATION_ID,
            defaults={
                "name": "Default Organization",
                "organization_code": "DEFAULT",
                "slug": "default",
                "status": 1,
            },
        )
        UserProfile.objects.create(
            user=user,
            full_name="Default User",
            organization=default_org,
        )
        model_id = uuid.uuid4()
        RMCMModel.objects.create(
            id=model_id,
            owner=user,
            organization=default_org,
            name="On Default Org",
        )

        org, created, linked = ensure_user_organization(user)

        self.assertTrue(created)
        self.assertEqual(linked, 1)
        m = RMCMModel.objects.get(id=model_id)
        self.assertEqual(m.organization_id, org.id)

    def test_login_fast_path_skips_when_fully_provisioned(self):
        user = User.objects.create_user(
            username="healthy@example.com",
            email="healthy@example.com",
            password="testpass123",
        )
        org, created, _ = ensure_user_organization(user)
        self.assertTrue(created)
        self.assertFalse(user_needs_provisioning(user))
        self.assertFalse(ensure_user_organization_if_needed(user))
        self.assertEqual(
            RMCMModel.objects.filter(owner=user).exclude(organization=org).count(),
            0,
        )

    def test_users_needing_organization_excludes_members(self):
        user = User.objects.create_user(
            username="member@example.com",
            email="member@example.com",
            password="testpass123",
        )
        from apps.organizations.models import Organization

        org = Organization.objects.create(
            name="Existing",
            organization_code="ORG-EXISTING",
            slug="existing-org",
        )
        OrganizationMember.objects.create(organization=org, user=user)
        UserProfile.objects.create(user=user, full_name="Member", organization=org)

        self.assertNotIn(user, users_needing_organization())
