from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db.models import (
    Sum,
    Count,
    Avg,
    F,
    Q,
    DecimalField,
    ExpressionWrapper,
    Case,
    When,
    Value,
    DateField,
)
from django.db.models.functions import (
    Cast, Coalesce, TruncDate, TruncWeek, TruncMonth, TruncHour,
)
from django.utils import timezone
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from apps.core.mixins import TenantQuerysetMixin
from apps.core.api_mixins import ActionPaginationMixin
from apps.core.api_permissions import IsTenantMember, HasActiveSubscription, HasPermission
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    get_membership_for_request,
)
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Sale, SaleItem, Payment
from apps.sales.profit_allocation import allocated_line_ht_revenues_for_sale, effective_unit_cost
from apps.products.models import Product
from apps.inventory.models import Stock, StockBatch
from apps.inventory.packaging import PackagingProfile, PackagingService
from apps.contacts.models import Customer
from apps.settings.services import CurrencyService
from apps.cashbook.models import CashMovement, Expense
# Agrégations comptables : les montants de caisse/dépenses sont convertis en
# devise principale (montant × exchange_rate) avant d'être sommés. Le livre de
# caisse, lui, reste ventilé par devise - voir apps/cashbook/views.py.
from apps.cashbook.services import (
    balance_in_primary,
    last_balance_by_currency,
    primary_sum,
)

from .models import ReportTemplate, SavedReport, Dashboard
from .serializers import (
    ReportTemplateSerializer,
    SavedReportSerializer,
    DashboardSerializer,
    SalesStatsSerializer,
    SalesByPeriodSerializer,
    TopProductSerializer,
    TopCustomerSerializer,
    StockStatsSerializer,
    CashbookStatsSerializer,
    CashFlowByPeriodSerializer,
    CustomerStatsSerializer,
    SalesByCategorySerializer,
    SalesByPaymentMethodSerializer,
    DashboardSummarySerializer,
    DailyCashReportSerializer,
    DailyCashMovementSerializer,
    ProfitMarginSerializer,
    ProductProfitSerializer,
    StockDetailSerializer,
    StockMovementSummarySerializer,
)


class ReportTemplateViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """ViewSet pour les modèles de rapports"""
    
    queryset = ReportTemplate.objects.all()
    serializer_class = ReportTemplateSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': 'reports.view',
        'retrieve': 'reports.view',
        'create': 'reports.create',
        'update': 'reports.create',
        'destroy': 'reports.delete',
    }
    
    def perform_create(self, serializer):
        serializer.save(
            organization=self.get_organization(),
            created_by=self.request.user
        )


class DashboardViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """ViewSet pour les dashboards personnalisés"""
    
    queryset = Dashboard.objects.all()
    serializer_class = DashboardSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': 'reports.view',
        'retrieve': 'reports.view',
        'create': 'reports.create',
        'update': 'reports.create',
        'destroy': 'reports.delete',
    }
    
    def perform_create(self, serializer):
        serializer.save(
            organization=self.get_organization(),
            created_by=self.request.user
        )


