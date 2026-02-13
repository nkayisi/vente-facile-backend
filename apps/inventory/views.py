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

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem,
    InventorySession, InventoryCount
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'branch']
    search_fields = ['name', 'code']
    ordering = ['name']
    
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['warehouse', 'product', 'variant']
    search_fields = ['product__name', 'product__sku']
    ordering_fields = ['quantity', 'last_movement_at']
    ordering = ['-last_movement_at']
    
    select_related_fields = ['product', 'variant', 'warehouse', 'location']
    
    action_permissions = {
        'list': 'stock.view',
        'retrieve': 'stock.view',
        'by_product': 'stock.view',
        'by_warehouse': 'stock.view',
        'low_stock': 'stock.view',
        'expiring': 'stock.view',
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['warehouse', 'product', 'movement_type']
    search_fields = ['product__name', 'product__sku', 'notes']
    ordering_fields = ['created_at', 'quantity']
    ordering = ['-created_at']
    
    select_related_fields = ['product', 'variant', 'warehouse', 'batch', 'created_by']
    
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


# =============================================================================
# INVENTORY SESSION VIEWSET
# =============================================================================

class InventorySessionViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
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
        
        return base_qs

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
                
                count_objects.append(InventoryCount(
                    organization=session.organization,
                    session=session,
                    product=product,
                    variant=None,
                    quantity_expected=current_qty,
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
                    count.quantity_counted = Decimal(str(quantity_counted))
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
                stock.quantity = count.quantity_counted
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
