"""
ViewSets pour le module Livre de Caisse.
"""
from decimal import Decimal
from django.db.models import Sum, Count, Q, F, DecimalField
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from apps.core.api_mixins import (
    TenantViewSetMixin,
    AuditMixin,
    WarehouseAssertCreateMixin,
)
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    get_membership_for_request,
    restrict_visibility_for_request,
)
from apps.core.api_permissions import IsTenantMember, HasPermission

from .models import IncomeCategory, ExpenseCategory, Expense, CashMovement
from .serializers import (
    IncomeCategoryListSerializer, IncomeCategoryCreateSerializer,
    IncomeCategoryDetailSerializer,
    ExpenseCategoryListSerializer, ExpenseCategoryCreateSerializer,
    ExpenseCategoryDetailSerializer,
    ExpenseListSerializer, ExpenseCreateSerializer,
    ExpenseDetailSerializer, ExpenseUpdateSerializer,
    CashMovementListSerializer, CashMovementCreateSerializer,
    CashMovementDetailSerializer,
)

# =============================================================================
# INCOME CATEGORY VIEWSET
# =============================================================================

class IncomeCategoryViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    CRUD pour les catégories d'entrées de caisse.
    """

    queryset = IncomeCategory.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'code', 'description']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']

    action_permissions = {
        'list': 'cashbook.view',
        'retrieve': 'cashbook.view',
        'create': 'cashbook.manage_categories',
        'update': 'cashbook.manage_categories',
        'partial_update': 'cashbook.manage_categories',
        'destroy': 'cashbook.manage_categories',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return IncomeCategoryListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return IncomeCategoryCreateSerializer
        return IncomeCategoryDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        # Annoter avec le nombre de mouvements et le total des entrées.
        # `total_amount` est CONVERTI en devise principale (montant × taux) :
        # une catégorie peut agréger des entrées de devises différentes, qu'on
        # ne peut pas additionner brutes. Le détail par devise est disponible
        # dans les rapports de caisse (`summary`, `daily-report`…).
        in_filter = Q(cash_movements__is_cancelled=False, cash_movements__direction='in')
        queryset = queryset.annotate(
            movement_count=Count('cash_movements', filter=in_filter),
            total_amount=Coalesce(
                Sum(
                    F('cash_movements__amount') * F('cash_movements__exchange_rate'),
                    filter=in_filter,
                    output_field=DecimalField(max_digits=24, decimal_places=6),
                ),
                Decimal('0'),
                output_field=DecimalField(max_digits=24, decimal_places=6),
            ),
        )
        return queryset


# =============================================================================
# EXPENSE CATEGORY VIEWSET
# =============================================================================

class ExpenseCategoryViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    CRUD pour les catégories de dépenses.
    """

    queryset = ExpenseCategory.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'code', 'description']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']

    action_permissions = {
        'list': 'cashbook.view',
        'retrieve': 'cashbook.view',
        'create': 'cashbook.manage_categories',
        'update': 'cashbook.manage_categories',
        'partial_update': 'cashbook.manage_categories',
        'destroy': 'cashbook.manage_categories',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return ExpenseCategoryListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return ExpenseCategoryCreateSerializer
        return ExpenseCategoryDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        # Annoter avec le nombre de dépenses et le total dépensé.
        # `total_spent` est CONVERTI en devise principale (montant × taux) pour
        # rester comparable à `budget_monthly`, qui est exprimé en principale.
        paid_filter = Q(expenses__status__in=['approved', 'paid'])
        queryset = queryset.annotate(
            expense_count=Count('expenses', filter=paid_filter),
            total_spent=Coalesce(
                Sum(
                    F('expenses__amount') * F('expenses__exchange_rate'),
                    filter=paid_filter,
                    output_field=DecimalField(max_digits=24, decimal_places=6),
                ),
                Decimal('0'),
                output_field=DecimalField(max_digits=24, decimal_places=6),
            ),
        )
        return queryset


# =============================================================================
# EXPENSE VIEWSET
# =============================================================================

class ExpenseViewSet(
    WarehouseAssertCreateMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    CRUD pour les dépenses + actions d'approbation et de paiement.
    
    Workflow :
    1. Créer une dépense (draft)
    2. Soumettre pour approbation (pending)
    3. Approuver (approved) → crée le mouvement de caisse
    4. Marquer comme payée (paid)
    
    Ou directement : créer + payer en une fois (pour les petites dépenses).
    """

    queryset = Expense.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'category', 'is_recurring', 'warehouse', 'currency']
    search_fields = ['reference', 'description', 'beneficiary', 'notes']
    ordering_fields = ['expense_date', 'amount', 'created_at']
    ordering = ['-expense_date']

    select_related_fields = ['category', 'warehouse', 'payment_method', 'created_by', 'approved_by']

    # Le champ ``warehouse`` est nullable (les dépenses globales restent
    # tolérées), mais filtrées par le périmètre membre quand renseigné.
    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = True
    warehouse_write_required = False
    warehouse_write_allow_none = True

    action_permissions = {
        'list': 'cashbook.view',
        'retrieve': 'cashbook.view',
        'create': 'cashbook.create_expense',
        'update': 'cashbook.create_expense',
        'partial_update': 'cashbook.create_expense',
        'destroy': 'cashbook.delete_expense',
        'submit': 'cashbook.create_expense',
        'approve': 'cashbook.approve_expense',
        'reject': 'cashbook.approve_expense',
        'pay': 'cashbook.approve_expense',
        'cancel': 'cashbook.approve_expense',
        'stats': 'cashbook.view',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return ExpenseListSerializer
        elif self.action == 'create':
            return ExpenseCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return ExpenseUpdateSerializer
        return ExpenseDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()

        # Visibilité par rôle :
        # - owner : toutes les dépenses ;
        # - caissier : uniquement ses propres dépenses (created_by), partout ;
        # - gérant / magasinier : dépenses de leurs entrepôts assignés. Les
        #   dépenses « org-level » sans entrepôt (loyer, salaires...) restent
        #   réservées au owner (include_null_warehouse=False).
        queryset = restrict_visibility_for_request(
            queryset,
            self.request,
            warehouse_field='warehouse_id',
            creator_field='created_by',
        )

        # Filtres de date
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(expense_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(expense_date__lte=date_to)

        return queryset

    @staticmethod
    def _resolve_currency(serializer, organization):
        """Devise + taux d'une dépense, résolus côté serveur.

        ``Expense.exchange_rate`` a un défaut modèle de 1.000000, donc
        ``validated_data`` en contient toujours un : on ne considère un taux
        comme « fourni par le client » que s'il est présent dans le payload
        brut. Sinon une dépense en USD serait enregistrée au taux 1.
        """
        from .services import resolve_currency_rate
        rate = (
            serializer.validated_data.get('exchange_rate')
            if 'exchange_rate' in serializer.initial_data
            else None
        )
        return resolve_currency_rate(
            organization, serializer.validated_data.get('currency'), rate
        )

    def perform_create(self, serializer):
        # Vérifie le périmètre warehouse avant de générer la référence.
        self._assert_warehouse_on_save(serializer)
        from apps.core.utils import ReferenceGenerator
        organization = self.get_organization()
        reference = ReferenceGenerator.generate_expense_reference(organization)
        currency, exchange_rate = self._resolve_currency(serializer, organization)
        serializer.save(
            organization=organization,
            reference=reference,
            currency=currency,
            exchange_rate=exchange_rate,
            created_by=self.request.user,
        )

    def perform_update(self, serializer):
        expense = self.get_object()
        if expense.status not in ['draft', 'pending']:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Seules les dépenses en brouillon ou en attente peuvent être modifiées.")
        if 'warehouse' in serializer.validated_data:
            self._assert_warehouse_on_save(serializer)
        # Une modification de devise doit re-résoudre le taux : on repart de la
        # devise du payload si fournie, sinon de celle déjà enregistrée.
        organization = self.get_organization()
        if 'currency' not in serializer.validated_data:
            serializer.validated_data['currency'] = expense.currency
        currency, exchange_rate = self._resolve_currency(serializer, organization)
        serializer.save(currency=currency, exchange_rate=exchange_rate)

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Soumettre une dépense pour approbation."""
        expense = self.get_object()
        if expense.status != 'draft':
            return Response(
                {'detail': "Seules les dépenses en brouillon peuvent être soumises."},
                status=status.HTTP_400_BAD_REQUEST
            )
        expense.status = 'pending'
        expense.save(update_fields=['status', 'updated_at'])
        return Response(ExpenseDetailSerializer(expense).data)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuver une dépense et créer le mouvement de caisse."""
        expense = self.get_object()
        if expense.status != 'pending':
            return Response(
                {'detail': "Seules les dépenses en attente peuvent être approuvées."},
                status=status.HTTP_400_BAD_REQUEST
            )

        expense.status = 'approved'
        expense.approved_by = request.user
        expense.save(update_fields=['status', 'approved_by', 'updated_at'])

        # Créer le mouvement de caisse
        self._create_cash_movement_for_expense(expense)

        return Response(ExpenseDetailSerializer(expense).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejeter une dépense."""
        expense = self.get_object()
        if expense.status != 'pending':
            return Response(
                {'detail': "Seules les dépenses en attente peuvent être rejetées."},
                status=status.HTTP_400_BAD_REQUEST
            )

        reason = request.data.get('reason', '')
        expense.status = 'rejected'
        expense.notes = f"{expense.notes}\n--- Rejet ---\n{reason}".strip() if reason else expense.notes
        expense.save(update_fields=['status', 'notes', 'updated_at'])
        return Response(ExpenseDetailSerializer(expense).data)

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        """
        Marquer une dépense comme payée.
        Si la dépense est en brouillon ou en attente, elle est automatiquement approuvée.
        """
        expense = self.get_object()
        if expense.status in ['paid', 'cancelled']:
            return Response(
                {'detail': "Cette dépense ne peut pas être marquée comme payée."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Paiement direct (shortcut pour petites dépenses)
        if expense.status in ['draft', 'pending']:
            expense.approved_by = request.user
            # Créer le mouvement de caisse si pas encore fait
            if not expense.cash_movements.filter(is_cancelled=False).exists():
                self._create_cash_movement_for_expense(expense)

        payment_method_id = request.data.get('payment_method')
        payment_reference = request.data.get('payment_reference', '')

        if payment_method_id:
            from apps.sales.models import PaymentMethod
            try:
                expense.payment_method = PaymentMethod.objects.get(id=payment_method_id)
            except PaymentMethod.DoesNotExist:
                pass
        if payment_reference:
            expense.payment_reference = payment_reference

        expense.status = 'paid'
        expense.paid_date = timezone.now().date()
        expense.save()
        return Response(ExpenseDetailSerializer(expense).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annuler une dépense et son mouvement de caisse."""
        expense = self.get_object()
        if expense.status == 'cancelled':
            return Response(
                {'detail': "Cette dépense est déjà annulée."},
                status=status.HTTP_400_BAD_REQUEST
            )

        reason = request.data.get('reason', '')

        # Annuler les mouvements de caisse liés
        for movement in expense.cash_movements.filter(is_cancelled=False):
            movement.is_cancelled = True
            movement.cancelled_at = timezone.now()
            movement.cancelled_by = request.user
            movement.cancel_reason = reason
            movement.save()

        expense.status = 'cancelled'
        expense.save(update_fields=['status', 'updated_at'])
        return Response(ExpenseDetailSerializer(expense).data)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Statistiques des dépenses."""
        organization = self.get_organization()
        queryset = Expense.objects.filter(
            organization=organization,
            status__in=['approved', 'paid']
        )

        # Restreindre au périmètre du membre (None pour owner = pas de filtre)
        membership = get_membership_for_request(request)
        if membership:
            allowed_ids = accessible_warehouse_ids(membership)
            if allowed_ids is not None:
                queryset = queryset.filter(
                    Q(warehouse_id__in=allowed_ids) | Q(warehouse__isnull=True)
                )

        # Filtres de date
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(expense_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(expense_date__lte=date_to)

        from .services import primary_sum
        primary = organization.currency or 'CDF'

        # Totaux PAR DEVISE (le tiroir ne mélange jamais les devises) ET total
        # converti en devise principale (chiffre comptable, pour le budget/P&L).
        by_currency = list(
            queryset.values('currency').annotate(
                total=Sum('amount', default=Decimal('0.00')),
                count=Count('id'),
            ).order_by('currency')
        )
        totals = queryset.aggregate(
            total_primary=primary_sum('amount'),
            count=Count('id'),
        )

        # Par catégorie (ventilé par devise)
        by_category = queryset.values(
            'currency', 'category__name', 'category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Par mois (ventilé par devise)
        by_month = queryset.annotate(
            month=TruncMonth('expense_date')
        ).values('month', 'currency').annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('month', 'currency')

        return Response({
            'currency': primary,
            'by_currency': by_currency,
            'total_primary': totals['total_primary'],
            # `total` = rétro-compat : désormais le total CONVERTI en devise
            # principale, plus jamais une somme de devises hétérogènes.
            'total': totals['total_primary'],
            'count': totals['count'],
            'by_category': list(by_category),
            'by_month': list(by_month),
        })

    def _create_cash_movement_for_expense(self, expense):
        """Crée un mouvement de caisse pour une dépense approuvée (multi-devise)."""
        from .services import get_open_session_for_user, _movement
        organization = expense.organization

        # Rattache la sortie de caisse à la session ouverte de l'utilisateur
        # qui paie (caissier au comptoir) pour le calcul de la caisse nette.
        session = get_open_session_for_user(organization, self.request.user)

        # La sortie de caisse hérite de la devise de la dépense ; `_movement`
        # calcule `balance_after` par devise.
        _movement(
            organization,
            direction='out',
            movement_type='expense',
            amount=expense.amount,
            currency=expense.currency,
            exchange_rate=expense.exchange_rate,
            description=f"Dépense: {expense.description}",
            payment_method=expense.payment_method,
            expense=expense,
            session=session,
            user=expense.created_by,
        )


# =============================================================================
# CASH MOVEMENT VIEWSET
# =============================================================================

class CashMovementViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les mouvements de caisse.
    
    Les mouvements liés aux ventes et dépenses sont créés automatiquement.
    Ce ViewSet permet aussi de créer des mouvements manuels
    (apports de fonds, retraits, ajustements, etc.).

    Filtrage par entrepôt : un mouvement est visible si
    - le membre est ``owner``, OU
    - la vente liée appartient au périmètre du membre, OU
    - la dépense liée appartient au périmètre du membre.
    """

    queryset = CashMovement.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['direction', 'movement_type', 'is_cancelled', 'currency']
    search_fields = ['reference', 'description', 'notes']
    ordering_fields = ['movement_date', 'amount', 'created_at']
    ordering = ['-movement_date']

    select_related_fields = [
        'payment_method', 'sale', 'expense', 'customer', 'supplier',
        'created_by', 'cancelled_by',
    ]

    action_permissions = {
        'list': 'cashbook.view',
        'retrieve': 'cashbook.view',
        'create': 'cashbook.create_movement',
        'update': 'cashbook.create_movement',
        'partial_update': 'cashbook.create_movement',
        'destroy': 'cashbook.delete_movement',
        'cancel': 'cashbook.cancel_movement',
        'summary': 'cashbook.view',
        'daily_report': 'cashbook.view_reports',
        'monthly_report': 'cashbook.view_reports',
        'annual_report': 'cashbook.view_reports',
        'custom_report': 'cashbook.view_reports',
        'balance': 'cashbook.view',
    }

    http_method_names = ['get', 'post', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'list':
            return CashMovementListSerializer
        elif self.action == 'create':
            return CashMovementCreateSerializer
        return CashMovementDetailSerializer

    def _scope_cash_movements_to_membership(self, queryset):
        """Restreint un queryset CashMovement selon le rôle du membre courant.

        - owner : tous les mouvements ;
        - caissier : uniquement ceux qu'il a créés (``created_by``), partout ;
        - gérant / magasinier : mouvements rattachés à leurs entrepôts, via la
          vente liée, la dépense liée, ou la session de caisse (entrées/sorties
          manuelles saisies en caisse).
        """
        from apps.organizations.models import OrganizationMembership

        membership = get_membership_for_request(self.request)
        if not membership:
            return queryset
        role = membership.role
        if role == OrganizationMembership.Role.OWNER:
            return queryset
        if role == OrganizationMembership.Role.CASHIER:
            return queryset.filter(created_by=membership.user)
        allowed_ids = accessible_warehouse_ids(membership)
        if not allowed_ids:
            # Aucun entrepôt assigné => aucun mouvement visible.
            return queryset.none()
        return queryset.filter(
            Q(sale__warehouse_id__in=allowed_ids)
            | Q(expense__warehouse_id__in=allowed_ids)
            | Q(session__register__warehouse_id__in=allowed_ids)
        )

    # ------------------------------------------------------------------
    # Agrégation PAR DEVISE (jamais de somme inter-devises, jamais de
    # conversion : une caisse multi-devise reflète le tiroir physique où
    # chaque devise se cumule séparément).
    # ------------------------------------------------------------------
    def _primary_currency(self):
        return (self.get_organization().currency or 'CDF')

    @staticmethod
    def _currencies_in(qs):
        # `.order_by()` neutralise l'ordering par défaut du modèle : sinon
        # DISTINCT inclut les colonnes de tri et renvoie des devises en double.
        return list(
            qs.order_by().values_list('currency', flat=True).distinct()
        )

    def _last_balance_by_currency(self, qs):
        """Solde courant (`balance_after` du dernier mouvement) par devise."""
        result = {}
        for ccy in self._currencies_in(qs):
            last = qs.filter(currency=ccy).order_by(
                '-movement_date', '-created_at'
            ).first()
            result[ccy] = last.balance_after if last else Decimal('0.00')
        return result

    @staticmethod
    def _totals_by_currency(qs):
        """Entrées/sorties/net/count regroupés par devise."""
        rows = qs.values('currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('currency')
        return [
            {
                'currency': r['currency'],
                'total_in': r['total_in'],
                'total_out': r['total_out'],
                'net': r['total_in'] - r['total_out'],
                'count': r['count'],
            }
            for r in rows
        ]

    def _report_by_currency(self, base_qs, period_qs, before_date):
        """Ouverture / entrées / sorties / clôture PAR DEVISE pour une période.

        ``before_date`` : les mouvements strictement antérieurs déterminent le
        solde d'ouverture de chaque devise (0 si la devise n'existait pas encore).
        """
        prev_qs = base_qs.filter(movement_date__date__lt=before_date)
        opening_map = self._last_balance_by_currency(prev_qs)
        period_totals = {r['currency']: r for r in self._totals_by_currency(period_qs)}
        rows = []
        for ccy in sorted(set(opening_map) | set(period_totals)):
            opening = opening_map.get(ccy, Decimal('0.00'))
            t = period_totals.get(ccy)
            tin = t['total_in'] if t else Decimal('0.00')
            tout = t['total_out'] if t else Decimal('0.00')
            rows.append({
                'currency': ccy,
                'opening_balance': opening,
                'total_in': tin,
                'total_out': tout,
                'net': tin - tout,
                'closing_balance': opening + tin - tout,
            })
        return rows

    def get_queryset(self):
        queryset = super().get_queryset()

        queryset = self._scope_cash_movements_to_membership(queryset)

        # Filtres de date
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(movement_date__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(movement_date__date__lte=date_to)

        return queryset

    def perform_create(self, serializer):
        """Créer un mouvement de caisse manuel (multi-devise)."""
        from apps.core.utils import ReferenceGenerator
        from .services import (
            get_open_session_for_user, _get_last_balance, resolve_currency_rate,
        )
        organization = self.get_organization()

        amount = serializer.validated_data['amount']
        direction = serializer.validated_data['direction']

        # Devise + taux résolus côté serveur (le taux sert à reconvertir en
        # devise principale dans les rapports comptables). Le défaut modèle de
        # `exchange_rate` étant 1.000000, on ne retient un taux client que s'il
        # figure explicitement dans le payload.
        rate = (
            serializer.validated_data.get('exchange_rate')
            if 'exchange_rate' in serializer.initial_data
            else None
        )
        currency, exchange_rate = resolve_currency_rate(
            organization, serializer.validated_data.get('currency'), rate
        )

        # Solde courant DE LA DEVISE du mouvement (le tiroir est suivi par devise).
        previous_balance = _get_last_balance(organization, currency)
        new_balance = previous_balance + amount if direction == 'in' else previous_balance - amount

        # Rattache l'apport/retrait à la session ouverte du membre (caisse nette
        # + visibilité entrepôt des mouvements manuels via session.register).
        session = get_open_session_for_user(organization, self.request.user)

        serializer.save(
            organization=organization,
            reference=ReferenceGenerator.generate_cash_movement_reference(organization),
            currency=currency,
            exchange_rate=exchange_rate,
            balance_after=new_balance,
            session=session,
            created_by=self.request.user,
        )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annuler un mouvement de caisse."""
        movement = self.get_object()
        if movement.is_cancelled:
            return Response(
                {'detail': "Ce mouvement est déjà annulé."},
                status=status.HTTP_400_BAD_REQUEST
            )

        reason = request.data.get('reason', '')
        movement.is_cancelled = True
        movement.cancelled_at = timezone.now()
        movement.cancelled_by = request.user
        movement.cancel_reason = reason
        movement.save()

        return Response(CashMovementDetailSerializer(movement).data)

    @action(detail=False, methods=['get'])
    def balance(self, request):
        """Solde actuel de la caisse, ventilé PAR DEVISE (tiroir physique)."""
        organization = self.get_organization()
        primary = self._primary_currency()

        scoped_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )

        # Solde courant par devise (dernier balance_after de chaque devise).
        balances = self._last_balance_by_currency(scoped_qs)

        # Totaux du jour par devise.
        today = timezone.now().date()
        today_totals = {
            r['currency']: r
            for r in self._totals_by_currency(scoped_qs.filter(movement_date__date=today))
        }

        currencies = sorted(set(balances) | set(today_totals))
        by_currency = []
        for ccy in currencies:
            bal = balances.get(ccy, Decimal('0.00'))
            t = today_totals.get(ccy)
            tin = t['total_in'] if t else Decimal('0.00')
            tout = t['total_out'] if t else Decimal('0.00')
            by_currency.append({
                'currency': ccy,
                'balance': bal,
                'today_in': tin,
                'today_out': tout,
                'today_net': tin - tout,
            })

        # Champs scalaires = devise principale uniquement (rétro-compat ; plus
        # jamais une somme mélangée de devises différentes).
        primary_row = next((r for r in by_currency if r['currency'] == primary), None)
        return Response({
            'by_currency': by_currency,
            'currency': primary,
            'balance': primary_row['balance'] if primary_row else Decimal('0.00'),
            'today_in': primary_row['today_in'] if primary_row else Decimal('0.00'),
            'today_out': primary_row['today_out'] if primary_row else Decimal('0.00'),
            'today_net': primary_row['today_net'] if primary_row else Decimal('0.00'),
        })

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Résumé des mouvements avec totaux par type et direction."""
        organization = self.get_organization()
        queryset = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )

        # Filtres de date
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(movement_date__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(movement_date__date__lte=date_to)

        primary = self._primary_currency()

        # Totaux PAR DEVISE (jamais de somme inter-devises).
        by_currency = self._totals_by_currency(queryset)

        # Par type de mouvement (ventilé par devise).
        by_type = queryset.values(
            'currency', 'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('currency', '-total')

        # Par jour (ventilé par devise).
        by_day = queryset.annotate(
            day=TruncDate('movement_date')
        ).values('day', 'currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day', 'currency')

        # Scalaires = devise principale (rétro-compat).
        primary_totals = next(
            (r for r in by_currency if r['currency'] == primary),
            {'total_in': Decimal('0.00'), 'total_out': Decimal('0.00'),
             'net': Decimal('0.00'), 'count': 0},
        )
        return Response({
            'currency': primary,
            'total_in': primary_totals['total_in'],
            'total_out': primary_totals['total_out'],
            'net': primary_totals['net'],
            'count': primary_totals['count'],
            'by_currency': by_currency,
            'by_type': list(by_type),
            'by_day': list(by_day),
        })

    @action(detail=False, methods=['get'], url_path='daily-report')
    def daily_report(self, request):
        """Rapport journalier détaillé."""
        organization = self.get_organization()
        date_str = request.query_params.get('date', timezone.now().date().isoformat())

        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )

        movements = base_qs.filter(
            movement_date__date=date_str,
        ).select_related(
            'payment_method', 'sale', 'expense', 'customer', 'supplier', 'created_by'
        ).order_by('movement_date')

        primary = self._primary_currency()

        # Ouverture / totaux / clôture PAR DEVISE.
        by_currency = self._report_by_currency(base_qs, movements, date_str)

        # Par type (ventilé par devise).
        by_type = movements.values(
            'currency', 'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('currency', 'direction', '-total')

        # Pagination des mouvements
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        all_movements = list(movements)
        total_movements = len(all_movements)

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_movements = all_movements[start_idx:end_idx]

        # Scalaires = devise principale (rétro-compat).
        primary_row = next(
            (r for r in by_currency if r['currency'] == primary),
            {'opening_balance': Decimal('0.00'), 'closing_balance': Decimal('0.00'),
             'total_in': Decimal('0.00'), 'total_out': Decimal('0.00'), 'net': Decimal('0.00')},
        )
        return Response({
            'date': date_str,
            'currency': primary,
            'opening_balance': primary_row['opening_balance'],
            'closing_balance': primary_row['closing_balance'],
            'total_in': primary_row['total_in'],
            'total_out': primary_row['total_out'],
            'net': primary_row['net'],
            'by_currency': by_currency,
            'by_type': list(by_type),
            'movements': {
                'results': CashMovementListSerializer(paginated_movements, many=True).data,
                'count': total_movements,
                'page': page,
                'page_size': page_size,
                'total_pages': (total_movements + page_size - 1) // page_size if page_size > 0 else 0,
            },
        })

    @action(detail=False, methods=['get'], url_path='monthly-report')
    def monthly_report(self, request):
        """Rapport mensuel avec totaux par jour."""
        organization = self.get_organization()
        year = int(request.query_params.get('year', timezone.now().year))
        month = int(request.query_params.get('month', timezone.now().month))

        primary = self._primary_currency()
        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        movements = base_qs.filter(
            movement_date__year=year,
            movement_date__month=month,
        )

        import datetime
        first_day = datetime.date(year, month, 1)

        # Ouverture / totaux / clôture PAR DEVISE.
        by_currency = self._report_by_currency(base_qs, movements, first_day)

        # Par jour (ventilé par devise).
        by_day = movements.annotate(
            day=TruncDate('movement_date')
        ).values('day', 'currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day', 'currency')

        # Par type (ventilé par devise)
        by_type = movements.values(
            'currency', 'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('currency', 'direction', '-total')

        # Par catégorie de dépense (ventilé par devise)
        expense_by_category = movements.filter(
            movement_type='expense',
            expense__isnull=False,
        ).values(
            'currency', 'expense__category__name', 'expense__category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Scalaires = devise principale (rétro-compat).
        primary_row = next(
            (r for r in by_currency if r['currency'] == primary),
            {'opening_balance': Decimal('0.00'), 'closing_balance': Decimal('0.00'),
             'total_in': Decimal('0.00'), 'total_out': Decimal('0.00'), 'net': Decimal('0.00')},
        )
        opening_balance = primary_row['opening_balance']
        closing_balance = primary_row['closing_balance']

        return Response({
            'year': year,
            'month': month,
            'currency': primary,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_in': primary_row['total_in'],
            'total_out': primary_row['total_out'],
            'net': primary_row['net'],
            'count': movements.count(),
            'by_currency': by_currency,
            'by_day': list(by_day),
            'by_type': list(by_type),
            'expense_by_category': list(expense_by_category),
        })

    @action(detail=False, methods=['get'], url_path='annual-report')
    def annual_report(self, request):
        """Rapport annuel avec totaux par mois."""
        organization = self.get_organization()
        year = int(request.query_params.get('year', timezone.now().year))

        primary = self._primary_currency()
        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        movements = base_qs.filter(movement_date__year=year)

        import datetime
        first_day = datetime.date(year, 1, 1)

        # Ouverture / totaux / clôture PAR DEVISE.
        by_currency = self._report_by_currency(base_qs, movements, first_day)

        # Par mois (ventilé par devise)
        by_month = movements.annotate(
            month=TruncMonth('movement_date')
        ).values('month', 'currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('month', 'currency')

        # Par type (ventilé par devise)
        by_type = movements.values(
            'currency', 'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('currency', 'direction', '-total')

        # Par catégorie de dépense (ventilé par devise)
        expense_by_category = movements.filter(
            movement_type='expense',
            expense__isnull=False,
        ).values(
            'currency', 'expense__category__name', 'expense__category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Scalaires = devise principale (rétro-compat).
        primary_row = next(
            (r for r in by_currency if r['currency'] == primary),
            {'opening_balance': Decimal('0.00'), 'closing_balance': Decimal('0.00'),
             'total_in': Decimal('0.00'), 'total_out': Decimal('0.00'), 'net': Decimal('0.00')},
        )
        return Response({
            'year': year,
            'currency': primary,
            'opening_balance': primary_row['opening_balance'],
            'closing_balance': primary_row['closing_balance'],
            'total_in': primary_row['total_in'],
            'total_out': primary_row['total_out'],
            'net': primary_row['net'],
            'count': movements.count(),
            'by_currency': by_currency,
            'by_month': list(by_month),
            'by_type': list(by_type),
            'expense_by_category': list(expense_by_category),
        })

    @action(detail=False, methods=['get'], url_path='custom-report')
    def custom_report(self, request):
        """Rapport personnalisé sur une période donnée."""
        organization = self.get_organization()
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        if not date_from or not date_to:
            return Response(
                {'detail': "Les paramètres date_from et date_to sont requis."},
                status=status.HTTP_400_BAD_REQUEST
            )

        primary = self._primary_currency()
        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        movements = base_qs.filter(
            movement_date__date__gte=date_from,
            movement_date__date__lte=date_to,
        )

        # Ouverture / totaux / clôture PAR DEVISE.
        by_currency = self._report_by_currency(base_qs, movements, date_from)

        # Par jour (ventilé par devise)
        by_day = movements.annotate(
            day=TruncDate('movement_date')
        ).values('day', 'currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day', 'currency')

        # Par type (ventilé par devise)
        by_type = movements.values(
            'currency', 'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('currency', 'direction', '-total')

        # Par catégorie de dépense (ventilé par devise)
        expense_by_category = movements.filter(
            movement_type='expense',
            expense__isnull=False,
        ).values(
            'currency', 'expense__category__name', 'expense__category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Scalaires = devise principale (rétro-compat).
        primary_row = next(
            (r for r in by_currency if r['currency'] == primary),
            {'opening_balance': Decimal('0.00'), 'closing_balance': Decimal('0.00'),
             'total_in': Decimal('0.00'), 'total_out': Decimal('0.00'), 'net': Decimal('0.00')},
        )
        return Response({
            'date_from': date_from,
            'date_to': date_to,
            'currency': primary,
            'opening_balance': primary_row['opening_balance'],
            'closing_balance': primary_row['closing_balance'],
            'total_in': primary_row['total_in'],
            'total_out': primary_row['total_out'],
            'net': primary_row['net'],
            'count': movements.count(),
            'by_currency': by_currency,
            'by_day': list(by_day),
            'by_type': list(by_type),
            'expense_by_category': list(expense_by_category),
        })
