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


#: Fermeture DÉLIBÉRÉE d'une action, à déclarer dans `action_permissions`.
#:
#: « Action non listée = accès refusé » est la bonne règle - une action oubliée
#: doit se fermer, pas s'ouvrir - mais elle n'émet aucun signal : la route
#: répond 403, le front n'affiche rien, et personne ne cherche un bug là où il
#: n'y a pas d'erreur. Le dépôt l'a payé sur `product_supplies`, sur
#: `locked_products` (qui rendait le verrou d'inventaire inerte et a fait
#: refuser une vente déjà encaissée et imprimée), et sur trois autres actions.
#:
#: Écrire `DENY` ne change RIEN au comportement : la valeur ne figure dans
#: aucune permission effective, donc l'action reste refusée. Ce qu'elle change
#: est la lecture : une fermeture voulue se distingue d'un oubli, et
#: `apps/sync/tests/test_parity_contract.py` peut exiger que toute action
#: routée soit l'une ou l'autre.
DENY = '!closed'


def _get_membership(request):
    """
    Membership actif de l'utilisateur pour l'organisation portée par l'en-tête.

    **Point d'entrée UNIQUE pour résoudre l'identité tenant d'une requête.**
    C'est important : le membership était résolu par trois chemins qui ne
    partageaient pas ce cache (`warehouse_scope.get_membership_for_request`,
    utilisé sur 37 sites, et `TenantViewSetMixin.get_organization`), si bien
    qu'une simple liste payait cinq résolutions de la même identité. Tout
    nouveau besoin doit passer par ici, jamais par une requête neuve.

    Le cache porte sur l'objet ``request``, donc sa durée de vie est celle de la
    requête HTTP : un changement d'appartenance est visible au prochain appel.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
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
    Ferme l'écriture quand l'abonnement ne la couvre plus.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN SEUL VERDICT POUR TOUTE LA PLATEFORME.                                │
    │                                                                          │
    │ Cette classe s'appuyait sur `get_active_subscription()`, dont la liste   │
    │ `[TRIAL, ACTIVE]` EXCLUT `PAST_DUE` - c'est-à-dire la période de grâce.  │
    │ Pendant ces jours-là, `/subscriptions/status/` annonçait « non bloqué,   │
    │ il vous reste N jours » et le bandeau du back-office le répétait,        │
    │ pendant que le moindre enregistrement était refusé. La grâce ne valait   │
    │ donc rien, et c'est l'écran qui mentait.                                 │
    │                                                                          │
    │ Le verdict vient désormais de `get_cached_block_state`, la MÊME source   │
    │ que l'endpoint de statut. Deux surfaces ne peuvent plus dire deux        │
    │ choses différentes du même abonnement.                                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ **Le cache n'est pas un confort, il est obligatoire.**
    `get_subscription_status` ÉCRIT en base (il bascule `PAST_DUE`/`EXPIRED`) :
    l'appeler directement ferait un `UPDATE` à chaque requête d'écriture.
    `get_cached_block_state` est écrit pour ce chemin chaud, caché ~60 s, et
    invalidé par les signaux de `Subscription` et `SubscriptionPayment`.

    ⚠ **`get_active_subscription()` n'est PAS modifiée** : sa liste décrit
    « un abonnement en cours », ce qui reste juste pour ses autres appelants
    (quotas). Elle n'a simplement jamais décrit le DROIT D'ÉCRIRE.
    """

    message = "Votre abonnement est inactif. Veuillez le renouveler."

    def has_permission(self, request, view):
        # Lire ses propres données ne se monnaie pas : un marchand impayé doit
        # pouvoir sortir son historique. C'est l'écriture qui se ferme. Ce
        # contrôle passe AVANT le garde de développement : la lecture ne doit
        # pas dépendre d'un réglage d'environnement.
        if request.method in permissions.SAFE_METHODS:
            return True

        # ⚠ En développement, la porte est ouverte - sinon il faudrait un
        # abonnement valide pour coder. `SUBSCRIPTION_ENFORCE_IN_DEBUG` permet
        # de la refermer sans toucher à `DEBUG`, qui emporterait aussi les
        # pages d'erreur et le rechargement : c'est le seul moyen de vérifier
        # le 402 de bout en bout sur un poste de développement.
        from django.conf import settings
        if settings.DEBUG and not getattr(
            settings, 'SUBSCRIPTION_ENFORCE_IN_DEBUG', False
        ):
            return True

        membership = _get_membership(request)
        if not membership:
            return False

        from apps.subscriptions.services import SubscriptionService
        etat = SubscriptionService.get_cached_block_state(membership.organization)
        if etat['is_blocked']:
            from apps.core.exceptions import AbonnementRequis
            raise AbonnementRequis(etat)
        return True


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

        # Fermeture délibérée : elle se lit dans la table, et se refuse ici.
        if required_perm == DENY:
            return False
        
        from apps.core.services import PermissionService
        effective_perms = PermissionService.get_effective_permissions(membership)
        
        # Supporte une permission unique ou une liste
        if isinstance(required_perm, (list, tuple)):
            return any(p in effective_perms for p in required_perm)
        
        return required_perm in effective_perms


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
