from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0004_userprofile_must_change_password_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='admin_stored_password',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
    ]
