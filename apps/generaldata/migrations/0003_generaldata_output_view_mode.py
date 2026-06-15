from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("generaldata", "0002_alter_prod_period_unit_quarter"),
    ]

    operations = [
        migrations.AddField(
            model_name="generaldata",
            name="output_view_mode",
            field=models.CharField(
                choices=[("normal", "Normal"), ("premium", "Premium")],
                default="normal",
                max_length=16,
            ),
        ),
    ]
