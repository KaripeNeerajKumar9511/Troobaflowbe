from django.db import models

from apps.rmct.models import RMCMModel


class GeneralData(models.Model):
    """
    Per-model general settings for RMCT.

    This mirrors the `GeneralData` shape used on the frontend
    (`model.general` in the dashboard store) so that each RMCT
    model can optionally have its general configuration stored
    in a dedicated relational table as well as in JSON.
    """

    model = models.OneToOneField(
        RMCMModel,
        on_delete=models.CASCADE,
        related_name="general_settings",
    )

    # Core metadata
    model_title = models.CharField(max_length=255, blank=True)
    author = models.CharField(max_length=255, blank=True)
    comments = models.TextField(blank=True)

    # Time units
    OPS_TIME_UNIT_CHOICES = [
        ("SEC", "Seconds"),
        ("MIN", "Minutes"),
        ("HR", "Hours"),
    ]
    MCT_TIME_UNIT_CHOICES = [
        ("MIN", "Minutes"),
        ("HR", "Hours"),
        ("DAY", "Days"),
        ("WEEK", "Weeks"),
    ]
    PROD_PERIOD_UNIT_CHOICES = [
        ("DAY", "Day"),
        ("WEEK", "Week"),
        ("MONTH", "Month"),
        ("QUARTER", "Quarter"),
        ("YEAR", "Year"),
    ]

    ops_time_unit = models.CharField(
        max_length=4,
        choices=OPS_TIME_UNIT_CHOICES,
        default="MIN",
    )
    mct_time_unit = models.CharField(
        max_length=5,
        choices=MCT_TIME_UNIT_CHOICES,
        default="DAY",
    )
    prod_period_unit = models.CharField(
        max_length=7,
        choices=PROD_PERIOD_UNIT_CHOICES,
        default="YEAR",
    )

    # Conversion factors and variability
    conv1 = models.FloatField(default=480)  # e.g. working minutes per MCT period
    conv2 = models.FloatField(default=210)  # e.g. working days per production period

    util_limit = models.FloatField(default=95.0)
    var_equip = models.FloatField(default=30.0)
    var_labor = models.FloatField(default=30.0)
    var_prod = models.FloatField(default=30.0)

    # Generic extra parameters
    gen1 = models.FloatField(default=0.0)
    gen2 = models.FloatField(default=0.0)
    gen3 = models.FloatField(default=0.0)
    gen4 = models.FloatField(default=0.0)

    OUTPUT_VIEW_MODE_CHOICES = [
        ("normal", "Normal"),
        ("premium", "Premium"),
    ]
    output_view_mode = models.CharField(
        max_length=16,
        choices=OUTPUT_VIEW_MODE_CHOICES,
        default="normal",
    )

    class Meta:
        db_table = "general_data"
        verbose_name = "General data"
        verbose_name_plural = "General data"

    def __str__(self) -> str:
        return self.model_title or f"General settings for {self.model_id}"

