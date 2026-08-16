"""
ViewSets DRF pour l'app Sales (POS).
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count, F
from django.utils import timezone
from decimal import Decimal

from apps.core.api_mixins import (
    TenantViewSetMixin,
    AuditMixin,
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
)
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_warehouse_allowed_for_request,
    filter_queryset_by_related_warehouse,
    filter_queryset_by_warehouse_ids,
    get_membership_for_request,
)
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission,
    has_perm_code, is_manager_or_above,
)
from .models import (
    Register, RegisterSession, Sale, SaleItem, PaymentMethod, Payment,
    SaleReturn, SaleReturnItem, Quotation, QuotationItem
)
from .serializers import (
    RegisterSerializer,
    RegisterSessionListSerializer, RegisterSessionDetailSerializer,
    RegisterSessionOpenSerializer, RegisterSessionCloseSerializer,
    PaymentMethodSerializer,
    SaleListSerializer, SaleDetailSerializer, SaleCreateSerializer, SalePaymentSerializer,
    SaleReturnListSerializer, SaleReturnDetailSerializer, SaleReturnCreateSerializer,
    QuotationListSerializer, QuotationDetailSerializer, QuotationCreateSerializer
)


# =============================================================================
# REGISTER VIEWSET
# =============================================================================

class RegisterViewSet(
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des caisses.
    
    Endpoints:
    - GET /registers/ : Liste des caisses
    - POST /registers/ : Créer une caisse
    - GET /registers/{id}/ : Détail d'une caisse
    - PUT/PATCH /registers/{id}/ : Modifier une caisse
    - DELETE /registers/{id}/ : Supprimer une caisse
    """
    
    queryset = Register.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'branch']
    search_fields = ['name', 'code']
    ordering = ['name']
    
    select_related_fields = ['branch', 'warehouse']

    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = False
    warehouse_write_required = True
    warehouse_write_allow_none = False
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'create': 'sales.manage_registers',
        'update': 'sales.manage_registers',
        'partial_update': 'sales.manage_registers',
        'destroy': 'sales.manage_registers',
    }


# =============================================================================
# REGISTER SESSION VIEWSET
# =============================================================================

