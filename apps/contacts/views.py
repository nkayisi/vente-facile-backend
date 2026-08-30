"""
ViewSets DRF pour l'app Contacts (Customers & Suppliers).
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count
from decimal import Decimal
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from apps.core.warehouse_scope import get_membership_for_request
from apps.settings.services import CurrencyService
from . import services as contacts_services
from .models import Customer, CustomerBalance, CustomerTransaction, Supplier, SupplierProduct
from .serializers import (
    CustomerListSerializer, CustomerDetailSerializer,
    CustomerCreateSerializer, CustomerUpdateSerializer,
    CustomerTransactionSerializer,
    RecordPaymentSerializer, AdjustBalanceSerializer,
    RedeemPointsToDebtSerializer,
    SupplierListSerializer, SupplierDetailSerializer,
    SupplierCreateSerializer, SupplierUpdateSerializer,
    SupplierProductSerializer
)


# =============================================================================
# CUSTOMER VIEWSET
# =============================================================================

class CustomerViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des clients.
    
    Endpoints:
    - GET /customers/ : Liste des clients
    - POST /customers/ : Créer un client
    - GET /customers/{id}/ : Détail d'un client
    - PUT/PATCH /customers/{id}/ : Modifier un client
    - DELETE /customers/{id}/ : Supprimer un client (soft delete)
    - GET /customers/{id}/sales/ : Historique des ventes
    - POST /customers/{id}/adjust-balance/ : Ajuster le solde
    - GET /customers/with-balance/ : Clients avec solde
    - GET /customers/search/ : Recherche rapide
    """
    
    queryset = Customer.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['customer_type', 'is_active']
    search_fields = ['name', 'code', 'email', 'phone', 'company_name']
    ordering_fields = ['name', 'code', 'created_at', 'current_balance']
    ordering = ['name']
    
    select_related_fields = ['created_by']
    # `CustomerBalancesMixin` lit `obj.balances.all()` sur CHAQUE client, en
    # liste comme en détail : sans ce prefetch, une page de clients déclenchait
    # une requête par client.
    prefetch_related_fields = ['balances']
    
    action_permissions = {
        'list': 'customers.view',
        'retrieve': 'customers.view',
        'create': 'customers.create',
        'update': 'customers.edit',
        'partial_update': 'customers.edit',
        'destroy': 'customers.delete',
        'sales': 'customers.view',
        'transactions': 'customers.view',
        'record_payment': 'customers.edit',
        'record_advance': 'customers.edit',
        'adjust_balance': 'customers.edit',
        'redeem_points_to_debt': 'customers.edit',
        'with_balance': 'customers.view',
        'debt_summary': 'customers.view',
        'search': 'customers.view',
        'stats': 'customers.view',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return CustomerListSerializer
        elif self.action == 'create':
            return CustomerCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return CustomerUpdateSerializer
        elif self.action == 'adjust_balance':
            return AdjustBalanceSerializer
        return CustomerDetailSerializer

    @action(detail=True, methods=['get'])
    def sales(self, request, pk=None):
        """Retourne l'historique des ventes du client."""
        customer = self.get_object()
        
        from apps.sales.models import Sale
        from apps.sales.serializers import SaleListSerializer
        
        sales = Sale.objects.filter(
            customer=customer,
            is_deleted=False
        ).select_related('sold_by').order_by('-sale_date')
        
        page = self.paginate_queryset(sales)
        if page is not None:
            serializer = SaleListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = SaleListSerializer(sales, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def transactions(self, request, pk=None):
        """Retourne l'historique des transactions financières du client."""
        customer = self.get_object()
        
        txns = CustomerTransaction.objects.filter(
            customer=customer
        ).select_related('created_by', 'sale').order_by('-created_at')

        # Visibilité : un caissier ne voit que les transactions qu'il a
        # lui-même enregistrées. Les autres rôles (gérant/owner) voient
        # l'historique complet du client (le crédit client n'est pas
        # rattaché à un entrepôt).
        from apps.organizations.models import OrganizationMembership

        membership = get_membership_for_request(request)
        if membership and membership.role == OrganizationMembership.Role.CASHIER:
            txns = txns.filter(created_by=request.user)

        # Filtre optionnel par type
        txn_type = request.query_params.get('type')
        if txn_type:
            txns = txns.filter(transaction_type=txn_type)
        
        page = self.paginate_queryset(txns)
        if page is not None:
            serializer = CustomerTransactionSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = CustomerTransactionSerializer(txns, many=True)
        return Response(serializer.data)

    def _settle_open_invoices(self, customer, amount, currency, user,
                              payment_method='cash', reference='', notes='',
                              settle_currency=None, receipt_number=None):
        """
        Imputation d'un règlement sur les factures ouvertes.

        Le corps vit dans `contacts.services` : le terminal mobile le rejoue par
        le journal d'opérations, et tant qu'il était écrit ici, le gestionnaire
        de synchronisation en tenait une seconde version, déjà divergente.
        """
        return contacts_services._settle_open_invoices(
            customer, amount, currency, user,
            payment_method=payment_method, reference=reference, notes=notes,
            settle_currency=settle_currency, receipt_number=receipt_number,
        )

    @action(detail=True, methods=['post'], url_path='record-payment')
    def record_payment(self, request, pk=None):
        """
        Enregistre un règlement du client et l'impute sur ses factures ouvertes.

        Le règlement solde d'abord les factures les plus anciennes ; un éventuel
        reliquat devient une avance (solde créditeur), dans la devise remise.

        ``currency`` est la devise remise, ``settle_currency`` celle des factures
        visées : un client qui doit en USD peut payer en francs congolais.

        Le corps est dans `contacts.services.record_payment`, que le terminal
        mobile rejoue par le journal d'opérations.
        """
        customer = self.get_object()
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        resultat = contacts_services.record_payment(
            customer, data['amount'],
            user=request.user,
            currency=data.get('currency'),
            exchange_rate=data.get('exchange_rate'),
            settle_currency=data.get('settle_currency'),
            payment_method=data.get('payment_method', 'cash'),
            reference=data.get('reference', ''),
            notes=data.get('notes', ''),
        )

        txn = resultat.pop('transaction')
        return Response({
            'transaction': CustomerTransactionSerializer(txn).data if txn else None,
            **resultat,
        })

    @action(detail=True, methods=['post'], url_path='record-advance')
    def record_advance(self, request, pk=None):
        """
        Enregistre de l'argent reçu d'un client, en avance sur ses achats.

        **Impute d'abord sur les factures ouvertes** de la devise, de la plus
        ancienne à la plus récente ; seul le reliquat devient une avance. C'est
        strictement le comportement de ``record_payment``, dont cette action
        n'est plus qu'un alias.

        Elle écrivait auparavant le solde directement, sans toucher aucune
        facture : appelée sur un client déjà endetté, elle faisait diverger le
        solde de la somme des ``amount_due`` ouverts - l'invariant que
        ``test_debt_and_open_invoices_never_diverge`` protège. Quand un client
        qui doit de l'argent en remet, c'est un règlement, pas une avance.
        """
        return self.record_payment(request, pk)

    @action(detail=True, methods=['post'], url_path='adjust-balance')
    def adjust_balance(self, request, pk=None):
        """
        Ajustement manuel du solde client, dans une devise donnée.

        Corps dans `contacts.services.adjust_customer_balance`, partagé avec le
        journal d'opérations du terminal mobile.
        """
        customer = self.get_object()
        serializer = AdjustBalanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            resultat = contacts_services.adjust_customer_balance(
                customer, data['amount'],
                user=request.user,
                currency=data.get('currency'),
                exchange_rate=data.get('exchange_rate'),
                notes=data.get('notes', ''),
            )
        except DRFValidationError as exc:
            return Response({'error': exc.detail}, status=status.HTTP_400_BAD_REQUEST)

        txn = resultat.pop('transaction')
        return Response({
            'transaction': CustomerTransactionSerializer(txn).data if txn else None,
            **resultat,
        })

    @action(detail=True, methods=['post'], url_path='redeem-points')
    def redeem_points_to_debt(self, request, pk=None):
        """
        Utilise les points de fidélité du client pour éponger sa dette.

        Les points sont convertis en montant puis imputés sur les factures
        ouvertes comme un règlement ordinaire. Aucun mouvement de caisse : ce
        n'est pas de l'argent physique.
        """
        from django.db import transaction as db_transaction
        from apps.contacts import services as contacts_services
        from apps.settings.services import LoyaltyService

        customer = self.get_object()
        serializer = RedeemPointsToDebtSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        points = serializer.validated_data['points']

        primary = CurrencyService.primary_code(customer.organization)
        outstanding = contacts_services.get_balance(customer, primary)
        if outstanding <= 0:
            return Response(
                {'error': "Ce client n'a aucune dette dans la devise principale."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resolution = LoyaltyService.resolve_redemption(
            customer, customer.organization, points, outstanding,
            target_currency=primary,
        )
        if resolution is None:
            return Response(
                {'error': (
                    "Points inutilisables : solde insuffisant, minimum non "
                    "atteint, ou aucun programme de fidélité actif."
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with db_transaction.atomic():
            # Les points sont consommés UNE fois, ici ; le montant obtenu est
            # ensuite imputé sur les factures comme un règlement ordinaire.
            LoyaltyService.consume_points(
                customer.organization, resolution, user=request.user,
                description=f"Points utilisés sur la dette de {customer.name}",
            )
            remaining, touched = self._settle_open_invoices(
                customer, resolution['amount'], primary, request.user,
                payment_method='loyalty',
                notes=f"{resolution['points']} points de fidélité",
            )
            if remaining > 0:
                contacts_services.settle_debt(
                    customer, remaining,
                    currency=primary,
                    notes=f"{resolution['points']} points de fidélité",
                    user=request.user,
                    payment_method='loyalty',
                )
            customer.refresh_from_db()

        return Response({
            'points_used': resolution['points'],
            'amount': str(resolution['amount']),
            'currency': primary,
            'settled_invoices': touched,
            'new_balance': str(customer.current_balance),
            'balances': contacts_services.balances_by_currency(customer),
        })

    @action(detail=False, methods=['get'], url_path='with-balance')
    def with_balance(self, request):
        """
        Clients dont le solde n'est pas nul : débiteurs d'abord, puis créditeurs
        (avances). C'est le filtre « Avec dette » de la liste des clients.

        Passe par `filter_queryset(get_queryset())` et non par un queryset
        reconstruit à la main : on hérite ainsi du périmètre tenant, du prefetch
        des soldes par devise (sinon N+1) et des filtres de recherche et de type
        déjà déclarés sur le viewset, pour que le filtre se combine avec eux.
        """
        customers = self.filter_queryset(self.get_queryset()).exclude(
            current_balance=0
        ).order_by('-current_balance')

        page = self.paginate_queryset(customers)
        if page is not None:
            serializer = CustomerListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = CustomerListSerializer(customers, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='debt-summary')
    def debt_summary(self, request):
        """Résumé global des dettes clients."""
        organization = self.get_organization()
        
        customers = Customer.objects.filter(
            organization=organization,
            is_deleted=False
        )
        
        debtors = customers.filter(current_balance__gt=0)
        creditors = customers.filter(current_balance__lt=0)
        
        return Response({
            'total_receivable': str(
                debtors.aggregate(total=Sum('current_balance'))['total'] or Decimal('0.00')
            ),
            'total_advances': str(
                abs(creditors.aggregate(total=Sum('current_balance'))['total'] or Decimal('0.00'))
            ),
            'debtors_count': debtors.count(),
            'creditors_count': creditors.count(),
            'top_debtors': CustomerListSerializer(
                debtors.order_by('-current_balance')[:5], many=True
            ).data
        })

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Recherche rapide de client (pour autocomplete)."""
        query = request.query_params.get('q', '')
        if len(query) < 2:
            return Response([])
        
        organization = self.get_organization()
        
        customers = Customer.objects.filter(
            organization=organization,
            is_deleted=False,
            is_active=True
        ).filter(
            models.Q(name__icontains=query) |
            models.Q(code__icontains=query) |
            models.Q(phone__icontains=query) |
            models.Q(email__icontains=query)
        )[:10]
        
        return Response([
            {
                'id': str(c.id),
                'code': c.code,
                'name': c.name,
                'phone': c.phone,
                'balance': str(c.current_balance)
            }
            for c in customers
        ])

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Statistiques des clients."""
        organization = self.get_organization()
        
        customers = Customer.objects.filter(
            organization=organization,
            is_deleted=False
        )
        
        stats = {
            'total': customers.count(),
            'active': customers.filter(is_active=True).count(),
            'with_balance': customers.exclude(current_balance=0).count(),
            'total_balance': str(
                customers.aggregate(total=Sum('current_balance'))['total'] or Decimal('0.00')
            ),
            'by_type': list(
                customers.values('customer_type').annotate(count=Count('id'))
            ),
        }
        
        return Response(stats)


# =============================================================================
# SUPPLIER VIEWSET
# =============================================================================

class SupplierViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des fournisseurs.
    
    Endpoints:
    - GET /suppliers/ : Liste des fournisseurs
    - POST /suppliers/ : Créer un fournisseur
    - GET /suppliers/{id}/ : Détail d'un fournisseur
    - PUT/PATCH /suppliers/{id}/ : Modifier un fournisseur
    - DELETE /suppliers/{id}/ : Supprimer un fournisseur (soft delete)
    - GET /suppliers/{id}/orders/ : Historique des commandes
    - GET /suppliers/{id}/products/ : Produits du fournisseur
    - GET /suppliers/with-balance/ : Fournisseurs avec solde
    """
    
    queryset = Supplier.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'code', 'email', 'contact_person']
    ordering_fields = ['name', 'code', 'created_at', 'current_balance']
    ordering = ['name']
    
    select_related_fields = ['created_by']
    prefetch_related_fields = ['products']
    
    action_permissions = {
        'list': 'suppliers.view',
        'retrieve': 'suppliers.view',
        'create': 'suppliers.create',
        'update': 'suppliers.edit',
        'partial_update': 'suppliers.edit',
        'destroy': 'suppliers.delete',
        'orders': 'purchases.view',
        'products': 'suppliers.view',
        'with_balance': 'suppliers.view',
        'search': 'suppliers.view',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return SupplierListSerializer
        elif self.action == 'create':
            return SupplierCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return SupplierUpdateSerializer
        return SupplierDetailSerializer

    @action(detail=True, methods=['get'])
    def orders(self, request, pk=None):
        """Retourne l'historique des commandes du fournisseur."""
        supplier = self.get_object()
        
        from apps.purchases.models import PurchaseOrder
        from apps.purchases.serializers import PurchaseOrderListSerializer
        
        orders = PurchaseOrder.objects.filter(
            supplier=supplier,
            is_deleted=False
        ).order_by('-order_date')
        
        page = self.paginate_queryset(orders)
        if page is not None:
            serializer = PurchaseOrderListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = PurchaseOrderListSerializer(orders, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def products(self, request, pk=None):
        """Retourne les produits du fournisseur."""
        supplier = self.get_object()
        
        products = SupplierProduct.objects.filter(
            supplier=supplier,
            is_active=True
        ).select_related('product')
        
        page = self.paginate_queryset(products)
        if page is not None:
            serializer = SupplierProductSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = SupplierProductSerializer(products, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='with-balance')
    def with_balance(self, request):
        """Retourne les fournisseurs avec un solde non nul."""
        organization = self.get_organization()
        
        suppliers = Supplier.objects.filter(
            organization=organization,
            is_deleted=False
        ).exclude(current_balance=0).order_by('-current_balance')
        
        page = self.paginate_queryset(suppliers)
        if page is not None:
            serializer = SupplierListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = SupplierListSerializer(suppliers, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Recherche rapide de fournisseur."""
        query = request.query_params.get('q', '')
        if len(query) < 2:
            return Response([])
        
        organization = self.get_organization()
        
        suppliers = Supplier.objects.filter(
            organization=organization,
            is_deleted=False,
            is_active=True
        ).filter(
            models.Q(name__icontains=query) |
            models.Q(code__icontains=query) |
            models.Q(contact_person__icontains=query)
        )[:10]
        
        return Response([
            {
                'id': str(s.id),
                'code': s.code,
                'name': s.name,
                'contact': s.contact_person,
                'balance': str(s.current_balance)
            }
            for s in suppliers
        ])


# =============================================================================
# SUPPLIER PRODUCT VIEWSET
# =============================================================================

class SupplierProductViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des produits fournisseur.
    
    Endpoints:
    - GET /suppliers/{supplier_id}/products/ : Liste des produits
    - POST /suppliers/{supplier_id}/products/ : Ajouter un produit
    - PUT/PATCH /suppliers/{supplier_id}/products/{id}/ : Modifier
    - DELETE /suppliers/{supplier_id}/products/{id}/ : Supprimer
    """
    
    queryset = SupplierProduct.objects.all()
    serializer_class = SupplierProductSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    action_permissions = {
        'list': 'suppliers.view',
        'retrieve': 'suppliers.view',
        'create': 'suppliers.create',
        'update': 'suppliers.edit',
        'partial_update': 'suppliers.edit',
        'destroy': 'suppliers.delete',
    }
    
    select_related_fields = ['product', 'supplier']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        supplier_id = self.kwargs.get('supplier_pk')
        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)
        return queryset

    def perform_create(self, serializer):
        supplier_id = self.kwargs.get('supplier_pk')
        organization = self.get_organization()
        serializer.save(supplier_id=supplier_id, organization=organization)


# Import pour les queries
from django.db import models
