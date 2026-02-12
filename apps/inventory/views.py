"""
ViewSets DRF pour l'app Inventory.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, F
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission
)
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem
)
from .serializers import (
    WarehouseListSerializer, WarehouseDetailSerializer, WarehouseCreateSerializer,
    StockLocationSerializer,
    StockListSerializer, StockDetailSerializer,
    StockBatchSerializer,
    StockMovementListSerializer, StockMovementDetailSerializer, StockMovementCreateSerializer,
    StockTransferListSerializer, StockTransferDetailSerializer, StockTransferCreateSerializer,
    StockAdjustmentListSerializer, StockAdjustmentDetailSerializer, StockAdjustmentCreateSerializer
)


# =============================================================================
# WAREHOUSE VIEWSET
# =============================================================================

class WarehouseViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'branch']
    search_fields = ['name', 'code']
    ordering = ['name']
    
    select_related_fields = ['branch', 'manager']
    prefetch_related_fields = ['locations']
    
    role_permissions = {
        'list': ['owner', 'admin', 'manager', 'stock_keeper', 'accountant', 'viewer'],
        'retrieve': ['owner', 'admin', 'manager', 'stock_keeper', 'accountant', 'viewer'],
        'create': ['owner', 'admin'],
        'update': ['owner', 'admin'],
        'partial_update': ['owner', 'admin'],
        'destroy': ['owner', 'admin'],
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return WarehouseListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return WarehouseCreateSerializer
        return WarehouseDetailSerializer

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

class StockLocationViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des emplacements de stock.
    
    Endpoints:
    - GET /stock-locations/ : Liste des emplacements
    - POST /stock-locations/ : Créer un emplacement
    - GET /stock-locations/{id}/ : Détail d'un emplacement
    - PUT/PATCH /stock-locations/{id}/ : Modifier un emplacement
    - DELETE /stock-locations/{id}/ : Supprimer un emplacement
    """
    
    queryset = StockLocation.objects.all()
    serializer_class = StockLocationSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['warehouse', 'is_active', 'parent']
    search_fields = ['name', 'code']
    
    select_related_fields = ['warehouse', 'parent']
    
    role_permissions = {
        'list': ['owner', 'admin', 'manager', 'stock_keeper'],
        'retrieve': ['owner', 'admin', 'manager', 'stock_keeper'],
        'create': ['owner', 'admin', 'manager'],
        'update': ['owner', 'admin', 'manager'],
        'partial_update': ['owner', 'admin', 'manager'],
        'destroy': ['owner', 'admin'],
    }


# =============================================================================
# STOCK VIEWSET
# =============================================================================

class StockViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['warehouse', 'product', 'variant']
    search_fields = ['product__name', 'product__sku']
    ordering_fields = ['quantity', 'last_movement_at']
    ordering = ['-last_movement_at']
    
    select_related_fields = ['product', 'variant', 'warehouse', 'location']
    
    role_permissions = {
        'list': ['owner', 'admin', 'manager', 'stock_keeper', 'cashier', 'accountant', 'viewer'],
        'retrieve': ['owner', 'admin', 'manager', 'stock_keeper', 'cashier', 'accountant', 'viewer'],
    }
    
    # Stock est en lecture seule - les modifications passent par les mouvements
    http_method_names = ['get', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'list':
            return StockListSerializer
        return StockDetailSerializer

    @action(detail=False, methods=['get'], url_path='by-product/(?P<product_id>[^/.]+)')
    def by_product(self, request, product_id=None):
        """Retourne le stock d'un produit dans tous les entrepôts."""
        organization = self.get_organization()
        stocks = Stock.objects.filter(
            organization=organization,
            product_id=product_id
        ).select_related('warehouse', 'location')
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='by-warehouse/(?P<warehouse_id>[^/.]+)')
    def by_warehouse(self, request, warehouse_id=None):
        """Retourne tout le stock d'un entrepôt."""
        organization = self.get_organization()
        stocks = Stock.objects.filter(
            organization=organization,
            warehouse_id=warehouse_id
        ).select_related('product', 'variant')
        
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
        ).select_related('product', 'warehouse')
        
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
        
        serializer = StockBatchSerializer(batches, many=True)
        return Response(serializer.data)


# =============================================================================
# STOCK BATCH VIEWSET
# =============================================================================

class StockBatchViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
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

class StockMovementViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les mouvements de stock.
    
    Endpoints:
    - GET /stock-movements/ : Liste des mouvements
    - POST /stock-movements/ : Créer un mouvement manuel
    - GET /stock-movements/{id}/ : Détail d'un mouvement
    """
    
    queryset = StockMovement.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['warehouse', 'product', 'movement_type']
    search_fields = ['product__name', 'product__sku', 'notes']
    ordering_fields = ['created_at', 'quantity']
    ordering = ['-created_at']
    
    select_related_fields = ['product', 'variant', 'warehouse', 'batch', 'created_by']
    
    role_permissions = {
        'list': ['owner', 'admin', 'manager', 'stock_keeper', 'accountant'],
        'retrieve': ['owner', 'admin', 'manager', 'stock_keeper', 'accountant'],
        'create': ['owner', 'admin', 'manager', 'stock_keeper'],
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
        organization = self.get_organization()
        data = serializer.validated_data
        
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
        
        # Si le stock existait mais sans avg_cost, initialiser depuis le produit
        if not created and stock.avg_cost == 0 and product_cost > 0:
            stock.avg_cost = product_cost
        
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
        
        stock.quantity += data['quantity']
        stock.last_movement_at = timezone.now()
        stock.save()
        
        # Créer le mouvement
        serializer.save(
            organization=organization,
            quantity_before=quantity_before,
            quantity_after=stock.quantity,
            created_by=self.request.user
        )


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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'source_warehouse', 'destination_warehouse']
    search_fields = ['reference']
    ordering = ['-requested_at']
    
    select_related_fields = ['source_warehouse', 'destination_warehouse', 'requested_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    role_permissions = {
        'list': ['owner', 'admin', 'manager', 'stock_keeper'],
        'retrieve': ['owner', 'admin', 'manager', 'stock_keeper'],
        'create': ['owner', 'admin', 'manager', 'stock_keeper'],
        'update': ['owner', 'admin', 'manager'],
        'partial_update': ['owner', 'admin', 'manager'],
        'destroy': ['owner', 'admin'],
        'approve': ['owner', 'admin', 'manager'],
        'ship': ['owner', 'admin', 'manager', 'stock_keeper'],
        'receive': ['owner', 'admin', 'manager', 'stock_keeper'],
        'cancel': ['owner', 'admin', 'manager'],
    }

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
        
        with transaction.atomic():
            # Déduire le stock de l'entrepôt source
            for item in transfer.items.all():
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
                stock.quantity -= item.quantity_requested
                stock.last_movement_at = timezone.now()
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
        
        with transaction.atomic():
            for item in transfer.items.all():
                # Chercher la quantité reçue dans les données
                received_qty = None
                for ri in received_items:
                    if str(ri.get('id')) == str(item.id):
                        received_qty = Decimal(str(ri.get('quantity_received')))
                        break
                
                if received_qty is None:
                    received_qty = item.quantity_shipped
                
                item.quantity_received = received_qty
                item.save()
                
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
                
                stock.quantity += received_qty
                stock.last_movement_at = timezone.now()
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
        
        with transaction.atomic():
            # Si déjà expédié, remettre le stock
            if transfer.status == 'in_transit':
                for item in transfer.items.all():
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
                    stock.quantity += quantity_to_restore
                    stock.last_movement_at = timezone.now()
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

class StockAdjustmentViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'adjustment_type', 'warehouse']
    search_fields = ['reference', 'reason']
    ordering = ['-created_at']
    
    select_related_fields = ['warehouse', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    role_permissions = {
        'list': ['owner', 'admin', 'manager', 'stock_keeper', 'accountant'],
        'retrieve': ['owner', 'admin', 'manager', 'stock_keeper', 'accountant'],
        'create': ['owner', 'admin', 'manager', 'stock_keeper'],
        'update': ['owner', 'admin', 'manager'],
        'partial_update': ['owner', 'admin', 'manager'],
        'destroy': ['owner', 'admin'],
        'approve': ['owner', 'admin', 'manager'],
        'reject': ['owner', 'admin', 'manager'],
    }

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
        
        with transaction.atomic():
            # Appliquer les ajustements
            for item in adjustment.items.all():
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
                
                stock.quantity = item.quantity_counted
                stock.last_counted_at = timezone.now()
                stock.last_movement_at = timezone.now()
                stock.save()
                
                # Déterminer le type de mouvement
                if item.quantity_difference > 0:
                    movement_type = 'adjustment_in'
                else:
                    movement_type = 'adjustment_out'
                
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
