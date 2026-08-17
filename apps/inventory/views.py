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

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin, WarehouseScopedQuerysetMixin
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_warehouse_allowed_for_request,
    filter_queryset_by_warehouse_ids,
    filter_stock_transfer_queryset,
    get_membership_for_request,
)
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from apps.subscriptions.services import SubscriptionService
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem,
    InventorySession, InventoryCount, STOCK_IN_MOVEMENT_TYPES
)
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

class StockViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
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
    filterset_fields = ['warehouse', 'product', 'variant']
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
        'unpack': 'stock_movements.create',
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

    @action(detail=True, methods=['post'])
    def unpack(self, request, pk=None):
        """
        Ouvre un ou plusieurs conditionnements sans attendre une vente.

        Sert au vendeur qui anticipe, et débloque le cas où le
        déconditionnement automatique est désactivé sur le produit.

        Corps : ``{"packages": 1}``
        """
        from .packaging import PackagingService

        try:
            packages = int(request.data.get('packages', 1))
        except (TypeError, ValueError):
            packages = 0
        if packages < 1:
            return Response(
                {'error': "Indiquez combien de conditionnements ouvrir."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        stock = self.get_object()
        product = stock.product
        factor = PackagingService.factor(product)
        if factor is None:
            return Response(
                {'error': "Ce produit n'est pas vendu par conditionnement."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            locked = Stock.objects.select_for_update().get(pk=stock.pk)
            _, loose = PackagingService.stored_split(locked, factor)
            opened, _movement = PackagingService.ensure_loose_available(
                locked, product,
                needed_loose=loose + packages * factor,
                user=request.user,
                reference_type='manual_unpack',
                force=True,
            )
            locked.last_movement_at = timezone.now()
            locked.save()

        locked.refresh_from_db()
        return Response({
            'packages_opened': opened,
            'stock_display': PackagingService.format_quantity(
                product, locked.quantity, locked.loose_quantity
            ),
        })

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
        
        expiry_date = timezone.now().date() + timezone.timedelta(days=days)
        
        batches = StockBatch.objects.filter(
            organization=organization,
            expiry_date__lte=expiry_date,
            expiry_date__gte=timezone.now().date(),
            quantity__gt=0
        ).select_related('product', 'warehouse').order_by('expiry_date')
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
        ).select_related('product', 'warehouse', 'variant')
        
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
            today = timezone.now().date()
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
    
    select_related_fields = ['product', 'warehouse']
    
    # Lots en lecture seule - créés via réception de marchandises
    http_method_names = ['get', 'head', 'options']


# =============================================================================
# STOCK MOVEMENT VIEWSET
# =============================================================================

class StockMovementViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
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
    filterset_fields = ['warehouse', 'product', 'movement_type']
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
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return StockMovementListSerializer
        elif self.action == 'create':
            return StockMovementCreateSerializer
        return StockMovementDetailSerializer

    @transaction.atomic
    def perform_create(self, serializer):
        """Crée un mouvement et met à jour le stock."""
        from .packaging import PackagingService
        from .services import FIFOService

        organization = self.get_organization()
        data = serializer.validated_data
        assert_warehouse_allowed_for_request(self.request, data['warehouse'].id)

        # Récupérer ou créer le stock avec verrouillage
        product = data['product']
        product_cost = product.cost_price if product.cost_price else Decimal('0.00')
        stock, created = Stock.objects.select_for_update().get_or_create(
            organization=organization,
            product=product,
            variant=data.get('variant'),
            warehouse=data['warehouse'],
            defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
        )
        
        quantity_before = stock.quantity
        unit_cost = data.get('unit_cost') or Decimal('0.00')
        movement_type = data.get('movement_type', '')
        
        # Si le stock existait mais sans avg_cost, initialiser depuis le produit
        if not created and stock.avg_cost == 0 and product_cost > 0:
            stock.avg_cost = product_cost
        
        # Pour les entrées de stock (approvisionnements), créer un lot
        batch = data.get('batch')
        if data['quantity'] > 0 and movement_type in STOCK_IN_MOVEMENT_TYPES:
            # Créer un lot avec numéro auto-généré
            location = data.get('location')
            expiry_date = data.get('expiry_date')
            
            batch = FIFOService.add_to_batch(
                organization=organization,
                product=product,
                warehouse=data['warehouse'],
                quantity=data['quantity'],
                cost_price=unit_cost if unit_cost > 0 else product_cost,
                batch_number=None,  # Auto-généré par le service
                variant=data.get('variant'),
                location=location,
                expiry_date=expiry_date,
                notes=data.get('notes', ''),
                user=self.request.user
            )
        
        # Pour les sorties de stock, consommer les lots en FIFO
        elif data['quantity'] < 0 and movement_type in ['sale', 'damage', 'expired', 'transfer_out', 'adjustment_out', 'production_out']:
            quantity_to_consume = abs(data['quantity'])
            allocations, remaining = FIFOService.consume_from_batches(
                organization=organization,
                product=product,
                warehouse=data['warehouse'],
                quantity=quantity_to_consume,
                variant=data.get('variant'),
                reference_type=movement_type,
                reference_id=data.get('reference_id'),
                user=self.request.user,
                notes=data.get('notes', ''),
                exclude_expired=(movement_type != 'expired'),
                use_fefo=product.has_expiry_date if hasattr(product, 'has_expiry_date') else False
            )
            
            # Associer le premier lot consommé au mouvement
            if allocations:
                batch = allocations[0].batch
        
        # Mettre à jour le coût moyen pondéré pour les entrées
        if data['quantity'] > 0 and unit_cost > 0:
            if stock.quantity > 0:
                total_existing_value = stock.quantity * stock.avg_cost
                total_new_value = data['quantity'] * unit_cost
                stock.avg_cost = (
                    (total_existing_value + total_new_value) /
                    (stock.quantity + data['quantity'])
                ).quantize(Decimal('0.01'))
            else:
                stock.avg_cost = unit_cost
        
        # Part de la saisie exprimée à l'unité : elle alimente (ou prélève) le
        # vrac. Une entrée en conditionnements entiers laisse le vrac inchangé,
        # puisque les emballages arrivent scellés.
        delta_loose = data.get('input_loose_quantity')
        if delta_loose is None:
            # Saisie en quantité simple : sans indication de conditionnement, on
            # considère qu'elle porte sur des unités hors emballage.
            PackagingService.apply_base_delta(stock, product, data['quantity'])
        else:
            # Saisie « X contenants + Y unités » : chaque part va dans son
            # compteur, sans jamais se convertir dans l'autre.
            sign = -1 if data['quantity'] < 0 else 1
            delta_packages = data.get('input_package_quantity') or Decimal('0.000')
            PackagingService.apply_delta(
                stock, product,
                delta_packages=sign * abs(delta_packages),
                delta_loose=sign * abs(delta_loose),
            )
        stock.last_movement_at = timezone.now()
        stock.save()

        # Créer le mouvement
        serializer.save(
            organization=organization,
            batch=batch,
            quantity_before=quantity_before,
            quantity_after=stock.quantity,
            created_by=self.request.user
        )

        # Report des prix sur la fiche produit, en dernier et dans la même
        # transaction. En dernier parce que l'initialisation de `avg_cost`
        # ci-dessus lit `product.cost_price` : écrire la fiche avant ferait
        # démarrer un stock neuf au nouveau prix au lieu de l'ancien.
        product_prices = serializer.validated_data.get('_product_prices')
        if product_prices:
            from apps.products.models import Product
            from apps.products.pricing import ProductPricingService

            # Verrou pris après celui du stock : ordre d'acquisition constant,
            # sinon deux approvisionnements simultanés peuvent s'interbloquer.
            locked_product = Product.objects.select_for_update().get(pk=product.pk)
            ProductPricingService.apply(locked_product, product_prices)


# =============================================================================
# STOCK TRANSFER VIEWSET
# =============================================================================

class StockTransferViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
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

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve un transfert en attente."""
        transfer = self.get_object()
        
        if transfer.status != 'draft':
            return Response(
                {'error': 'Seuls les transferts en brouillon peuvent être approuvés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        transfer.status = 'pending'
        transfer.approved_by = request.user
        transfer.save()
        
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def ship(self, request, pk=None):
        """Marque un transfert comme expédié et déduit le stock source."""
        transfer = self.get_object()
        
        if transfer.status not in ['draft', 'pending']:
            return Response(
                {'error': 'Ce transfert ne peut pas être expédié'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from .packaging import PackagingService

        with transaction.atomic():
            # Déduire le stock de l'entrepôt source
            for item in transfer.items.select_related('product').all():
                product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
                stock, created = Stock.objects.select_for_update().get_or_create(
                    organization=transfer.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=transfer.source_warehouse,
                    defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
                )

                if not created and stock.avg_cost == 0 and product_cost > 0:
                    stock.avg_cost = product_cost

                quantity_before = stock.quantity

                # Conditionnement : on ne charge pas un contenant scellé qui
                # n'existe pas, et servir la part au détail peut exiger d'en
                # ouvrir un. Le stock est déjà verrouillé, contrat exigé par
                # `ensure_loose_available`.
                loose_shipped = PackagingService.loose_share(
                    item.product, item.quantity_requested, item.loose_quantity
                )
                PackagingService.assert_sealed_available(
                    stock, item.product, item.package_quantity,
                    action_label='transférer',
                )
                if loose_shipped > 0:
                    PackagingService.ensure_loose_available(
                        stock, item.product, loose_shipped,
                        user=request.user,
                        reference_type='stock_transfer',
                        reference_id=transfer.id,
                    )

                PackagingService.apply_base_delta(
                    stock, item.product,
                    -item.quantity_requested,
                    loose_hint=loose_shipped,
                )
                PackagingService.touch(stock)
                stock.save()

                # Créer le mouvement sortant
                StockMovement.objects.create(
                    organization=transfer.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=transfer.source_warehouse,
                    movement_type='transfer_out',
                    quantity=-item.quantity_requested,
                    unit_cost=stock.avg_cost,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    input_package_quantity=item.package_quantity,
                    input_loose_quantity=item.loose_quantity,
                    packaging_factor=item.packaging_factor,
                    reference_type='stock_transfer',
                    reference_id=transfer.id,
                    notes=f"Transfert {transfer.reference}",
                    created_by=request.user
                )

                item.quantity_shipped = item.quantity_requested
                item.save()
            
            transfer.status = 'in_transit'
            transfer.shipped_at = timezone.now()
            transfer.save()
        
        return Response({'status': 'shipped'})

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Marque un transfert comme reçu et ajoute le stock destination."""
        transfer = self.get_object()
        
        if transfer.status != 'in_transit':
            return Response(
                {'error': 'Seuls les transferts en transit peuvent être reçus'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        received_items = request.data.get('items', [])

        from .packaging import PackagingService

        with transaction.atomic():
            for item in transfer.items.select_related('product').all():
                # Chercher la quantité reçue dans les données. Elle peut arriver
                # en contenants (« 3 cartons + 2 bouteilles ») : c'est la forme
                # sous laquelle le magasinier compte ce qu'il décharge.
                received_qty = None
                received_loose = None
                for ri in received_items:
                    if str(ri.get('id')) != str(item.id):
                        continue
                    packages = ri.get('package_quantity')
                    loose = ri.get('loose_quantity')
                    if packages is not None or loose is not None:
                        received_loose = Decimal(str(loose or 0))
                        received_qty = PackagingService.to_base(
                            item.product, Decimal(str(packages or 0)), received_loose
                        )
                    elif ri.get('quantity_received') is not None:
                        received_qty = Decimal(str(ri.get('quantity_received')))
                    break

                if received_qty is None:
                    received_qty = item.quantity_shipped

                item.quantity_received = received_qty
                item.save()

                # Une réception partielle ne conserve pas forcément le partage
                # d'origine : `loose_share` replafonne la part scellée sur ce qui
                # arrive vraiment.
                loose_received = PackagingService.loose_share(
                    item.product,
                    received_qty,
                    received_loose if received_loose is not None else item.loose_quantity,
                )

                # Récupérer le coût moyen de la source
                product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
                source_stock = Stock.objects.filter(
                    organization=transfer.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=transfer.source_warehouse
                ).first()
                source_avg_cost = source_stock.avg_cost if source_stock and source_stock.avg_cost > 0 else product_cost
                
                # Ajouter au stock destination avec verrouillage
                stock, created = Stock.objects.select_for_update().get_or_create(
                    organization=transfer.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=transfer.destination_warehouse,
                    defaults={'quantity': Decimal('0.000'), 'avg_cost': source_avg_cost}
                )
                
                if not created and stock.avg_cost == 0 and source_avg_cost > 0:
                    stock.avg_cost = source_avg_cost
                
                quantity_before = stock.quantity
                
                # Mettre à jour le coût moyen pondéré
                if received_qty > 0 and source_avg_cost > 0:
                    if stock.quantity > 0:
                        total_existing = stock.quantity * stock.avg_cost
                        total_incoming = received_qty * source_avg_cost
                        stock.avg_cost = (
                            (total_existing + total_incoming) /
                            (stock.quantity + received_qty)
                        ).quantize(Decimal('0.01'))
                    else:
                        stock.avg_cost = source_avg_cost
                
                PackagingService.apply_base_delta(
                    stock, item.product,
                    received_qty,
                    loose_hint=loose_received,
                )
                PackagingService.touch(stock)
                stock.save()

                # Créer le mouvement entrant
                StockMovement.objects.create(
                    organization=transfer.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=transfer.destination_warehouse,
                    movement_type='transfer_in',
                    quantity=received_qty,
                    unit_cost=source_avg_cost,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    input_package_quantity=(
                        (received_qty - loose_received) / item.packaging_factor
                        if item.packaging_factor else Decimal('0.000')
                    ),
                    input_loose_quantity=loose_received if item.packaging_factor else Decimal('0.000'),
                    packaging_factor=item.packaging_factor,
                    reference_type='stock_transfer',
                    reference_id=transfer.id,
                    notes=f"Transfert {transfer.reference}",
                    created_by=request.user
                )
            
            transfer.status = 'completed'
            transfer.received_at = timezone.now()
            transfer.save()
        
        return Response({'status': 'received'})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule un transfert."""
        transfer = self.get_object()
        
        if transfer.status == 'completed':
            return Response(
                {'error': 'Un transfert terminé ne peut pas être annulé'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from .packaging import PackagingService

        with transaction.atomic():
            # Si déjà expédié, remettre le stock
            if transfer.status == 'in_transit':
                for item in transfer.items.select_related('product').all():
                    product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
                    stock, created = Stock.objects.select_for_update().get_or_create(
                        organization=transfer.organization,
                        product=item.product,
                        variant=item.variant,
                        warehouse=transfer.source_warehouse,
                        defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
                    )
                    
                    if not created and stock.avg_cost == 0 and product_cost > 0:
                        stock.avg_cost = product_cost
                    
                    quantity_before = stock.quantity
                    quantity_to_restore = item.quantity_shipped or Decimal('0.000')

                    # Annuler une expédition, c'est décharger le camion : les
                    # contenants qui n'ont jamais été ouverts reviennent scellés.
                    # Le partage restitué est donc l'exact symétrique de celui
                    # retiré à l'expédition.
                    loose_to_restore = PackagingService.loose_share(
                        item.product, quantity_to_restore, item.loose_quantity
                    )
                    PackagingService.apply_base_delta(
                        stock, item.product,
                        quantity_to_restore,
                        loose_hint=loose_to_restore,
                    )
                    PackagingService.touch(stock)
                    stock.save()

                    # Créer le mouvement de retour
                    if quantity_to_restore > 0:
                        StockMovement.objects.create(
                            organization=transfer.organization,
                            product=item.product,
                            variant=item.variant,
                            warehouse=transfer.source_warehouse,
                            movement_type='transfer_in',
                            quantity=quantity_to_restore,
                            quantity_before=quantity_before,
                            quantity_after=stock.quantity,
                            input_package_quantity=item.package_quantity,
                            input_loose_quantity=item.loose_quantity,
                            packaging_factor=item.packaging_factor,
                            reference_type='stock_transfer_cancel',
                            reference_id=transfer.id,
                            notes=f"Annulation transfert {transfer.reference}",
                            created_by=request.user
                        )
            
            transfer.status = 'cancelled'
            transfer.save()
        
        return Response({'status': 'cancelled'})


# =============================================================================
# STOCK ADJUSTMENT VIEWSET
# =============================================================================

class StockAdjustmentViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
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

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve et applique l'ajustement de stock."""
        adjustment = self.get_object()
        
        if adjustment.status != 'draft':
            return Response(
                {'error': 'Seuls les ajustements en brouillon peuvent être approuvés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from .packaging import PackagingService

        with transaction.atomic():
            # Appliquer les ajustements
            for item in adjustment.items.select_related('product').all():
                product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
                stock, created = Stock.objects.select_for_update().get_or_create(
                    organization=adjustment.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=adjustment.warehouse,
                    defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
                )
                
                if not created and stock.avg_cost == 0 and product_cost > 0:
                    stock.avg_cost = product_cost
                
                quantity_before = stock.quantity
                
                # Mettre à jour le coût moyen si un coût unitaire est fourni
                if item.unit_cost and item.unit_cost > 0 and item.quantity_difference > 0:
                    if stock.quantity > 0:
                        total_existing = stock.quantity * stock.avg_cost
                        total_incoming = item.quantity_difference * item.unit_cost
                        stock.avg_cost = (
                            (total_existing + total_incoming) /
                            (stock.quantity + item.quantity_difference)
                        ).quantize(Decimal('0.01'))
                    else:
                        stock.avg_cost = item.unit_cost
                
                # Le comptage physique fait foi sur les DEUX canaux : « j'ai
                # compté 3 casiers et 2 bouteilles » se pose tel quel, sans
                # repasser par une division. `reconcile` réaligne `quantity`.
                stock.quantity = item.quantity_counted
                if item.counted_loose_quantity is not None:
                    stock.loose_quantity = item.counted_loose_quantity
                if item.counted_package_quantity is not None:
                    stock.package_quantity = item.counted_package_quantity
                stock.last_counted_at = timezone.now()
                stock.last_movement_at = timezone.now()
                stock.save()

                # Déterminer le type de mouvement
                if item.quantity_difference > 0:
                    movement_type = 'adjustment_in'
                else:
                    movement_type = 'adjustment_out'

                # L'écart se relit en contenants dans l'historique : « il
                # manquait 2 cartons + 1 bouteille » parle au marchand, « -25 »
                # non. Le signe reste porté par `quantity`.
                factor = item.packaging_factor or PackagingService.factor(item.product)
                gap = abs(item.quantity_difference)
                loose_gap = (
                    PackagingService.loose_share(item.product, gap)
                    if factor else Decimal('0.000')
                )
                package_gap = (gap - loose_gap) / factor if factor else Decimal('0.000')

                # Créer le mouvement
                StockMovement.objects.create(
                    organization=adjustment.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=adjustment.warehouse,
                    movement_type=movement_type,
                    quantity=item.quantity_difference,
                    unit_cost=item.unit_cost,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    input_package_quantity=package_gap,
                    input_loose_quantity=loose_gap,
                    packaging_factor=factor,
                    reference_type='stock_adjustment',
                    reference_id=adjustment.id,
                    notes=f"Ajustement {adjustment.reference}: {adjustment.get_adjustment_type_display()}",
                    created_by=request.user
                )
            
            adjustment.status = 'approved'
            adjustment.approved_by = request.user
            adjustment.approved_at = timezone.now()
            adjustment.save()
        
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejette un ajustement."""
        adjustment = self.get_object()
        
        if adjustment.status != 'draft':
            return Response(
                {'error': 'Seuls les ajustements en brouillon peuvent être rejetés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        adjustment.status = 'rejected'
        adjustment.save()
        
        return Response({'status': 'rejected'})


# =============================================================================
# INVENTORY SESSION VIEWSET
# =============================================================================

class InventorySessionViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
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
    - GET    /inventory-sessions/{id}/print-data/    : Données pour impression
    """
    
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
        'print_data': 'inventory.print',
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
        """Retourne les produits ciblés par la session selon son scope."""
        from apps.products.models import Product
        
        organization = session.organization
        base_qs = Product.objects.filter(
            organization=organization,
            is_active=True,
            track_inventory=True,
        )
        
        if session.scope_type == 'category':
            category_ids = list(session.categories.values_list('id', flat=True))
            if category_ids:
                base_qs = base_qs.filter(category_id__in=category_ids)
        elif session.scope_type == 'product':
            product_ids = list(session.products.values_list('id', flat=True))
            if product_ids:
                base_qs = base_qs.filter(id__in=product_ids)

        # N'inclure que les produits avec stock disponible dans l'entrepôt ciblé.
        # available_quantity = quantity - reserved_quantity > 0
        return base_qs.filter(
            stocks__warehouse=session.warehouse,
            stocks__variant__isnull=True,
            stocks__quantity__gt=F('stocks__reserved_quantity'),
        ).distinct()

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """
        Démarre une session d'inventaire :
        1. Verrouille le stock des produits ciblés dans l'entrepôt
        2. Prend un snapshot du stock actuel
        3. Génère les lignes de comptage (InventoryCount)
        """
        session = self.get_object()
        
        if session.status != 'draft':
            return Response(
                {'error': 'Seules les sessions en brouillon peuvent être démarrées'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with transaction.atomic():
            products = self._get_target_products(session)
            
            # Generate count lines with stock snapshot
            count_objects = []
            for product in products:
                stock = Stock.objects.filter(
                    organization=session.organization,
                    product=product,
                    warehouse=session.warehouse,
                    variant=None,
                ).first()
                
                current_qty = stock.quantity if stock else Decimal('0.000')
                unit_cost = Decimal('0.00')
                if stock and stock.avg_cost > 0:
                    unit_cost = stock.avg_cost
                elif product.cost_price:
                    unit_cost = product.cost_price
                
                from .packaging import PackagingService
                factor = PackagingService.factor(product)

                count_objects.append(InventoryCount(
                    organization=session.organization,
                    session=session,
                    product=product,
                    variant=None,
                    quantity_expected=current_qty,
                    expected_loose_quantity=(
                        stock.loose_quantity if stock else Decimal('0.000')
                    ),
                    packaging_factor=factor,
                    unit_cost=unit_cost,
                ))
            
            InventoryCount.objects.bulk_create(count_objects)
            
            # Lock stock
            session.status = 'in_progress'
            session.is_stock_locked = True
            session.started_at = timezone.now()
            session.save()
        
        serializer = InventorySessionDetailSerializer(session)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def count(self, request, pk=None):
        """
        Enregistre les comptages pour une ou plusieurs lignes.
        
        Body: { "counts": [{ "id": "<count_id>", "quantity_counted": 10, "notes": "" }, ...] }
        """
        session = self.get_object()
        
        if session.status != 'in_progress':
            return Response(
                {'error': "L'inventaire doit être en cours pour enregistrer des comptages"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        counts_data = request.data.get('counts', [])
        if not counts_data:
            return Response(
                {'error': 'Aucun comptage fourni'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        updated_ids = []
        with transaction.atomic():
            for item in counts_data:
                count_id = item.get('id')
                quantity_counted = item.get('quantity_counted')
                notes = item.get('notes', '')
                
                if count_id is None or quantity_counted is None:
                    continue
                
                try:
                    count = InventoryCount.objects.select_for_update().get(
                        id=count_id,
                        session=session,
                    )
                    # Comptage en « X conditionnements + Y unités » : le modèle
                    # recompose la quantité de base. La saisie simple reste
                    # acceptée pour les produits vendus à l'unité.
                    packages = item.get('counted_package_quantity')
                    loose = item.get('counted_loose_quantity')
                    if count.packaging_factor and (packages is not None or loose is not None):
                        count.counted_package_quantity = Decimal(str(packages or 0))
                        count.counted_loose_quantity = Decimal(str(loose or 0))
                    else:
                        count.quantity_counted = Decimal(str(quantity_counted))
                        count.counted_loose_quantity = Decimal(str(quantity_counted))
                    count.is_counted = True
                    count.counted_by = request.user
                    count.counted_at = timezone.now()
                    if notes:
                        count.notes = notes
                    count.save()
                    updated_ids.append(str(count.id))
                except InventoryCount.DoesNotExist:
                    continue
        
        return Response({
            'status': 'counted',
            'updated_count': len(updated_ids),
            'updated_ids': updated_ids,
        })

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Soumet la session pour révision après le comptage."""
        session = self.get_object()
        
        if session.status != 'in_progress':
            return Response(
                {'error': "Seules les sessions en cours peuvent être soumises"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check that all items have been counted
        uncounted = session.counts.filter(is_counted=False).count()
        if uncounted > 0:
            return Response(
                {'error': f'{uncounted} produit(s) n\'ont pas encore été comptés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Compute summary
        from django.db.models import Sum
        totals = session.counts.aggregate(
            total_expected=Sum('quantity_expected'),
            total_counted=Sum('quantity_counted'),
            total_diff=Sum('quantity_difference'),
            total_diff_value=Sum('difference_value'),
        )
        
        session.total_expected_quantity = totals['total_expected'] or Decimal('0.000')
        session.total_counted_quantity = totals['total_counted'] or Decimal('0.000')
        session.total_difference_quantity = totals['total_diff'] or Decimal('0.000')
        session.total_difference_value = totals['total_diff_value'] or Decimal('0.00')
        session.status = 'review'
        session.completed_at = timezone.now()
        session.save()
        
        serializer = InventorySessionDetailSerializer(session)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def validate(self, request, pk=None):
        """
        Valide la session d'inventaire :
        1. Applique les ajustements de stock pour chaque différence
        2. Crée les mouvements de stock correspondants
        3. Déverrouille le stock
        """
        session = self.get_object()
        
        if session.status != 'review':
            return Response(
                {'error': 'Seules les sessions en révision peuvent être validées'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with transaction.atomic():
            for count in session.counts.filter(is_counted=True).select_related('product'):
                if count.quantity_difference == 0:
                    continue
                
                product_cost = count.unit_cost or (count.product.cost_price or Decimal('0.00'))
                stock, created = Stock.objects.select_for_update().get_or_create(
                    organization=session.organization,
                    product=count.product,
                    variant=count.variant,
                    warehouse=session.warehouse,
                    defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
                )
                
                if not created and stock.avg_cost == 0 and product_cost > 0:
                    stock.avg_cost = product_cost
                
                quantity_before = stock.quantity
                # Le comptage physique constate les deux canaux : ses valeurs
                # écrasent les anciennes, sinon l'affichage continuerait
                # d'annoncer des paquets qui n'existent plus.
                stock.quantity = count.quantity_counted
                if count.packaging_factor:
                    stock.loose_quantity = count.counted_loose_quantity
                    stock.package_quantity = count.counted_package_quantity or Decimal('0.000')
                stock.last_counted_at = timezone.now()
                stock.last_movement_at = timezone.now()
                stock.save()
                
                movement_type = 'adjustment_in' if count.quantity_difference > 0 else 'adjustment_out'
                
                StockMovement.objects.create(
                    organization=session.organization,
                    product=count.product,
                    variant=count.variant,
                    warehouse=session.warehouse,
                    movement_type=movement_type,
                    quantity=count.quantity_difference,
                    unit_cost=count.unit_cost,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    reference_type='inventory_session',
                    reference_id=session.id,
                    notes=f"Inventaire {session.reference}: {session.name}",
                    created_by=request.user
                )
            
            # Unlock stock and finalize
            session.status = 'validated'
            session.is_stock_locked = False
            session.validated_at = timezone.now()
            session.validated_by = request.user
            session.save()
        
        serializer = InventorySessionDetailSerializer(session)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule une session d'inventaire et déverrouille le stock."""
        session = self.get_object()
        
        if session.status in ['validated', 'cancelled']:
            return Response(
                {'error': 'Cette session ne peut pas être annulée'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        session.status = 'cancelled'
        session.is_stock_locked = False
        session.save()
        
        return Response({'status': 'cancelled'})

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

    @action(detail=True, methods=['get'], url_path='print-data')
    def print_data(self, request, pk=None):
        """
        Retourne les données formatées pour l'impression de l'inventaire.
        Inclut les informations de la session et toutes les lignes de comptage.
        """
        session = self.get_object()
        
        counts = session.counts.select_related(
            'product', 'product__category', 'product__unit', 'variant', 'counted_by'
        ).order_by('product__category__name', 'product__name')
        
        # Group by category
        categories_data = {}
        for count in counts:
            cat_name = count.product.category.name if count.product.category else 'Sans catégorie'
            if cat_name not in categories_data:
                categories_data[cat_name] = []
            categories_data[cat_name].append(InventoryCountSerializer(count).data)
        
        return Response({
            'session': InventorySessionListSerializer(session).data,
            'warehouse': {
                'name': session.warehouse.name,
                'code': session.warehouse.code,
                'address': session.warehouse.address,
            },
            'categories': categories_data,
            'summary': {
                'total_products': session.items_total,
                'counted_products': session.items_counted,
                'products_with_difference': session.items_with_difference,
                'total_expected_quantity': str(session.total_expected_quantity),
                'total_counted_quantity': str(session.total_counted_quantity),
                'total_difference_quantity': str(session.total_difference_quantity),
                'total_difference_value': str(session.total_difference_value),
            },
            'printed_at': timezone.now().isoformat(),
            'printed_by': request.user.full_name or request.user.email,
        })

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
