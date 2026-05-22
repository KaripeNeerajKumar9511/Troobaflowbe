from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('generaldata', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='generaldata',
            name='prod_period_unit',
            field=models.CharField(
                choices=[
                    ('DAY', 'Day'),
                    ('WEEK', 'Week'),
                    ('MONTH', 'Month'),
                    ('QUARTER', 'Quarter'),
                    ('YEAR', 'Year'),
                ],
                default='YEAR',
                max_length=7,
            ),
        ),
    ]
