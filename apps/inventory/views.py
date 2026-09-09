"""
ViewSets DRF pour l'app Inventory.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db import models as db_models
from django.db.models import Sum, F, Q
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from apps.core.api_mixins import (
    TenantViewSetMixin, AuditMixin, WarehouseScopedQuerysetMixin, ExportResponseMixin,
)
from rest_framework.exceptions import ValidationError as DRFValidationError
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_warehouse_allowed_for_request,
    filter_queryset_by_warehouse_ids,
    filter_stock_transfer_queryset,
    get_membership_for_request,
)
from apps.core.api_permissions import (
    DENY,
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from apps.subscriptions.services import SubscriptionService
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem,
    InventorySession, InventoryCount, STOCK_IN_MOVEMENT_TYPES
)
from .filters import StockFilter, StockMovementFilter
from .report_params import build_export_context
from .serializers import (
    WarehouseListSerializer, WarehouseDetailSerializer, WarehouseCreateSerializer,
    StockLocationSerializer,
    StockListSerializer, StockDetailSerializer,
    StockBatchSerializer,
    StockMovementListSerializer, StockMovementDetailSerializer, StockMovementCreateSerializer,
    StockTransferListSerializer, StockTransferDetailSerializer, StockTransferCreateSerializer,
    StockAdjustmentListSerializer, StockAdjustmentDetailSerializer, StockAdjustmentCreateSerializer,
    InventorySessionListSerializer, InventorySessionDetailSerializer, InventorySessionCreateSerializer,
    InventoryCountSerializer
)


# =============================================================================
# WAREHOUSE VIEWSET
# =============================================================================

class WarehouseViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des entrepôts.
    
    Endpoints:
    - GET /warehouses/ : Liste des entrepôts
    - POST /warehouses/ : Créer un entrepôt
    - GET /warehouses/{id}/ : Détail d'un entrepôt
    - PUT/PATCH /warehouses/{id}/ : Modifier un entrepôt
    - DELETE /warehouses/{id}/ : Supprimer un entrepôt (soft delete)
    - GET /warehouses/{id}/stock-summary/ : Résumé du stock
    """
    
    queryset = Warehouse.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'branch']
    search_fields = ['name', 'code']
    ordering = ['name']

    warehouse_scope_field = 'id'
    
    select_related_fields = ['branch', 'manager']
    prefetch_related_fields = ['locations']
    
    action_permissions = {
        'list': 'warehouses.view',
        'retrieve': 'warehouses.view',
        'create': 'warehouses.create',
        'update': 'warehouses.edit',
        'partial_update': 'warehouses.edit',
        'destroy': 'warehouses.delete',
        'stock_summary': 'stock.view',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return WarehouseListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return WarehouseCreateSerializer
        return WarehouseDetailSerializer

    def perform_create(self, serializer):
        organization = self.get_organization()
        SubscriptionService.assert_can_add_warehouse(organization)
        return super().perform_create(serializer)

    @action(detail=True, methods=['get'], url_path='stock-summary')
    def stock_summary(self, request, pk=None):
        """Retourne un résumé du stock de l'entrepôt."""
        warehouse = self.get_object()
        
        stocks = Stock.objects.filter(warehouse=warehouse).select_related('product')
        
        summary = {
            'total_products': stocks.values('product').distinct().count(),
            'total_quantity': stocks.aggregate(total=Sum('quantity'))['total'] or 0,
            'total_value': str(sum(
                s.quantity * (s.avg_cost if s.avg_cost > 0 else (s.product.cost_price or 0))
                for s in stocks
            )),
            'low_stock_count': stocks.filter(
                quantity__lte=F('product__reorder_point')
            ).count(),
            'out_of_stock_count': stocks.filter(quantity__lte=0).count()
        }
        
        return Response(summary)


# =============================================================================
# STOCK LOCATION VIEWSET
# =============================================================================

class StockLocationViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des emplacements de stock.
    
    Endpoints:
    - GET /stock-locations/ : Liste des emplacements
    - POST /stock-locations/ : Créer un emplacement
    - GET /stock-locations/{id}/ : Détail d'un emplacement
    - PUT/PATCH /stock-locations/{id}/ : Modifier un emplacement
    - DELETE /stock-locations/{id}/ : Supprimer un emplacement
    - GET /stock-locations/by-warehouse/{warehouse_id}/ : Emplacements d'un entrepôt
    """
    
    queryset = StockLocation.objects.all()
    serializer_class = StockLocationSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['warehouse', 'is_active', 'parent']
    search_fields = ['name', 'code']
    
    select_related_fields = ['warehouse', 'parent']
    
    action_permissions = {
        'list': 'warehouses.view',
        'retrieve': 'warehouses.view',
        'create': 'warehouses.create',
        'update': 'warehouses.edit',
        'partial_update': 'warehouses.edit',
        'destroy': 'warehouses.delete',
        'by_warehouse': 'warehouses.view',
    }

    def perform_create(self, serializer):
        assert_warehouse_allowed_for_request(
            self.request, serializer.validated_data['warehouse'].id
        )
        super().perform_create(serializer)

    def perform_update(self, serializer):
        if 'warehouse' in serializer.validated_data:
            assert_warehouse_allowed_for_request(
                self.request, serializer.validated_data['warehouse'].id
            )
        super().perform_update(serializer)

    @action(detail=False, methods=['get'], url_path='by-warehouse/(?P<warehouse_id>[^/.]+)')
    def by_warehouse(self, request, warehouse_id=None):
        """Retourne tous les emplacements actifs d'un entrepôt."""
        assert_warehouse_allowed_for_request(request, warehouse_id)
        organization = self.get_organization()
        locations = StockLocation.objects.filter(
            organization=organization,
            warehouse_id=warehouse_id,
            is_active=True
        ).select_related('warehouse', 'parent').order_by('name')
        
        serializer = StockLocationSerializer(locations, many=True)
        return Response(serializer.data)


# =============================================================================
# STOCK VIEWSET
# =============================================================================

class StockViewSet(ExportResponseMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la consultation du stock.
    
    Endpoints:
    - GET /stocks/ : Liste du stock
    - GET /stocks/{id}/ : Détail du stock
    - GET /stocks/by-product/{product_id}/ : Stock par produit
    - GET /stocks/by-warehouse/{warehouse_id}/ : Stock par entrepôt
    - GET /stocks/low-stock/ : Produits en stock bas
    - GET /stocks/expiring/ : Lots bientôt périmés
    """
    
    queryset = Stock.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockFilter
    search_fields = ['product__name', 'product__sku']
    ordering_fields = ['quantity', 'last_movement_at']
    ordering = ['-last_movement_at']
    
    # Les unités alimentent `stock_display` (« 12 cartons + 3 bouteilles ») :
    # sans elles, deux requêtes de plus par ligne de stock.
    select_related_fields = [
        'product', 'product__unit', 'product__packaging_unit',
        'variant', 'warehouse', 'location',
    ]
    
    action_permissions = {
        'list': 'stock.view',
        'retrieve': 'stock.view',
        'by_product': 'stock.view',
        'by_warehouse': 'stock.view',
        'low_stock': 'stock.view',
        'expiring': 'stock.view',
        # Lots d'un produit, en ordre FIFO : une lecture de stock comme ses
        # voisines. Non déclarée, elle répondait 403 à tous les rôles.
        'product_batches': 'stock.view',
        'export': 'stock.view',
        'unpack': 'stock_movements.create',
        # Le corps de `create` répond déjà 405 avec la marche à suivre. Le
        # laisser non déclaré le rendait inatteignable derrière un 403, qui
        # dit « il vous manque un droit » là où la vérité est « cette écriture
        # n'existe pas ». C'est la vue qui refuse, et elle le dit mieux.
        'create': '*',
    }

    # Stock est en lecture seule - les modifications passent par les mouvements.
    # `post` n'est autorisé que pour l'action `unpack` ci-dessous ; `create` est
    # explicitement refusé.
    http_method_names = ['get', 'post', 'head', 'options']

    def create(self, request, *args, **kwargs):
        return Response(
            {'error': "Le stock ne se modifie pas directement : passez par un mouvement."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return StockListSerializer
        return StockDetailSerializer

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """
        Exporte la situation de stock filtrée, en PDF ou en Excel.

        Le fichier porte TOUTES les lignes du périmètre, pas la seule page
        affichée : c'est la raison d'être d'un export côté serveur. Le queryset
        traverse les mêmes filtres et le même scoping d'entrepôt que la liste,
        donc un magasinier n'exporte jamais un entrepôt qui ne lui est pas
        assigné.
        """
        from apps.settings.services import CurrencyService

        from .reports import build_stock_levels_report

        organization = self.get_organization()
        fmt = self.get_export_format(request)
        queryset = self.filter_queryset(self.get_queryset())

        filters_applied, _ = build_export_context(request, organization)
        group_by_category = request.query_params.get('group_by', 'category') == 'category'

        spec = build_stock_levels_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filters_applied,
            group_by_category=group_by_category,
        )
        return self.render_export(spec, 'niveau_de_stock', fmt)

    @action(detail=True, methods=['post'])
    def unpack(self, request, pk=None):
        """
        Ouvre un ou plusieurs conditionnements sans attendre une vente.

        Corps dans `inventory.services.unpack_stock`, partagé avec le journal du
        terminal : ouvrir un carton est un geste de comptoir, il doit
        fonctionner hors ligne.

        Corps : ``{"packages": 1}``
        """
        from .services import TransitionRefusee, unpack_stock
        try:
            return Response(unpack_stock(
                self.get_object(), request.user, request.data.get('packages', 1),
            ))
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='by-product/(?P<product_id>[^/.]+)')
    def by_product(self, request, product_id=None):
        """Retourne le stock d'un produit dans tous les entrepôts."""
        organization = self.get_organization()
        stocks = Stock.objects.filter(
            organization=organization,
            product_id=product_id
        ).select_related('warehouse', 'location')
        m = get_membership_for_request(request)
        if m:
            stocks = filter_queryset_by_warehouse_ids(stocks, m, 'warehouse_id')
        
        page = self.paginate_queryset(stocks)
        if page is not None:
            serializer = StockListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='by-warehouse/(?P<warehouse_id>[^/.]+)')
    def by_warehouse(self, request, warehouse_id=None):
        """Retourne tout le stock d'un entrepôt."""
        organization = self.get_organization()
        assert_warehouse_allowed_for_request(request, warehouse_id)
        stocks = Stock.objects.filter(
            organization=organization,
            warehouse_id=warehouse_id
        ).select_related('product', 'variant')
        
        page = self.paginate_queryset(stocks)
        if page is not None:
            serializer = StockListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='low-stock')
    def low_stock(self, request):
        """Retourne les produits en stock bas."""
        organization = self.get_organization()
        
        stocks = Stock.objects.filter(
            organization=organization,
            quantity__lte=F('product__reorder_point'),
            product__track_inventory=True
        ).select_related(
            'product', 'product__unit', 'product__packaging_unit', 'warehouse'
        )
        m = get_membership_for_request(request)
        if m:
            stocks = filter_queryset_by_warehouse_ids(stocks, m, 'warehouse_id')
        
        page = self.paginate_queryset(stocks)
        if page is not None:
            serializer = StockListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def expiring(self, request):
        """Retourne les lots bientôt périmés."""
        organization = self.get_organization()
        days = int(request.query_params.get('days', 30))
        
        expiry_date = timezone.localdate() + timezone.timedelta(days=days)
        
        batches = StockBatch.objects.filter(
            organization=organization,
            expiry_date__lte=expiry_date,
            expiry_date__gte=timezone.localdate(),
            quantity__gt=0
        ).select_related('product', 'product__unit', 'warehouse').order_by('expiry_date')
        m = get_membership_for_request(request)
        if m:
            batches = filter_queryset_by_warehouse_ids(batches, m, 'warehouse_id')
        
        page = self.paginate_queryset(batches)
        if page is not None:
            serializer = StockBatchSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockBatchSerializer(batches, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='batches/(?P<product_id>[^/.]+)')
    def product_batches(self, request, product_id=None):
        """Retourne tous les lots d'un produit avec stock disponible (FIFO order)."""
        organization = self.get_organization()
        warehouse_id = request.query_params.get('warehouse')
        include_empty = request.query_params.get('include_empty', 'false').lower() == 'true'
        include_expired = request.query_params.get('include_expired', 'false').lower() == 'true'
        
        batches = StockBatch.objects.filter(
            organization=organization,
            product_id=product_id
        ).select_related('product', 'product__unit', 'warehouse', 'variant')
        
        if warehouse_id:
            assert_warehouse_allowed_for_request(request, warehouse_id)
            batches = batches.filter(warehouse_id=warehouse_id)
        else:
            m = get_membership_for_request(request)
            if m:
                batches = filter_queryset_by_warehouse_ids(batches, m, 'warehouse_id')
        
        if not include_empty:
            batches = batches.filter(quantity__gt=0)
        
        if not include_expired:
            today = timezone.localdate()
            batches = batches.filter(
                db_models.Q(expiry_date__isnull=True) | db_models.Q(expiry_date__gte=today)
            )
        
        # Ordre FIFO (les plus anciens en premier)
        batches = batches.order_by('received_at')
        
        page = self.paginate_queryset(batches)
        if page is not None:
            serializer = StockBatchSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockBatchSerializer(batches, many=True)
        return Response(serializer.data)


