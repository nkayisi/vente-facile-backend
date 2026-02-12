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
