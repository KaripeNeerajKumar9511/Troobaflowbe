# Generated manually — extended operation timing + oper1–4

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0003_alter_operation_equipment_run_per_piece_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='operation',
            name='equipment_setup_per_piece',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='equipment_setup_per_tbatch',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='equipment_run_per_lot',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='equipment_run_per_tbatch',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='labor_setup_per_piece',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='labor_setup_per_tbatch',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='labor_run_per_lot',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='labor_run_per_tbatch',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='oper1',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='oper2',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='oper3',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='operation',
            name='oper4',
            field=models.FloatField(default=0),
        ),
    ]
