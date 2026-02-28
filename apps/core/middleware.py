import threading
from django.utils.deprecation import MiddlewareMixin

_thread_locals = threading.local()


def get_current_organization():
    """Get the current organization from thread local storage."""
    return getattr(_thread_locals, 'organization', None)


def get_current_user():
    """Get the current user from thread local storage."""
    return getattr(_thread_locals, 'user', None)


class TenantMiddleware(MiddlewareMixin):
    """
    Middleware that sets the current organization in thread local storage.
    This allows automatic tenant filtering in managers and querysets.
    """
    
    def process_request(self, request):
        _thread_locals.user = getattr(request, 'user', None)
        _thread_locals.organization = None
        
        if hasattr(request, 'user') and request.user.is_authenticated:
            if hasattr(request.user, 'active_organization'):
                _thread_locals.organization = request.user.active_organization

    def process_response(self, request, response):
        if hasattr(_thread_locals, 'organization'):
            del _thread_locals.organization
        if hasattr(_thread_locals, 'user'):
            del _thread_locals.user
        return response


class OrganizationHeaderMiddleware(MiddlewareMixin):
    """
    Middleware that reads organization from X-Organization-ID header.
    Useful for API requests where user may belong to multiple organizations.
    """
    
    def process_request(self, request):
        org_id = request.headers.get('X-Organization-ID')
        if org_id:
            request.organization_id = org_id


class SubscriptionMiddleware(MiddlewareMixin):
    """
    Middleware qui vérifie que l'organisation a un abonnement actif.
    
    Comportement inspiré de Netflix/Spotify :
    - Les requêtes de LECTURE (GET, HEAD, OPTIONS) sont toujours autorisées
      → L'utilisateur peut voir le contenu de l'application
    - Les requêtes d'ÉCRITURE (POST, PUT, PATCH, DELETE) sont bloquées
      → L'utilisateur ne peut pas effectuer d'actions
    
    Retourne 402 Payment Required pour permettre au frontend d'afficher
    un modal de renouvellement au lieu de bloquer complètement.
    """

    # Chemins exemptés du contrôle d'abonnement (même pour les écritures)
    EXEMPT_PATHS = [
        '/api/v1/auth/',
        '/api/v1/users/me/',
        '/api/v1/organizations/',
        '/api/v1/subscriptions/',
        '/admin/',
        '/api/schema/',
        '/api/docs/',
        '/api/redoc/',
    ]

    # Méthodes HTTP considérées comme "lecture seule"
    SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

    def process_request(self, request):
        # Ne vérifier que les requêtes API authentifiées
        if not request.path.startswith('/api/v1/'):
            return None

        # Chemins exemptés
        for exempt in self.EXEMPT_PATHS:
            if request.path.startswith(exempt):
                return None

        # L'utilisateur doit être authentifié
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return None

        # Superusers non bloqués
        if request.user.is_superuser:
            return None

        # Récupérer l'organisation depuis le header
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return None

        from apps.organizations.models import Organization
        try:
            organization = Organization.objects.get(id=org_id, is_deleted=False)
        except (Organization.DoesNotExist, ValueError):
            return None

        from apps.subscriptions.services import SubscriptionService
        status = SubscriptionService.get_subscription_status(organization)

        if status['is_blocked']:
            # Permettre les requêtes de lecture (GET, HEAD, OPTIONS)
            # L'utilisateur peut voir le contenu mais pas agir
            if request.method in self.SAFE_METHODS:
                # Ajouter un header pour informer le frontend du statut
                request._subscription_blocked = True
                request._subscription_status = status
                return None

            # Bloquer les requêtes d'écriture avec 402 Payment Required
            from django.http import JsonResponse
            return JsonResponse(
                {
                    'detail': status['message'],
                    'subscription_status': status['status'],
                    'is_blocked': True,
                    'days_remaining': status.get('days_remaining', 0),
                    'code': 'subscription_required',
                },
                status=402,  # Payment Required
            )

    def process_response(self, request, response):
        # Ajouter un header X-Subscription-Status si l'abonnement est bloqué
        if getattr(request, '_subscription_blocked', False):
            status = getattr(request, '_subscription_status', {})
            response['X-Subscription-Status'] = status.get('status', 'expired')
            response['X-Subscription-Blocked'] = 'true'
        return response
