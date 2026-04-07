from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rmct", "0002_remove_rmcmmodel_equipment_remove_rmcmmodel_general_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="rmcmmodel",
            name="param_names",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="rmcmmodel",
            name="dept_codes",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
