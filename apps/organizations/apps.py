from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.organizations'
    verbose_name = 'Organizations'

    def ready(self):
        # Le filet qui empêche un membre borné de naître sans entrepôt.
        from . import signals  # noqa: F401