# =============================================================================
# STOCK BATCH VIEWSET
# =============================================================================

class StockBatchViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des lots de stock.
    
    Endpoints:
    - GET /stock-batches/ : Liste des lots
    - GET /stock-batches/{id}/ : Détail d'un lot
    - GET /stock-batches/expiring/ : Lots bientôt périmés
    """
    
    queryset = StockBatch.objects.all()
    serializer_class = StockBatchSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['warehouse', 'product']
    search_fields = ['batch_number', 'product__name']
    ordering_fields = ['expiry_date', 'received_at']
    ordering = ['expiry_date']
    
    # `product__unit` alimente `quantity_display` (« 240 bouteilles ») : sans
    # elle, une requête de plus par lot listé.
    select_related_fields = ['product', 'product__unit', 'warehouse']
    
    # Lots en lecture seule - créés via réception de marchandises
    http_method_names = ['get', 'head', 'options']


# =============================================================================
# STOCK MOVEMENT VIEWSET
# =============================================================================

class StockMovementViewSet(ExportResponseMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les mouvements de stock.
    
    Endpoints:
    - GET /stock-movements/ : Liste des mouvements
    - POST /stock-movements/ : Créer un mouvement manuel
    - GET /stock-movements/{id}/ : Détail d'un mouvement
    """
    
    queryset = StockMovement.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockMovementFilter
    search_fields = ['product__name', 'product__sku', 'notes']
    ordering_fields = ['created_at', 'quantity']
    ordering = ['-created_at']
    
    # `product__unit` et `product__packaging_unit` alimentent `quantity_display`
    # (« 10 cartons + 5 bouteilles ») : sans eux, deux requêtes par ligne.
    select_related_fields = [
        'product', 'product__unit', 'product__packaging_unit',
        'variant', 'warehouse', 'batch', 'created_by',
    ]

    action_permissions = {
        'list': 'stock_movements.view',
        'retrieve': 'stock_movements.view',
        'create': 'stock_movements.create',
        'export': 'stock_movements.view',
        'supplies_export': 'stock_movements.view',
        # Un mouvement de stock est une ÉCRITURE, au sens comptable : il se
        # contrepasse par un autre mouvement, il ne se rature pas. Le
        # modifier laisserait les `quantity_before`/`after` des mouvements
        # suivants décrire un stock qui n'a jamais existé.
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return StockMovementListSerializer
        elif self.action == 'create':
            return StockMovementCreateSerializer
        return StockMovementDetailSerializer

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """Exporte le journal des mouvements filtré, en PDF ou en Excel."""
        from apps.settings.services import CurrencyService

        from .reports import build_movements_report

        organization = self.get_organization()
        fmt = self.get_export_format(request)
        queryset = self.filter_queryset(self.get_queryset())

        filters_applied, period = build_export_context(
            request, organization, include_period=True,
        )

        spec = build_movements_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filters_applied,
            period_label=period,
        )
        return self.render_export(spec, 'mouvements_de_stock', fmt)

    @action(detail=False, methods=['get'], url_path='supplies-export')
    def supplies_export(self, request):
        """
        Exporte le rapport d'approvisionnement valorisé.

        Part des mouvements qui font ENTRER de la marchandise. Le paramètre
        `source` resserre au besoin sur les seules réceptions fournisseur :
        `all` (défaut) répond à « tout ce qui est entré », `receipts` à « ce que
        j'ai acheté », deux questions distinctes que le même écran doit servir.

        `group_by=product` (défaut) donne la valeur d'achat par produit,
        `group_by=movement` déroule le détail chronologique.
        """
        from apps.settings.services import CurrencyService

        from .reports import build_supplies_report

        organization = self.get_organization()
        fmt = self.get_export_format(request)

        queryset = self.filter_queryset(self.get_queryset()).filter(
            movement_type__in=STOCK_IN_MOVEMENT_TYPES,
        )
        if request.query_params.get('source') == 'receipts':
            queryset = queryset.filter(reference_type='goods_receipt')

        filters_applied, period = build_export_context(
            request, organization, include_period=True,
        )
        group_by = (
            'movement' if request.query_params.get('group_by') == 'movement'
            else 'product'
        )

        spec = build_supplies_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filters_applied,
            group_by=group_by,
            period_label=period,
        )
        return self.render_export(spec, 'approvisionnement', fmt)

    def perform_create(self, serializer):
        """Le corps vit dans `stock_movements.create_stock_movement`.

        Il y est descendu pour que le journal de synchronisation puisse le
        rejouer : `stock_movement.create` appelait `serializer.save()` en
        direct, et ni le stock ni les lots ne bougeaient.
        """
        from .stock_movements import create_stock_movement

        create_stock_movement(
            serializer,
            organization=self.get_organization(),
            user=self.request.user,
            request=self.request,
        )


