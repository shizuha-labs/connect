from django.apps import AppConfig


class ConnectConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'connect'
    verbose_name = 'Shizuha Connect'

    def ready(self):
        import connect.signals  # noqa: F401
