"""
RMCT domain models: manufacturing models, versions (checkpoints), scenarios (what-if), changes, results.
All nested data (general, labor, equipment, products, operations, routing, ibom, param_names) stored as JSON.
"""
from django.db import models
from django.contrib.auth.models import User
from apps.organizations.models import Organization


class RMCMModel(models.Model):
    """
    One RMCT manufacturing model. Metadata + full nested data as JSON.
    """
    id = models.UUIDField(primary_key=True, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='rmct_models')
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_index=True,
        related_name="rmct_models",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    tags = models.JSONField(default=list)  # list of strings
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    run_status = models.CharField(max_length=20, default='never_run')  # never_run, current, needs_recalc
    is_archived = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False)
    is_starred = models.BooleanField(default=False)
    param_names = models.JSONField(default=dict, blank=True)
    dept_codes = models.JSONField(default=dict, blank=True)
    # Full nested data (mirrors frontend Model type)
    # general = models.JSONField(default=dict)
    # param_names = models.JSONField(default=dict)
    # labor = models.JSONField(default=list)
    # equipment = models.JSONField(default=list)
    # products = models.JSONField(default=list)
    # operations = models.JSONField(default=list)
    # routing = models.JSONField(default=list)
    # ibom = models.JSONField(default=list)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name


class ModelVersion(models.Model):
    """Checkpoint/snapshot of a model for restore."""

    KIND_MANUAL = "manual"
    KIND_PRE_RESTORE = "pre_restore"
    KIND_CHOICES = [
        (KIND_MANUAL, "Manual"),
        (KIND_PRE_RESTORE, "Pre-restore rollback"),
    ]

    id = models.UUIDField(primary_key=True, editable=False)
    model = models.ForeignKey(RMCMModel, on_delete=models.CASCADE, related_name='versions')
    label = models.CharField(max_length=255)
    version_kind = models.CharField(
        max_length=20,
        choices=KIND_CHOICES,
        default=KIND_MANUAL,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    snapshot = models.JSONField()  # { general, labor, equipment, products, operations, routing, ibom, param_names, dept_codes }

    class Meta:
        ordering = ['-created_at']


class Scenario(models.Model):
    """What-if scenario for a model. Basecase is the baseline; others have changes."""
    id = models.UUIDField(primary_key=True, editable=False)
    model = models.ForeignKey(RMCMModel, on_delete=models.CASCADE, related_name='scenarios')
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    family_id = models.UUIDField(null=True, blank=True)
    is_basecase = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default='needs_recalc')  # needs_recalc, calculated
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['model', 'is_basecase'],
                condition=models.Q(is_basecase=True),
                name='unique_basecase_per_model',
            )
        ]


class ScenarioChange(models.Model):
    """One what-if override (field difference) for a scenario."""
    id = models.UUIDField(primary_key=True, editable=False)
    scenario = models.ForeignKey(Scenario, on_delete=models.CASCADE, related_name='changes')
    data_type = models.CharField(max_length=32)  # Labor, Equipment, Product, General, Routing, Product Inclusion
    entity_id = models.CharField(max_length=255, blank=True)
    entity_name = models.CharField(max_length=255, blank=True)
    field_name = models.CharField(max_length=255)
    basecase_value = models.TextField(blank=True)
    whatif_value = models.TextField(blank=True)


class ScenarioResult(models.Model):
    """Cached calculation results for a scenario (or basecase)."""
    scenario = models.OneToOneField(Scenario, on_delete=models.CASCADE, related_name='result')
    results = models.JSONField()  # CalcResults: equipment, labor, products, warnings, errors, overLimitResources, calculatedAt
    created_at = models.DateTimeField(auto_now=True)
