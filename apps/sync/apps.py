from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.sync'
    verbose_name = 'Synchronisation WatermelonDB'

    def ready(self):
        # Charge les gestionnaires d'operations : ils s'enregistrent par
        # decorateur, donc sans import le registre resterait vide et chaque
        # envoi repondrait « operation inconnue ».
        from . import handlers  # noqa: F401
