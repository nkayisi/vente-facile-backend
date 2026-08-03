from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.subscriptions'
    verbose_name = 'Subscriptions'

    def ready(self):
        # Enregistre les signals d'invalidation du cache d'abonnement/quotas.
        from . import signals  # noqa: F401
