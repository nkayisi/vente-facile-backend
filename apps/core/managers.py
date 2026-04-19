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


class SyncableManager(TenantSoftDeleteManager):
    """
    Manager for syncable models with delta sync support.
    
    Provides methods to retrieve changes since a given timestamp,
    formatted for WatermelonDB sync protocol.
    """
    
    def changes_since(self, organization, last_pulled_at, select_related_fields=None):
        """
        Get all changes since the given timestamp for sync.
        
        Args:
            organization: The organization to filter by
            last_pulled_at: datetime - Only return records changed after this time
            select_related_fields: Optional list of related fields to prefetch
            
        Returns:
            dict with 'created', 'updated', 'deleted' querysets
        """
        from django.db.models import Q
        
        # Base queryset including deleted records for sync
        base_qs = self.with_deleted().filter(organization=organization)
        
        if select_related_fields:
            base_qs = base_qs.select_related(*select_related_fields)
        
        if last_pulled_at is None:
            # First sync - return all non-deleted records as created
            created = base_qs.filter(is_deleted=False)
            updated = base_qs.none()
            deleted = base_qs.none()
        else:
            # Delta sync - categorize by change type
            # Use sync_updated_at for sync queries, fallback to updated_at
            time_filter = Q(sync_updated_at__gt=last_pulled_at) | (
                Q(sync_updated_at__isnull=True) & Q(updated_at__gt=last_pulled_at)
            )
            
            # Created: records created after last_pulled_at and not deleted
            created = base_qs.filter(
                created_at__gt=last_pulled_at,
                is_deleted=False
            )
            
            # Updated: records updated after last_pulled_at, created before, not deleted
            updated = base_qs.filter(
                time_filter,
                created_at__lte=last_pulled_at,
                is_deleted=False
            )
            
            # Deleted: records deleted after last_pulled_at
            deleted = base_qs.filter(
                deleted_at__gt=last_pulled_at,
                is_deleted=True
            )
        
        return {
            'created': created,
            'updated': updated,
            'deleted': deleted,
        }
    
    def for_sync(self, organization):
        """
        Get all records for initial sync (first pull).
        
        Args:
            organization: The organization to filter by
            
        Returns:
            QuerySet of all non-deleted records for the organization
        """
        return self.for_organization(organization).filter(is_deleted=False)
    
    def get_by_ids(self, organization, ids):
        """
        Get records by their IDs for a specific organization.
        Useful for validating incoming sync data.
        
        Args:
            organization: The organization to filter by
            ids: List of UUIDs to retrieve
            
        Returns:
            QuerySet of matching records (including deleted)
        """
        return self.with_deleted().filter(
            organization=organization,
            id__in=ids
        )
    
    def bulk_sync_create(self, records, organization):
        """
        Bulk create records from sync data.
        
        Args:
            records: List of model instances to create
            organization: The organization these records belong to
            
        Returns:
            List of created instances
        """
        for record in records:
            record.organization = organization
            record.synced_from_client = True
        
        return self.bulk_create(records, ignore_conflicts=True)
    
    def bulk_sync_update(self, records, fields):
        """
        Bulk update records from sync data.
        
        Args:
            records: List of model instances to update
            fields: List of field names to update
            
        Returns:
            Number of updated records
        """
        # Ensure sync fields are included
        update_fields = list(set(fields) | {'sync_updated_at', 'synced_from_client', 'updated_at'})
        return self.bulk_update(records, update_fields)
