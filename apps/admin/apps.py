from django.apps import AppConfig


class TfAdminConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.admin'
    label = 'tf_admin'
    verbose_name = 'Trooba Flow Admin'
