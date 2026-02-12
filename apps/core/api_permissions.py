"""
Permissions DRF personnalisées pour le multi-tenant et django-guardian.
"""
from rest_framework import permissions
from guardian.shortcuts import get_perms, assign_perm


class IsTenantMember(permissions.BasePermission):
    """
    Vérifie que l'utilisateur appartient à l'organisation demandée.
    L'organisation est identifiée via le header X-Organization-ID.
    """
    message = "Vous n'avez pas accès à cette organisation."

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
    Vérifie que l'utilisateur est admin ou owner de l'organisation.
    """
    message = "Vous devez être administrateur pour effectuer cette action."

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
    Vérifie que l'utilisateur est owner de l'organisation.
    """
    message = "Vous devez être propriétaire pour effectuer cette action."

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


class HasActiveSubscription(permissions.BasePermission):
    """
    Vérifie que l'organisation a un abonnement actif.
    Bloque les opérations d'écriture si l'abonnement est inactif.
    """
    message = "Votre abonnement est inactif. Veuillez le renouveler."

    def has_permission(self, request, view):
        # En mode DEBUG, on désactive la vérification d'abonnement
        from django.conf import settings
        if settings.DEBUG:
            return True
            
        if request.method in permissions.SAFE_METHODS:
            return True
        
        if not request.user.is_authenticated:
            return False
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        from apps.organizations.models import Organization
        try:
            organization = Organization.objects.get(id=org_id)
            subscription = organization.get_active_subscription()
            return subscription is not None and subscription.is_active
        except Organization.DoesNotExist:
            return False


class TenantObjectPermission(permissions.BasePermission):
    """
    Permission object-level : vérifie que l'objet appartient à l'organisation
    de l'utilisateur et qu'il a les permissions guardian nécessaires.
    """
    
    def has_object_permission(self, request, view, obj):
        if not request.user.is_authenticated:
            return False
        
        if not hasattr(obj, 'organization'):
            return True
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        if str(obj.organization_id) != org_id:
            return False
        
        return request.user.memberships.filter(
            organization_id=org_id,
            is_active=True
        ).exists()


class GuardianPermission(permissions.BasePermission):
    """
    Permission basée sur django-guardian.
    Vérifie les permissions object-level définies dans guardian.
    """
    
    perms_map = {
        'GET': ['view_%(model_name)s'],
        'OPTIONS': [],
        'HEAD': [],
        'POST': ['add_%(model_name)s'],
        'PUT': ['change_%(model_name)s'],
        'PATCH': ['change_%(model_name)s'],
        'DELETE': ['delete_%(model_name)s'],
    }

    def get_required_permissions(self, method, model_cls):
        """Retourne les permissions requises pour la méthode HTTP."""
        kwargs = {
            'model_name': model_cls._meta.model_name
        }
        return [perm % kwargs for perm in self.perms_map.get(method, [])]

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        if hasattr(view, 'get_queryset'):
            queryset = view.get_queryset()
            model_cls = queryset.model
            perms = self.get_required_permissions(request.method, model_cls)
            
            return all(
                request.user.has_perm(perm)
                for perm in perms
            )
        
        return True

    def has_object_permission(self, request, view, obj):
        if not request.user.is_authenticated:
            return False
        
        model_cls = obj.__class__
        perms = self.get_required_permissions(request.method, model_cls)
        
        user_perms = get_perms(request.user, obj)
        return all(perm.split('.')[-1] in user_perms for perm in perms)


class RoleBasedPermission(permissions.BasePermission):
    """
    Permission basée sur le rôle de l'utilisateur dans l'organisation.
    Configurable par ViewSet via l'attribut `role_permissions`.
    
    Exemple d'utilisation dans un ViewSet:
        role_permissions = {
            'list': ['owner', 'admin', 'manager', 'cashier'],
            'create': ['owner', 'admin', 'manager'],
            'update': ['owner', 'admin', 'manager'],
            'destroy': ['owner', 'admin'],
        }
    """
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        membership = request.user.memberships.filter(
            organization_id=org_id,
            is_active=True
        ).first()
        
        if not membership:
            return False
        
        role_permissions = getattr(view, 'role_permissions', None)
        if not role_permissions:
            return True
        
        action = getattr(view, 'action', None)
        if not action:
            return True
        
        allowed_roles = role_permissions.get(action, [])
        if not allowed_roles:
            return True
        
        return membership.role in allowed_roles


def assign_object_permissions(user, obj, permissions_list=None):
    """
    Attribue les permissions guardian sur un objet.
    
    Args:
        user: L'utilisateur qui reçoit les permissions
        obj: L'objet sur lequel attribuer les permissions
        permissions_list: Liste des permissions (si None, attribue view/change/delete)
    """
    if permissions_list is None:
        model_name = obj._meta.model_name
        permissions_list = [
            f'view_{model_name}',
            f'change_{model_name}',
            f'delete_{model_name}',
        ]
    
    for perm in permissions_list:
        assign_perm(perm, user, obj)
