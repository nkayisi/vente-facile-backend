from django.db import models


class TenantManager(models.Manager):
    """
    Manager pour les modèles multi-tenant.

    ⚠ ATTENTION ⚠ Ce manager ne filtre **pas** automatiquement sur
    ``organization``. Le filtrage par tenant est *opt-in* via
    ``.for_organization(org)`` ou via ``TenantViewSetMixin`` qui re-filtre dans
    ``get_queryset()`` à partir du header ``X-Organization-ID``.

    Conséquence - règle stricte à respecter :
    - **Dans les ViewSets DRF** : ``TenantViewSetMixin`` fait le filtrage.
      Aucune action requise.
    - **Hors ViewSets** (tâches Celery, commandes management, signals,
      admin Django, scripts, code interne) : **toujours utiliser
      ``Model.objects.for_organization(org)``**. Un ``Model.objects.all()``
      renverra des données toutes orgs confondues - fuite cross-tenant
      silencieuse.

    Pour un accès volontaire à toutes les organisations (ex. tâches de
    reporting plateforme), c'est explicite et conscient : ``.all()`` reste
    disponible mais le code devrait porter un commentaire ``# cross-tenant
    intentionnel`` pour signaler la levée du garde-fou.
    """

    def __init__(self, *args, **kwargs):
        self._organization = None
        super().__init__(*args, **kwargs)

    def for_organization(self, organization):
        """Filter queryset by organization. Voir docstring de la classe."""
        return self.get_queryset().filter(organization=organization)

    def get_queryset(self):
        return super().get_queryset()


class SoftDeleteManager(models.Manager):
    """Manager that excludes soft-deleted objects by default."""
    
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

    def with_deleted(self):
        """Include soft-deleted objects."""
        return super().get_queryset()

    def only_deleted(self):
        """Return only soft-deleted objects."""
        return super().get_queryset().filter(is_deleted=True)


class TenantSoftDeleteManager(TenantManager, SoftDeleteManager):
    """Combined manager for tenant-scoped soft-delete models.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `with_deleted()` NE RENDAIT PAS LES LIGNES SUPPRIMÉES.                  │
    │                                                                          │
    │ Elle appelait `TenantManager.get_queryset(self)`, dont le corps est un   │
    │ `super().get_queryset()`. Or ce `super()` se résout sur le MRO de        │
    │ l'INSTANCE, pas sur celui de la classe qui l'écrit : depuis une          │
    │ `TenantSoftDeleteManager`, il descend donc sur `SoftDeleteManager`, qui  │
    │ refiltre `is_deleted=False`. L'échappatoire refermait la porte qu'elle   │
    │ existait pour ouvrir.                                                    │
    │                                                                          │
    │ MESURÉ : une vente passée en `soft_delete()` donnait                     │
    │ `Sale.objects.with_deleted().count() == 0`. Aucun appelant ne s'en était │
    │ aperçu parce qu'il n'y en avait AUCUN - la méthode était morte, et c'est │
    │ la remise à zéro d'essai qui en est devenue le premier consommateur.     │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ `models.Manager.get_queryset(self)` NOMME la classe dont on veut le
    corps, et court-circuite la coopération de `super()`. C'est le seul moyen
    d'atteindre le queryset NU depuis un manager qui en hérite deux filtrants.
    """

    def get_queryset(self):
        # ⚠ AUCUN `filter(is_deleted=False)` ICI, ET LE MRO EST LA RAISON.
        #   `super()` atteint `TenantManager.get_queryset`, dont le corps est un
        #   `super()` qui descend sur `SoftDeleteManager.get_queryset` - lequel
        #   filtre DÉJÀ. Le filtre écrit ici émettait donc
        #   `is_deleted = false AND is_deleted = false` à chaque lecture de
        #   `Sale`, `Product`, `Category`… C'est le même mécanisme que l'encadré
        #   ci-dessus décrit pour `with_deleted`, pris par l'autre bout : là il
        #   refermait une porte, ici il la referme deux fois.
        return super().get_queryset()

    def for_organization(self, organization):
        return self.get_queryset().filter(organization=organization)

    def with_deleted(self):
        return models.Manager.get_queryset(self)

    def only_deleted(self):
        return models.Manager.get_queryset(self).filter(is_deleted=True)
