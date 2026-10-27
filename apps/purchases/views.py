"""
ViewSets DRF pour l'app Purchases.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from decimal import Decimal

from apps.core.api_mixins import (
    ExportableListMixin,
    ExportResponseMixin,
    TenantViewSetMixin,
    AuditMixin,
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
    describe_filters,
)
from apps.core.report_params import perimeter_filters, period_label
from .filters import (
    GoodsReceiptFilter, PurchaseOrderFilter, PurchaseReturnFilter,
    SupplierPaymentFilter,
)
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_warehouse_allowed_for_request,
    get_membership_for_request,
)
from apps.contacts import supplier_debt
from apps.core.api_permissions import (
    DENY,
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from .models import (
    PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem,
    SupplierPayment, PurchaseReturn, Requisition
)
from .serializers import (
    PurchaseOrderListSerializer, PurchaseOrderDetailSerializer, PurchaseOrderCreateSerializer,
    GoodsReceiptListSerializer, GoodsReceiptDetailSerializer, GoodsReceiptCreateSerializer,
    SupplierPaymentListSerializer, SupplierPaymentDetailSerializer, SupplierPaymentCreateSerializer,
    PurchaseReturnListSerializer, PurchaseReturnDetailSerializer, PurchaseReturnCreateSerializer,
    RequisitionSerializer, RequisitionCreateSerializer,
)
from .services import (
    RequisitionRefusee,
    create_requisition,
    lignes_proposees,
    transition_requisition,
)


# =============================================================================
# PURCHASE ORDER VIEWSET
# =============================================================================

class PurchaseOrderViewSet(
    ExportableListMixin,
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = PurchaseOrderFilter
    search_fields = ['reference', 'supplier__name']
    ordering_fields = ['order_date', 'total', 'reference']
    ordering = ['-order_date']
    
    select_related_fields = ['supplier', 'warehouse', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']

    # PO peut avoir un ``warehouse`` nullable (legacy/brouillons).
    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = True
    warehouse_write_required = False
    warehouse_write_allow_none = True
    
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
        # Lecture des commandes en attente de réception. Non déclarée, donc
        # refusée à tous : l'endpoint figure au schéma OpenAPI et ne répondait
        # à personne.
        'pending': 'purchases.view',
        # Un document d'achat ne dit rien de plus que la liste qu'il
        # reproduit : même droit, et surtout pas un droit à part - une
        # action non déclarée est REFUSÉE à tous, et c'est ainsi que
        # `pending` et `locked_products` ont dormi sans appelant.
        'export': 'purchases.view',
    }

    export_basename = 'commandes-achat'

    def build_export_spec(self, request, queryset):
        """Décrit le journal des commandes tel que l'écran le montre."""
        from apps.settings.services import CurrencyService

        from .reports import build_purchase_orders_report

        organization = self.get_organization()
        params = request.query_params
        statuts = dict(PurchaseOrder.Status.choices)
        filtres = describe_filters(params, {
            'status': ('Statut', lambda v: statuts.get(v, v), 'Tous'),
            'search': ('Recherche', lambda v: v, '-'),
        })
        filtres.extend(perimeter_filters(params, organization))
        periode = period_label(params)
        if periode:
            filtres.insert(0, ('Période', periode))

        return build_purchase_orders_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filtres,
        )

    def create(self, request, *args, **kwargs):
        """
        Crée la pièce et rend son DÉTAIL complet (avec `id` et `reference`).

        ┌──────────────────────────────────────────────────────────────────────┐
        │ SANS CET OVERRIDE, L'ÉCRAN NAVIGUE VERS `undefined`.                │
        │                                                                      │
        │ DRF répond à un POST avec le serializer d'ÉCRITURE, qui ne déclare   │
        │ ni `id`, ni `reference`, ni aucun nom résolu. Les quatre écrans       │
        │ d'achat lisent `r.data.id` pour ouvrir la fiche qu'ils viennent de   │
        │ créer, et `r.data.reference` pour la nommer dans leur message : les  │
        │ deux valaient `undefined`, et l'URL d'arrivée était un 404.          │
        │                                                                      │
        │ C'est le défaut exact corrigé sur `POST /expenses/`, où le reçu de   │
        │ dépense sortait SANS NUMÉRO et le fichier s'appelait                 │
        │ `depense-undefined.pdf`. `createExpense` annonçait pourtant          │
        │ `Promise<ApiResponse<Expense>>` : axios rend `any`, et TypeScript    │
        │ n'avait rien à dire.                                                  │
        └──────────────────────────────────────────────────────────────────────┘

        Calqué sur `SaleViewSet.create` et `ExpenseViewSet.create`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        piece = self.perform_create(serializer)
        if piece is None:
            piece = serializer.instance
        piece.refresh_from_db()
        return Response(
            PurchaseOrderDetailSerializer(piece, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return PurchaseOrderListSerializer
        elif self.action == 'create':
            return PurchaseOrderCreateSerializer
        return PurchaseOrderDetailSerializer

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
        """Retourne les commandes en attente de réception avec pagination."""
        organization = self.get_organization()
        
        pos = PurchaseOrder.objects.filter(
            organization=organization,
            status__in=['sent', 'confirmed', 'partially_received'],
            is_deleted=False
        ).select_related('supplier').order_by('expected_date')

        # Restreindre au périmètre du membre
        membership = get_membership_for_request(request)
        if membership:
            allowed_ids = accessible_warehouse_ids(membership)
            if allowed_ids is not None:
                from django.db.models import Q
                pos = pos.filter(
                    Q(warehouse_id__in=allowed_ids) | Q(warehouse__isnull=True)
                )
        
        page = self.paginate_queryset(pos)
        if page is not None:
            serializer = PurchaseOrderListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = PurchaseOrderListSerializer(pos, many=True)
        return Response(serializer.data)


# =============================================================================
# GOODS RECEIPT VIEWSET
# =============================================================================

class GoodsReceiptViewSet(
    ExportableListMixin,
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = GoodsReceiptFilter
    search_fields = ['reference', 'supplier_invoice']
    ordering = ['-receipt_date']
    
    select_related_fields = ['purchase_order', 'warehouse', 'received_by']
    prefetch_related_fields = ['items', 'items__product']

    # GoodsReceipt.warehouse est obligatoire en base (NOT NULL).
    warehouse_scope_field = 'warehouse_id'
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.receive',
        'complete': 'purchases.receive',
        'cancel': 'purchases.edit',
        # Une réception se complète ou s'annule, elle ne se réécrit pas :
        # ses lignes ont déjà bougé le stock et le coût moyen pondéré.
        'export': 'purchases.view',
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    export_basename = 'receptions'

    def build_export_spec(self, request, queryset):
        """Décrit le journal des réceptions tel que l'écran le montre.

        Le queryset arrive DÉJÀ filtré et NON paginé (`ExportableListMixin`) :
        c'est ce qui garantit que le fichier couvre le même périmètre que
        l'écran qui l'a déclenché, et non ses cinquante premières lignes.
        """
        from apps.settings.services import CurrencyService

        from .reports import build_goods_receipts_report

        organization = self.get_organization()
        params = request.query_params
        statuts = dict(GoodsReceipt.Status.choices)
        filtres = describe_filters(params, {
            'status': ('Statut', lambda v: statuts.get(v, v), 'Tous'),
            'search': ('Recherche', lambda v: v, '-'),
        })
        # Le PÉRIMÈTRE, et il n'est jamais tu : la liste accepte `warehouse` et
        # `user`, donc le fichier serait filtré sans le dire.
        filtres.extend(perimeter_filters(params, organization))

        return build_goods_receipts_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filtres,
        )

    def create(self, request, *args, **kwargs):
        """
        Crée la pièce et rend son DÉTAIL complet (avec `id` et `reference`).

        ┌──────────────────────────────────────────────────────────────────────┐
        │ SANS CET OVERRIDE, L'ÉCRAN NAVIGUE VERS `undefined`.                │
        │                                                                      │
        │ DRF répond à un POST avec le serializer d'ÉCRITURE, qui ne déclare   │
        │ ni `id`, ni `reference`, ni aucun nom résolu. Les quatre écrans       │
        │ d'achat lisent `r.data.id` pour ouvrir la fiche qu'ils viennent de   │
        │ créer, et `r.data.reference` pour la nommer dans leur message : les  │
        │ deux valaient `undefined`, et l'URL d'arrivée était un 404.          │
        │                                                                      │
        │ C'est le défaut exact corrigé sur `POST /expenses/`, où le reçu de   │
        │ dépense sortait SANS NUMÉRO et le fichier s'appelait                 │
        │ `depense-undefined.pdf`. `createExpense` annonçait pourtant          │
        │ `Promise<ApiResponse<Expense>>` : axios rend `any`, et TypeScript    │
        │ n'avait rien à dire.                                                  │
        └──────────────────────────────────────────────────────────────────────┘

        Calqué sur `SaleViewSet.create` et `ExpenseViewSet.create`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        piece = self.perform_create(serializer)
        if piece is None:
            piece = serializer.instance
        piece.refresh_from_db()
        return Response(
            GoodsReceiptDetailSerializer(piece, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return GoodsReceiptListSerializer
        elif self.action == 'create':
            return GoodsReceiptCreateSerializer
        return GoodsReceiptDetailSerializer

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Valide la réception : le stock entre, la commande avance, la dette s'inscrit.

        Le corps vit dans `services.completer_reception`. Il était écrit ICI, et
        trois règles y avaient dérivé : la quantité reçue s'incrémentait dès le
        brouillon, le statut de la commande se décidait sur une somme
        inter-articles, et la dette fournisseur ne s'inscrivait nulle part.
        """
        from .services import TransitionReceptionRefusee, completer_reception

        grn = self.get_object()
        try:
            completer_reception(grn, request.user)
        except TransitionReceptionRefusee as exc:
            return Response(
                {'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({'status': 'completed'})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule une réception (avant validation uniquement)."""
        from .services import TransitionReceptionRefusee, annuler_reception

        grn = self.get_object()
        try:
            annuler_reception(grn, request.user)
        except TransitionReceptionRefusee as exc:
            return Response(
                {'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({'status': 'cancelled'})


# =============================================================================
# SUPPLIER PAYMENT VIEWSET
# =============================================================================

class SupplierPaymentViewSet(
    ExportableListMixin,
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des paiements fournisseurs.
    
    Endpoints:
    - GET /supplier-payments/ : Liste des paiements
    - POST /supplier-payments/ : Créer un paiement
    - GET /supplier-payments/{id}/ : Détail d'un paiement
    - POST /supplier-payments/{id}/cancel/ : Annuler un paiement
    """
    
    queryset = SupplierPayment.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = SupplierPaymentFilter
    search_fields = ['reference', 'supplier__name', 'payment_reference']
    ordering = ['-payment_date']
    
    select_related_fields = ['supplier', 'payment_method', 'created_by']
    prefetch_related_fields = ['allocations', 'allocations__purchase_order']

    # Filtre par l'entrepôt de la PO la plus liée (au moins une allocation).
    # ``include_null=True`` car certains paiements peuvent être créés sans
    # allocation (rare mais possible).
    warehouse_scope_field = 'allocations__purchase_order__warehouse_id'
    warehouse_scope_include_null = True
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.edit',
        'cancel': 'purchases.edit',
        # Un règlement fournisseur s'annule, il ne se corrige pas : le
        # réécrire changerait un montant déjà imputé à des factures.
        'export': 'purchases.view',
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    export_basename = 'reglements-fournisseurs'

    def build_export_spec(self, request, queryset):
        """Décrit le journal des règlements tel que l'écran le montre."""
        from apps.settings.services import CurrencyService

        from .reports import build_supplier_payments_report

        organization = self.get_organization()
        params = request.query_params
        statuts = dict(SupplierPayment.Status.choices)
        filtres = describe_filters(params, {
            'status': ('Statut', lambda v: statuts.get(v, v), 'Tous'),
            'search': ('Recherche', lambda v: v, '-'),
        })
        filtres.extend(perimeter_filters(params, organization))

        return build_supplier_payments_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filtres,
        )

    def get_queryset(self):
        return super().get_queryset().distinct()

    def create(self, request, *args, **kwargs):
        """
        Crée la pièce et rend son DÉTAIL complet (avec `id` et `reference`).

        ┌──────────────────────────────────────────────────────────────────────┐
        │ SANS CET OVERRIDE, L'ÉCRAN NAVIGUE VERS `undefined`.                │
        │                                                                      │
        │ DRF répond à un POST avec le serializer d'ÉCRITURE, qui ne déclare   │
        │ ni `id`, ni `reference`, ni aucun nom résolu. Les quatre écrans       │
        │ d'achat lisent `r.data.id` pour ouvrir la fiche qu'ils viennent de   │
        │ créer, et `r.data.reference` pour la nommer dans leur message : les  │
        │ deux valaient `undefined`, et l'URL d'arrivée était un 404.          │
        │                                                                      │
        │ C'est le défaut exact corrigé sur `POST /expenses/`, où le reçu de   │
        │ dépense sortait SANS NUMÉRO et le fichier s'appelait                 │
        │ `depense-undefined.pdf`. `createExpense` annonçait pourtant          │
        │ `Promise<ApiResponse<Expense>>` : axios rend `any`, et TypeScript    │
        │ n'avait rien à dire.                                                  │
        └──────────────────────────────────────────────────────────────────────┘

        Calqué sur `SaleViewSet.create` et `ExpenseViewSet.create`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        piece = self.perform_create(serializer)
        if piece is None:
            piece = serializer.instance
        piece.refresh_from_db()
        return Response(
            SupplierPaymentDetailSerializer(piece, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return SupplierPaymentListSerializer
        elif self.action == 'create':
            return SupplierPaymentCreateSerializer
        return SupplierPaymentDetailSerializer

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def cancel(self, request, pk=None):
        """Annule un paiement fournisseur.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ CE CORPS N'ÉTAIT PAS ATOMIQUE, ET LA DETTE SE RECRÉDITAIT DEUX FOIS.│
        │                                                                      │
        │ `ATOMIC_REQUESTS` est à `False` (et doit y rester, le journal de     │
        │ synchronisation en dépend) : il n'y avait donc AUCUNE transaction    │
        │ autour de ces quatre écritures. Or `supplier_debt.reverse_payment`   │
        │ porte son propre `@transaction.atomic` et COMMITE seul : un échec du │
        │ mouvement de caisse ou du `save()` final laissait la dette déjà      │
        │ restituée pendant que `status` restait `completed`. Le seul garde-fou │
        │ d'idempotence étant ce statut, un second essai recréditait la dette   │
        │ une seconde fois et redécrémentait `amount_paid` des commandes.      │
        │                                                                      │
        │ Le verrou sur la ligne ferme la même porte côté concurrence : deux   │
        │ annulations simultanées franchissaient toutes deux le contrôle de    │
        │ statut.                                                              │
        └──────────────────────────────────────────────────────────────────────┘
        """
        payment = self.get_object()
        verrouille = SupplierPayment.objects.select_for_update().filter(
            pk=payment.pk
        ).first()
        if verrouille is None:
            return Response(
                {'error': 'Ce paiement est introuvable'},
                status=status.HTTP_404_NOT_FOUND
            )
        payment.status = verrouille.status

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
        
        # Restitue la dette que ce règlement avait éteinte. Écrire
        # `current_balance += amount` à la main, c'est ce que faisaient les
        # trois sites d'origine : aucun grand livre, aucune devise, et le
        # scalaire dérivait des soldes par devise qu'il résume.
        supplier = payment.supplier
        supplier_debt.reverse_payment(
            supplier, payment.amount,
            currency=payment.currency,
            exchange_rate=payment.exchange_rate,
            supplier_payment=payment,
            reference=payment.reference,
            notes=f"Annulation du règlement {payment.reference}",
            user=request.user,
        )
        
        # Enregistrer le mouvement de caisse inverse (remboursement fournisseur).
        # Passe par `_movement` comme tous les autres flux : la devise et le taux
        # y sont résolus, et `balance_after` est suivi PAR DEVISE.
        #
        # Devise du règlement annulé, pour rester symétrique de l'entrée créée
        # par `record_purchase_payment`.
        from apps.cashbook.services import _movement
        first_alloc = payment.allocations.first()
        _movement(
            payment.organization,
            direction='in',
            movement_type='supplier_refund',
            amount=payment.amount,
            currency=payment.currency,
            exchange_rate=payment.exchange_rate,
            description=f"Annulation paiement fournisseur {payment.reference} - {supplier.name}",
            purchase_order=first_alloc.purchase_order if first_alloc else None,
            supplier=supplier,
            user=request.user,
        )
        
        payment.status = 'cancelled'
        payment.save()
        
        return Response({'status': 'cancelled'})


# =============================================================================
# PURCHASE RETURN VIEWSET
# =============================================================================

class PurchaseReturnViewSet(
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
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
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = PurchaseReturnFilter
    search_fields = ['reference', 'supplier__name']
    ordering = ['-return_date']
    
    select_related_fields = ['supplier', 'purchase_order', 'warehouse', 'created_by']
    prefetch_related_fields = ['items', 'items__product']

    # PurchaseReturn.warehouse est obligatoire en base.
    warehouse_scope_field = 'warehouse_id'
    
    action_permissions = {
        'list': 'purchases.view',
        'retrieve': 'purchases.view',
        'create': 'purchases.edit',
        'approve': 'purchases.edit',
        'ship': 'purchases.receive',
        'cancel': 'purchases.edit',
        # Un retour fournisseur avance par ses transitions (`approve`,
        # `ship`, `cancel`). Le modifier après coup ferait diverger le stock
        # sorti de la pièce qui le justifie.
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    def create(self, request, *args, **kwargs):
        """
        Crée la pièce et rend son DÉTAIL complet (avec `id` et `reference`).

        ┌──────────────────────────────────────────────────────────────────────┐
        │ SANS CET OVERRIDE, L'ÉCRAN NAVIGUE VERS `undefined`.                │
        │                                                                      │
        │ DRF répond à un POST avec le serializer d'ÉCRITURE, qui ne déclare   │
        │ ni `id`, ni `reference`, ni aucun nom résolu. Les quatre écrans       │
        │ d'achat lisent `r.data.id` pour ouvrir la fiche qu'ils viennent de   │
        │ créer, et `r.data.reference` pour la nommer dans leur message : les  │
        │ deux valaient `undefined`, et l'URL d'arrivée était un 404.          │
        │                                                                      │
        │ C'est le défaut exact corrigé sur `POST /expenses/`, où le reçu de   │
        │ dépense sortait SANS NUMÉRO et le fichier s'appelait                 │
        │ `depense-undefined.pdf`. `createExpense` annonçait pourtant          │
        │ `Promise<ApiResponse<Expense>>` : axios rend `any`, et TypeScript    │
        │ n'avait rien à dire.                                                  │
        └──────────────────────────────────────────────────────────────────────┘

        Calqué sur `SaleViewSet.create` et `ExpenseViewSet.create`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        piece = self.perform_create(serializer)
        if piece is None:
            piece = serializer.instance
        piece.refresh_from_db()
        return Response(
            PurchaseReturnDetailSerializer(piece, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return PurchaseReturnListSerializer
        elif self.action == 'create':
            return PurchaseReturnCreateSerializer
        return PurchaseReturnDetailSerializer

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve un retour : la marchandise sort du rayon.

        Le corps vit dans `services.approuver_retour`. Il était écrit ICI, où
        il verrouillait `Stock` par `select_for_update` HORS de toute
        transaction - donc 500 sur PostgreSQL, et invisible à tout test
        (`APITestCase` ouvre sa propre transaction).
        """
        from .services import TransitionReceptionRefusee, approuver_retour

        purchase_return = self.get_object()
        try:
            approuver_retour(purchase_return, request.user)
        except TransitionReceptionRefusee as exc:
            return Response(
                {'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def ship(self, request, pk=None):
        """Expédie un retour : la dette s'éteint d'abord, le reliquat revient en caisse.

        Le corps vit dans `services.expedier_retour`. Il était écrit ICI, et il
        enregistrait une entrée de caisse du montant ENTIER tout en décrémentant
        aussi le solde du fournisseur : le même retour comptait deux fois.
        """
        from .services import TransitionReceptionRefusee, expedier_retour

        purchase_return = self.get_object()
        try:
            expedier_retour(purchase_return, request.user)
        except TransitionReceptionRefusee as exc:
            return Response(
                {'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
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
            from apps.inventory.packaging import PackagingService

            for item in purchase_return.items.select_related('product').all():
                stock, _ = Stock.objects.select_for_update().get_or_create(
                    organization=purchase_return.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=purchase_return.warehouse,
                    defaults={'quantity': 0}
                )
                # Les unités rendues reviennent en vrac : annuler un retour
                # fournisseur ne reconstitue pas des contenants scellés.
                PackagingService.apply_base_delta(stock, item.product, item.quantity)
                stock.save()
        
        purchase_return.status = 'cancelled'
        purchase_return.save()
        
        return Response({'status': 'cancelled'})


# ==============================================================================
# RÉQUISITIONS DE PRODUITS (T12)
# ==============================================================================

class RequisitionViewSet(
    ExportResponseMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin,
    AuditMixin, viewsets.ModelViewSet
):
    """
    Réquisitions : la liste de ce qu'il faut acheter.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ T12 DU RETOUR DE TEST. Rien n'existait sous ce nom.                     │
    │                                                                          │
    │ Et ce n'est PAS une commande d'achat : `PurchaseOrder.supplier` est      │
    │ obligatoire, alors qu'une réquisition dit « il nous manque ceci » sans   │
    │ qu'on sache encore chez qui l'acheter.                                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ LE MAGASINIER DEMANDE, LE GÉRANT DÉCIDE. C'est lui qui voit le rayon
    vide ; c'est le gérant qui engage l'argent. `requisitions.create` va donc
    au magasinier, `requisitions.approve` non.
    """

    queryset = Requisition.objects.all()
    serializer_class = RequisitionSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'warehouse']
    search_fields = ['reference', 'notes']
    ordering_fields = ['requested_at', 'reference']
    ordering = ['-requested_at']

    # L'entrepôt POUR lequel on demande borne la lecture : un magasinier ne voit
    # pas ce qui manque dans un dépôt qui ne lui est pas confié.
    #
    # ⚠ IL EST LU PAR `WarehouseScopedQuerysetMixin`, ET PAR RIEN D'AUTRE.
    #   Cette classe était la seule des cinq de ce fichier à le déclarer SANS
    #   hériter du mixin : l'attribut était alors mort, seul le filtre de tenant
    #   s'appliquait, et un magasinier de WH-NORD listait les réquisitions de
    #   WH-MAIN. Rien ne le signalait - c'est le défaut d'attribut mort déjà
    #   relevé sur `ExpenseViewSet`. Le tirage, lui, EST borné
    #   (`PullTable('requisitions', warehouse_path='warehouse_id')`), donc les
    #   deux surfaces disaient deux choses du même périmètre.
    #
    # ⚠ PAS de `WarehouseAssertCreateMixin` : `create()` appelle déjà
    #   `assert_warehouse_allowed_for_request` plus bas, et l'ajouter ferait
    #   deux contrôles du même geste.
    warehouse_scope_field = 'warehouse_id'

    action_permissions = {
        'list': 'requisitions.view',
        'retrieve': 'requisitions.view',
        'create': 'requisitions.create',
        'proposer': 'requisitions.create',
        # Le document s'emporte au marché : le LIRE suffit, le décider non.
        'export': 'requisitions.view',
        'submit': 'requisitions.create',
        'cancel': 'requisitions.create',
        'approve': 'requisitions.approve',
        'reject': 'requisitions.approve',
        # Modifier et supprimer n'existent pas : une réquisition se CORRIGE en
        # l'annulant et en en créant une autre. Elle s'imprime et s'emporte au
        # marché ; la réécrire sous le même numéro ferait que le papier du
        # magasinier ne désigne plus la même demande.
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    def get_serializer_class(self):
        if self.action == 'create':
            return RequisitionCreateSerializer
        return RequisitionSerializer

    def get_queryset(self):
        return super().get_queryset().select_related(
            'warehouse', 'requested_by', 'decided_by'
        ).prefetch_related('items__product__unit')

    @action(detail=False, methods=['get'])
    def proposer(self, request):
        """Les articles DÉJÀ sous leur seuil, prêts à être réquisitionnés.

        Params : ``warehouse`` (requis), ``rupture=true`` pour s'en tenir aux
        ruptures - ce qu'on emporte au marché quand on n'a pas le temps de tout
        racheter.
        """
        from apps.inventory.models import Warehouse

        org = self.get_organization()
        wid = request.query_params.get('warehouse')
        if not wid:
            return Response(
                {'warehouse': "Indiquez l'entrepôt pour lequel vous demandez."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Le périmètre, par le même chemin que partout : un refus LÈVE, il ne
        # rend jamais un ensemble vide.
        assert_warehouse_allowed_for_request(request, wid)
        depot = Warehouse.objects.filter(organization=org, id=wid).first()
        if depot is None:
            return Response(
                {'warehouse': "Entrepôt introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        rupture = str(request.query_params.get('rupture', '')).lower() in ('1', 'true')
        lignes = lignes_proposees(org, depot, inclure_rupture_seule=rupture)

        return Response({
            'warehouse': str(depot.id),
            'warehouse_name': depot.name,
            'items': [
                {
                    'product': str(l['product'].id),
                    'product_name': l['product'].name,
                    'product_sku': l['product'].sku,
                    'unit_name': getattr(l['product'].unit, 'name', None),
                    'stock': l['stock'],
                    'reorder_point': l['reorder_point'],
                    'suggested': l['suggested'],
                }
                for l in lignes
            ],
        })

    def create(self, request, *args, **kwargs):
        from apps.inventory.models import Warehouse

        entree = RequisitionCreateSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data

        org = self.get_organization()
        assert_warehouse_allowed_for_request(request, str(donnees['warehouse']))
        depot = Warehouse.objects.filter(
            organization=org, id=donnees['warehouse']
        ).first()
        if depot is None:
            return Response(
                {'warehouse': "Entrepôt introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            requisition = create_requisition(
                org,
                warehouse=depot,
                lignes=[
                    {
                        'product': l['product'],
                        'quantity_requested': l['quantity_requested'],
                        'notes': l.get('notes', ''),
                    }
                    for l in donnees['items']
                ],
                requested_by=request.user,
                notes=donnees.get('notes', ''),
                reference=donnees.get('reference') or None,
            )
        except RequisitionRefusee as refus:
            return Response({'detail': str(refus)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            RequisitionSerializer(requisition).data, status=status.HTTP_201_CREATED
        )

    def _transition(self, request, action_nom):
        requisition = self.get_object()
        try:
            transition_requisition(
                requisition,
                action_nom,
                user=request.user,
                notes=request.data.get('notes', ''),
            )
        except RequisitionRefusee as refus:
            return Response({'detail': str(refus)}, status=status.HTTP_400_BAD_REQUEST)
        requisition.refresh_from_db()
        return Response(RequisitionSerializer(requisition).data)

    @action(detail=True, methods=['get'], url_path='export')
    def export(self, request, pk=None):
        """La réquisition, en PDF, classeur ou CSV - celle qu'on emporte au marché.

        ⚠ ELLE LIT L'INSTANTANÉ, jamais le stock du jour : `stock_at_request` a
        été figé à la saisie, et le relire donnerait le stock d'aujourd'hui, qui
        a bougé depuis. Le papier ne dirait plus pourquoi la demande a été faite.
        """
        from apps.purchases.requisition_document import (
            DOCUMENT_BASENAMES,
            build_requisition_document,
        )
        from apps.settings.services import CurrencyService

        fmt = self.get_export_format(request)
        requisition = self.get_object()
        organization = self.get_organization()
        items = requisition.items.select_related(
            'product', 'product__unit', 'product__packaging_unit'
        ).order_by('product__name')

        spec = build_requisition_document(
            requisition, items, organization,
            currency=CurrencyService.primary_code(organization),
        )
        # ⚠ `render_export` et NON `export_file_response` : le second attend un
        # tampon DÉJÀ rendu, et lui passer la description rend
        # « 'ReportSpec' object has no attribute 'getvalue' » - un 500 sur les
        # trois formats à la fois.
        return self.render_export(
            spec, f"{DOCUMENT_BASENAMES['requisition']}_{requisition.reference}", fmt
        )

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Soumettre la demande à décision."""
        return self._transition(request, 'submit')

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuver : c'est le geste qui engage l'argent."""
        return self._transition(request, 'approve')

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejeter. Ne bouge NI le stock NI la caisse : l'état le plus prudent."""
        return self._transition(request, 'reject')

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annuler sa propre demande, depuis n'importe quel état non terminal."""
        return self._transition(request, 'cancel')
