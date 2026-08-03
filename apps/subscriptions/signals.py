"""
Invalidation du cache d'abonnement/quotas.

Le statut de blocage (chemin chaud du middleware) et le snapshot de quotas sont
cachés ~60s par organisation. On les purge immédiatement dès qu'un abonnement ou
un paiement change d'état (souscription, renouvellement, activation, expiration,
blocage) pour que l'accès soit à jour sans attendre l'expiration du TTL.
"""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import Subscription, SubscriptionPayment
from .services import SubscriptionService


def _invalidate(instance):
    org_id = getattr(instance, 'organization_id', None)
    if org_id:
        SubscriptionService.invalidate_org_cache(org_id)


@receiver([post_save, post_delete], sender=Subscription)
def _on_subscription_change(sender, instance, **kwargs):
    _invalidate(instance)


@receiver([post_save, post_delete], sender=SubscriptionPayment)
def _on_payment_change(sender, instance, **kwargs):
    _invalidate(instance)
