import uuid

from django.test import TestCase
from django.utils import timezone

from apps.operations.models import Operation
from apps.organizations.models import Organization
from apps.products.models import Product
from apps.rmct.models import RMCMModel
from apps.routing.models import Routing
from apps.routing.routing_path import apply_routing_cell_update


class RoutingPathTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.model = RMCMModel.objects.create(
            id=uuid.uuid4(),
            organization=self.org,
            name="M1",
        )
        self.product = Product.objects.create(
            organization=self.org,
            model=self.model,
            name="P1",
        )
        self.from_op = Operation.objects.create(
            organization=self.org,
            product=self.product,
            name="OP1",
            op_number=10,
        )
        self.to_op_a = Operation.objects.create(
            organization=self.org,
            product=self.product,
            name="OP2",
            op_number=20,
        )
        self.to_op_b = Operation.objects.create(
            organization=self.org,
            product=self.product,
            name="STOCK",
            op_number=30,
        )

    def test_patch_destination_removes_soft_deleted_duplicate(self):
        live = Routing.objects.create(
            organization=self.org,
            product=self.product,
            from_operation=self.from_op,
            to_operation=self.to_op_a,
            probability=50,
        )
        ghost = Routing.objects.create(
            organization=self.org,
            product=self.product,
            from_operation=self.from_op,
            to_operation=self.to_op_b,
            probability=100,
        )
        ghost.deleted_at = timezone.now()
        ghost.save(update_fields=["deleted_at"])

        updated, merged = apply_routing_cell_update(
            live, to_operation=self.to_op_b, probability=75
        )

        self.assertFalse(merged)
        self.assertEqual(updated.id, live.id)
        self.assertEqual(updated.to_operation_id, self.to_op_b.id)
        self.assertEqual(updated.probability, 75)
        self.assertFalse(Routing.objects.filter(id=ghost.id).exists())

    def test_patch_destination_merges_into_live_duplicate(self):
        primary = Routing.objects.create(
            organization=self.org,
            product=self.product,
            from_operation=self.from_op,
            to_operation=self.to_op_a,
            probability=40,
        )
        secondary = Routing.objects.create(
            organization=self.org,
            product=self.product,
            from_operation=self.from_op,
            to_operation=self.to_op_b,
            probability=60,
        )

        updated, merged = apply_routing_cell_update(
            secondary, to_operation=self.to_op_a, probability=80
        )

        self.assertTrue(merged)
        self.assertEqual(updated.id, primary.id)
        self.assertEqual(updated.probability, 80)
        secondary.refresh_from_db()
        self.assertIsNotNone(secondary.deleted_at)
