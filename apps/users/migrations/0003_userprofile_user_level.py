from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0002_userprofile_deleted_at_userprofile_is_active_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="user_level",
            field=models.PositiveSmallIntegerField(default=1),
        ),
    ]
