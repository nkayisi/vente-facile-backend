"""
Le filet qui empêche un membre borné de naître sans entrepôt.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN MEMBRE SANS ENTREPÔT NE VOIT RIEN, ET C'EST VOULU.                       │
│                                                                              │
│ `.none()` est la réponse des trois surfaces depuis ce lot - un périmètre par │
│ défaut se ferme, il ne s'ouvre pas, surtout sur un téléphone qui se perd.    │
│ Mais un écran vide sans explication est une mauvaise façon de signaler une   │
│ configuration incomplète.                                                    │
│                                                                              │
│ `assign_default_warehouse` normalisait l'existant et n'avait AUCUN appelant. │
│ Ce signal ferme le robinet : le cas cesse de se créer, au lieu d'être        │
│ rattrapé à la main quand quelqu'un y pense.                                  │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ À LA CRÉATION SEULEMENT. Sur chaque écriture, on reposerait l'entrepôt
principal à un membre dont on vient délibérément de retirer sa dernière
affectation - et il serait impossible de le priver d'accès.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations.models import OrganizationMembership
from apps.organizations.services import assurer_entrepot_du_membre


@receiver(post_save, sender=OrganizationMembership, dispatch_uid='membre_entrepot_defaut')
def poser_entrepot_par_defaut(sender, instance, created, **kwargs):
    if not created:
        return
    assurer_entrepot_du_membre(instance)
