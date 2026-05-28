"""
Views for WatermelonDB sync API.
"""
import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiExample
from drf_spectacular.types import OpenApiTypes

from apps.core.api_permissions import HasPermission
from apps.organizations.models import Organization
from .services import SyncService
from .constants import SYNCABLE_MODELS_MAP

logger = logging.getLogger(__name__)


def get_organization_from_request(request):
    """
    Retourne l'organisation associée au header ``X-Organization-ID``, après
    avoir vérifié que ``request.user`` est membre actif de cette organisation.

    Retourne ``None`` si :
    - le header est absent ;
    - l'UUID est invalide ;
    - l'utilisateur n'est pas membre actif de l'organisation.

    Vérifier la membership ici est critique : sans ce filtre, un utilisateur
    authentifié peut tirer ou pousser les données de n'importe quelle org
    en spoofant simplement le header (fuite/écrasement cross-tenant).
    """
    org_id = request.headers.get('X-Organization-ID')
    if not org_id:
        return None
    try:
        return Organization.objects.filter(
            memberships__user=request.user,
            memberships__is_active=True,
            is_deleted=False,
        ).distinct().get(id=org_id)
    except (Organization.DoesNotExist, ValueError):
        return None


class SyncView(APIView):
    """
    WatermelonDB sync endpoint.
    
    GET: Pull changes since last_pulled_at
    POST: Push changes and optionally pull updates
    """
    
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="Pull sync changes",
        description="""
        Pull changes from the server since the last sync.
        
        For initial sync, omit last_pulled_at or set it to 0.
        The response follows WatermelonDB sync protocol format.
        """,
        parameters=[
            OpenApiParameter(
                name='last_pulled_at',
                type=OpenApiTypes.INT64,
                location=OpenApiParameter.QUERY,
                description='Unix timestamp in milliseconds of last pull. 0 or omit for initial sync.',
                required=False,
            ),
            OpenApiParameter(
                name='tables',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description='Comma-separated list of tables to sync. Omit for all tables.',
                required=False,
            ),
        ],
        responses={
            200: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample(
                'Initial sync response',
                value={
                    'changes': {
                        'products': {
                            'created': [{'id': '...', 'name': 'Product 1'}],
                            'updated': [],
                            'deleted': [],
                        },
                        'customers': {
                            'created': [{'id': '...', 'name': 'Customer 1'}],
                            'updated': [],
                            'deleted': [],
                        },
                    },
                    'timestamp': 1705315800000,
                    'schema_version': 1,
                },
            ),
        ],
    )
    def get(self, request):
        """Pull changes from server."""
        organization = get_organization_from_request(request)
        if not organization:
            return Response(
                {'error': 'Organisation manquante ou accès refusé'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Parse parameters
        last_pulled_at = request.query_params.get('last_pulled_at', 0)
        tables_param = request.query_params.get('tables', '')
        
        tables = None
        if tables_param:
            tables = [t.strip() for t in tables_param.split(',') if t.strip()]
            # Validate table names
            invalid_tables = [t for t in tables if t not in SYNCABLE_MODELS_MAP]
            if invalid_tables:
                return Response(
                    {'error': f'Invalid tables: {", ".join(invalid_tables)}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        try:
            service = SyncService(organization, request.user)
            result = service.pull(last_pulled_at, tables)
            
            logger.info(
                f"Sync pull for org {organization.id} by user {request.user.id}: "
                f"last_pulled_at={last_pulled_at}, tables={tables}"
            )
            
            return Response(result)
            
        except Exception as e:
            logger.exception(f"Sync pull error: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @extend_schema(
        summary="Push sync changes",
        description="""
        Push local changes to the server and optionally pull updates.
        
        The request body should contain:
        - changes: Object with table names as keys, each containing created/updated/deleted arrays
        - last_pulled_at: Timestamp of last pull (for conflict detection)
        
        The response includes push stats and optionally pull changes.
        """,
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample(
                'Push request',
                value={
                    'changes': {
                        'sales': {
                            'created': [
                                {
                                    'id': 'uuid-here',
                                    'reference': 'VT-20240115-0001',
                                    'customer_id': 'customer-uuid',
                                    'total': '15000.00',
                                    'status': 'completed',
                                }
                            ],
                            'updated': [],
                            'deleted': [],
                        },
                    },
                    'last_pulled_at': 1705315800000,
                },
            ),
            OpenApiExample(
                'Push response',
                value={
                    'push': {
                        'success': True,
                        'stats': {
                            'created': 1,
                            'updated': 0,
                            'deleted': 0,
                            'conflicts': 0,
                            'errors': 0,
                        },
                        'errors': [],
                        'timestamp': 1705315900000,
                    },
                    'pull': {
                        'changes': {},
                        'timestamp': 1705315900000,
                        'schema_version': 1,
                    },
                },
            ),
        ],
    )
    def post(self, request):
        """Push changes to server."""
        organization = get_organization_from_request(request)
        if not organization:
            return Response(
                {'error': 'Organisation manquante ou accès refusé'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Parse request body
        changes = request.data.get('changes', {})
        last_pulled_at = request.data.get('last_pulled_at', 0)
        pull_after_push = request.data.get('pull_after_push', True)
        tables_param = request.data.get('tables')
        
        tables = None
        if tables_param:
            if isinstance(tables_param, str):
                tables = [t.strip() for t in tables_param.split(',') if t.strip()]
            elif isinstance(tables_param, list):
                tables = tables_param
        
        if not changes:
            return Response(
                {'error': 'No changes provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = SyncService(organization, request.user)
            
            # Push changes
            push_result = service.push(changes, last_pulled_at)
            
            result = {
                'push': push_result,
            }
            
            # Optionally pull updates after push
            if pull_after_push:
                # Use the timestamp from push result for pull
                pull_timestamp = push_result.get('timestamp', last_pulled_at)
                result['pull'] = service.pull(last_pulled_at, tables)
            
            # Log sync operation
            logger.info(
                f"Sync push for org {organization.id} by user {request.user.id}: "
                f"stats={push_result.get('stats')}"
            )
            
            return Response(result)
            
        except Exception as e:
            logger.exception(f"Sync push error: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SyncStatusView(APIView):
    """
    Get sync status and available tables.
    """
    
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="Get sync status",
        description="Returns available sync tables and their record counts.",
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        """Get sync status."""
        organization = get_organization_from_request(request)
        if not organization:
            return Response(
                {'error': 'Organisation manquante ou accès refusé'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        from django.apps import apps
        from .constants import SYNCABLE_MODELS, SCHEMA_VERSION
        
        tables = {}
        for table_name, app_label, model_name, _ in SYNCABLE_MODELS:
            try:
                model = apps.get_model(app_label, model_name)
                
                # Count records
                if hasattr(model, 'organization'):
                    count = model.objects.filter(
                        organization=organization,
                        is_deleted=False
                    ).count() if hasattr(model, 'is_deleted') else model.objects.filter(
                        organization=organization
                    ).count()
                else:
                    count = model.objects.count()
                
                tables[table_name] = {
                    'count': count,
                    'model': f"{app_label}.{model_name}",
                }
            except Exception as e:
                tables[table_name] = {
                    'count': 0,
                    'error': str(e),
                }
        
        return Response({
            'schema_version': SCHEMA_VERSION,
            'tables': tables,
            'organization_id': str(organization.id),
        })
