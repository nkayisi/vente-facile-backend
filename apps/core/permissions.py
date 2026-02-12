from rest_framework import permissions
from guardian.shortcuts import get_perms


class IsTenantMember(permissions.BasePermission):
    """
    Permission check for tenant membership.
    User must belong to the organization to access resources.
    """
    
    message = "You do not have access to this organization."

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        return request.user.memberships.filter(
            organization_id=org_id,
            is_active=True
        ).exists()


class IsTenantAdmin(permissions.BasePermission):
    """
    Permission check for tenant admin role.
    User must be an admin of the organization.
    """
    
    message = "You must be an organization admin to perform this action."

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        return request.user.memberships.filter(
            organization_id=org_id,
            is_active=True,
            role__in=['owner', 'admin']
        ).exists()


class IsTenantOwner(permissions.BasePermission):
    """
    Permission check for tenant owner role.
    User must be the owner of the organization.
    """
    
    message = "You must be the organization owner to perform this action."

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        return request.user.memberships.filter(
            organization_id=org_id,
            is_active=True,
            role='owner'
        ).exists()


class HasObjectPermission(permissions.BasePermission):
    """
    Object-level permission check using django-guardian.
    Checks if user has specific permission on the object.
    """
    
    def __init__(self, perm_name=None):
        self.perm_name = perm_name

    def has_object_permission(self, request, view, obj):
        if not request.user.is_authenticated:
            return False
        
        perm = self.perm_name or f'{view.action}_{obj._meta.model_name}'
        return perm in get_perms(request.user, obj)


class TenantObjectPermission(permissions.BasePermission):
    """
    Combined tenant membership and object permission check.
    """
    
    def has_object_permission(self, request, view, obj):
        if not request.user.is_authenticated:
            return False
        
        if not hasattr(obj, 'organization'):
            return True
        
        return request.user.memberships.filter(
            organization=obj.organization,
            is_active=True
        ).exists()
