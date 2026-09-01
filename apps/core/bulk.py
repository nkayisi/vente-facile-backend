"""
Écrire en masse SANS disparaître du tirage.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `queryset.update()` NE PASSE PAS PAR `save()`, DONC PAR RIEN.               │
│                                                                              │
│ `TimeStampedModel.save` garantit qu'`updated_at` suit un                     │
│ `save(update_fields=[...])` : c'est le correctif du lot 6, et il tient. Mais │
│ `queryset.update()` court-circuite `save()` ENTIÈREMENT, `auto_now` compris. │
│ Une écriture en masse laisse donc l'horodatage où il était.                  │
│                                                                              │
│ Le tirage pagine sur un curseur `(updated_at, id)`. Une ligne dont           │
│ l'horodatage ne bouge pas est INVISIBLE au tirage, définitivement, sur tous  │
│ les terminaux à la fois. Ce n'est pas un retard qu'une synchronisation       │
│ rattrape : c'est une divergence permanente, et silencieuse.                  │
│                                                                              │
│ RELEVÉ SUR LA BASE DE DÉVELOPPEMENT, pas déduit : la fiche de fidélité d'une │
│ cliente portait 704,13 points côté serveur et 372,65 sur le terminal, avec   │
│ le MÊME `updated_at`, égal à sa date de création. Le comptoir lui refusait   │
│ la moitié de sa remise, et aucune synchronisation n'y pouvait rien.          │
│                                                                              │
│ La réserve était déjà écrite en tête de `apps/sync/pull.py` (« toute         │
│ écriture en masse doit toucher `updated_at` explicitement ») ; aucun des     │
│ six sites du dépôt ne le faisait. Une règle qu'il faut se rappeler à chaque  │
│ appel finit par être oubliée : ce module la rend impossible à oublier.       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.utils import timezone


def bulk_update_rows(queryset, **champs) -> int:
    """
    `queryset.update()`, l'horodatage en plus.

    À employer partout où le modèle descend au tirage. Rend le nombre de lignes
    touchées, exactement comme `update()`.

    Deux horodatages, pour deux mécanismes distincts :

    - **`updated_at`** porte le curseur du tirage. C'est lui qui rend la ligne
      visible aux terminaux.
    - **`sync_updated_at`** sert la résolution de conflit et n'est posé que par
      `SyncableModel.save`. Le laisser en arrière ferait passer une écriture
      serveur pour plus ancienne qu'une écriture client qu'elle suit pourtant.

    Un appelant qui pose lui-même l'un des deux garde la main : on ne l'écrase
    pas, une reprise de données pouvant vouloir un horodatage choisi.
    """
    maintenant = timezone.now()
    modele = queryset.model
    noms = {f.name for f in modele._meta.get_fields() if hasattr(f, 'attname')}

    if 'updated_at' in noms:
        champs.setdefault('updated_at', maintenant)
    if 'sync_updated_at' in noms:
        champs.setdefault('sync_updated_at', maintenant)

    return queryset.update(**champs)
