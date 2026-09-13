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
    ExportResponseMixin,
    WarehouseAssertCreateMixin,
)
from apps.core.report_params import (
    format_day,
    month_label,
    parse_day,
    parse_month,
    parse_year,
)
from rest_framework.exceptions import ValidationError as DRFValidationError
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

    def create(self, request, *args, **kwargs):
        """
        Créer une dépense et rendre le DÉTAIL complet (avec `id` et `reference`).

        ┌──────────────────────────────────────────────────────────────────────┐
        │ SANS CET OVERRIDE, LE REÇU DE DÉPENSE SORTAIT SANS NUMÉRO.          │
        │                                                                      │
        │ DRF répond à un POST avec le serializer d'ÉCRITURE, et               │
        │ `ExpenseCreateSerializer` ne déclare ni `id`, ni `reference`, ni     │
        │ `category_name`, ni `payment_method_name`. Or le back-office bâtit   │
        │ son ticket thermique avec ces quatre-là : les quatre valaient        │
        │ `undefined`, le papier sortait sans numéro ni catégorie, et le       │
        │ fichier s'appelait `depense-undefined.pdf`. `createExpense` annonce  │
        │ pourtant `Promise<ApiResponse<Expense>>` - axios rend `any`, et      │
        │ TypeScript n'avait rien à dire.                                       │
        │                                                                      │
        │ Le chemin du JOURNAL rendait déjà `ExpenseDetailSerializer` : les    │
        │ deux surfaces reçoivent désormais le même corps, ce qui est la       │
        │ définition même de la parité de §5.5.                                 │
        └──────────────────────────────────────────────────────────────────────┘

        Calqué sur `SaleViewSet.create`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expense = self.perform_create(serializer)
        expense.refresh_from_db()
        return Response(
            ExpenseDetailSerializer(expense).data, status=status.HTTP_201_CREATED,
        )

    def perform_create(self, serializer):
        """
        Le corps vit dans `services.create_expense`, que le journal appelle aussi.

        Il REND l'instance : `create()` en a besoin pour répondre par la fiche
        détaillée, et le contrat de DRF ne l'interdit pas.
        """
        from .services import create_expense

        return create_expense(
            serializer,
            organization=self.get_organization(),
            user=self.request.user,
            request=self.request,
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
        from .services import resolve_currency_rate, _taux_du_client

        currency, exchange_rate = resolve_currency_rate(
            organization,
            serializer.validated_data.get('currency'),
            _taux_du_client(serializer),
        )
        serializer.save(currency=currency, exchange_rate=exchange_rate)

    # ┌──────────────────────────────────────────────────────────────────────┐
    # │ LES CINQ TRANSITIONS VIVENT DANS `cashbook.services`.               │
    # │                                                                      │
    # │ Elles étaient écrites ici, donc hors d'atteinte du journal : un      │
    # │ terminal ne pouvait ni approuver ni payer une dépense, et les y      │
    # │ rejouer aurait demandé de les réécrire. Le CONTRAT DE RÉPONSE ne     │
    # │ bouge pas - `{'detail': ...}` en 400, la fiche détaillée en 200 - le │
    # │ back-office y branchant déjà ses messages.                           │
    # └──────────────────────────────────────────────────────────────────────┘

    def _transition(self, fonction, *args, **kwargs):
        from .services import TransitionRefusee
        expense = self.get_object()
        try:
            fonction(expense, *args, **kwargs)
        except TransitionRefusee as exc:
            return Response(
                {'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        expense.refresh_from_db()
        return Response(ExpenseDetailSerializer(expense).data)

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Soumettre une dépense pour approbation."""
        from .services import submit_expense
        return self._transition(submit_expense, request.user)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuver une dépense et créer le mouvement de caisse."""
        from .services import approve_expense
        return self._transition(approve_expense, request.user)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejeter une dépense."""
        from .services import reject_expense
        return self._transition(
            reject_expense, request.data.get('reason', ''), request.user
        )

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        """
        Marquer une dépense comme payée.
        Si la dépense est en brouillon ou en attente, elle est automatiquement approuvée.
        """
        from .services import pay_expense
        return self._transition(
            pay_expense,
            request.user,
            payment_method_id=request.data.get('payment_method'),
            payment_reference=request.data.get('payment_reference', ''),
        )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annuler une dépense et son mouvement de caisse."""
        from .services import cancel_expense
        return self._transition(
            cancel_expense, request.user, request.data.get('reason', '')
        )

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


# =============================================================================
# CASH MOVEMENT VIEWSET
# =============================================================================

class CashMovementViewSet(ExportResponseMixin, TenantViewSetMixin,
                          viewsets.ModelViewSet):
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
        # Exporter, c'est LIRE. Sans cette ligne, `HasPermission` refuse la
        # route à TOUS les rôles, en silence.
        'export_report': 'cashbook.view',
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
        """Le corps vit dans `services.create_manual_cash_movement`, partagé avec le journal."""
        from .services import create_manual_cash_movement

        create_manual_cash_movement(
            serializer,
            organization=self.get_organization(),
            user=self.request.user,
        )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annuler un mouvement de caisse. Le corps vit dans `services`."""
        from .services import TransitionRefusee, cancel_cash_movement
        movement = self.get_object()
        try:
            cancel_cash_movement(movement, request.user, request.data.get('reason', ''))
        except TransitionRefusee as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
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
        today = timezone.localdate()
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

    # ------------------------------------------------------------------
    # Constructeurs partagés par les rapports et par leur EXPORT
    # ------------------------------------------------------------------
    #
    # Les quatre rapports (journalier, mensuel, annuel, personnalisé) ne
    # diffèrent que par leur fenêtre et leur pas de temps. Ce qui suit est
    # appelé par les actions paginées ET par `export_report`, pour que le
    # fichier ne puisse pas dire autre chose que l'écran.

    @staticmethod
    def _movements_by_day(movements):
        """Totaux par jour, ventilés par devise."""
        return movements.annotate(
            day=TruncDate('movement_date')
        ).values('day', 'currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day', 'currency')

    @staticmethod
    def _movements_by_month(movements):
        """Totaux par mois, ventilés par devise."""
        return movements.annotate(
            month=TruncMonth('movement_date')
        ).values('month', 'currency').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('month', 'currency')

    def _report_window(self, request, scope):
        """
        La fenêtre d'un rapport de caisse, et ce qu'elle recouvre.

        Rend `(libelle, base_qs, movements, borne)` : `base_qs` sert à calculer
        le solde d'ouverture (il porte TOUT l'historique), `movements` la
        période, et `borne` la date à laquelle l'ouverture est relevée.
        """
        import datetime

        organization = self.get_organization()
        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        p = request.query_params

        aujourdhui = timezone.localdate()

        if scope == 'daily':
            jour = parse_day(p['date']) if p.get('date') else aujourdhui
            return (
                f"Journée du {format_day(jour.isoformat())}",
                base_qs,
                base_qs.filter(movement_date__date=jour).select_related(
                    'payment_method', 'sale', 'expense', 'customer', 'supplier',
                    'created_by',
                ).order_by('movement_date'),
                jour,
            )

        if scope == 'monthly':
            annee = parse_year(p.get('year'), aujourdhui.year)
            mois = parse_month(p.get('month'), aujourdhui.month)
            premier = datetime.date(annee, mois, 1)
            return (
                month_label(f"{annee:04d}-{mois:02d}"),
                base_qs,
                base_qs.filter(
                    movement_date__year=annee, movement_date__month=mois
                ),
                premier,
            )

        if scope == 'annual':
            annee = parse_year(p.get('year'), aujourdhui.year)
            return (
                f"Année {annee}",
                base_qs,
                base_qs.filter(movement_date__year=annee),
                datetime.date(annee, 1, 1),
            )

        debut, fin = p.get('date_from'), p.get('date_to')
        if not debut or not fin:
            raise DRFValidationError(
                {'detail': "Les paramètres date_from et date_to sont requis."}
            )
        debut = parse_day(debut, champ='date_from')
        fin = parse_day(fin, champ='date_to')
        return (
            f"Du {format_day(debut.isoformat())} au {format_day(fin.isoformat())}",
            base_qs,
            base_qs.filter(
                movement_date__date__gte=debut, movement_date__date__lte=fin
            ),
            debut,
        )

    @action(detail=False, methods=['get'], url_path='export-report')
    def export_report(self, request):
        """
        Les quatre rapports de caisse, en PDF, classeur ou CSV.

        ┌──────────────────────────────────────────────────────────────────┐
        │ LE JOURNALIER PORTE LA JOURNÉE ENTIÈRE.                          │
        │                                                                  │
        │ Le document dessiné dans le navigateur ne mettait dans son        │
        │ tableau que `movements.results`, c'est-à-dire les vingt lignes    │
        │ paginées à l'écran, sous une synthèse qui annonçait tout le jour. │
        │ Ici la liste n'est pas paginée.                                   │
        └──────────────────────────────────────────────────────────────────┘
        """
        from apps.cashbook.reports import (
            SCOPE_BASENAMES,
            build_daily_cash_report,
            build_period_cash_report,
        )

        fmt = self.get_export_format(request)
        scope = (request.query_params.get('scope') or 'daily').strip()
        if scope not in SCOPE_BASENAMES:
            raise DRFValidationError({
                'scope': "Portée inconnue. Attendu : "
                         + ', '.join(sorted(SCOPE_BASENAMES)) + '.',
            })

        organization = self.get_organization()
        libelle, base_qs, movements, borne = self._report_window(request, scope)
        by_currency = self._report_by_currency(base_qs, movements, borne)
        primary = self._primary_currency()

        if scope == 'daily':
            spec = build_daily_cash_report(
                organization, libelle=libelle, movements=movements,
                by_currency=by_currency, currency=primary,
            )
        else:
            par_mois = scope == 'annual'
            buckets = (
                self._movements_by_month(movements) if par_mois
                else self._movements_by_day(movements)
            )
            spec = build_period_cash_report(
                organization, scope=scope, libelle=libelle, buckets=buckets,
                by_currency=by_currency, currency=primary, par_mois=par_mois,
            )

        return self.render_export(spec, SCOPE_BASENAMES[scope], fmt)

    @action(detail=False, methods=['get'], url_path='daily-report')
    def daily_report(self, request):
        """Rapport journalier détaillé."""
        organization = self.get_organization()
        date_str = request.query_params.get('date', timezone.localdate().isoformat())

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

        by_day = self._movements_by_day(movements)

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

        by_month = self._movements_by_month(movements)

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

        by_day = self._movements_by_day(movements)

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