class StatisticsViewSet(ActionPaginationMixin, TenantQuerysetMixin, viewsets.ViewSet):
    """
    ViewSet pour les statistiques et rapports en temps réel.
    Fournit des endpoints pour différentes métriques de l'entreprise.
    """
    
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        # Le résumé alimente la page d'accueil (dashboard) de tous les rôles :
        # accessible via `dashboard.view`, les données restant scopées par rôle
        # (entrepôt pour gérant/magasinier, propres données pour le caissier).
        'summary': 'dashboard.view',
        'sales': 'reports.view',
        'sales_by_period': 'reports.view',
        'sales_by_category': 'reports.view',
        'sales_by_payment_method': 'reports.view',
        'top_products': 'reports.view',
        'top_customers': 'reports.view',
        'stock': 'reports.view',
        'stock_details': 'reports.view',
        'stock_movements_summary': 'reports.view',
        # Sans cette entrée, `HasPermission` refuse l'action à TOUS les rôles
        # (« action non listée = accès refusé »), alors que le frontend l'appelle.
        'product_supplies': 'reports.view',
        'cashbook': 'reports.view',
        'cash_flow': 'reports.view',
        'daily_cash_report': 'reports.view',
        'customers': 'reports.view',
        'receivables': 'reports.view',
        'profit_margins': 'reports.view',
        'product_profits': 'reports.view',
        'sales_by_packaging': 'reports.view',
        'user_activity': 'reports.view',
    }
    
    def _accessible_warehouse_ids(self, request):
        """Renvoie ``None`` (owner = tout) ou la liste des UUID autorisés.

        Utilisé pour scoper toutes les statistiques au périmètre du membre.
        """
        membership = get_membership_for_request(request)
        if not membership:
            return None
        return accessible_warehouse_ids(membership)

    def _is_cashier(self, request):
        """True si le membre courant est un caissier (visibilité = ses données)."""
        membership = get_membership_for_request(request)
        return (
            membership is not None
            and membership.role == OrganizationMembership.Role.CASHIER
        )

    def _apply_creator_scope(self, qs, request, creator_field):
        """Restreint aux enregistrements créés par l'utilisateur si caissier.

        Garantit que les KPIs/rapports d'un caissier n'agrègent que ses propres
        données (ventes, dépenses, mouvements), conformément à la visibilité
        par rôle. Sans effet pour owner/gérant/magasinier.
        """
        if self._is_cashier(request):
            return qs.filter(**{creator_field: request.user})
        return qs

    def _scope_sales(self, qs, request):
        """Applique le filtre warehouse aux ventes (+ créateur si caissier)."""
        qs = self._apply_creator_scope(qs, request, 'sold_by')
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.filter(warehouse__isnull=True)
        return qs.filter(Q(warehouse_id__in=wh_ids) | Q(warehouse__isnull=True))

    def _scope_sale_items(self, qs, request):
        """Applique le filtre warehouse aux items de vente via ``sale``."""
        qs = self._apply_creator_scope(qs, request, 'sale__sold_by')
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.filter(sale__warehouse__isnull=True)
        return qs.filter(
            Q(sale__warehouse_id__in=wh_ids) | Q(sale__warehouse__isnull=True)
        )

    def _scope_payments(self, qs, request):
        """Applique le filtre warehouse aux paiements via ``sale``."""
        qs = self._apply_creator_scope(qs, request, 'sale__sold_by')
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.filter(sale__warehouse__isnull=True)
        return qs.filter(
            Q(sale__warehouse_id__in=wh_ids) | Q(sale__warehouse__isnull=True)
        )

    def _scope_cash_movements(self, qs, request):
        """Applique le filtre warehouse aux mouvements de caisse.

        Tolère les mouvements sans vente/dépense liée (apports, retraits) afin
        de ne pas masquer les opérations générales aux non-owner. Pour un
        caissier, restreint aux mouvements qu'il a créés.
        """
        if self._is_cashier(request):
            return qs.filter(created_by=request.user)
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.filter(sale__isnull=True, expense__isnull=True)
        return qs.filter(
            Q(sale__warehouse_id__in=wh_ids)
            | Q(expense__warehouse_id__in=wh_ids)
            | Q(session__register__warehouse_id__in=wh_ids)
            | Q(sale__isnull=True, expense__isnull=True)
            | Q(sale__warehouse__isnull=True, expense__isnull=True)
            | Q(sale__isnull=True, expense__warehouse__isnull=True)
        )

    def _scope_expenses(self, qs, request):
        """Applique le filtre warehouse aux dépenses (+ créateur si caissier)."""
        if self._is_cashier(request):
            return qs.filter(created_by=request.user)
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.filter(warehouse__isnull=True)
        return qs.filter(
            Q(warehouse_id__in=wh_ids) | Q(warehouse__isnull=True)
        )

    def _scope_stocks(self, qs, request):
        """Applique le filtre warehouse aux stocks (warehouse strict)."""
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.none()
        return qs.filter(warehouse_id__in=wh_ids)

    def _scope_stock_batches(self, qs, request):
        """Applique le filtre warehouse aux lots de stock (warehouse strict)."""
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.none()
        return qs.filter(warehouse_id__in=wh_ids)

    def _scope_stock_movements(self, qs, request):
        """Applique le filtre warehouse aux mouvements de stock."""
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            return qs
        if not wh_ids:
            return qs.none()
        return qs.filter(warehouse_id__in=wh_ids)

    def _parse_date_range(self, request):
        """Parse les paramètres de période"""
        period = request.query_params.get('period', 'month')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        
        today = timezone.now().date()
        
        if date_from and date_to:
            from datetime import datetime

            # Une date malformée est une erreur de l'appelant, pas du serveur :
            # sans ce garde-fou, `strptime` remonte en 500 et masque la cause.
            try:
                start_date = datetime.strptime(date_from, '%Y-%m-%d').date()
                end_date = datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                raise DRFValidationError({
                    'date_from': "Dates attendues au format AAAA-MM-JJ.",
                })
            if start_date > end_date:
                raise DRFValidationError({
                    'date_from': "La date de début doit précéder la date de fin.",
                })
        elif period == 'today':
            start_date = today
            end_date = today
        elif period == 'week':
            start_date = today - timedelta(days=today.weekday())
            end_date = today
        elif period == 'month':
            start_date = today.replace(day=1)
            end_date = today
        elif period == 'quarter':
            quarter = (today.month - 1) // 3
            start_date = today.replace(month=quarter * 3 + 1, day=1)
            end_date = today
        elif period == 'year':
            start_date = today.replace(month=1, day=1)
            end_date = today
        else:
            start_date = today - timedelta(days=30)
            end_date = today
        
        # Période précédente pour comparaison
        period_length = (end_date - start_date).days + 1
        prev_end_date = start_date - timedelta(days=1)
        prev_start_date = prev_end_date - timedelta(days=period_length - 1)
        
        return start_date, end_date, prev_start_date, prev_end_date

    @action(detail=False, methods=['get'])
    def user_activity(self, request):
        """Rapport d'activité d'un utilisateur sur une période (avec heures).

        Params : ``user`` (id, requis), ``date_from``/``date_to`` ou ``period``,
        ``group_by=hour|day`` (défaut ``day``). Réservé à ``reports.view``
        (owner + gérant) ; les données restent scopées au périmètre du
        demandeur (un gérant ne voit que l'activité dans ses entrepôts).
        """
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')

        target_id = request.query_params.get('user')
        if not target_id:
            return Response(
                {'user': "Le paramètre 'user' est requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership = OrganizationMembership.objects.filter(
            organization=org, user_id=target_id
        ).select_related('user').first()
        if not membership:
            return Response(
                {'user': "Utilisateur introuvable dans cette organisation."},
                status=status.HTTP_404_NOT_FOUND,
            )
        target_user = membership.user

        money = dict(output_field=DecimalField())

        # --- Ventes de l'utilisateur (scopées au périmètre du demandeur) ---
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sold_by=target_user,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
            ),
            request,
        )
        completed_sales = sales.filter(status__in=['completed', 'partially_paid'])
        sales_agg = completed_sales.aggregate(
            count=Count('id'),
            total=Coalesce(Sum('total'), Decimal('0'), **money),
        )

        # Ventilation par méthode de paiement
        payments = self._scope_payments(
            Payment.objects.filter(
                organization=org,
                sale__sold_by=target_user,
                status='completed',
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
            ),
            request,
        ).select_related('payment_method')
        by_method = {}
        for p in payments:
            name = p.payment_method.name if p.payment_method else 'Autre'
            by_method[name] = by_method.get(name, Decimal('0')) + p.amount

        # --- Dépenses créées par l'utilisateur ---
        expenses = self._scope_expenses(
            Expense.objects.filter(
                organization=org,
                created_by=target_user,
                expense_date__gte=start_date,
                expense_date__lte=end_date,
            ),
            request,
        )
        # Converti en devise principale : une dépense peut être en USD dans une
        # org CDF, on ne somme jamais des montants bruts de devises différentes.
        expenses_agg = expenses.aggregate(
            count=Count('id'),
            total=primary_sum('amount'),
        )

        # --- Mouvements de caisse créés par l'utilisateur ---
        movements = self._scope_cash_movements(
            CashMovement.objects.filter(
                organization=org,
                created_by=target_user,
                is_cancelled=False,
                movement_date__date__gte=start_date,
                movement_date__date__lte=end_date,
            ),
            request,
        )
        cash_in = movements.filter(direction='in').aggregate(
            total=primary_sum('amount'))['total']
        cash_out = movements.filter(direction='out').aggregate(
            total=primary_sum('amount'))['total']

        # --- Ventilation temporelle des ventes (heure ou jour) ---
        trunc = TruncHour('sale_date') if group_by == 'hour' else TruncDate('sale_date')
        breakdown_qs = (
            completed_sales.annotate(bucket=trunc)
            .values('bucket')
            .annotate(
                count=Count('id'),
                total=Coalesce(Sum('total'), Decimal('0'), **money),
            )
            .order_by('bucket')
        )
        fmt = '%Y-%m-%d %H:00' if group_by == 'hour' else '%Y-%m-%d'
        breakdown = [
            {
                'bucket': row['bucket'].strftime(fmt) if row['bucket'] else '',
                'count': row['count'],
                'total': row['total'],
            }
            for row in breakdown_qs
        ]

        return Response({
            'user': {
                'id': str(target_user.id),
                'name': f"{target_user.first_name} {target_user.last_name}".strip() or target_user.email,
                'email': target_user.email,
                'role': membership.role,
            },
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
                'group_by': group_by,
            },
            'sales': {
                'count': sales_agg['count'],
                'total': sales_agg['total'],
                'by_payment_method': [
                    {'method': k, 'total': v} for k, v in by_method.items()
                ],
            },
            'expenses': {
                'count': expenses_agg['count'],
                'total': expenses_agg['total'],
            },
            'cash': {
                'cash_in': cash_in,
                'cash_out': cash_out,
                'net': cash_in - cash_out,
            },
            'breakdown': breakdown,
        })

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Résumé global pour le dashboard principal"""
        org = self.get_organization()
        start_date, end_date, prev_start, prev_end = self._parse_date_range(request)
        
        sales_stats = self._get_sales_stats(org, start_date, end_date, prev_start, prev_end, request=request)
        stock_stats = self._get_stock_stats(org, request=request)
        cashbook_stats = self._get_cashbook_stats(org, start_date, end_date, request=request)
        customer_stats = self._get_customer_stats(org, start_date, end_date)
        
        data = {
            'sales': sales_stats,
            'stock': stock_stats,
            'cashbook': cashbook_stats,
            'customers': customer_stats,
        }
        
        serializer = DashboardSummarySerializer(data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales(self, request):
        """Statistiques détaillées des ventes"""
        org = self.get_organization()
        start_date, end_date, prev_start, prev_end = self._parse_date_range(request)
        
        stats = self._get_sales_stats(org, start_date, end_date, prev_start, prev_end, request=request)
        serializer = SalesStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales_by_period(self, request):
        """Ventes groupées par période (jour/semaine/mois) avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid']
            ),
            request,
        )
        
        if group_by == 'week':
            trunc_func = TruncWeek('sale_date')
        elif group_by == 'month':
            trunc_func = TruncMonth('sale_date')
        else:
            trunc_func = TruncDate('sale_date')
        
        data = sales.annotate(
            period=trunc_func
        ).values('period').annotate(
            total=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField()),
            count=Count('id')
        ).order_by('period')
        
        all_data = list(data)
        total_count = len(all_data)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'period': item['period'].strftime('%Y-%m-%d') if item['period'] else '',
                'total': item['total'],
                'count': item['count']
            }
            for item in paginated_data
        ]
        
        serializer = SalesByPeriodSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def sales_by_category(self, request):
        """Ventes par catégorie de produit avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        items = self._scope_sale_items(
            SaleItem.objects.filter(
                sale__organization=org,
                sale__sale_date__date__gte=start_date,
                sale__sale_date__date__lte=end_date,
                sale__status__in=['completed', 'partially_paid']
            ),
            request,
        ).select_related('product__category')
        
        data = items.values(
            'product__category__id',
            'product__category__name'
        ).annotate(
            total_revenue=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField()),
            quantity_sold=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField())
        ).order_by('-total_revenue')
        
        # Calculer le total pour les pourcentages
        all_data = list(data)
        total_revenue = sum(item['total_revenue'] for item in all_data)
        total_count = len(all_data)
        
        # Pagination
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'category_id': item['product__category__id'],
                'category_name': item['product__category__name'] or 'Sans catégorie',
                'total_revenue': item['total_revenue'],
                'quantity_sold': item['quantity_sold'],
                'percentage': round((item['total_revenue'] / total_revenue * 100) if total_revenue > 0 else 0, 2)
            }
            for item in paginated_data
        ]
        
        serializer = SalesByCategorySerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def sales_by_payment_method(self, request):
        """Ventes par méthode de paiement avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        payments = self._scope_payments(
            Payment.objects.filter(
                sale__organization=org,
                paid_at__date__gte=start_date,
                paid_at__date__lte=end_date
            ),
            request,
        ).select_related('payment_method')
        
        data = payments.values(
            'payment_method__id',
            'payment_method__name'
        ).annotate(
            total=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()),
            count=Count('id')
        ).order_by('-total')
        
        all_data = list(data)
        total_amount = sum(item['total'] for item in all_data)
        total_count = len(all_data)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'payment_method': str(item['payment_method__id']) if item['payment_method__id'] else 'unknown',
                'payment_method_name': item['payment_method__name'] or 'Inconnu',
                'total': item['total'],
                'count': item['count'],
                'percentage': round((item['total'] / total_amount * 100) if total_amount > 0 else 0, 2)
            }
            for item in paginated_data
        ]
        
        serializer = SalesByPaymentMethodSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @staticmethod
    def _top_product_row(item):
        """
        Traduit une ligne agrégée de ventes en quantité lisible.

        L'agrégation travaille sur ``values()`` : il n'y a donc pas de ``Product``
        sous la main. On en reconstitue le strict nécessaire, un objet léger dont
        ``PackagingService`` ne lit que ``selling_mode``, ``units_per_package``
        et le nom des deux unités.
        """
        packages = Decimal(item.get('packages_sold') or 0)
        packaged_units = Decimal(item.get('packaged_units') or 0)
        quantity = Decimal(item['quantity_sold'] or 0)

        stand_in = PackagingProfile.from_values(item)
        factor = PackagingService.factor(stand_in)

        if factor is None or packages <= 0:
            display = PackagingService.format_quantity(stand_in, quantity)
            packages_out = None if factor is None else Decimal('0')
            loose_out = None if factor is None else quantity
        else:
            # Le vrac ne peut pas être négatif : une ligne dont la part
            # conditionnée dépasse le total signale un facteur modifié après
            # coup, on retombe alors sur la lecture au total.
            loose_out = max(Decimal('0'), quantity - packaged_units)
            display = PackagingService.format_split(stand_in, packages, loose_out)
            packages_out = packages

        return {
            'product_id': item['product__id'],
            'product_name': item['product__name'],
            'product_sku': item['product__sku'],
            'quantity_sold': quantity,
            'quantity_display': display,
            'packages_sold': packages_out,
            'loose_sold': loose_out,
            'packaging_factor': factor,
            'total_revenue': item['total_revenue'],
        }

    @action(detail=False, methods=['get'])
    def top_products(self, request):
        """Top produits les plus vendus avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        items = self._scope_sale_items(
            SaleItem.objects.filter(
                sale__organization=org,
                sale__sale_date__date__gte=start_date,
                sale__sale_date__date__lte=end_date,
                sale__status__in=['completed', 'partially_paid']
            ),
            request,
        ).select_related('product')
        
        # `packages_sold` additionne les contenants réellement facturés et
        # `packaged_units` ce qu'ils pèsent en unités de détail : la part vraiment
        # vendue à la pièce est le reste. Redécouper `quantity_sold` au facteur
        # d'aujourd'hui donnerait « 10 casiers » là où le marchand a vendu
        # 5 casiers et 120 bouteilles, et le facteur a pu changer depuis.
        data = items.values(
            'product__id',
            'product__name',
            'product__sku',
            'product__selling_mode',
            'product__units_per_package',
            'product__unit__name',
            'product__packaging_unit__name',
        ).annotate(
            quantity_sold=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField()),
            packages_sold=Coalesce(
                Sum('package_quantity'), Decimal('0'), output_field=DecimalField()
            ),
            packaged_units=Coalesce(
                Sum(F('package_quantity') * F('packaging_factor')),
                Decimal('0'), output_field=DecimalField(),
            ),
            total_revenue=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField())
        ).order_by('-quantity_sold')
        
        # Pagination
        total_count = data.count()
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = data[start_idx:end_idx]
        
        result = [self._top_product_row(item) for item in paginated_data]
        
        serializer = TopProductSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def top_customers(self, request):
        """Meilleurs clients avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid'],
                customer__isnull=False
            ),
            request,
        ).select_related('customer')
        
        # Converti en devise principale : un `Sum('total')` brut classait un
        # client à 50 USD derrière un client à 40 000 CDF en comparant des
        # nombres nus. `current_balance` est déjà exprimé en principale.
        from apps.cashbook.services import primary_sum

        data = sales.values(
            'customer__id',
            'customer__name',
            'customer__current_balance'
        ).annotate(
            total_purchases=primary_sum('total'),
            order_count=Count('id')
        ).order_by('-total_purchases')
        
        total_count = data.count()
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = data[start_idx:end_idx]
        
        result = [
            {
                'customer_id': item['customer__id'],
                'customer_name': item['customer__name'],
                'total_purchases': item['total_purchases'],
                'order_count': item['order_count'],
                'current_balance': item['customer__current_balance'] or Decimal('0')
            }
            for item in paginated_data
        ]
        
        serializer = TopCustomerSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def stock(self, request):
        """Statistiques du stock"""
        org = self.get_organization()
        stats = self._get_stock_stats(org, request=request)
        serializer = StockStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def cashbook(self, request):
        """Statistiques de la caisse"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        stats = self._get_cashbook_stats(org, start_date, end_date, request=request)
        serializer = CashbookStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def cash_flow(self, request):
        """Flux de trésorerie par période avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        movements = self._scope_cash_movements(
            CashMovement.objects.filter(
                organization=org,
                movement_date__date__gte=start_date,
                movement_date__date__lte=end_date
            ),
            request,
        )
        
        if group_by == 'week':
            trunc_func = TruncWeek('movement_date')
        elif group_by == 'month':
            trunc_func = TruncMonth('movement_date')
        else:
            trunc_func = TruncDate('movement_date')
        
        # Montants convertis en devise principale : un flux de trésorerie est un
        # chiffre comptable unique, il ne peut pas mélanger USD et CDF bruts.
        data = movements.annotate(
            period=trunc_func
        ).values('period').annotate(
            income=primary_sum('amount', filter=Q(direction='in')),
            expenses=primary_sum('amount', filter=Q(direction='out')),
        ).order_by('period')
        
        all_data = list(data)
        total_count = len(all_data)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'period': item['period'].strftime('%Y-%m-%d') if item['period'] else '',
                'income': item['income'],
                'expenses': item['expenses'],
                'net': item['income'] - item['expenses']
            }
            for item in paginated_data
        ]
        
        serializer = CashFlowByPeriodSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def customers(self, request):
        """Statistiques des clients"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        stats = self._get_customer_stats(org, start_date, end_date)
        serializer = CustomerStatsSerializer(stats)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def receivables(self, request):
        """
        Balance âgée des créances clients, ventilée par devise.

        Le seul chiffre existant jusqu'ici (`total_receivables`) était la somme
        des `current_balance`, un agrégat converti en devise principale : utile
        pour un total, muet sur l'ancienneté et sur la devise réellement due.
        Un marchand qui doit relancer a besoin de savoir QUI est en retard, de
        COMBIEN et DEPUIS QUAND.

        Tranches calculées depuis `due_date`, avec repli sur `sale_date` quand
        aucune échéance n'a été fixée : une facture sans échéance vieillit
        quand même. Les montants ne sont jamais additionnés entre devises ;
        `total_primary` fournit à part la conversion en devise principale.
        """
        from apps.cashbook.services import primary_sum

        org = self.get_organization()
        today = timezone.now().date()

        open_invoices = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                status__in=[Sale.Status.PENDING, Sale.Status.PARTIALLY_PAID],
                amount_due__gt=0,
                customer__isnull=False,
            ),
            request,
        ).select_related('customer')

        # Ancienneté : échéance si elle existe, sinon date de vente.
        reference_date = Coalesce('due_date', Cast('sale_date', DateField()))
        open_invoices = open_invoices.annotate(aging_date=reference_date)

        buckets = [
            ('current', None, 0),
            ('d1_30', 1, 30),
            ('d31_60', 31, 60),
            ('d61_90', 61, 90),
            ('d90_plus', 91, None),
        ]

        def bucket_for(days_late):
            if days_late <= 0:
                return 'current'
            if days_late <= 30:
                return 'd1_30'
            if days_late <= 60:
                return 'd31_60'
            if days_late <= 90:
                return 'd61_90'
            return 'd90_plus'

        by_currency = {}
        by_customer = {}

        for sale in open_invoices:
            days_late = (today - sale.aging_date).days
            slot = bucket_for(days_late)

            row = by_currency.setdefault(
                sale.currency,
                {'currency': sale.currency, 'total': Decimal('0.00'),
                 **{name: Decimal('0.00') for name, _, _ in buckets}},
            )
            row[slot] += sale.amount_due
            row['total'] += sale.amount_due

            key = (sale.customer_id, sale.currency)
            entry = by_customer.setdefault(key, {
                'customer_id': str(sale.customer_id),
                'customer_name': sale.customer.name,
                'currency': sale.currency,
                'amount_due': Decimal('0.00'),
                'invoice_count': 0,
                'oldest_days': 0,
                'overdue_amount': Decimal('0.00'),
            })
            entry['amount_due'] += sale.amount_due
            entry['invoice_count'] += 1
            entry['oldest_days'] = max(entry['oldest_days'], days_late)
            if days_late > 0:
                entry['overdue_amount'] += sale.amount_due

        total_primary = open_invoices.aggregate(
            total=primary_sum('amount_due')
        )['total'] or Decimal('0.00')

        overdue_primary = open_invoices.filter(
            aging_date__lt=today
        ).aggregate(total=primary_sum('amount_due'))['total'] or Decimal('0.00')

        debtors = sorted(
            by_customer.values(), key=lambda e: e['amount_due'], reverse=True,
        )

        return Response({
            'as_of': str(today),
            'buckets': [name for name, _, _ in buckets],
            'by_currency': sorted(by_currency.values(), key=lambda r: r['currency']),
            'by_customer': debtors,
            'invoice_count': open_invoices.count(),
            'debtor_count': len({cid for cid, _ in by_customer}),
            # Seuls chiffres convertis, et clairement nommés comme tels.
            'total_primary': total_primary,
            'overdue_primary': overdue_primary,
            'primary_currency': CurrencyService.primary_code(org),
        })
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def _get_sales_stats(self, org, start_date, end_date, prev_start, prev_end, request=None):
        """Calcule les statistiques des ventes (filtrées par périmètre warehouse)."""
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=start_date,
            sale_date__date__lte=end_date
        )
        if request is not None:
            sales = self._scope_sales(sales, request)
        
        completed_sales = sales.filter(status__in=['completed', 'partially_paid'])
        
        total_sales = completed_sales.aggregate(
            total=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        total_orders = completed_sales.count()
        
        avg_order = completed_sales.aggregate(
            avg=Coalesce(Avg('total'), Decimal('0'))
        )['avg']
        
        total_items = SaleItem.objects.filter(
            sale__in=completed_sales
        ).aggregate(
            total=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        # Période précédente
        prev_sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=prev_start,
            sale_date__date__lte=prev_end,
            status__in=['completed', 'partially_paid']
        )
        if request is not None:
            prev_sales = self._scope_sales(prev_sales, request)
        
        prev_total = prev_sales.aggregate(
            total=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        prev_orders = prev_sales.count()
        
        # Calcul de la croissance
        sales_growth = None
        if prev_total > 0:
            sales_growth = round(((total_sales - prev_total) / prev_total) * 100, 2)
        
        orders_growth = None
        if prev_orders > 0:
            orders_growth = round(((total_orders - prev_orders) / prev_orders) * 100, 2)
        
        return {
            'total_sales': total_sales,
            'total_orders': total_orders,
            'average_order_value': round(avg_order, 2),
            'total_items_sold': total_items,
            'completed_sales': sales.filter(status='completed').count(),
            'pending_sales': sales.filter(status__in=['pending', 'partially_paid']).count(),
            'cancelled_sales': sales.filter(status='cancelled').count(),
            'sales_growth': sales_growth,
            'orders_growth': orders_growth,
        }
    
    def _get_stock_stats(self, org, request=None):
        """Calcule les statistiques du stock (filtrées par périmètre warehouse)."""
        products = Product.objects.filter(organization=org, is_active=True)
        
        stocks = Stock.objects.filter(
            organization=org,
            product__is_active=True
        )
        if request is not None:
            stocks = self._scope_stocks(stocks, request)
        
        # ``total_products`` doit refléter ce qui est visible : on compte les
        # produits qui ont au moins une position de stock dans le périmètre.
        if request is not None and self._accessible_warehouse_ids(request) is not None:
            scoped_product_ids = stocks.values_list('product_id', flat=True).distinct()
            total_products = products.filter(id__in=scoped_product_ids).count()
        else:
            total_products = products.count()
        
        # Valeur totale du stock
        total_value = Decimal('0')
        low_stock = 0
        out_of_stock = 0
        
        for stock in stocks.select_related('product'):
            qty = Decimal(str(stock.quantity))
            cost = stock.product.cost_price or Decimal('0')
            total_value += qty * cost
            
            if qty <= 0:
                out_of_stock += 1
            elif stock.product.min_stock_level and qty <= stock.product.min_stock_level:
                low_stock += 1
        
        # Lots expirant bientôt (30 jours)
        expiring_date = timezone.now().date() + timedelta(days=30)
        batches = StockBatch.objects.filter(
            organization=org,
            expiry_date__lte=expiring_date,
            expiry_date__gte=timezone.now().date(),
            quantity__gt=0
        )
        if request is not None:
            batches = self._scope_stock_batches(batches, request)
        expiring_count = batches.count()
        
        return {
            'total_products': total_products,
            'total_stock_value': total_value,
            'low_stock_count': low_stock,
            'out_of_stock_count': out_of_stock,
            'expiring_soon_count': expiring_count,
        }
    
    def _get_cashbook_stats(self, org, start_date, end_date, request=None):
        """Calcule les statistiques de la caisse (filtrées par périmètre warehouse)."""
        movements = CashMovement.objects.filter(
            organization=org,
            movement_date__date__gte=start_date,
            movement_date__date__lte=end_date
        )
        if request is not None:
            movements = self._scope_cash_movements(movements, request)
        
        # Convertis en devise principale (montant × taux) : sans cela, une
        # dépense de 10 USD comptait pour 10 CDF.
        totals = movements.aggregate(
            income=primary_sum('amount', filter=Q(direction='in')),
            expenses=primary_sum('amount', filter=Q(direction='out')),
        )

        # Solde actuel : le tiroir est suivi PAR DEVISE, donc prendre le dernier
        # mouvement toutes devises confondues renvoyait le solde de la devise qui
        # a bougé en dernier. On somme le dernier solde de CHAQUE devise, converti.
        last_movement_qs = CashMovement.objects.filter(
            organization=org, is_cancelled=False
        )
        if request is not None:
            last_movement_qs = self._scope_cash_movements(last_movement_qs, request)

        balances = last_balance_by_currency(last_movement_qs)
        current_balance = sum(
            (bal * rate for bal, rate in balances.values()), Decimal('0')
        )
        balance_by_currency = [
            {'currency': ccy, 'balance': bal}
            for ccy, (bal, _rate) in sorted(balances.items())
        ]

        # Dépenses en attente
        pending_expenses_qs = Expense.objects.filter(
            organization=org,
            status__in=['draft', 'pending', 'approved']
        )
        if request is not None:
            pending_expenses_qs = self._scope_expenses(pending_expenses_qs, request)
        pending_expenses = pending_expenses_qs.count()
        
        return {
            # Scalaires = devise principale (montants convertis).
            'currency': org.currency or 'CDF',
            'current_balance': current_balance,
            # Détail du tiroir : chaque devise avec son solde réel, non converti.
            'balance_by_currency': balance_by_currency,
            'total_income': totals['income'],
            'total_expenses': totals['expenses'],
            'net_flow': totals['income'] - totals['expenses'],
            'pending_expenses': pending_expenses,
        }
    
    def _get_customer_stats(self, org, start_date, end_date):
        """Calcule les statistiques des clients"""
        customers = Customer.objects.filter(organization=org)
        
        total = customers.count()
        active = customers.filter(is_active=True).count()
        
        # Nouveaux clients sur la période
        new_customers = customers.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
        ).count()
        
        # Total des créances (soldes positifs = dette client)
        receivables = customers.filter(
            current_balance__gt=0
        ).aggregate(
            total=Coalesce(Sum('current_balance'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        customers_with_debt = customers.filter(current_balance__gt=0).count()
        
        return {
            'total_customers': total,
            'active_customers': active,
            'new_customers_period': new_customers,
            'total_receivables': receivables,
            'customers_with_debt': customers_with_debt,
        }
    
    # ========================================================================
    # RAPPORT JOURNALIER DE CAISSE
    # ========================================================================
    
    @action(detail=False, methods=['get'])
    def daily_cash_report(self, request):
        """Rapport journalier de caisse détaillé"""
        org = self.get_organization()
        date_str = request.query_params.get('date')
        
        if date_str:
            from datetime import datetime
            report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            report_date = timezone.now().date()
        
        # Mouvements du jour
        movements = self._scope_cash_movements(
            CashMovement.objects.filter(
                organization=org,
                movement_date__date=report_date
            ),
            request,
        ).order_by('movement_date')
        
        # Soldes d'ouverture / clôture : le tiroir étant suivi PAR DEVISE, on
        # somme le dernier solde de CHAQUE devise converti en principale (avant :
        # le solde de la dernière devise ayant bougé, toutes devises confondues).
        opening_balance = balance_in_primary(
            self._scope_cash_movements(
                CashMovement.objects.filter(
                    organization=org,
                    is_cancelled=False,
                    movement_date__date__lt=report_date,
                ),
                request,
            )
        )

        # Sans mouvement jusqu'à cette date, `balance_in_primary` renvoie 0, ce
        # qui est aussi la valeur d'ouverture : pas de cas particulier à traiter.
        closing_balance = balance_in_primary(
            self._scope_cash_movements(
                CashMovement.objects.filter(
                    organization=org,
                    is_cancelled=False,
                    movement_date__date__lte=report_date,
                ),
                request,
            )
        )

        # Ventes du jour
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date=report_date,
                status__in=['completed', 'partially_paid']
            ),
            request,
        )
        
        # Montants convertis en devise principale (total × exchange_rate) pour
        # additionner des ventes de devises différentes sans les mélanger.
        total_sales = sales.aggregate(
            total=Coalesce(Sum(F('total') * F('exchange_rate')), Decimal('0'), output_field=DecimalField())
        )['total']
        total_sales_count = sales.count()
        
        # Paiements par type
        payments = self._scope_payments(
            Payment.objects.filter(
                sale__organization=org,
                paid_at__date=report_date
            ),
            request,
        ).select_related('payment_method', 'sale')

        cash_sales = Decimal('0')
        mobile_money_sales = Decimal('0')
        card_sales = Decimal('0')

        for payment in payments:
            method_name = payment.payment_method.name.lower() if payment.payment_method else ''
            # payment.amount est dans la devise de la vente ; on convertit dans la
            # devise principale (sale.exchange_rate = principale par unité de vente)
            # afin de pouvoir additionner des ventes de devises différentes.
            amount_primary = payment.amount * (payment.sale.exchange_rate if payment.sale else Decimal('1'))
            if 'cash' in method_name or 'espèce' in method_name or 'liquide' in method_name:
                cash_sales += amount_primary
            elif 'mobile' in method_name or 'mpesa' in method_name or 'airtel' in method_name or 'orange' in method_name:
                mobile_money_sales += amount_primary
            elif 'card' in method_name or 'carte' in method_name or 'visa' in method_name:
                card_sales += amount_primary
        
        # Ventes à crédit (montant restant dû, converti en devise principale)
        credit_sales = sales.filter(amount_due__gt=0).aggregate(
            total=Coalesce(Sum(F('amount_due') * F('exchange_rate')), Decimal('0'), output_field=DecimalField())
        )['total']
        
        # Mouvements de caisse convertis en devise principale, comme les ventes
        # ci-dessus (un mouvement en USD ne peut pas être sommé brut avec du CDF).
        debt_collections = movements.filter(
            movement_type='debt_collection'
        ).aggregate(total=primary_sum('amount'))['total']

        # Dépenses
        expenses_movements = movements.filter(direction='out')
        expenses_total = expenses_movements.aggregate(total=primary_sum('amount'))['total']
        expenses_count = expenses_movements.count()

        # Flux net
        income = movements.filter(direction='in').aggregate(total=primary_sum('amount'))['total']
        net_cash_flow = income - expenses_total
        
        report_data = {
            'date': report_date,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_sales': total_sales,
            'total_sales_count': total_sales_count,
            'cash_sales': cash_sales,
            'mobile_money_sales': mobile_money_sales,
            'card_sales': card_sales,
            'credit_sales': credit_sales,
            'debt_collections': debt_collections,
            'expenses': expenses_total,
            'expenses_count': expenses_count,
            'net_cash_flow': net_cash_flow,
        }
        
        # Liste des mouvements avec pagination
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        all_movements = list(movements)
        total_movements = len(all_movements)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_movements = all_movements[start_idx:end_idx]
        
        movements_list = []
        for m in paginated_movements:
            movements_list.append({
                'id': m.id,
                'time': m.movement_date.time(),
                'type': m.movement_type,
                'type_display': m.get_movement_type_display(),
                'description': m.description or '',
                'reference': m.reference,
                'amount': m.amount,
                'direction': m.direction,
                'balance_after': m.balance_after,
            })
        
        return Response({
            'report': DailyCashReportSerializer(report_data).data,
            'movements': {
                'results': DailyCashMovementSerializer(movements_list, many=True).data,
                'count': total_movements,
                'page': page,
                'page_size': page_size,
                'total_pages': (total_movements + page_size - 1) // page_size if page_size > 0 else 0,
            },
        })
    
    # ========================================================================
    # BÉNÉFICES ET MARGES
    # ========================================================================
    
    @action(detail=False, methods=['get'])
    def profit_margins(self, request):
        """Calcul des bénéfices et marges globaux"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        
        # Ventes complétées
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid']
            ),
            request,
        )
        
        # CA HT net (toutes remises), cohérent avec sale.total = subtotal - discount_amount + tax
        revenue_expr = ExpressionWrapper(
            F('subtotal') - F('discount_amount'),
            output_field=DecimalField(max_digits=15, decimal_places=2),
        )
        total_revenue = sales.aggregate(
            total=Coalesce(Sum(revenue_expr), Decimal('0'), output_field=DecimalField(max_digits=15, decimal_places=2))
        )['total']
        total_revenue = (total_revenue or Decimal('0')).quantize(Decimal('0.01'))

        # CMV : coût ligne (FIFO) si > 0, sinon coût produit
        cost_unit = Case(
            When(cost_price__gt=0, then=F('cost_price')),
            default=Coalesce(F('product__cost_price'), Value(Decimal('0'))),
            output_field=DecimalField(max_digits=15, decimal_places=2),
        )
        line_cmv = ExpressionWrapper(
            F('quantity') * cost_unit,
            output_field=DecimalField(max_digits=24, decimal_places=6),
        )
        total_cost = SaleItem.objects.filter(sale__in=sales).aggregate(
            tc=Coalesce(Sum(line_cmv), Decimal('0'), output_field=DecimalField(max_digits=24, decimal_places=6))
        )['tc']
        total_cost = (total_cost or Decimal('0')).quantize(Decimal('0.01'))
        
        # Bénéfice brut
        gross_profit = total_revenue - total_cost
        gross_margin = (gross_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0')
        
        # Dépenses de la période, converties en devise principale (montant × taux)
        # pour être soustraites d'un chiffre d'affaires lui aussi en principale.
        expenses = self._scope_expenses(
            Expense.objects.filter(
                organization=org,
                expense_date__gte=start_date,
                expense_date__lte=end_date,
                status='paid'
            ),
            request,
        ).aggregate(total=primary_sum('amount'))['total']
        
        # Bénéfice net
        net_profit = gross_profit - expenses
        net_margin = (net_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0')
        
        data = {
            'total_revenue': total_revenue,
            'total_cost': total_cost,
            'gross_profit': gross_profit,
            'gross_margin_percentage': round(gross_margin, 2),
            'total_expenses': expenses,
            'net_profit': net_profit,
            'net_margin_percentage': round(net_margin, 2),
        }
        
        serializer = ProfitMarginSerializer(data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], url_path='sales-by-packaging')
    def sales_by_packaging(self, request):
        """
        Chiffre d'affaires et marge ventilés entre vente en gros et vente au détail.

        La marge n'est pas la même selon la forme de vente : c'est ce que ce
        rapport rend visible. Une ligne mixte (« 2 paquets + 3 bouteilles ») est
        répartie **au prorata** de chaque part, de sorte que la somme des deux
        colonnes égale exactement le chiffre d'affaires de la période.
        """
        from apps.sales.profit_allocation import (
            allocated_line_ht_revenues_for_sale, effective_unit_cost,
        )

        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)

        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid'],
            ),
            request,
        ).prefetch_related('items__product')

        buckets = {
            'wholesale': {'revenue': Decimal('0'), 'cost': Decimal('0'), 'quantity': Decimal('0')},
            'retail': {'revenue': Decimal('0'), 'cost': Decimal('0'), 'quantity': Decimal('0')},
        }

        for sale in sales:
            for item, line_revenue in allocated_line_ht_revenues_for_sale(sale):
                unit_cost = effective_unit_cost(item)
                line_cost = (item.quantity * unit_cost).quantize(Decimal('0.01'))

                packaged_units = (
                    (item.package_quantity or Decimal('0')) * (item.packaging_factor or 0)
                )
                if packaged_units <= 0:
                    buckets['retail']['revenue'] += line_revenue
                    buckets['retail']['cost'] += line_cost
                    buckets['retail']['quantity'] += item.quantity
                    continue

                if packaged_units >= item.quantity:
                    buckets['wholesale']['revenue'] += line_revenue
                    buckets['wholesale']['cost'] += line_cost
                    buckets['wholesale']['quantity'] += item.quantity
                    continue

                # Ligne mixte : répartition au prorata du nombre d'unités, pour
                # que les deux colonnes se recollent au total de la vente.
                share = packaged_units / item.quantity
                wholesale_revenue = (line_revenue * share).quantize(Decimal('0.01'))
                wholesale_cost = (line_cost * share).quantize(Decimal('0.01'))

                buckets['wholesale']['revenue'] += wholesale_revenue
                buckets['wholesale']['cost'] += wholesale_cost
                buckets['wholesale']['quantity'] += packaged_units
                buckets['retail']['revenue'] += line_revenue - wholesale_revenue
                buckets['retail']['cost'] += line_cost - wholesale_cost
                buckets['retail']['quantity'] += item.quantity - packaged_units

        def summarize(key, label):
            revenue = buckets[key]['revenue'].quantize(Decimal('0.01'))
            cost = buckets[key]['cost'].quantize(Decimal('0.01'))
            profit = revenue - cost
            margin = (profit / revenue * 100) if revenue > 0 else Decimal('0')
            return {
                'sale_form': key,
                'label': label,
                'revenue': revenue,
                'cost': cost,
                'gross_profit': profit,
                'margin_percentage': round(margin, 2),
                'quantity': buckets[key]['quantity'].quantize(Decimal('0.001')),
            }

        results = [
            summarize('wholesale', 'Vente en gros'),
            summarize('retail', 'Vente au détail'),
        ]
        return Response({
            'results': results,
            'total_revenue': sum((r['revenue'] for r in results), Decimal('0')),
            'total_gross_profit': sum((r['gross_profit'] for r in results), Decimal('0')),
        })

    @action(detail=False, methods=['get'])
    def product_profits(self, request):
        """Bénéfices par produit avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        sale_items = self._scope_sale_items(
            SaleItem.objects.filter(
                sale__organization=org,
                sale__sale_date__date__gte=start_date,
                sale__sale_date__date__lte=end_date,
                sale__status__in=['completed', 'partially_paid']
            ),
            request,
        ).select_related('product', 'sale').order_by('sale_id', 'id')

        sale_ids = list(sale_items.values_list('sale_id', flat=True).distinct())
        sales_by_id = {
            s.id: s
            for s in Sale.objects.filter(id__in=sale_ids).prefetch_related('items__product')
        }

        by_sale_lines = defaultdict(list)
        for row in sale_items:
            by_sale_lines[row.sale_id].append(row)

        product_data = {}
        for sid, lines in by_sale_lines.items():
            sale = sales_by_id.get(sid)
            if not sale:
                continue
            alloc_list = allocated_line_ht_revenues_for_sale(sale)
            alloc_by_item_id = {i.id: rev for i, rev in alloc_list}

            for item in lines:
                pid = str(item.product.id)
                if pid not in product_data:
                    product_data[pid] = {
                        'product_id': item.product.id,
                        'product_name': item.product.name,
                        'product_sku': item.product.sku,
                        'quantity_sold': Decimal('0'),
                        'total_revenue': Decimal('0'),
                        'total_cost': Decimal('0'),
                        # Ventilation gros/détail, cumulée depuis les lignes :
                        # elles portent le nombre de contenants facturés et le
                        # facteur en vigueur au moment de la vente.
                        '_product': item.product,
                        '_packages': Decimal('0'),
                        '_packaged_units': Decimal('0'),
                    }

                cu = effective_unit_cost(item)
                rev = alloc_by_item_id.get(item.id, Decimal('0')).quantize(Decimal('0.01'))
                product_data[pid]['quantity_sold'] += item.quantity
                product_data[pid]['total_revenue'] += rev
                product_data[pid]['total_cost'] += (cu * item.quantity).quantize(Decimal('0.01'))
                packages = Decimal(item.package_quantity or 0)
                product_data[pid]['_packages'] += packages
                product_data[pid]['_packaged_units'] += packages * (item.packaging_factor or 0)
        
        # Calculer profit et marge
        result = []
        for data in product_data.values():
            profit = data['total_revenue'] - data['total_cost']
            margin = (profit / data['total_revenue'] * 100) if data['total_revenue'] > 0 else Decimal('0')
            product = data.pop('_product')
            packages = data.pop('_packages')
            packaged_units = data.pop('_packaged_units')
            if packages > 0:
                loose = max(Decimal('0'), data['quantity_sold'] - packaged_units)
                display = PackagingService.format_split(product, packages, loose)
            else:
                display = PackagingService.format_quantity(product, data['quantity_sold'])
            result.append({
                **data,
                'quantity_display': display,
                'packaging_factor': PackagingService.factor(product),
                'profit': profit,
                'margin_percentage': round(margin, 2),
            })
        
        # Trier par profit décroissant
        result.sort(key=lambda x: x['profit'], reverse=True)
        
        # Pagination
        total_count = len(result)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_result = result[start_idx:end_idx]
        
        serializer = ProductProfitSerializer(paginated_result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    # ========================================================================
    # STOCKS DÉTAILLÉS
    # ========================================================================
    
    @action(detail=False, methods=['get'])
    def stock_details(self, request):
        """Liste détaillée du stock par produit avec pagination"""
        org = self.get_organization()
        filter_status = request.query_params.get('status')  # low, out, available
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        # `product__unit` et `product__packaging_unit` alimentent l'affichage
        # « 12 cartons + 3 bouteilles » : sans eux, deux requêtes par ligne.
        stocks = self._scope_stocks(
            Stock.objects.filter(
                organization=org,
                product__is_active=True
            ),
            request,
        ).select_related(
            'product', 'product__category',
            'product__unit', 'product__packaging_unit',
        )
        
        result = []
        for stock in stocks:
            qty = Decimal(str(stock.quantity))
            reserved = Decimal(str(stock.reserved_quantity))
            available = qty - reserved
            cost = stock.product.cost_price or Decimal('0')
            min_level = stock.product.min_stock_level
            
            # Déterminer le statut
            if qty <= 0:
                status = 'out_of_stock'
            elif min_level and qty <= min_level:
                status = 'low_stock'
            else:
                status = 'available'
            
            # Filtrer si demandé
            if filter_status:
                if filter_status == 'low' and status != 'low_stock':
                    continue
                elif filter_status == 'out' and status != 'out_of_stock':
                    continue
                elif filter_status == 'available' and status != 'available':
                    continue
            
            # Le stock d'un produit vendu en gros se lit en contenants : « 147 »
            # ne dit pas au gérant s'il peut honorer une commande de 10 cartons.
            # Les deux compteurs sont LUS sur la ligne de stock, jamais
            # reconstitués depuis le total : « 3 casiers + 27 bouteilles » ne
            # doit pas se réécrire « 4 casiers + 3 bouteilles ».
            factor = PackagingService.factor(stock.product)
            packages, loose_split = (
                PackagingService.stored_split(stock, factor)
                if factor is not None else (None, None)
            )

            result.append({
                'product_id': stock.product.id,
                'product_name': stock.product.name,
                'product_sku': stock.product.sku,
                'category_name': stock.product.category.name if stock.product.category else None,
                'current_stock': qty,
                'stock_display': PackagingService.format_stock(stock),
                'stock_packages': packages,
                'stock_loose': loose_split,
                'reserved_stock': reserved,
                'reserved_display': PackagingService.format_base_total(
                    stock.product, reserved
                ),
                'available_stock': available,
                'available_display': PackagingService.format_available(stock),
                'packaging_factor': factor,
                'min_stock_level': min_level,
                'cost_price': cost,
                'stock_value': qty * cost,
                'status': status,
            })
        
        # Trier par statut (ruptures en premier) puis par nom
        status_order = {'out_of_stock': 0, 'low_stock': 1, 'available': 2}
        result.sort(key=lambda x: (status_order.get(x['status'], 3), x['product_name']))
        
        # Pagination
        total_count = len(result)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_result = result[start_idx:end_idx]
        
        serializer = StockDetailSerializer(paginated_result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def stock_movements_summary(self, request):
        """Résumé des mouvements de stock par type"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        
        from apps.inventory.models import StockMovement
        
        movements = self._scope_stock_movements(
            StockMovement.objects.filter(
                organization=org,
                created_at__date__gte=start_date,
                created_at__date__lte=end_date
            ),
            request,
        )
        
        # Agrégation par type
        data = {
            'total_in': Decimal('0'),
            'total_out': Decimal('0'),
            'sales_out': Decimal('0'),
            'adjustments_in': Decimal('0'),
            'adjustments_out': Decimal('0'),
            'transfers_in': Decimal('0'),
            'transfers_out': Decimal('0'),
            'returns_in': Decimal('0'),
        }
        
        for m in movements:
            qty = Decimal(str(m.quantity))
            if m.movement_type == 'sale':
                data['total_out'] += qty
                data['sales_out'] += qty
            elif m.movement_type == 'return_in':
                data['total_in'] += qty
                data['returns_in'] += qty
            elif m.movement_type == 'adjustment_in':
                data['total_in'] += qty
                data['adjustments_in'] += qty
            elif m.movement_type == 'adjustment_out':
                data['total_out'] += qty
                data['adjustments_out'] += qty
            elif m.movement_type == 'transfer_in':
                data['total_in'] += qty
                data['transfers_in'] += qty
            elif m.movement_type == 'transfer_out':
                data['total_out'] += qty
                data['transfers_out'] += qty
            elif m.movement_type in ['purchase', 'initial']:
                data['total_in'] += qty
        
        serializer = StockMovementSummarySerializer(data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def product_supplies(self, request):
        """
        Approvisionnements par produit pour une période donnée.

        Chaque entrée porte le total en unité de détail ET sa lecture en
        contenants (« 10 cartons + 5 bouteilles »). Les contenants viennent des
        champs de saisie figés sur le mouvement, jamais d'une division du total :
        une réception de 5 cartons plus 120 bouteilles ne doit pas se relire
        « 10 cartons ».
        """
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        
        from apps.inventory.models import StockMovement
        
        # Récupérer les mouvements d'entrée (approvisionnements) par produit
        supplies = self._scope_stock_movements(
            StockMovement.objects.filter(
                organization=org,
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
                movement_type__in=['purchase', 'initial', 'transfer_in', 'adjustment_in', 'return_in']
            ),
            request,
        ).values(
            'product_id',
            'product__name',
            'product__selling_mode',
            'product__units_per_package',
            'product__unit__name',
            'product__packaging_unit__name',
        ).annotate(
            total_supply=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField()),
            total_packages=Coalesce(
                Sum('input_package_quantity'), Decimal('0'), output_field=DecimalField()
            ),
            packaged_units=Coalesce(
                Sum(F('input_package_quantity') * F('packaging_factor')),
                Decimal('0'), output_field=DecimalField(),
            ),
        )
        
        result = {}
        for row in supplies:
            profile = PackagingProfile.from_values(row)
            factor = PackagingService.factor(profile)
            quantity = Decimal(row['total_supply'] or 0)
            packages = Decimal(row['total_packages'] or 0)
            packaged_units = Decimal(row['packaged_units'] or 0)

            if factor is not None and packages > 0:
                loose = max(Decimal('0'), quantity - packaged_units)
                display = PackagingService.format_split(profile, packages, loose)
            else:
                loose = quantity if factor is not None else None
                packages = Decimal('0') if factor is not None else None
                display = PackagingService.format_quantity(profile, quantity)

            result[str(row['product_id'])] = {
                'quantity': float(quantity),
                'display': display,
                'packages': float(packages) if packages is not None else None,
                'loose': float(loose) if loose is not None else None,
            }
        return Response(result)
