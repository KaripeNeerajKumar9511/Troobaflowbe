import uuid
from django.db import models
from apps.organizations.models import Organization
from apps.products.models import Product
from apps.equipment.models import EquipmentGroup
from apps.labor.models import Labor


class Operation(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="operations"
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="operations"
    )

    op_number = models.IntegerField()

    name = models.CharField(max_length=255)

    equipment_group = models.ForeignKey(
        EquipmentGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="operations"
    )

    labor = models.ForeignKey(
        Labor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="operations"
    )

    percent_assign = models.FloatField(default=100)

    equipment_setup_per_lot = models.FloatField(default=0)

    equipment_run_per_piece = models.FloatField(default=0)

    labor_setup_per_lot = models.FloatField(default=0)

    labor_run_per_piece = models.FloatField(default=0)

    FORMULA_CHOICES = [
        ("E_SETUP_LOT", "E.Setup/Lot"),
        ("E_RUN_PC", "E.Run/Pc"),
        ("L_SETUP_LOT", "L.Setup/Lot"),
        ("L_RUN_PC", "L.Run/Pc"),
    ]

    formula_override = models.CharField(
        max_length=20,
        choices=FORMULA_CHOICES,
        null=True,
        blank=True
    )

    comments = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:

        db_table = "operations"

        constraints = [
            models.UniqueConstraint(
                fields=["product", "op_number"],
                name="unique_operation_per_product"
            )
        ]

        indexes = [
            models.Index(fields=["organization", "product"]),
            models.Index(fields=["product", "op_number"]),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.name}"