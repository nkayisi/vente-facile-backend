from django.db import models


class TenantManager(models.Manager):
    """
    Manager that automatically filters by organization.
    Used with TenantModel subclasses.
    """
    
    def __init__(self, *args, **kwargs):
        self._organization = None
        super().__init__(*args, **kwargs)

    def for_organization(self, organization):
        """Filter queryset by organization."""
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
