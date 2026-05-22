from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rmct", "0003_rmcmmodel_param_names_dept_codes"),
    ]

    operations = [
        migrations.AddField(
            model_name="modelversion",
            name="version_kind",
            field=models.CharField(
                choices=[("manual", "Manual"), ("pre_restore", "Pre-restore rollback")],
                db_index=True,
                default="manual",
                max_length=20,
            ),
        ),
    ]
