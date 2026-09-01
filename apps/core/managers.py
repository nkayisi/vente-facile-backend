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
    """Combined manager for tenant-scoped soft-delete models."""
    
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

    def for_organization(self, organization):
        return self.get_queryset().filter(organization=organization)

    def with_deleted(self):
        return TenantManager.get_queryset(self)

    def only_deleted(self):
        return TenantManager.get_queryset(self).filter(is_deleted=True)