class RegisterSessionViewSet(
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des sessions de caisse.
    
    Endpoints:
    - GET /register-sessions/ : Liste des sessions
    - GET /register-sessions/{id}/ : Détail d'une session
    - POST /register-sessions/open/ : Ouvrir une session
    - POST /register-sessions/{id}/close/ : Fermer une session
    - GET /register-sessions/current/ : Session courante de l'utilisateur
    """
    
    queryset = RegisterSession.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status', 'register', 'opened_by']
    ordering = ['-opened_at']
    
    select_related_fields = ['register', 'opened_by', 'closed_by']

    # Filtre par l'entrepôt de la caisse : uniquement les sessions dont la
    # caisse est dans le périmètre ``assigned_warehouses`` du membre (owner : tout).
    warehouse_scope_field = 'register__warehouse_id'
    warehouse_scope_include_null = False
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'open': 'sales.create',
        'close': 'sales.create',
        'current': 'sales.view',
    }
    
    # Sessions en lecture seule sauf pour open/close
    http_method_names = ['get', 'post', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'list':
            return RegisterSessionListSerializer
        elif self.action == 'open':
            return RegisterSessionOpenSerializer
        elif self.action == 'close':
            return RegisterSessionCloseSerializer
        return RegisterSessionDetailSerializer

    @action(detail=False, methods=['post'])
    def open(self, request):
        """
        Ouvre une nouvelle session de caisse.

        - Bloque la création si une session est déjà ouverte sur cette caisse
          (check applicatif + UniqueConstraint DB en filet de sécurité).
        - Hérite `opening_balance` du `closing_balance` de la dernière session
          fermée sur cette caisse (sinon 0).
        """
        from django.db import transaction, IntegrityError

        serializer = RegisterSessionOpenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        organization = self.get_organization()
        register_id = serializer.validated_data['register']

        # Caisse dans l'org, active, et dans le périmètre entrepôt du membre
        register_qs = Register.objects.filter(
            id=register_id,
            organization=organization,
            is_active=True,
        )
        membership = get_membership_for_request(request)
        if membership:
            register_qs = filter_queryset_by_warehouse_ids(
                register_qs, membership, 'warehouse_id'
            )
        register = register_qs.first()

        if not register:
            return Response(
                {'error': 'Caisse non trouvée ou inactive'},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            with transaction.atomic():
                # Lock sur les sessions existantes de cette caisse pour exclure
                # toute requête concurrente avant la vérification + insertion.
                existing_session = (
                    RegisterSession.objects.select_for_update()
                    .filter(register=register, status='open')
                    .first()
                )
                if existing_session:
                    return Response(
                        {'error': 'Une session est déjà ouverte sur cette caisse'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                from .models import RegisterSessionCurrencyBalance
                primary = organization.currency or 'CDF'

                # Hériter le solde d'ouverture, PAR DEVISE, de la dernière
                # session fermée sur cette caisse.
                previous = (
                    RegisterSession.objects.filter(
                        register=register, status='closed'
                    )
                    .order_by('-closed_at', '-opened_at')
                    .first()
                )
                inherited_by_ccy = {}
                if previous is not None:
                    for cb in previous.currency_balances.all():
                        val = (
                            cb.counted_balance if cb.counted_balance is not None
                            else (cb.expected_balance or Decimal('0.00'))
                        )
                        inherited_by_ccy[cb.currency] = val
                    if not inherited_by_ccy:
                        # Session héritée sans ventilation par devise (legacy).
                        inherited_by_ccy[primary] = (
                            previous.counted_balance
                            if previous.counted_balance is not None
                            else (previous.closing_balance or previous.expected_balance or Decimal('0.00'))
                        )

                # Surcharge éventuelle des fonds d'ouverture par devise.
                for item in serializer.validated_data.get('opening_balances') or []:
                    inherited_by_ccy[item['currency']] = Decimal(item['amount'])

                primary_opening = inherited_by_ccy.get(primary, Decimal('0.00'))

                session = RegisterSession.objects.create(
                    organization=organization,
                    register=register,
                    opened_by=request.user,
                    opening_balance=primary_opening,
                    notes='',
                    status='open',
                )
                for ccy, amount in inherited_by_ccy.items():
                    RegisterSessionCurrencyBalance.objects.create(
                        organization=organization,
                        session=session,
                        currency=ccy,
                        opening_balance=amount,
                    )
        except IntegrityError:
            # UniqueConstraint DB : une autre requête a créé la session en parallèle
            return Response(
                {'error': 'Une session est déjà ouverte sur cette caisse'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            RegisterSessionDetailSerializer(session).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """
        Ferme une session de caisse.

        - Tout membre ayant accès à l'entrepôt de la caisse peut fermer la
          session, y compris une session ouverte par un autre utilisateur
          (``get_object`` est déjà filtré par périmètre entrepôt, donc on ne
          peut fermer que les sessions de ses propres entrepôts). L'identité du
          clôtureur est journalisée (voir ``UserActivity`` en fin de méthode).
        - Accepte `counted_balance` optionnel (comptage manuel) ; calcule
          `difference = counted_balance - expected_balance` si fourni.
        - Si différence non nulle, `notes` est obligatoire.
        """
        session = self.get_object()

        if session.status != 'open':
            return Response(
                {'error': 'Cette session est déjà fermée'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = RegisterSessionCloseSerializer(data=request.data if request.data else {})
        serializer.is_valid(raise_exception=True)
        notes_input = (serializer.validated_data.get('notes') or '').strip()

        from django.db.models import Q as _Q
        from django.db.models.functions import Coalesce
        from apps.cashbook.models import CashMovement
        from .models import RegisterSessionCurrencyBalance

        primary = session.organization.currency or 'CDF'
        TWO = Decimal('0.01')

        # Fonds d'ouverture PAR DEVISE : depuis les currency_balances de la session
        # si présents (nouvelles sessions), sinon la devise principale = scalaire.
        opening_by_ccy = {
            cb.currency: cb.opening_balance
            for cb in session.currency_balances.all()
        }
        if not opening_by_ccy:
            opening_by_ccy = {primary: session.opening_balance}

        # Entrées espèces par devise = somme des règlements cash (montant remis)
        # des ventes de la session.
        cash_in_rows = Payment.objects.filter(
            sale__session=session,
            payment_method__method_type='cash',
            status='completed',
        ).values('currency').annotate(
            total=Sum(Coalesce('tendered_amount', 'amount'))
        )
        cash_in_by_ccy = {
            (r['currency'] or primary): (r['total'] or Decimal('0.00'))
            for r in cash_in_rows
        }

        # Sorties espèces par devise = mouvements cash 'out' rattachés à la session
        # (dépenses, monnaie rendue, retraits). payment_method NULL = espèces comptoir.
        cash_out_rows = CashMovement.objects.filter(
            session=session,
            direction='out',
            is_cancelled=False,
        ).filter(
            _Q(payment_method__isnull=True) | _Q(payment_method__method_type='cash')
        ).values('currency').annotate(total=Sum('amount'))
        cash_out_by_ccy = {
            (r['currency'] or primary): (r['total'] or Decimal('0.00'))
            for r in cash_out_rows
        }

        # Comptage manuel par devise (+ compat scalaire = devise principale).
        counted_by_ccy = {}
        if serializer.validated_data.get('counted_balance') is not None:
            counted_by_ccy[primary] = Decimal(serializer.validated_data['counted_balance'])
        for item in serializer.validated_data.get('counted_balances') or []:
            counted_by_ccy[item['currency']] = Decimal(item['amount'])

        currencies = (
            set(opening_by_ccy) | set(cash_in_by_ccy)
            | set(cash_out_by_ccy) | set(counted_by_ccy)
        )

        rows = []
        any_diff = False
        for ccy in sorted(currencies):
            opening = opening_by_ccy.get(ccy, Decimal('0.00'))
            cin = cash_in_by_ccy.get(ccy, Decimal('0.00'))
            cout = cash_out_by_ccy.get(ccy, Decimal('0.00'))
            expected = (opening + cin - cout).quantize(TWO)
            counted = counted_by_ccy.get(ccy)
            if counted is not None:
                counted = counted.quantize(TWO)
                diff = (counted - expected).quantize(TWO)
            else:
                diff = Decimal('0.00')
            if diff != 0:
                any_diff = True
            rows.append({
                'currency': ccy, 'opening_balance': opening,
                'expected_balance': expected, 'counted_balance': counted,
                'difference': diff,
            })

        # Notes obligatoires si un écart est non nul dans une devise.
        if any_diff and not notes_input:
            return Response(
                {'notes': "Une note explicative est obligatoire lorsque le comptage diffère du solde attendu."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Persister les soldes par devise.
        for row in rows:
            RegisterSessionCurrencyBalance.objects.update_or_create(
                session=session, currency=row['currency'],
                defaults={
                    'organization': session.organization,
                    'opening_balance': row['opening_balance'],
                    'expected_balance': row['expected_balance'],
                    'counted_balance': row['counted_balance'],
                    'difference': row['difference'],
                },
            )

        # Renseigner les champs scalaires (devise principale) pour compat ascendante.
        primary_row = next((r for r in rows if r['currency'] == primary), None)
        if primary_row is None:
            primary_row = {
                'opening_balance': session.opening_balance,
                'expected_balance': session.opening_balance,
                'counted_balance': None, 'difference': Decimal('0.00'),
            }
        session.expected_balance = primary_row['expected_balance']
        session.counted_balance = primary_row['counted_balance']
        session.difference = primary_row['difference']
        session.closing_balance = (
            primary_row['counted_balance'] if primary_row['counted_balance'] is not None
            else primary_row['expected_balance']
        )
        session.closed_by = request.user
        session.closed_at = timezone.now()
        session.status = 'closed'
        session.notes = notes_input
        session.save()

        # Audit log de la fermeture - détail par devise inclus.
        from apps.users.models import UserActivity

        closed_by_other = session.opened_by_id != request.user.id
        UserActivity.objects.create(
            user=request.user,
            organization=session.organization,
            action=UserActivity.ActionType.UPDATE,
            resource_type='register_session',
            resource_id=str(session.id),
            details={
                'event': 'session_closed',
                'register_id': str(session.register_id),
                'opened_by_id': str(session.opened_by_id),
                'closed_by_id': str(request.user.id),
                'closed_by_other_user': closed_by_other,
                'currency_balances': [
                    {
                        'currency': r['currency'],
                        'opening_balance': str(r['opening_balance']),
                        'expected_balance': str(r['expected_balance']),
                        'counted_balance': str(r['counted_balance']) if r['counted_balance'] is not None else None,
                        'difference': str(r['difference']),
                    }
                    for r in rows
                ],
                'notes': notes_input,
            },
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        )

        return Response(RegisterSessionDetailSerializer(session).data)

    @action(detail=False, methods=['get'])
    def current(self, request):
        """Retourne la session courante de l'utilisateur."""
        organization = self.get_organization()

        session_qs = RegisterSession.objects.filter(
            organization=organization,
            opened_by=request.user,
            status='open',
        ).select_related('register')
        membership = get_membership_for_request(request)
        if membership:
            session_qs = filter_queryset_by_related_warehouse(
                session_qs,
                membership,
                'register__warehouse_id',
                include_null=False,
            )
        session = session_qs.first()

        if not session:
            return Response(
                {'error': 'Aucune session ouverte'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        return Response(RegisterSessionDetailSerializer(session).data)


# =============================================================================
# PAYMENT METHOD VIEWSET
# =============================================================================

class PaymentMethodViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des méthodes de paiement.
    
    Endpoints:
    - GET /payment-methods/ : Liste des méthodes
    - POST /payment-methods/ : Créer une méthode
    - GET /payment-methods/{id}/ : Détail d'une méthode
    - PUT/PATCH /payment-methods/{id}/ : Modifier une méthode
    - DELETE /payment-methods/{id}/ : Supprimer une méthode
    """
    
    queryset = PaymentMethod.objects.all()
    serializer_class = PaymentMethodSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'method_type']
    search_fields = ['name', 'code']
    ordering = ['name']
    
    action_permissions = {
        'list': 'payment_methods.view',
        'retrieve': 'payment_methods.view',
        'create': 'payment_methods.manage',
        'update': 'payment_methods.manage',
        'partial_update': 'payment_methods.manage',
        'destroy': 'payment_methods.manage',
    }


# =============================================================================
# SALE VIEWSET
# =============================================================================

class SaleViewSet(
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des ventes (POS).
    
    Endpoints:
    - GET /sales/ : Liste des ventes
    - POST /sales/ : Créer une vente
    - GET /sales/{id}/ : Détail d'une vente
    - POST /sales/{id}/add-payment/ : Ajouter un paiement
    - POST /sales/{id}/cancel/ : Annuler une vente
    - GET /sales/today/ : Ventes du jour
    - GET /sales/stats/ : Statistiques de ventes
    """
    
    queryset = Sale.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'sale_type', 'customer', 'register', 'is_pos']
    search_fields = ['reference', 'customer__name']
    ordering_fields = ['sale_date', 'total', 'reference']
    ordering = ['-sale_date']
    
    # Champs relationnels communs à toutes les actions. La liste et le détail
    # ayant des besoins différents, ils sont affinés par action dans
    # get_queryset() - pour éviter à la fois le N+1 (détail) et le
    # sur-préchargement des items/paiements (liste).
    select_related_fields = ['customer', 'sold_by']
    prefetch_related_fields = []

    # Filtre direct par ``warehouse_id`` ; les ventes legacy sans entrepôt
    # restent visibles aux non-owner pour ne pas masquer l'historique.
    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = True
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'create': 'sales.create',
        'update': 'sales.create',
        'partial_update': 'sales.create',
        'destroy': 'sales.cancel',
        'add_payment': 'sales.create',
        'cancel': 'sales.cancel',
        'today': 'sales.view',
        'stats': 'sales.view',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return SaleListSerializer
        elif self.action == 'create':
            return SaleCreateSerializer
        elif self.action == 'add_payment':
            return SalePaymentSerializer
        return SaleDetailSerializer

    def create(self, request, *args, **kwargs):
        """Créer une vente et retourner le détail complet (avec reference, id, etc.)."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Le serializer expose ``warehouse`` ; on revalide le périmètre ici
        # pour bloquer toute tentative de soumettre un entrepôt non autorisé.
        wh = serializer.validated_data.get('warehouse')
        wh_id = getattr(wh, 'id', None) if wh is not None else None
        assert_warehouse_allowed_for_request(request, wh_id, allow_none=True)
        sale = serializer.save()
        sale.refresh_from_db()
        detail_serializer = SaleDetailSerializer(sale)
        return Response(detail_serializer.data, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        """
        Filtres : (1) par date, (2) restriction aux ventes de l'utilisateur si
        celui-ci n'a pas la permission `sales.view_all` (i.e. caissier).
        """
        queryset = super().get_queryset()

        # Optimisation des requêtes par action :
        # - liste : compteur d'items via annotation `_items_count` (un seul COUNT
        #   agrégé au lieu d'une requête par ligne), sans précharger items/paiements
        #   que la liste ne sérialise pas.
        # - détail : préchargement complet des relations lues par SaleDetailSerializer
        #   (items → product/variant, payments → payment_method/received_by).
        if self.action == 'list':
            queryset = queryset.annotate(_items_count=Count('items'))
        else:
            queryset = queryset.select_related('register', 'warehouse', 'session').prefetch_related(
                'items__product', 'items__variant',
                'payments__payment_method', 'payments__received_by',
            )

        # Restriction par caissier : un cashier ne voit que ses propres ventes.
        # Owner et manager ont `sales.view_all` et voient tout.
        if not has_perm_code(self.request, 'sales.view_all'):
            queryset = queryset.filter(sold_by=self.request.user)

        # Filtres de date
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')

        if date_from:
            queryset = queryset.filter(sale_date__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(sale_date__date__lte=date_to)

        return queryset
    
    def _award_loyalty_points(self, sale, user):
        """Attribue les points de fidélité au client pour une vente complétée.

        Idempotent : la contrainte unique ``(sale, transaction_type='earn')``
        sur ``LoyaltyTransaction`` empêche un double-award en cas de retry
        ou de race condition.
        """
        from django.db import IntegrityError
        from apps.settings.models import LoyaltyProgram, CustomerLoyalty, LoyaltyTransaction

        try:
            program = LoyaltyProgram.objects.get(organization=sale.organization, is_active=True)
        except LoyaltyProgram.DoesNotExist:
            return  # Pas de programme de fidélité actif

        # Vérifier si seuls les clients enregistrés peuvent gagner des points
        if program.only_registered_customers and not sale.customer:
            return

        # Calculer les points gagnés
        points = program.calculate_points(sale.total)
        if points <= 0:
            return

        # Lock + idempotence : si une ligne EARN existe déjà pour cette
        # vente, on sort sans rien faire (cas du retry).
        if LoyaltyTransaction.objects.filter(
            sale=sale, transaction_type=LoyaltyTransaction.TransactionType.EARN
        ).exists():
            return

        loyalty, _ = CustomerLoyalty.objects.select_for_update().get_or_create(
            organization=sale.organization,
            customer=sale.customer,
        )

        loyalty.add_points(points)

        try:
            LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.EARN,
                points=points,
                balance_after=loyalty.current_points,
                sale=sale,
                description=f"Points gagnés sur vente {sale.reference}",
                created_by=user,
            )
        except IntegrityError:
            # Race extrême : une autre transaction a créé la ligne EARN
            # juste avant. On retire les points qu'on venait d'ajouter
            # (la première transaction a déjà ajouté les siens).
            loyalty.add_points(-points)

    @action(detail=True, methods=['post'], url_path='add-payment')
    def add_payment(self, request, pk=None):
        """Ajoute un paiement à une vente existante."""
        from django.db import transaction
        from .services import SaleStockService

        # Pré-validation hors transaction (permissions, statut grossier).
        # Le sale qu'on lit ici sert uniquement à valider l'accès - le vrai
        # objet utilisé pour la mise à jour est relu avec `select_for_update`
        # à l'intérieur de la transaction pour bloquer les race conditions
        # sur `amount_paid` quand deux add_payment concurrents arrivent.
        sale_for_check = self.get_object()
        if sale_for_check.sold_by_id != request.user.id and not is_manager_or_above(request):
            return Response(
                {'error': "Vous ne pouvez ajouter un paiement qu'à vos propres ventes."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if sale_for_check.status in ['completed', 'cancelled', 'refunded']:
            return Response(
                {'error': 'Impossible d\'ajouter un paiement à cette vente'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            # Lock exclusif sur la Sale pour la durée du recalcul amount_paid /
            # amount_due / status. Deux requêtes concurrentes sont sérialisées.
            # `of=('self',)` lock UNIQUEMENT la table Sale (Postgres refuse
            # FOR UPDATE sur le côté nullable d'un LEFT JOIN - customer/warehouse).
            sale = (
                Sale.objects.select_related('customer', 'organization', 'warehouse')
                .select_for_update(of=('self',))
                .get(pk=pk)
            )
            # Re-check sous lock (le statut a pu changer entre la pré-check et le lock).
            if sale.status in ['completed', 'cancelled', 'refunded']:
                return Response(
                    {'error': 'Impossible d\'ajouter un paiement à cette vente'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            previous_status = sale.status

            # Gestion multi-devise centralisée : `create_payment` convertit le
            # montant remis (dans `currency`) vers la devise de la vente et
            # renseigne `amount` + `tendered_amount`.
            from .services import create_payment, resolve_change
            payment = create_payment(
                sale, request.user,
                payment_method_id=serializer.validated_data['payment_method'],
                tendered_amount=serializer.validated_data['amount'],
                currency=serializer.validated_data.get('currency'),
                exchange_rate=serializer.validated_data.get('exchange_rate'),
                reference=serializer.validated_data.get('reference', ''),
                notes=serializer.validated_data.get('notes', ''),
            )

            # Mettre à jour la vente (toujours en devise de la vente)
            sale.amount_paid += payment.amount
            sale.amount_due = (sale.total - sale.amount_paid).quantize(Decimal('0.01'))

            change_amount = Decimal('0.00')
            change_currency = sale.change_currency or sale.currency
            if sale.amount_paid >= sale.total:
                change_currency = serializer.validated_data.get('change_currency') or change_currency
                change_amount, change_currency, _ = resolve_change(sale, change_currency)
                sale.change_amount = change_amount
                sale.change_currency = change_currency
                sale.amount_due = Decimal('0.00')
                sale.status = 'completed'
            else:
                sale.status = 'partially_paid'

            sale.save()
            
            # Si la vente vient de passer en completed, mettre à jour le stock
            if sale.status == 'completed' and previous_status != 'completed':
                # Fallback: assigner l'entrepôt par défaut si aucun n'est défini
                if not sale.warehouse:
                    from apps.inventory.models import Warehouse as WarehouseModel
                    default_wh = WarehouseModel.objects.filter(
                        organization=sale.organization,
                        is_default=True, is_active=True, is_deleted=False
                    ).first() or WarehouseModel.objects.filter(
                        organization=sale.organization,
                        is_active=True, is_deleted=False
                    ).first()
                    if default_wh:
                        sale.warehouse = default_wh
                        sale.save(update_fields=['warehouse'])

                # Décrémentation centralisée (FIFO + re-check allow_negative + idempotent)
                SaleStockService.apply_decrement(sale, request.user)

                # Attribuer les points de fidélité si applicable
                if sale.customer:
                    self._award_loyalty_points(sale, request.user)
            
            # Enregistrer le mouvement de caisse dans la devise réellement remise
            # (+ la monnaie rendue en sortie si la vente est soldée).
            from apps.cashbook.services import (
                record_sale_payment_income, record_debt_collection_payment, record_change,
            )
            if sale.sale_type == 'credit' and sale.customer:
                record_debt_collection_payment(
                    sale.organization, sale, payment, sale.customer, request.user,
                )
            else:
                record_sale_payment_income(sale.organization, sale, payment, request.user)
            if change_amount > 0:
                record_change(
                    sale.organization, sale, change_amount, change_currency, request.user,
                )
            
            # Mettre à jour le solde client pour les ventes à crédit
            if sale.customer and sale.sale_type == 'credit':
                from apps.contacts.models import Customer, CustomerTransaction
                customer = Customer.objects.select_for_update().get(id=sale.customer.id)
                balance_before = customer.current_balance
                customer.current_balance -= payment.amount
                customer.save()
                
                # Créer une transaction client pour l'historique
                CustomerTransaction.objects.create(
                    organization=sale.organization,
                    customer=customer,
                    transaction_type='payment',
                    amount=payment.amount,
                    balance_before=balance_before,
                    balance_after=customer.current_balance,
                    sale=sale,
                    reference=sale.reference,
                    payment_method=payment.payment_method.method_type if payment.payment_method else 'cash',
                    notes=f"Paiement sur facture {sale.reference}",
                    created_by=request.user
                )
        
        return Response(SaleDetailSerializer(sale).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Annule une vente.

        Permission : sold_by (caissier auteur) OU manager+. Refusée pour les
        ventes déjà annulées/remboursées.
        """
        from django.db import transaction
        from .services import SaleStockService

        sale = self.get_object()

        if sale.sold_by_id != request.user.id and not is_manager_or_above(request):
            return Response(
                {'error': "Vous ne pouvez annuler que vos propres ventes."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if sale.status in ['cancelled', 'refunded']:
            return Response(
                {'error': 'Cette vente est déjà annulée'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Si la vente provient d'un devis converti mais n'a pas encore
            # été encaissée, libérer la réservation de stock. Idempotent :
            # ne fait rien pour les ventes POS directes (stock_reserved=False).
            SaleStockService.release_reservation(sale, request.user)

            # Restitution centralisée (idempotente : ne fait rien si stock pas committed)
            SaleStockService.revert(sale, request.user)

            # Enregistrer le mouvement de caisse (remboursement) si la vente avait été payée
            if sale.amount_paid > 0:
                from apps.cashbook.services import record_sale_cancellation
                record_sale_cancellation(
                    organization=sale.organization,
                    sale=sale,
                    amount=sale.amount_paid,
                    user=request.user,
                )

            # Restaurer le solde client pour les ventes à crédit.
            #
            # Logique : à la création, on a fait `balance += amount_due_initial`.
            # Chaque add_payment a fait `balance -= payment.amount`.
            # L'impact NET courant sur le solde client est donc `sale.total - sale.amount_paid`
            # (peut être négatif si le client a sur-payé → il a un crédit).
            # Pour annuler proprement, on soustrait exactement cet impact net,
            # ce qui ramène le solde à sa valeur d'avant la vente.
            if sale.customer and sale.sale_type == 'credit':
                from apps.contacts.models import Customer, CustomerTransaction
                customer = Customer.objects.select_for_update().get(id=sale.customer.id)
                net_balance_impact = (sale.total - sale.amount_paid).quantize(Decimal('0.01'))
                if net_balance_impact != 0:
                    balance_before = customer.current_balance
                    customer.current_balance -= net_balance_impact
                    customer.save()
                    # `amount` du modèle CustomerTransaction est une magnitude
                    # positive (>= 0.01). La direction est portée par
                    # balance_before/after et le type 'adjustment'.
                    CustomerTransaction.objects.create(
                        organization=sale.organization,
                        customer=customer,
                        transaction_type=CustomerTransaction.TransactionType.ADJUSTMENT,
                        amount=abs(net_balance_impact),
                        balance_before=balance_before,
                        balance_after=customer.current_balance,
                        sale=sale,
                        reference=sale.reference,
                        notes=(
                            f"Annulation vente {sale.reference} "
                            f"(impact net solde : {-net_balance_impact:+})"
                        ),
                        created_by=request.user,
                    )

            # Reverser les transactions de fidélité liées à cette vente.
            # Idempotent : on ne crée un REVERSAL que s'il n'existe pas déjà.
            self._reverse_loyalty_transactions(sale, request.user)

            sale.status = 'cancelled'
            sale.save()

        return Response({'status': 'cancelled'})

    def _reverse_loyalty_transactions(self, sale, user):
        """
        Inverse les transactions de fidélité d'une vente annulée :
        - une ligne ``EARN`` → on retire les points et on crée ``EARN_REVERSAL``
        - une ligne ``REDEEM`` → on restitue les points et on crée ``REDEEM_REVERSAL``

        Idempotent : si un REVERSAL existe déjà pour cette vente, ne fait rien.
        """
        from apps.settings.models import CustomerLoyalty, LoyaltyTransaction

        TxType = LoyaltyTransaction.TransactionType

        for original_type, reversal_type, reversal_label in (
            (TxType.EARN, TxType.EARN_REVERSAL, "Annulation des points gagnés"),
            (TxType.REDEEM, TxType.REDEEM_REVERSAL, "Restitution des points utilisés"),
        ):
            if LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=reversal_type
            ).exists():
                continue  # déjà reversé

            original = LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=original_type
            ).first()
            if not original:
                continue

            loyalty = CustomerLoyalty.objects.select_for_update().filter(
                pk=original.customer_loyalty_id
            ).first()
            if not loyalty:
                continue

            # Inversion : on annule l'effet sur le solde de points.
            # original.points est positif pour EARN, négatif pour REDEEM.
            loyalty.add_points(-original.points)

            LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=loyalty,
                transaction_type=reversal_type,
                points=-original.points,
                balance_after=loyalty.current_points,
                sale=sale,
                description=f"{reversal_label} - annulation vente {sale.reference}",
                created_by=user,
            )

    @action(detail=False, methods=['get'])
    def today(self, request):
        """Retourne les ventes du jour avec pagination."""
        organization = self.get_organization()
        today = timezone.now().date()
        
        sales = Sale.objects.filter(
            organization=organization,
            sale_date__date=today,
            is_deleted=False
        ).select_related('customer', 'sold_by').order_by('-sale_date')
        
        page = self.paginate_queryset(sales)
        if page is not None:
            serializer = SaleListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = SaleListSerializer(sales, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='mark-receipt-printed')
    def mark_receipt_printed(self, request, pk=None):
        """Marque le reçu comme imprimé."""
        sale = self.get_object()
        
        sale.receipt_printed = True
        sale.save(update_fields=['receipt_printed'])
        
        return Response(SaleDetailSerializer(sale).data)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Retourne les statistiques de ventes."""
        organization = self.get_organization()
        
        # Paramètres de période
        period = request.query_params.get('period', 'today')
        today = timezone.now().date()
        
        if period == 'today':
            date_filter = {'sale_date__date': today}
        elif period == 'week':
            start = today - timezone.timedelta(days=7)
            date_filter = {'sale_date__date__gte': start}
        elif period == 'month':
            start = today - timezone.timedelta(days=30)
            date_filter = {'sale_date__date__gte': start}
        else:
            date_filter = {}
        
        sales = Sale.objects.filter(
            organization=organization,
            status='completed',
            is_deleted=False,
            **date_filter
        )
        
        stats = sales.aggregate(
            total_sales=Sum('total'),
            total_tax=Sum('tax_amount'),
            total_discount=Sum('discount_amount'),
            count=Count('id')
        )
        
        avg_sale = 0
        if stats['count'] and stats['count'] > 0 and stats['total_sales']:
            avg_sale = stats['total_sales'] / stats['count']
        
        # Ventes par méthode de paiement
        by_payment = Payment.objects.filter(
            sale__in=sales,
            status='completed'
        ).values('payment_method__name').annotate(
            total=Sum('amount'),
            count=Count('id')
        )
        
        # Ventes par type
        by_type = sales.values('sale_type').annotate(
            total=Sum('total'),
            count=Count('id')
        )
        
        return Response({
            'summary': {
                'total_sales': str(stats['total_sales'] or 0),
                'total_tax': str(stats['total_tax'] or 0),
                'total_discount': str(stats['total_discount'] or 0),
                'count': stats['count'],
                'average': str(avg_sale)
            },
            'by_payment_method': list(by_payment),
            'by_type': list(by_type)
        })


# =============================================================================
# SALE RETURN VIEWSET
# =============================================================================

class SaleReturnViewSet(
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des retours de vente.
    
    Endpoints:
    - GET /sale-returns/ : Liste des retours
    - POST /sale-returns/ : Créer un retour
    - GET /sale-returns/{id}/ : Détail d'un retour
    - POST /sale-returns/{id}/approve/ : Approuver un retour
    - POST /sale-returns/{id}/reject/ : Rejeter un retour
    """
    
    queryset = SaleReturn.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'return_type']
    search_fields = ['reference', 'original_sale__reference']
    ordering = ['-return_date']
    
    select_related_fields = ['original_sale', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__original_item__product']

    warehouse_scope_field = 'original_sale__warehouse_id'
    warehouse_scope_include_null = True
    
    action_permissions = {
        'list': 'sale_returns.view',
        'retrieve': 'sale_returns.view',
        'create': 'sale_returns.create',
        'approve': 'sale_returns.approve',
        'reject': 'sale_returns.approve',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return SaleReturnListSerializer
        elif self.action == 'create':
            return SaleReturnCreateSerializer
        return SaleReturnDetailSerializer

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve un retour et remet le stock."""
        from django.db import transaction
        from apps.inventory.models import Stock, StockMovement
        
        sale_return = self.get_object()
        
        if sale_return.status != 'draft':
            return Response(
                {'error': 'Seuls les retours en brouillon peuvent être approuvés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with transaction.atomic():
            # Remise en stock déléguée au service, qui centralise le recalcul du
            # coût moyen et la mise à jour du partage scellé/vrac.
            from .services import SaleStockService
            SaleStockService.apply_return(sale_return, request.user)

            sale_return.status = 'completed'
            sale_return.approved_by = request.user
            sale_return.approved_at = timezone.now()
            sale_return.save()
            
            # Enregistrer le mouvement de caisse si remboursement au client
            if sale_return.refund_amount and sale_return.refund_amount > 0:
                from apps.cashbook.services import record_sale_return_refund
                record_sale_return_refund(
                    organization=sale_return.organization,
                    sale_return=sale_return,
                    amount=sale_return.refund_amount,
                    user=request.user,
                )
        
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejette un retour."""
        sale_return = self.get_object()
        
        if sale_return.status != 'draft':
            return Response(
                {'error': 'Seuls les retours en brouillon peuvent être rejetés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        sale_return.status = 'rejected'
        sale_return.save()
        
        return Response({'status': 'rejected'})


# =============================================================================
# QUOTATION VIEWSET
# =============================================================================

class QuotationViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des devis.
    
    Endpoints:
    - GET /quotations/ : Liste des devis
    - POST /quotations/ : Créer un devis
    - GET /quotations/{id}/ : Détail d'un devis
    - PUT/PATCH /quotations/{id}/ : Modifier un devis
    - DELETE /quotations/{id}/ : Supprimer un devis
    - POST /quotations/{id}/convert/ : Convertir en vente
    - POST /quotations/{id}/send/ : Marquer comme envoyé
    """
    
    queryset = Quotation.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer']
    search_fields = ['reference', 'customer__name']
    ordering = ['-created_at']
    
    select_related_fields = ['customer', 'created_by', 'converted_sale']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'create': 'sales.create',
        'update': 'sales.create',
        'partial_update': 'sales.create',
        'destroy': 'sales.cancel',
        'convert': 'sales.create',
        'send': 'sales.create',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return QuotationListSerializer
        elif self.action == 'create':
            return QuotationCreateSerializer
        return QuotationDetailSerializer

    @action(detail=True, methods=['post'])
    def convert(self, request, pk=None):
        """Convertit un devis en vente."""
        from django.db import transaction
        from apps.inventory.models import Stock
        from .services import SaleStockService

        quotation = self.get_object()
        
        if quotation.status == 'converted':
            return Response(
                {'error': 'Ce devis a déjà été converti'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if quotation.status == 'expired':
            return Response(
                {'error': 'Ce devis est expiré'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Récupérer l'entrepôt cible : prioriser l'entrepôt envoyé,
        # sinon le premier entrepôt assigné au membre, sinon le défaut.
        from apps.inventory.models import Warehouse

        membership = get_membership_for_request(request)
        allowed_ids = accessible_warehouse_ids(membership) if membership else None

        warehouse = None
        explicit_wh = request.data.get('warehouse') if hasattr(request, 'data') else None
        if explicit_wh:
            assert_warehouse_allowed_for_request(request, explicit_wh)
            warehouse = Warehouse.objects.filter(
                id=explicit_wh,
                organization=quotation.organization,
                is_active=True,
                is_deleted=False,
            ).first()

        if warehouse is None:
            base_qs = Warehouse.objects.filter(
                organization=quotation.organization,
                is_active=True,
                is_deleted=False,
            )
            if allowed_ids is not None:
                base_qs = base_qs.filter(id__in=allowed_ids)
            warehouse = (
                base_qs.filter(is_default=True).first()
                or base_qs.first()
            )

        if warehouse is None and allowed_ids is not None:
            return Response(
                {'error': "Aucun entrepôt accessible pour convertir ce devis."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Vérifier le stock si un entrepôt est disponible
        if warehouse:
            for item in quotation.items.all():
                if item.product.track_inventory and not item.product.allow_negative_stock:
                    stock = Stock.objects.filter(
                        product=item.product,
                        variant=item.variant,
                        warehouse=warehouse
                    ).first()
                    
                    available = stock.available_quantity if stock else 0
                    if item.quantity > available:
                        return Response(
                            {'error': f"Stock insuffisant pour {item.product.name}. Disponible: {available}"},
                            status=status.HTTP_400_BAD_REQUEST
                        )
        
        with transaction.atomic():
            # Créer la vente à partir du devis
            from apps.core.utils import ReferenceGenerator
            
            sale = Sale.objects.create(
                organization=quotation.organization,
                reference=ReferenceGenerator.generate_sale_reference(quotation.organization),
                customer=quotation.customer,
                warehouse=warehouse,
                sale_type='retail',
                status='pending',
                subtotal=quotation.subtotal,
                tax_amount=quotation.tax_amount,
                discount_amount=quotation.discount_amount,
                total=quotation.total,
                amount_due=quotation.total,
                notes=quotation.notes,
                sold_by=request.user,
                is_pos=False
            )
            
            # Copier les items
            for item in quotation.items.all():
                SaleItem.objects.create(
                    sale=sale,
                    organization=quotation.organization,
                    product=item.product,
                    variant=item.variant,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    cost_price=item.product.cost_price,
                    discount_percentage=item.discount_percentage,
                    tax_rate=item.tax_rate,
                    subtotal=item.quantity * item.unit_price,
                    total=item.total
                )
            
            quotation.status = 'converted'
            quotation.converted_sale = sale
            quotation.save()

            # Réserver immédiatement le stock pour éviter qu'un autre cashier
            # ne vende les mêmes unités pendant la fenêtre conversion →
            # encaissement. Le décrément effectif sera fait par
            # ``SaleStockService.apply_decrement`` lors du ``add_payment``.
            if sale.warehouse:
                SaleStockService.reserve_stock(sale, request.user)

        return Response({
            'status': 'converted',
            'sale_id': str(sale.id),
            'sale_reference': sale.reference
        })

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Marque un devis comme envoyé."""
        quotation = self.get_object()
        
        if quotation.status != 'draft':
            return Response(
                {'error': 'Seuls les devis en brouillon peuvent être envoyés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        quotation.status = 'sent'
        quotation.save()

        # Email envoyé en best-effort : si l'API SMTP est down ou pas
        # configurée, on continue (la vente / le devis reste créé). Le mode
        # console (dev) affiche le mail en stdout pour vérification.
        from apps.core.email_service import send_quotation_email
        recipient = request.data.get('recipient_email') if hasattr(request, 'data') else None
        send_quotation_email(quotation, recipient_email=recipient)

        return Response({'status': 'sent'})
