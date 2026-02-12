import uuid
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """Base model with created/updated timestamps."""
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """Base model with UUID primary key."""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TenantModel(TimeStampedModel, UUIDModel):
    """
    Base model for all tenant-scoped models.
    All models that belong to an organization should inherit from this.
    """
    
    organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.CASCADE,
        related_name='%(app_label)s_%(class)s_set',
        db_index=True
    )

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    """Base model with soft delete functionality."""
    
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=['is_deleted', 'deleted_at'])

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save(update_fields=['is_deleted', 'deleted_at'])


class TenantSoftDeleteModel(TenantModel, SoftDeleteModel):
    """Combined tenant-scoped model with soft delete."""
    
    class Meta:
        abstract = True