# =============================================================================
# STOCK TRANSFER VIEWSET
# =============================================================================

class TransitionActionMixin:
    """
    Exécute une transition de `inventory.services` et traduit son refus.

    Le refus est DÉTERMINISTE (« déjà expédié », « pas en révision ») : il
    devient un 400 ici, et un verdict `rejected` côté journal - jamais un
    réessai, qui rejouerait un effet déjà appliqué.

    En mixin plutôt qu'en trois copies : les transferts, les ajustements et les
    sessions d'inventaire appellent tous le même schéma, et trois copies
    auraient divergé sur le traitement de l'erreur.
    """

    #: Serializer employé quand la transition rend un OBJET. Sans lui, le
    #: résultat de la fonction part tel quel : les transferts et les
    #: ajustements rendent `{'status': 'shipped'}`, et ce contrat était déjà
    #: publié - le changer casserait le back-office en silence.
    transition_serializer = None

    def _transition(self, fonction, serialiser=False, **extra):
        from .services import TransitionRefusee
        try:
            resultat = fonction(self.get_object(), self.request.user, **extra)
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if serialiser and self.transition_serializer is not None:
            return Response(self.transition_serializer(resultat).data)
        return Response(resultat)


class StockTransferViewSet(TransitionActionMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les transferts de stock entre entrepôts.
    
    Endpoints:
    - GET /stock-transfers/ : Liste des transferts
    - POST /stock-transfers/ : Créer un transfert
    - GET /stock-transfers/{id}/ : Détail d'un transfert
    - POST /stock-transfers/{id}/approve/ : Approuver un transfert
    - POST /stock-transfers/{id}/ship/ : Marquer comme expédié
    - POST /stock-transfers/{id}/receive/ : Marquer comme reçu
    - POST /stock-transfers/{id}/cancel/ : Annuler un transfert
    """
    
    queryset = StockTransfer.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'source_warehouse', 'destination_warehouse']
    search_fields = ['reference']
    ordering = ['-requested_at']
    
    select_related_fields = ['source_warehouse', 'destination_warehouse', 'requested_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'stock_transfers.view',
        'retrieve': 'stock_transfers.view',
        'create': 'stock_transfers.create',
        'update': 'stock_transfers.create',
        'partial_update': 'stock_transfers.create',
        'destroy': 'stock_transfers.cancel',
        'approve': 'stock_transfers.ship',
        'ship': 'stock_transfers.ship',
        'receive': 'stock_transfers.receive',
        'cancel': 'stock_transfers.cancel',
    }

    def get_queryset(self):
        qs = super().get_queryset()
        m = get_membership_for_request(self.request)
        if m:
            qs = filter_stock_transfer_queryset(qs, m)
        return qs

    def perform_create(self, serializer):
        va = serializer.validated_data
        assert_warehouse_allowed_for_request(self.request, va['source_warehouse'].id)
        assert_warehouse_allowed_for_request(self.request, va['destination_warehouse'].id)
        super().perform_create(serializer)

    def get_serializer_class(self):
        if self.action == 'list':
            return StockTransferListSerializer
        elif self.action == 'create':
            return StockTransferCreateSerializer
        return StockTransferDetailSerializer

    # Les quatre transitions ci-dessous ne portent PLUS de logique : leur corps
    # vit dans `inventory.services`, que le journal d'opérations du terminal
    # mobile rejoue aussi. Deux corps auraient divergé, comme la dette client
    # l'avait fait avant le lot 6.

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve un transfert en attente."""
        from .services import approve_transfer
        return self._transition(approve_transfer)

    @action(detail=True, methods=['post'])
    def ship(self, request, pk=None):
        """Marque un transfert comme expédié et déduit le stock source."""
        from .services import ship_transfer
        return self._transition(ship_transfer)

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Marque un transfert comme reçu et ajoute le stock destination."""
        from .services import receive_transfer
        return self._transition(
            receive_transfer, received_items=request.data.get('items', []),
        )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule un transfert."""
        from .services import cancel_transfer
        return self._transition(cancel_transfer)



class StockAdjustmentViewSet(TransitionActionMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les ajustements de stock (inventaire).
    
    Endpoints:
    - GET /stock-adjustments/ : Liste des ajustements
    - POST /stock-adjustments/ : Créer un ajustement
    - GET /stock-adjustments/{id}/ : Détail d'un ajustement
    - POST /stock-adjustments/{id}/approve/ : Approuver et appliquer
    - POST /stock-adjustments/{id}/reject/ : Rejeter
    """
    
    queryset = StockAdjustment.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'adjustment_type', 'warehouse']
    search_fields = ['reference', 'reason']
    ordering = ['-created_at']
    
    select_related_fields = ['warehouse', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'stock_adjustments.view',
        'retrieve': 'stock_adjustments.view',
        'create': 'stock_adjustments.create',
        'update': 'stock_adjustments.create',
        'partial_update': 'stock_adjustments.create',
        'destroy': 'stock_adjustments.create',
        'approve': 'stock_adjustments.approve',
        'reject': 'stock_adjustments.approve',
    }

    def perform_create(self, serializer):
        assert_warehouse_allowed_for_request(
            self.request, serializer.validated_data['warehouse'].id
        )
        super().perform_create(serializer)

    def perform_update(self, serializer):
        if 'warehouse' in serializer.validated_data:
            assert_warehouse_allowed_for_request(
                self.request, serializer.validated_data['warehouse'].id
            )
        super().perform_update(serializer)

    def get_serializer_class(self):
        if self.action == 'list':
            return StockAdjustmentListSerializer
        elif self.action == 'create':
            return StockAdjustmentCreateSerializer
        return StockAdjustmentDetailSerializer

    # Corps dans `inventory.services`, partagé avec le journal du terminal.

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve et applique l'ajustement de stock."""
        from .services import approve_adjustment
        return self._transition(approve_adjustment)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejette un ajustement."""
        from .services import reject_adjustment
        return self._transition(reject_adjustment)


# =============================================================================

class InventorySessionViewSet(ExportResponseMixin, TransitionActionMixin,
                              WarehouseScopedQuerysetMixin, TenantViewSetMixin,
                              AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des sessions d'inventaire.
    
    Endpoints:
    - GET    /inventory-sessions/                    : Liste des sessions
    - POST   /inventory-sessions/                    : Créer une session (brouillon)
    - GET    /inventory-sessions/{id}/               : Détail d'une session
    - DELETE /inventory-sessions/{id}/               : Supprimer (brouillon uniquement)
    - POST   /inventory-sessions/{id}/start/         : Démarrer (verrouille le stock, génère les lignes)
    - POST   /inventory-sessions/{id}/count/         : Enregistrer les comptages
    - POST   /inventory-sessions/{id}/submit/        : Soumettre pour révision
    - POST   /inventory-sessions/{id}/validate/      : Valider et appliquer les ajustements
    - POST   /inventory-sessions/{id}/cancel/        : Annuler (déverrouille le stock)
    - GET    /inventory-sessions/{id}/counts/        : Liste des lignes de comptage
    """

    transition_serializer = InventorySessionDetailSerializer
    
    queryset = InventorySession.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'scope_type', 'warehouse']
    search_fields = ['reference', 'name']
    ordering = ['-created_at']
    
    select_related_fields = ['warehouse', 'created_by', 'validated_by']
    prefetch_related_fields = ['categories', 'products']
    
    action_permissions = {
        'list': 'inventory.view',
        'retrieve': 'inventory.view',
        'create': 'inventory.create',
        'destroy': 'inventory.cancel',
        'start': 'inventory.start',
        'count': 'inventory.count',
        'submit': 'inventory.submit',
        'validate': 'inventory.validate',
        'cancel': 'inventory.cancel',
        'counts': 'inventory.view',
        # Les deux documents (fiche et rapport) se lisent avec le même droit
        # que l'impression : ils ne portent rien que l'écran n'affiche déjà.
        'export': 'inventory.print',
        # Le VERROU se lit au comptoir, donc par le caissier, qui n'a pas
        # `inventory.view`. Sans cette ligne l'action n'était pas listée, donc
        # refusée (403) à TOUS les rôles : le POS web appelait une route qui
        # refusait, échouait en silence, et n'a jamais posé le moindre verrou.
        # C'est ce qui a fait refuser une vente déjà encaissée et imprimée.
        'locked_products': ['sales.view', 'inventory.view'],
        # Une session avance par ses transitions. La modifier en écriture
        # directe permettrait d'en changer le périmètre ou l'entrepôt après
        # le verrouillage du stock, donc de compter un jeu de produits et
        # d'en ajuster un autre.
        'update': DENY,
        'partial_update': DENY,
    }

    def perform_create(self, serializer):
        assert_warehouse_allowed_for_request(
            self.request, serializer.validated_data['warehouse'].id
        )
        super().perform_create(serializer)

    def get_serializer_class(self):
        if self.action == 'list':
            return InventorySessionListSerializer
        elif self.action == 'create':
            return InventorySessionCreateSerializer
        return InventorySessionDetailSerializer

    def destroy(self, request, *args, **kwargs):
        session = self.get_object()
        if session.status != 'draft':
            return Response(
                {'error': 'Seules les sessions en brouillon peuvent être supprimées'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    def _get_target_products(self, session):
        """Produits visés. Corps dans `inventory.services.target_products`."""
        from .services import target_products
        return target_products(session)

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """Démarre la session : verrouille le stock, engendre les comptages."""
        from .services import start_inventory_session
        return self._transition(start_inventory_session, serialiser=True)

    @action(detail=True, methods=['post'])
    def count(self, request, pk=None):
        """
        Enregistre les comptages pour une ou plusieurs lignes.

        Body: { "counts": [{ "id": "<count_id>", "quantity_counted": 10, "notes": "" }, ...] }
        """
        from .services import record_inventory_counts
        return self._transition(
            record_inventory_counts, lignes=request.data.get('counts', []),
        )

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Soumet la session pour révision après le comptage."""
        from .services import submit_inventory_session
        return self._transition(submit_inventory_session, serialiser=True)

    @action(detail=True, methods=['post'])
    def validate(self, request, pk=None):
        """Valide la session : applique les écarts et déverrouille le stock."""
        from .services import validate_inventory_session
        return self._transition(validate_inventory_session, serialiser=True)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule une session d'inventaire et déverrouille le stock."""
        from .services import cancel_inventory_session
        # ⚠ `serialiser=True` comme ses quatre soeurs : le service rend une
        # `InventorySession`, et sans cet argument l'objet partait tel quel au
        # rendu JSON. Django répondait 500 - APRÈS que le service ait écrit,
        # puisque le rendu est la dernière étape : le stock était bel et bien
        # déverrouillé, et le gérant lisait un échec. C'est le défaut déjà
        # corrigé sur le chemin du JOURNAL, resté ouvert sur celui de la VUE.
        return self._transition(cancel_inventory_session, serialiser=True)

    @action(detail=True, methods=['get'])
    def counts(self, request, pk=None):
        """Retourne les lignes de comptage d'une session avec filtres."""
        session = self.get_object()
        
        qs = session.counts.select_related('product', 'variant', 'counted_by')
        
        # Filters
        is_counted = request.query_params.get('is_counted')
        if is_counted is not None:
            qs = qs.filter(is_counted=is_counted.lower() == 'true')
        
        has_difference = request.query_params.get('has_difference')
        if has_difference is not None and has_difference.lower() == 'true':
            qs = qs.filter(is_counted=True).exclude(quantity_difference=Decimal('0.000'))
        
        search = request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(product__name__icontains=search) |
                Q(product__sku__icontains=search)
            )
        
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(product__category_id=category)
        
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = InventoryCountSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = InventoryCountSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='export')
    def export(self, request, pk=None):
        """
        La fiche de comptage ou le rapport d'écarts, en PDF, classeur ou CSV.

        Les deux documents étaient dessinés dans le navigateur, en jsPDF, à
        partir du JSON de `print-data/`. Ils portent désormais la marque de tous
        les autres documents du produit.
        """
        from apps.inventory.session_documents import (
            DOCUMENT_BASENAMES,
            build_inventory_report,
            build_inventory_sheet,
        )
        from apps.settings.services import CurrencyService

        fmt = self.get_export_format(request)
        document = (request.query_params.get('document') or 'sheet').strip()
        if document not in DOCUMENT_BASENAMES:
            raise DRFValidationError({
                'document': "Document inconnu. Attendu : "
                            + ', '.join(sorted(DOCUMENT_BASENAMES)) + '.',
            })

        session = self.get_object()
        organization = self.get_organization()
        counts = session.counts.select_related(
            'product', 'product__category', 'product__unit',
            'product__packaging_unit',
        ).order_by('product__category__name', 'product__name')

        constructeur = (
            build_inventory_sheet if document == 'sheet' else build_inventory_report
        )
        spec = constructeur(
            session, counts, organization,
            currency=CurrencyService.primary_code(organization),
        )
        return self.render_export(
            spec, f"{DOCUMENT_BASENAMES[document]}_{session.reference}", fmt
        )


    @action(detail=False, methods=['get'], url_path='locked-products')
    def locked_products(self, request):
        """
        Retourne les IDs des produits bloqués par des inventaires en cours.
        Utilisé par le frontend pour désactiver ces produits dans le POS.
        """
        organization = self.get_organization()
        m = get_membership_for_request(request)
        wh_scope = accessible_warehouse_ids(m) if m else None
        if wh_scope is not None and not wh_scope:
            return Response({
                'locked_product_ids': [],
                'active_sessions': [],
                'has_active_inventory': False,
            })
        locked_product_ids = InventorySession.get_all_locked_product_ids(
            organization, warehouse_ids=wh_scope
        )

        # Récupérer les sessions actives pour information
        active_sessions_qs = InventorySession.objects.filter(
            organization=organization,
            is_stock_locked=True,
            status__in=['in_progress', 'review'],
            is_deleted=False,
        )
        if wh_scope is not None:
            active_sessions_qs = active_sessions_qs.filter(warehouse_id__in=wh_scope)
        active_sessions = active_sessions_qs.select_related('warehouse').values(
            'id', 'reference', 'name', 'warehouse__name', 'status'
        )
        
        return Response({
            'locked_product_ids': list(locked_product_ids),
            'active_sessions': list(active_sessions),
            'has_active_inventory': len(locked_product_ids) > 0
        })
