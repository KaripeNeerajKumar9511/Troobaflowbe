"""
Align nested model rows (labor, equipment, products, etc.) with their parent model's organization.

Run on live after org migration or legacy imports where child rows kept a stale organization_id.
"""
from django.core.management.base import BaseCommand

from apps.equipment.models import EquipmentGroup
from apps.ibom.models import BOM
from apps.labor.models import Labor
from apps.operations.models import Operation
from apps.products.models import Product
from apps.routing.models import Routing
from apps.rmct.models import RMCMModel


class Command(BaseCommand):
    help = "Repair organization_id on nested rows to match parent RMCMModel."

    def handle(self, *args, **options):
        fixed = 0
        for model in RMCMModel.objects.filter(organization_id__isnull=False):
            org_id = model.organization_id
            for qs in (
                Labor.objects.filter(model=model).exclude(organization_id=org_id),
                EquipmentGroup.objects.filter(model=model).exclude(organization_id=org_id),
                Product.objects.filter(model=model).exclude(organization_id=org_id),
                Operation.objects.filter(product__model=model).exclude(organization_id=org_id),
                Routing.objects.filter(product__model=model).exclude(organization_id=org_id),
                BOM.objects.filter(parent_product__model=model).exclude(organization_id=org_id),
            ):
                count = qs.update(organization_id=org_id)
                fixed += count
        self.stdout.write(self.style.SUCCESS(f"Updated {fixed} nested row(s)."))
