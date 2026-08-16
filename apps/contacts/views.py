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
                              payment_method='cash', reference='', notes=''):
        """
        Impute un règlement sur les factures ouvertes du client, de la plus
        ancienne à la plus récente, et renvoie ``(reliquat, factures_soldées)``.

        C'est le correctif central de la dette : auparavant ce chemin ne faisait
        que déplacer ``current_balance``, laissant les ventes en
        ``pending``/``partially_paid`` avec un ``amount_due`` non nul. Le solde
        du client et la somme des factures dues divergeaient dès le premier
        acompte, et comme la vente n'atteignait jamais ``completed``, les points
        de fidélité n'étaient jamais attribués.
        """
        from apps.sales.services import apply_payment_to_sale
        from apps.sales.models import PaymentMethod
        from apps.contacts import services as contacts_services

        method = PaymentMethod.objects.filter(
            organization=customer.organization,
            method_type=payment_method,
            is_active=True,
        ).first() or PaymentMethod.objects.filter(
            organization=customer.organization, is_active=True,
        ).first()

        remaining = amount
        touched = []
        for sale in contacts_services.open_credit_sales(customer, currency):
            if remaining <= 0:
                break
            applied = min(remaining, sale.amount_due)
            if applied <= 0:
                continue
            apply_payment_to_sale(
                sale.id, user,
                payment_method_id=method.id if method else None,
                tendered_amount=applied,
                currency=currency,
                reference=reference,
                notes=notes or f"Règlement client {customer.name}",
            )
            remaining -= applied
            touched.append(sale.reference)

        return remaining, touched

    @action(detail=True, methods=['post'], url_path='record-payment')
    def record_payment(self, request, pk=None):
        """
        Enregistre un règlement du client et l'impute sur ses factures ouvertes.

        Le règlement solde d'abord les factures les plus anciennes dans la même
        devise ; un éventuel reliquat devient une avance (solde créditeur).
        """
        from django.db import transaction as db_transaction
        from apps.contacts import services as contacts_services

        customer = self.get_object()
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        amount = serializer.validated_data['amount']
        currency, exchange_rate = CurrencyService.resolve(
            customer.organization,
            serializer.validated_data.get('currency'),
            serializer.validated_data.get('exchange_rate'),
            strict=True,
        )
        payment_method = serializer.validated_data.get('payment_method', 'cash')
        notes = serializer.validated_data.get('notes', '')
        reference = serializer.validated_data.get('reference', '')

        with db_transaction.atomic():
            # `apply_payment_to_sale` met déjà à jour la dette de chaque facture
            # soldée : on n'enregistre ici que le reliquat, en avance.
            remaining, touched = self._settle_open_invoices(
                customer, amount, currency, request.user,
                payment_method=payment_method, reference=reference, notes=notes,
            )

            txn = None
            if remaining > 0:
                txn = contacts_services.settle_debt(
                    customer, remaining,
                    currency=currency,
                    exchange_rate=exchange_rate,
                    transaction_type=CustomerTransaction.TransactionType.ADVANCE,
                    reference=reference,
                    notes=notes or "Avance client (aucune facture à solder)",
                    user=request.user,
                    payment_method=payment_method,
                )
                # Le reliquat n'a soldé aucune facture : il entre au tiroir ici.
                from apps.cashbook.services import record_customer_advance
                record_customer_advance(
                    organization=customer.organization,
                    customer=customer,
                    amount=remaining,
                    user=request.user,
                    notes=notes,
                    currency=currency,
                    exchange_rate=exchange_rate,
                )

            customer.refresh_from_db()

        return Response({
            'transaction': CustomerTransactionSerializer(txn).data if txn else None,
            'settled_invoices': touched,
            'advance_amount': str(remaining),
            'currency': currency,
            'new_balance': str(customer.current_balance),
            'balances': contacts_services.balances_by_currency(customer),
        })

    @action(detail=True, methods=['post'], url_path='record-advance')
    def record_advance(self, request, pk=None):
        """
        Enregistre une avance du client sans l'imputer sur une facture.

        Le solde de la devise devient créditeur (négatif) : l'avance sera
        consommée par une vente à crédit future ou par un règlement ultérieur.
        """
        from django.db import transaction as db_transaction
        from apps.contacts import services as contacts_services

        customer = self.get_object()
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        amount = serializer.validated_data['amount']
        currency, exchange_rate = CurrencyService.resolve(
            customer.organization,
            serializer.validated_data.get('currency'),
            serializer.validated_data.get('exchange_rate'),
            strict=True,
        )

        with db_transaction.atomic():
            txn = contacts_services.settle_debt(
                customer, amount,
                currency=currency,
                exchange_rate=exchange_rate,
                transaction_type=CustomerTransaction.TransactionType.ADVANCE,
                reference=serializer.validated_data.get('reference', ''),
                notes=serializer.validated_data.get('notes', ''),
                user=request.user,
                payment_method=serializer.validated_data.get('payment_method', 'cash'),
            )

            from apps.cashbook.services import record_customer_advance
            record_customer_advance(
                organization=customer.organization,
                customer=customer,
                amount=amount,
                user=request.user,
                notes=serializer.validated_data.get('notes', ''),
                currency=currency,
                exchange_rate=exchange_rate,
            )
            customer.refresh_from_db()

        return Response({
            'transaction': CustomerTransactionSerializer(txn).data,
            'new_balance': str(customer.current_balance),
            'balances': contacts_services.balances_by_currency(customer),
        })

    @action(detail=True, methods=['post'], url_path='adjust-balance')
    def adjust_balance(self, request, pk=None):
        """Ajustement manuel du solde client, dans une devise donnée."""
        from django.db import transaction as db_transaction
        from apps.contacts import services as contacts_services

        customer = self.get_object()
        serializer = AdjustBalanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        amount = serializer.validated_data['amount']
        currency, exchange_rate = CurrencyService.resolve(
            customer.organization,
            serializer.validated_data.get('currency'),
            serializer.validated_data.get('exchange_rate'),
            strict=True,
        )
        notes = serializer.validated_data.get('notes', '')

        try:
            with db_transaction.atomic():
                if amount > 0:
                    txn = contacts_services.apply_debt(
                        customer, amount,
                        currency=currency,
                        exchange_rate=exchange_rate,
                        transaction_type=CustomerTransaction.TransactionType.ADJUSTMENT,
                        notes=notes,
                        user=request.user,
                    )
                else:
                    txn = contacts_services.adjust_balance(
                        customer, amount,
                        currency=currency,
                        exchange_rate=exchange_rate,
                        notes=notes,
                        user=request.user,
                    )
                    # Réduire la dette = argent reçu : ça entre au tiroir.
                    from apps.cashbook.services import record_customer_debt_payment
                    record_customer_debt_payment(
                        organization=customer.organization,
                        customer=customer,
                        amount=abs(amount),
                        user=request.user,
                        notes=f"Ajustement solde client - {notes}",
                        currency=currency,
                        exchange_rate=exchange_rate,
                    )
                customer.refresh_from_db()
        except DRFValidationError as exc:
            return Response({'error': exc.detail}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'transaction': CustomerTransactionSerializer(txn).data if txn else None,
            'new_balance': str(customer.current_balance),
            'balances': contacts_services.balances_by_currency(customer),
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
        """Retourne les clients avec un solde non nul (débiteurs)."""
        organization = self.get_organization()
        
        customers = Customer.objects.filter(
            organization=organization,
            is_deleted=False
        ).exclude(current_balance=0).order_by('-current_balance')
        
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
