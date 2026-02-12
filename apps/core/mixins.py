from rest_framework import status
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from apps.organizations.models import Organization


class TenantQuerysetMixin:
    """
    Mixin for ViewSets that automatically filters queryset by organization.
    Requires X-Organization-ID header in requests.
    """
    
    def get_organization(self):
        """Get organization from header."""
        org_id = self.request.headers.get('X-Organization-ID')
        if not org_id:
            return None
        
        return get_object_or_404(
            Organization.objects.filter(
                memberships__user=self.request.user,
                memberships__is_active=True
            ),
            id=org_id
        )

    def get_queryset(self):
        """Filter queryset by organization."""
        queryset = super().get_queryset()
        organization = self.get_organization()
        
        if organization and hasattr(queryset.model, 'organization'):
            queryset = queryset.filter(organization=organization)
        
        return queryset

    def perform_create(self, serializer):
        """Automatically set organization on create."""
        organization = self.get_organization()
        if organization:
            serializer.save(organization=organization)
        else:
            serializer.save()


class BulkActionMixin:
    """Mixin for bulk operations on ViewSets."""
    
    def bulk_delete(self, request):
        """Bulk delete objects."""
        ids = request.data.get('ids', [])
        if not ids:
            return Response(
                {'error': 'No IDs provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(id__in=ids)
        count = queryset.count()
        
        if hasattr(queryset.model, 'soft_delete'):
            for obj in queryset:
                obj.soft_delete()
        else:
            queryset.delete()
        
        return Response({'deleted': count})

    def bulk_update(self, request):
        """Bulk update objects."""
        ids = request.data.get('ids', [])
        data = request.data.get('data', {})
        
        if not ids or not data:
            return Response(
                {'error': 'IDs and data required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(id__in=ids)
        count = queryset.update(**data)
        
        return Response({'updated': count})


class AuditMixin:
    """Mixin to track who created/modified objects."""
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
