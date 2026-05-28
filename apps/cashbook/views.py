"""
ViewSets pour le module Livre de Caisse.
"""
from decimal import Decimal
from django.db.models import Sum, Count, Q, F
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from apps.core.api_mixins import (
    TenantViewSetMixin,
    AuditMixin,
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
)
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    get_membership_for_request,
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
        # Annoter avec le nombre de mouvements et le total des entrées
        queryset = queryset.annotate(
            movement_count=Count(
                'cash_movements',
                filter=Q(cash_movements__is_cancelled=False, cash_movements__direction='in')
            ),
            total_amount=Sum(
                'cash_movements__amount',
                filter=Q(cash_movements__is_cancelled=False, cash_movements__direction='in'),
                default=Decimal('0.00')
            )
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
        # Annoter avec le nombre de dépenses et le total dépensé
        queryset = queryset.annotate(
            expense_count=Count(
                'expenses',
                filter=Q(expenses__status__in=['approved', 'paid'])
            ),
            total_spent=Sum(
                'expenses__amount',
                filter=Q(expenses__status__in=['approved', 'paid']),
                default=Decimal('0.00')
            )
        )
        return queryset


# =============================================================================
# EXPENSE VIEWSET
# =============================================================================

class ExpenseViewSet(
    WarehouseScopedQuerysetMixin,
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
    filterset_fields = ['status', 'category', 'is_recurring', 'warehouse']
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

        # Scope explicite : les dépenses sans warehouse (org-level) ne sont
        # visibles que pour les rôles owner/manager. Sans ce filtre, un
        # caissier scopé sur un warehouse verrait tous les frais généraux
        # (loyer, salaires, abonnements...) — inadapté pour un POS.
        # ``warehouse_scope_include_null=True`` laisse passer ces lignes en
        # base ; on les exclut ici pour les rôles non-managériaux.
        from apps.core.api_permissions import is_manager_or_above

        if not is_manager_or_above(self.request):
            queryset = queryset.exclude(warehouse__isnull=True)

        # Filtres de date
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(expense_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(expense_date__lte=date_to)

        return queryset

    def perform_create(self, serializer):
        # Vérifie le périmètre warehouse avant de générer la référence.
        self._assert_warehouse_on_save(serializer)
        from apps.core.utils import ReferenceGenerator
        organization = self.get_organization()
        reference = ReferenceGenerator.generate_expense_reference(organization)
        serializer.save(
            organization=organization,
            reference=reference,
            created_by=self.request.user,
        )

    def perform_update(self, serializer):
        expense = self.get_object()
        if expense.status not in ['draft', 'pending']:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Seules les dépenses en brouillon ou en attente peuvent être modifiées.")
        if 'warehouse' in serializer.validated_data:
            self._assert_warehouse_on_save(serializer)
        serializer.save()

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

        # Total
        totals = queryset.aggregate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        )

        # Par catégorie
        by_category = queryset.values(
            'category__name', 'category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Par mois
        by_month = queryset.annotate(
            month=TruncMonth('expense_date')
        ).values('month').annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('month')

        return Response({
            'total': totals['total'],
            'count': totals['count'],
            'by_category': list(by_category),
            'by_month': list(by_month),
        })

    def _create_cash_movement_for_expense(self, expense):
        """Crée un mouvement de caisse pour une dépense approuvée."""
        from apps.core.utils import ReferenceGenerator
        organization = expense.organization

        # Calculer le solde
        last_movement = CashMovement.objects.filter(
            organization=organization,
            is_cancelled=False,
        ).order_by('-movement_date', '-created_at').first()

        previous_balance = last_movement.balance_after if last_movement else Decimal('0.00')
        new_balance = previous_balance - expense.amount

        CashMovement.objects.create(
            organization=organization,
            reference=ReferenceGenerator.generate_cash_movement_reference(organization),
            direction='out',
            movement_type='expense',
            amount=expense.amount,
            description=f"Dépense: {expense.description}",
            payment_method=expense.payment_method,
            expense=expense,
            balance_after=new_balance,
            movement_date=timezone.now(),
            created_by=expense.created_by,
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
    filterset_fields = ['direction', 'movement_type', 'is_cancelled']
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
        """Restreint un queryset CashMovement au périmètre du membre courant."""
        membership = get_membership_for_request(self.request)
        if not membership:
            return queryset
        allowed_ids = accessible_warehouse_ids(membership)
        if allowed_ids is None:
            return queryset
        if not allowed_ids:
            # Aucun entrepôt assigné => aucun mouvement visible.
            return queryset.none()
        return queryset.filter(
            Q(sale__warehouse_id__in=allowed_ids)
            | Q(expense__warehouse_id__in=allowed_ids)
        )

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
        """Créer un mouvement de caisse manuel."""
        from apps.core.utils import ReferenceGenerator
        organization = self.get_organization()

        # Calculer le solde
        last_movement = CashMovement.objects.filter(
            organization=organization,
            is_cancelled=False,
        ).order_by('-movement_date', '-created_at').first()

        previous_balance = last_movement.balance_after if last_movement else Decimal('0.00')
        amount = serializer.validated_data['amount']
        direction = serializer.validated_data['direction']

        if direction == 'in':
            new_balance = previous_balance + amount
        else:
            new_balance = previous_balance - amount

        serializer.save(
            organization=organization,
            reference=ReferenceGenerator.generate_cash_movement_reference(organization),
            balance_after=new_balance,
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
        """Solde actuel de la caisse."""
        organization = self.get_organization()

        scoped_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )

        last_movement = scoped_qs.order_by('-movement_date', '-created_at').first()

        balance = last_movement.balance_after if last_movement else Decimal('0.00')

        # Totaux du jour
        today = timezone.now().date()
        today_movements = scoped_qs.filter(movement_date__date=today)

        today_in = today_movements.filter(direction='in').aggregate(
            total=Sum('amount', default=Decimal('0.00'))
        )['total']
        today_out = today_movements.filter(direction='out').aggregate(
            total=Sum('amount', default=Decimal('0.00'))
        )['total']

        return Response({
            'balance': balance,
            'today_in': today_in,
            'today_out': today_out,
            'today_net': today_in - today_out,
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

        # Totaux globaux
        totals = queryset.aggregate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        )
        totals['net'] = totals['total_in'] - totals['total_out']

        # Par type de mouvement
        by_type = queryset.values(
            'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Par jour
        by_day = queryset.annotate(
            day=TruncDate('movement_date')
        ).values('day').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day')

        return Response({
            **totals,
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

        # Solde d'ouverture (dernier mouvement avant cette date)
        opening_movement = base_qs.filter(
            movement_date__date__lt=date_str,
        ).order_by('-movement_date', '-created_at').first()

        opening_balance = opening_movement.balance_after if opening_movement else Decimal('0.00')

        # Totaux
        totals = movements.aggregate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
        )

        closing_balance = opening_balance + totals['total_in'] - totals['total_out']

        # Par type
        by_type = movements.values(
            'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('direction', '-total')

        # Pagination des mouvements
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        all_movements = list(movements)
        total_movements = len(all_movements)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_movements = all_movements[start_idx:end_idx]

        return Response({
            'date': date_str,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_in': totals['total_in'],
            'total_out': totals['total_out'],
            'net': totals['total_in'] - totals['total_out'],
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

        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        movements = base_qs.filter(
            movement_date__year=year,
            movement_date__month=month,
        )

        # Totaux globaux du mois
        totals = movements.aggregate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        )

        # Par jour
        by_day = movements.annotate(
            day=TruncDate('movement_date')
        ).values('day').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day')

        # Par type
        by_type = movements.values(
            'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('direction', '-total')

        # Par catégorie de dépense
        expense_by_category = movements.filter(
            movement_type='expense',
            expense__isnull=False,
        ).values(
            'expense__category__name', 'expense__category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Solde d'ouverture du mois
        import datetime
        first_day = datetime.date(year, month, 1)
        opening_movement = base_qs.filter(
            movement_date__date__lt=first_day,
        ).order_by('-movement_date', '-created_at').first()

        opening_balance = opening_movement.balance_after if opening_movement else Decimal('0.00')
        closing_balance = opening_balance + totals['total_in'] - totals['total_out']

        return Response({
            'year': year,
            'month': month,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_in': totals['total_in'],
            'total_out': totals['total_out'],
            'net': totals['total_in'] - totals['total_out'],
            'count': totals['count'],
            'by_day': list(by_day),
            'by_type': list(by_type),
            'expense_by_category': list(expense_by_category),
        })

    @action(detail=False, methods=['get'], url_path='annual-report')
    def annual_report(self, request):
        """Rapport annuel avec totaux par mois."""
        organization = self.get_organization()
        year = int(request.query_params.get('year', timezone.now().year))

        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        movements = base_qs.filter(movement_date__year=year)

        # Totaux globaux de l'année
        totals = movements.aggregate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        )

        # Par mois
        by_month = movements.annotate(
            month=TruncMonth('movement_date')
        ).values('month').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('month')

        # Par type
        by_type = movements.values(
            'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('direction', '-total')

        # Par catégorie de dépense
        expense_by_category = movements.filter(
            movement_type='expense',
            expense__isnull=False,
        ).values(
            'expense__category__name', 'expense__category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Solde d'ouverture de l'année
        import datetime
        first_day = datetime.date(year, 1, 1)
        opening_movement = base_qs.filter(
            movement_date__date__lt=first_day,
        ).order_by('-movement_date', '-created_at').first()

        opening_balance = opening_movement.balance_after if opening_movement else Decimal('0.00')
        closing_balance = opening_balance + totals['total_in'] - totals['total_out']

        return Response({
            'year': year,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_in': totals['total_in'],
            'total_out': totals['total_out'],
            'net': totals['total_in'] - totals['total_out'],
            'count': totals['count'],
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

        base_qs = self._scope_cash_movements_to_membership(
            CashMovement.objects.filter(organization=organization, is_cancelled=False)
        )
        movements = base_qs.filter(
            movement_date__date__gte=date_from,
            movement_date__date__lte=date_to,
        )

        # Totaux globaux
        totals = movements.aggregate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        )

        # Par jour
        by_day = movements.annotate(
            day=TruncDate('movement_date')
        ).values('day').annotate(
            total_in=Sum('amount', filter=Q(direction='in'), default=Decimal('0.00')),
            total_out=Sum('amount', filter=Q(direction='out'), default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('day')

        # Par type
        by_type = movements.values(
            'movement_type', 'direction'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('direction', '-total')

        # Par catégorie de dépense
        expense_by_category = movements.filter(
            movement_type='expense',
            expense__isnull=False,
        ).values(
            'expense__category__name', 'expense__category__color'
        ).annotate(
            total=Sum('amount', default=Decimal('0.00')),
            count=Count('id'),
        ).order_by('-total')

        # Solde d'ouverture
        opening_movement = base_qs.filter(
            movement_date__date__lt=date_from,
        ).order_by('-movement_date', '-created_at').first()

        opening_balance = opening_movement.balance_after if opening_movement else Decimal('0.00')
        closing_balance = opening_balance + totals['total_in'] - totals['total_out']

        return Response({
            'date_from': date_from,
            'date_to': date_to,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_in': totals['total_in'],
            'total_out': totals['total_out'],
            'net': totals['total_in'] - totals['total_out'],
            'count': totals['count'],
            'by_day': list(by_day),
            'by_type': list(by_type),
            'expense_by_category': list(expense_by_category),
        })
