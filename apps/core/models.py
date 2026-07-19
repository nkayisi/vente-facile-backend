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


class SyncableModel(models.Model):
    """
    Base mixin for models that need to be synchronized with WatermelonDB.
    
    Key features:
    - `sync_updated_at`: Manually settable timestamp for sync conflict resolution
    - `server_created_at`: Server-side creation timestamp (auto)
    - Methods for sync operations
    
    Note: This mixin should be used alongside TenantSoftDeleteModel for full sync support.
    The `created_at`, `updated_at`, `deleted_at` fields come from parent classes.
    """
    
    # Separate field for sync timestamp that can be set manually during push
    # This allows conflict resolution based on client timestamps
    sync_updated_at = models.DateTimeField(
        db_index=True,
        null=True,
        blank=True,
        help_text="Timestamp used for sync conflict resolution. Can be set by client."
    )
    
    # Track if this record was created via sync (from mobile)
    synced_from_client = models.BooleanField(
        default=False,
        help_text="True if this record was created/updated via sync from mobile client"
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        """
        Rafraîchit ``sync_updated_at`` à chaque écriture serveur (sémantique
        proche de ``auto_now``).

        Indispensable au pull delta : sans ça, une édition web d'un
        enregistrement déjà synchronisé (donc ``sync_updated_at`` non-null mais
        ancien) ne serait jamais renvoyée au mobile, car le filtre delta
        ``sync_updated_at__gt`` ne la verrait pas. Le push de sync place de
        toute façon ce champ juste avant ``save()`` — la valeur reste cohérente.

        La résolution de conflit (``should_accept_sync_update``) lit la valeur
        STOCKÉE avant l'écriture, donc ce rafraîchissement n'interfère pas.
        """
        self.sync_updated_at = timezone.now()
        super().save(*args, **kwargs)

    def mark_synced(self, client_timestamp=None):
        """
        Mark this record as synced with the given client timestamp.
        
        Args:
            client_timestamp: The timestamp from the client. If None, uses current server time.
        """
        self.sync_updated_at = client_timestamp or timezone.now()
        self.synced_from_client = True
        self.save(update_fields=['sync_updated_at', 'synced_from_client', 'updated_at'])

    def should_accept_sync_update(self, incoming_timestamp):
        """
        Determine if an incoming sync update should be accepted based on timestamps.
        
        Conflict resolution rule: Last-Write-Wins based on sync_updated_at.
        
        Args:
            incoming_timestamp: The timestamp from the incoming sync data.
            
        Returns:
            bool: True if the update should be accepted, False otherwise.
        """
        if self.sync_updated_at is None:
            return True
        return incoming_timestamp > self.sync_updated_at


class TenantSyncableModel(TenantSoftDeleteModel, SyncableModel):
    """
    Combined model for tenant-scoped, soft-deletable, syncable records.
    
    This is the base class for all models that need to be synchronized
    with WatermelonDB mobile clients.
    
    Inherits:
    - TenantModel: organization scoping, created_at, updated_at, UUID pk
    - SoftDeleteModel: is_deleted, deleted_at, soft_delete(), restore()
    - SyncableModel: sync_updated_at, synced_from_client, sync methods
    """
    
    class Meta:
        abstract = True
