"""
Permissions DRF personnalisées pour le multi-tenant.

Système de permissions basé sur les rôles :
- owner (Admin) : toutes les permissions
- manager (Gérant) : gestion complète sauf abonnement/paramètres
- stock_keeper (Magasinier) : stock, inventaire, réceptions
- cashier (Caissier) : ventes, consultation produits/prix
"""
from functools import wraps
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework import status as http_status


def _get_membership(request):
    """Helper : récupère le membership de l'utilisateur pour l'organisation courante."""
    if not request.user.is_authenticated:
        return None
    org_id = request.headers.get('X-Organization-ID')
    if not org_id:
        return None
    # Cache le membership sur la request pour éviter les requêtes répétées
    cache_key = f'_membership_{org_id}'
    if not hasattr(request, cache_key):
        membership = request.user.memberships.filter(
            organization_id=org_id,
            is_active=True
        ).select_related('organization').first()
        setattr(request, cache_key, membership)
    return getattr(request, cache_key)


def has_perm_code(request, perm_code: str) -> bool:
    """
    Helper utilitaire : vérifie si l'utilisateur courant possède une permission
    (rôle + extra_permissions) pour l'organisation portée par X-Organization-ID.

    Utilisable dans les ViewSets pour des branches de logique conditionnelles
    (ex : filtrer le queryset différemment selon `sales.view_all`).
    """
    membership = _get_membership(request)
    if not membership:
        return False
    from apps.core.services import PermissionService
    return perm_code in PermissionService.get_effective_permissions(membership)


def is_manager_or_above(request) -> bool:
    """Helper : True si l'utilisateur est manager ou owner dans l'org courante."""
    membership = _get_membership(request)
    return membership is not None and membership.role in ('owner', 'manager')


class IsTenantMember(permissions.BasePermission):
    """
    Vérifie que l'utilisateur appartient à l'organisation demandée.
    L'organisation est identifiée via le header X-Organization-ID.
    """
    message = "Vous n'avez pas accès à cette organisation."

    def has_permission(self, request, view):
        return _get_membership(request) is not None


class IsTenantAdmin(permissions.BasePermission):
    """
    Vérifie que l'utilisateur est admin (owner) de l'organisation.
    """
    message = "Vous devez être administrateur pour effectuer cette action."

    def has_permission(self, request, view):
        membership = _get_membership(request)
        return membership is not None and membership.role == 'owner'


class IsTenantOwner(permissions.BasePermission):
    """
    Alias de IsTenantAdmin (owner = admin dans notre système).
    """
    message = "Vous devez être propriétaire pour effectuer cette action."

    def has_permission(self, request, view):
        membership = _get_membership(request)
        return membership is not None and membership.role == 'owner'


class IsTenantManager(permissions.BasePermission):
    """
    Vérifie que l'utilisateur est au moins gérant (owner ou manager).
    """
    message = "Vous devez être gérant ou administrateur pour effectuer cette action."

    def has_permission(self, request, view):
        membership = _get_membership(request)
        return membership is not None and membership.role in ['owner', 'manager']


class HasActiveSubscription(permissions.BasePermission):
    """
    Vérifie que l'organisation a un abonnement actif.
    Bloque les opérations d'écriture si l'abonnement est inactif.
    """
    message = "Votre abonnement est inactif. Veuillez le renouveler."

    def has_permission(self, request, view):
        from django.conf import settings
        if settings.DEBUG:
            return True
            
        if request.method in permissions.SAFE_METHODS:
            return True
        
        membership = _get_membership(request)
        if not membership:
            return False
        
        subscription = membership.organization.get_active_subscription()
        return subscription is not None and subscription.is_active


class TenantObjectPermission(permissions.BasePermission):
    """
    Permission object-level : vérifie que l'objet appartient à l'organisation.
    """
    
    def has_object_permission(self, request, view, obj):
        if not hasattr(obj, 'organization'):
            return True
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return False
        
        return str(obj.organization_id) == org_id


class HasPermission(permissions.BasePermission):
    """
    Permission granulaire basée sur le système de permissions par rôle.
    
    Utilisation dans un ViewSet via l'attribut `action_permissions` :
    
        action_permissions = {
            'list': 'products.view',
            'create': 'products.create',
            'update': 'products.edit',
            'partial_update': 'products.edit',
            'destroy': 'products.delete',
            # Actions custom
            'approve': 'stock_adjustments.approve',
        }
    
    Si une action n'est pas dans le dict, l'accès est refusé par défaut.
    Utiliser '*' comme valeur pour autoriser tous les membres.
    """
    message = "Vous n'avez pas la permission d'effectuer cette action."

    def has_permission(self, request, view):
        membership = _get_membership(request)
        if not membership:
            return False
        
        action_permissions = getattr(view, 'action_permissions', None)
        if not action_permissions:
            return True
        
        action = getattr(view, 'action', None)
        if not action:
            return True
        
        required_perm = action_permissions.get(action)
        if required_perm is None:
            # Action non listée = accès refusé
            return False
        
        if required_perm == '*':
            return True
        
        from apps.core.services import PermissionService
        effective_perms = PermissionService.get_effective_permissions(membership)
        
        # Supporte une permission unique ou une liste
        if isinstance(required_perm, (list, tuple)):
            return any(p in effective_perms for p in required_perm)
        
        return required_perm in effective_perms


class RoleBasedPermission(permissions.BasePermission):
    """
    Permission basée sur le rôle de l'utilisateur dans l'organisation.
    Configurable par ViewSet via l'attribut `role_permissions`.
    
    Exemple d'utilisation dans un ViewSet:
        role_permissions = {
            'list': ['owner', 'manager', 'cashier'],
            'create': ['owner', 'manager'],
            'destroy': ['owner'],
        }
    """
    
    def has_permission(self, request, view):
        membership = _get_membership(request)
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


def require_permission(*perms):
    """
    Décorateur pour les actions custom de ViewSet.
    Vérifie que l'utilisateur a la permission requise.
    
    Usage:
        @action(detail=True, methods=['post'])
        @require_permission('inventory.validate')
        def validate(self, request, pk=None):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(self, request, *args, **kwargs):
            membership = _get_membership(request)
            if not membership:
                return Response(
                    {'detail': "Vous n'avez pas accès à cette organisation."},
                    status=http_status.HTTP_403_FORBIDDEN
                )
            
            from apps.core.services import PermissionService
            effective_perms = PermissionService.get_effective_permissions(membership)
            
            if not any(p in effective_perms for p in perms):
                return Response(
                    {'detail': "Vous n'avez pas la permission d'effectuer cette action."},
                    status=http_status.HTTP_403_FORBIDDEN
                )
            
            return func(self, request, *args, **kwargs)
        return wrapper
    return decorator
