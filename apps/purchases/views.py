"""
ViewSets DRF pour l'app Purchases.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum
from django.utils import timezone
from decimal import Decimal

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from .models import (
    PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem,
    SupplierPayment, PurchaseReturn
)
from .serializers import (
    PurchaseOrderListSerializer, PurchaseOrderDetailSerializer, PurchaseOrderCreateSerializer,
    GoodsReceiptListSerializer, GoodsReceiptDetailSerializer, GoodsReceiptCreateSerializer,
    SupplierPaymentListSerializer, SupplierPaymentDetailSerializer, SupplierPaymentCreateSerializer,
    PurchaseReturnListSerializer, PurchaseReturnDetailSerializer, PurchaseReturnCreateSerializer
)


# =============================================================================
# PURCHASE ORDER VIEWSET
# =============================================================================

class PurchaseOrderViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des commandes d'achat.
    
    Endpoints:
    - GET /purchase-orders/ : Liste des commandes
    - POST /purchase-orders/ : Créer une commande
    - GET /purchase-orders/{id}/ : Détail d'une commande
    - PUT/PATCH /purchase-orders/{id}/ : Modifier une commande
    - DELETE /purchase-orders/{id}/ : Supprimer une commande
    - POST /purchase-orders/{id}/approve/ : Approuver une commande
    - POST /purchase-orders/{id}/send/ : Marquer comme envoyée
    - POST /purchase-orders/{id}/cancel/ : Annuler une commande
    """
    
    queryset = PurchaseOrder.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'supplier', 'warehouse']
    search_fields = ['reference', 'supplier__name']
    ordering_fields = ['order_date', 'total', 'reference']
    ordering = ['-order_date']
    
    select_related_fields = ['supplier', 'warehouse', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.create',
        'update': 'purchases.edit',
        'partial_update': 'purchases.edit',
        'destroy': 'purchases.edit',
        'approve': 'purchases.edit',
        'send': 'purchases.edit',
        'cancel': 'purchases.edit',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return PurchaseOrderListSerializer
        elif self.action == 'create':
            return PurchaseOrderCreateSerializer
        return PurchaseOrderDetailSerializer

    def get_queryset(self):
        """Filtre supplémentaire par date."""
        queryset = super().get_queryset()
        
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        
        if date_from:
            queryset = queryset.filter(order_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(order_date__lte=date_to)
        
        return queryset

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve une commande d'achat."""
        po = self.get_object()
        
        if po.status != 'draft':
            return Response(
                {'error': 'Seules les commandes en brouillon peuvent être approuvées'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        po.status = 'confirmed'
        po.approved_by = request.user
        po.save()
        
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Marque une commande comme envoyée au fournisseur."""
        po = self.get_object()
        
        if po.status not in ['draft', 'confirmed']:
            return Response(
                {'error': 'Cette commande ne peut pas être envoyée'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        po.status = 'sent'
        po.save()
        
        return Response({'status': 'sent'})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule une commande d'achat."""
        po = self.get_object()
        
        if po.status in ['received', 'cancelled']:
            return Response(
                {'error': 'Cette commande ne peut pas être annulée'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        po.status = 'cancelled'
        po.save()
        
        return Response({'status': 'cancelled'})

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Retourne les commandes en attente de réception."""
        organization = self.get_organization()
        
        pos = PurchaseOrder.objects.filter(
            organization=organization,
            status__in=['sent', 'confirmed', 'partially_received'],
            is_deleted=False
        ).select_related('supplier').order_by('expected_date')
        
        serializer = PurchaseOrderListSerializer(pos, many=True)
        return Response(serializer.data)


# =============================================================================
# GOODS RECEIPT VIEWSET
# =============================================================================

class GoodsReceiptViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des réceptions de marchandises.
    
    Endpoints:
    - GET /goods-receipts/ : Liste des réceptions
    - POST /goods-receipts/ : Créer une réception
    - GET /goods-receipts/{id}/ : Détail d'une réception
    - POST /goods-receipts/{id}/complete/ : Valider et mettre à jour le stock
    - POST /goods-receipts/{id}/cancel/ : Annuler une réception
    """
    
    queryset = GoodsReceipt.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'purchase_order', 'warehouse']
    search_fields = ['reference', 'supplier_invoice']
    ordering = ['-receipt_date']
    
    select_related_fields = ['purchase_order', 'warehouse', 'received_by']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.receive',
        'complete': 'purchases.receive',
        'cancel': 'purchases.edit',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return GoodsReceiptListSerializer
        elif self.action == 'create':
            return GoodsReceiptCreateSerializer
        return GoodsReceiptDetailSerializer

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Valide la réception et met à jour le stock."""
        grn = self.get_object()
        
        if grn.status != 'draft':
            return Response(
                {'error': 'Cette réception est déjà validée'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from apps.inventory.models import Stock, StockMovement, StockBatch
        
        for item in grn.items.all():
            if item.quantity_accepted > 0:
                # Créer ou mettre à jour le stock
                stock, _ = Stock.objects.get_or_create(
                    organization=grn.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=grn.warehouse,
                    defaults={'quantity': 0, 'avg_cost': item.unit_cost}
                )
                
                quantity_before = stock.quantity
                
                # Calculer le nouveau coût moyen pondéré
                total_value = (stock.quantity * stock.avg_cost) + (item.quantity_accepted * item.unit_cost)
                new_quantity = stock.quantity + item.quantity_accepted
                if new_quantity > 0:
                    stock.avg_cost = total_value / new_quantity
                
                stock.quantity = new_quantity
                stock.last_movement_at = timezone.now()
                stock.save()
                
                # Créer le lot si batch_number fourni
                if item.batch_number:
                    StockBatch.objects.create(
                        organization=grn.organization,
                        product=item.product,
                        variant=item.variant,
                        warehouse=grn.warehouse,
                        batch_number=item.batch_number,
                        quantity=item.quantity_accepted,
                        cost_price=item.unit_cost,
                        expiry_date=item.expiry_date
                    )
                
                # Créer le mouvement de stock
                StockMovement.objects.create(
                    organization=grn.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=grn.warehouse,
                    movement_type='purchase',
                    quantity=item.quantity_accepted,
                    unit_cost=item.unit_cost,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    reference_type='goods_receipt',
                    reference_id=grn.id,
                    created_by=request.user
                )
        
        # Mettre à jour le statut de la commande
        po = grn.purchase_order
        total_ordered = po.items.aggregate(total=Sum('quantity_ordered'))['total'] or 0
        total_received = po.items.aggregate(total=Sum('quantity_received'))['total'] or 0
        
        if total_received >= total_ordered:
            po.status = 'received'
        else:
            po.status = 'partially_received'
        po.save()
        
        grn.status = 'completed'
        grn.save()
        
        return Response({'status': 'completed'})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule une réception (avant validation uniquement)."""
        grn = self.get_object()
        
        if grn.status != 'draft':
            return Response(
                {'error': 'Seules les réceptions en brouillon peuvent être annulées'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Annuler les quantités reçues sur la commande
        for item in grn.items.all():
            if item.purchase_order_item:
                item.purchase_order_item.quantity_received -= item.quantity_accepted
                item.purchase_order_item.save()
        
        grn.status = 'cancelled'
        grn.save()
        
        return Response({'status': 'cancelled'})


# =============================================================================
# SUPPLIER PAYMENT VIEWSET
# =============================================================================

class SupplierPaymentViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des paiements fournisseurs.
    
    Endpoints:
    - GET /supplier-payments/ : Liste des paiements
    - POST /supplier-payments/ : Créer un paiement
    - GET /supplier-payments/{id}/ : Détail d'un paiement
    - POST /supplier-payments/{id}/cancel/ : Annuler un paiement
    """
    
    queryset = SupplierPayment.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'supplier', 'payment_method']
    search_fields = ['reference', 'supplier__name', 'payment_reference']
    ordering = ['-payment_date']
    
    select_related_fields = ['supplier', 'payment_method', 'created_by']
    prefetch_related_fields = ['allocations', 'allocations__purchase_order']
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.edit',
        'cancel': 'purchases.edit',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return SupplierPaymentListSerializer
        elif self.action == 'create':
            return SupplierPaymentCreateSerializer
        return SupplierPaymentDetailSerializer

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule un paiement fournisseur."""
        payment = self.get_object()
        
        if payment.status == 'cancelled':
            return Response(
                {'error': 'Ce paiement est déjà annulé'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Annuler les allocations
        for alloc in payment.allocations.all():
            po = alloc.purchase_order
            po.amount_paid -= alloc.amount
            po.amount_due = po.total - po.amount_paid
            po.save()
        
        # Remettre le solde fournisseur
        supplier = payment.supplier
        supplier.current_balance += payment.amount
        supplier.save()
        
        payment.status = 'cancelled'
        payment.save()
        
        return Response({'status': 'cancelled'})


# =============================================================================
# PURCHASE RETURN VIEWSET
# =============================================================================

class PurchaseReturnViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des retours fournisseurs.
    
    Endpoints:
    - GET /purchase-returns/ : Liste des retours
    - POST /purchase-returns/ : Créer un retour
    - GET /purchase-returns/{id}/ : Détail d'un retour
    - POST /purchase-returns/{id}/approve/ : Approuver et déduire le stock
    - POST /purchase-returns/{id}/ship/ : Marquer comme expédié
    - POST /purchase-returns/{id}/cancel/ : Annuler un retour
    """
    
    queryset = PurchaseReturn.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'supplier']
    search_fields = ['reference', 'supplier__name']
    ordering = ['-return_date']
    
    select_related_fields = ['supplier', 'purchase_order', 'warehouse', 'created_by']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.edit',
        'approve': 'purchases.edit',
        'ship': 'purchases.receive',
        'cancel': 'purchases.edit',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return PurchaseReturnListSerializer
        elif self.action == 'create':
            return PurchaseReturnCreateSerializer
        return PurchaseReturnDetailSerializer

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve un retour et déduit le stock."""
        purchase_return = self.get_object()
        
        if purchase_return.status != 'draft':
            return Response(
                {'error': 'Seuls les retours en brouillon peuvent être approuvés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from apps.inventory.models import Stock, StockMovement
        
        for item in purchase_return.items.all():
            stock = Stock.objects.filter(
                organization=purchase_return.organization,
                product=item.product,
                variant=item.variant,
                warehouse=purchase_return.warehouse
            ).first()
            
            if stock:
                quantity_before = stock.quantity
                stock.quantity -= item.quantity
                stock.last_movement_at = timezone.now()
                stock.save()
                
                StockMovement.objects.create(
                    organization=purchase_return.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=purchase_return.warehouse,
                    batch=item.batch,
                    movement_type='return_out',
                    quantity=-item.quantity,
                    unit_cost=item.unit_cost,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    reference_type='purchase_return',
                    reference_id=purchase_return.id,
                    created_by=request.user
                )
        
        purchase_return.status = 'approved'
        purchase_return.save()
        
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def ship(self, request, pk=None):
        """Marque un retour comme expédié."""
        purchase_return = self.get_object()
        
        if purchase_return.status != 'approved':
            return Response(
                {'error': 'Seuls les retours approuvés peuvent être expédiés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        purchase_return.status = 'shipped'
        purchase_return.save()
        
        return Response({'status': 'shipped'})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule un retour."""
        purchase_return = self.get_object()
        
        if purchase_return.status in ['shipped', 'completed', 'cancelled']:
            return Response(
                {'error': 'Ce retour ne peut pas être annulé'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Si approuvé, remettre le stock
        if purchase_return.status == 'approved':
            from apps.inventory.models import Stock
            
            for item in purchase_return.items.all():
                stock, _ = Stock.objects.get_or_create(
                    organization=purchase_return.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=purchase_return.warehouse,
                    defaults={'quantity': 0}
                )
                stock.quantity += item.quantity
                stock.save()
        
        purchase_return.status = 'cancelled'
        purchase_return.save()
        
        return Response({'status': 'cancelled'})
