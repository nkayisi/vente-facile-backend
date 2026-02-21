from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Avg, F, Q, DecimalField
from django.db.models.functions import Coalesce, TruncDate, TruncWeek, TruncMonth
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from apps.core.mixins import TenantQuerysetMixin
from apps.core.api_permissions import IsTenantMember, HasActiveSubscription, HasPermission
from apps.sales.models import Sale, SaleItem, Payment
from apps.products.models import Product
from apps.inventory.models import Stock, StockBatch
from apps.contacts.models import Customer
from apps.cashbook.models import CashMovement, Expense

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


class StatisticsViewSet(TenantQuerysetMixin, viewsets.ViewSet):
    """
    ViewSet pour les statistiques et rapports en temps réel.
    Fournit des endpoints pour différentes métriques de l'entreprise.
    """
    
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'summary': 'reports.view',
        'sales': 'reports.view',
        'sales_by_period': 'reports.view',
        'sales_by_category': 'reports.view',
        'sales_by_payment_method': 'reports.view',
        'top_products': 'reports.view',
        'top_customers': 'reports.view',
        'stock': 'reports.view',
        'stock_details': 'reports.view',
        'stock_movements_summary': 'reports.view',
        'cashbook': 'reports.view',
        'cash_flow': 'reports.view',
        'daily_cash_report': 'reports.view',
        'customers': 'reports.view',
        'profit_margins': 'reports.view',
        'product_profits': 'reports.view',
    }
    
    def _parse_date_range(self, request):
        """Parse les paramètres de période"""
        period = request.query_params.get('period', 'month')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        
        today = timezone.now().date()
        
        if date_from and date_to:
            from datetime import datetime
            start_date = datetime.strptime(date_from, '%Y-%m-%d').date()
            end_date = datetime.strptime(date_to, '%Y-%m-%d').date()
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
    def summary(self, request):
        """Résumé global pour le dashboard principal"""
        org = self.get_organization()
        start_date, end_date, prev_start, prev_end = self._parse_date_range(request)
        
        # Statistiques des ventes
        sales_stats = self._get_sales_stats(org, start_date, end_date, prev_start, prev_end)
        
        # Statistiques du stock
        stock_stats = self._get_stock_stats(org)
        
        # Statistiques de la caisse
        cashbook_stats = self._get_cashbook_stats(org, start_date, end_date)
        
        # Statistiques des clients
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
        
        stats = self._get_sales_stats(org, start_date, end_date, prev_start, prev_end)
        serializer = SalesStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales_by_period(self, request):
        """Ventes groupées par période (jour/semaine/mois)"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')
        
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=start_date,
            sale_date__date__lte=end_date,
            status__in=['completed', 'partially_paid']
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
        
        result = [
            {
                'period': item['period'].strftime('%Y-%m-%d') if item['period'] else '',
                'total': item['total'],
                'count': item['count']
            }
            for item in data
        ]
        
        serializer = SalesByPeriodSerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales_by_category(self, request):
        """Ventes par catégorie de produit"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        
        items = SaleItem.objects.filter(
            sale__organization=org,
            sale__sale_date__date__gte=start_date,
            sale__sale_date__date__lte=end_date,
            sale__status__in=['completed', 'partially_paid']
        ).select_related('product__category')
        
        data = items.values(
            'product__category__id',
            'product__category__name'
        ).annotate(
            total_revenue=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField()),
            quantity_sold=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField())
        ).order_by('-total_revenue')
        
        total_revenue = sum(item['total_revenue'] for item in data)
        
        result = [
            {
                'category_id': item['product__category__id'],
                'category_name': item['product__category__name'] or 'Sans catégorie',
                'total_revenue': item['total_revenue'],
                'quantity_sold': item['quantity_sold'],
                'percentage': round((item['total_revenue'] / total_revenue * 100) if total_revenue > 0 else 0, 2)
            }
            for item in data
        ]
        
        serializer = SalesByCategorySerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales_by_payment_method(self, request):
        """Ventes par méthode de paiement"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        
        payments = Payment.objects.filter(
            sale__organization=org,
            paid_at__date__gte=start_date,
            paid_at__date__lte=end_date
        ).select_related('payment_method')
        
        data = payments.values(
            'payment_method__id',
            'payment_method__name'
        ).annotate(
            total=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()),
            count=Count('id')
        ).order_by('-total')
        
        total_amount = sum(item['total'] for item in data)
        
        result = [
            {
                'payment_method': str(item['payment_method__id']) if item['payment_method__id'] else 'unknown',
                'payment_method_name': item['payment_method__name'] or 'Inconnu',
                'total': item['total'],
                'count': item['count'],
                'percentage': round((item['total'] / total_amount * 100) if total_amount > 0 else 0, 2)
            }
            for item in data
        ]
        
        serializer = SalesByPaymentMethodSerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def top_products(self, request):
        """Top produits les plus vendus"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        limit = int(request.query_params.get('limit', 10))
        
        items = SaleItem.objects.filter(
            sale__organization=org,
            sale__sale_date__date__gte=start_date,
            sale__sale_date__date__lte=end_date,
            sale__status__in=['completed', 'partially_paid']
        ).select_related('product')
        
        data = items.values(
            'product__id',
            'product__name',
            'product__sku'
        ).annotate(
            quantity_sold=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField()),
            total_revenue=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField())
        ).order_by('-quantity_sold')[:limit]
        
        result = [
            {
                'product_id': item['product__id'],
                'product_name': item['product__name'],
                'product_sku': item['product__sku'],
                'quantity_sold': item['quantity_sold'],
                'total_revenue': item['total_revenue']
            }
            for item in data
        ]
        
        serializer = TopProductSerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def top_customers(self, request):
        """Meilleurs clients"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        limit = int(request.query_params.get('limit', 10))
        
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=start_date,
            sale_date__date__lte=end_date,
            status__in=['completed', 'partially_paid'],
            customer__isnull=False
        ).select_related('customer')
        
        data = sales.values(
            'customer__id',
            'customer__name',
            'customer__current_balance'
        ).annotate(
            total_purchases=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField()),
            order_count=Count('id')
        ).order_by('-total_purchases')[:limit]
        
        result = [
            {
                'customer_id': item['customer__id'],
                'customer_name': item['customer__name'],
                'total_purchases': item['total_purchases'],
                'order_count': item['order_count'],
                'current_balance': item['customer__current_balance'] or Decimal('0')
            }
            for item in data
        ]
        
        serializer = TopCustomerSerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def stock(self, request):
        """Statistiques du stock"""
        org = self.get_organization()
        stats = self._get_stock_stats(org)
        serializer = StockStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def cashbook(self, request):
        """Statistiques de la caisse"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        stats = self._get_cashbook_stats(org, start_date, end_date)
        serializer = CashbookStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def cash_flow(self, request):
        """Flux de trésorerie par période"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')
        
        movements = CashMovement.objects.filter(
            organization=org,
            movement_date__date__gte=start_date,
            movement_date__date__lte=end_date
        )
        
        if group_by == 'week':
            trunc_func = TruncWeek('movement_date')
        elif group_by == 'month':
            trunc_func = TruncMonth('movement_date')
        else:
            trunc_func = TruncDate('movement_date')
        
        data = movements.annotate(
            period=trunc_func
        ).values('period').annotate(
            income=Coalesce(
                Sum('amount', filter=Q(direction='in')),
                Decimal('0')
            ),
            expenses=Coalesce(
                Sum('amount', filter=Q(direction='out')),
                Decimal('0')
            )
        ).order_by('period')
        
        result = [
            {
                'period': item['period'].strftime('%Y-%m-%d') if item['period'] else '',
                'income': item['income'],
                'expenses': item['expenses'],
                'net': item['income'] - item['expenses']
            }
            for item in data
        ]
        
        serializer = CashFlowByPeriodSerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def customers(self, request):
        """Statistiques des clients"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        stats = self._get_customer_stats(org, start_date, end_date)
        serializer = CustomerStatsSerializer(stats)
        return Response(serializer.data)
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def _get_sales_stats(self, org, start_date, end_date, prev_start, prev_end):
        """Calcule les statistiques des ventes"""
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=start_date,
            sale_date__date__lte=end_date
        )
        
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
    
    def _get_stock_stats(self, org):
        """Calcule les statistiques du stock"""
        products = Product.objects.filter(organization=org, is_active=True)
        total_products = products.count()
        
        stocks = Stock.objects.filter(
            organization=org,
            product__is_active=True
        )
        
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
        expiring_count = StockBatch.objects.filter(
            organization=org,
            expiry_date__lte=expiring_date,
            expiry_date__gte=timezone.now().date(),
            quantity__gt=0
        ).count()
        
        return {
            'total_products': total_products,
            'total_stock_value': total_value,
            'low_stock_count': low_stock,
            'out_of_stock_count': out_of_stock,
            'expiring_soon_count': expiring_count,
        }
    
    def _get_cashbook_stats(self, org, start_date, end_date):
        """Calcule les statistiques de la caisse"""
        movements = CashMovement.objects.filter(
            organization=org,
            movement_date__date__gte=start_date,
            movement_date__date__lte=end_date
        )
        
        totals = movements.aggregate(
            income=Coalesce(Sum('amount', filter=Q(direction='in')), Decimal('0'), output_field=DecimalField()),
            expenses=Coalesce(Sum('amount', filter=Q(direction='out')), Decimal('0'), output_field=DecimalField())
        )
        
        # Solde actuel (dernier mouvement)
        last_movement = CashMovement.objects.filter(
            organization=org
        ).order_by('-movement_date', '-created_at').first()
        
        current_balance = last_movement.balance_after if last_movement else Decimal('0')
        
        # Dépenses en attente
        pending_expenses = Expense.objects.filter(
            organization=org,
            status__in=['draft', 'pending', 'approved']
        ).count()
        
        return {
            'current_balance': current_balance,
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
        movements = CashMovement.objects.filter(
            organization=org,
            movement_date__date=report_date
        ).order_by('movement_date')
        
        # Solde d'ouverture (dernier mouvement avant ce jour)
        prev_movement = CashMovement.objects.filter(
            organization=org,
            movement_date__date__lt=report_date
        ).order_by('-movement_date', '-created_at').first()
        
        opening_balance = prev_movement.balance_after if prev_movement else Decimal('0')
        
        # Solde de clôture (dernier mouvement du jour)
        last_movement = movements.order_by('-movement_date', '-created_at').first()
        closing_balance = last_movement.balance_after if last_movement else opening_balance
        
        # Ventes du jour
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date=report_date,
            status__in=['completed', 'partially_paid']
        )
        
        total_sales = sales.aggregate(total=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField()))['total']
        total_sales_count = sales.count()
        
        # Paiements par type
        payments = Payment.objects.filter(
            sale__organization=org,
            paid_at__date=report_date
        ).select_related('payment_method')
        
        cash_sales = Decimal('0')
        mobile_money_sales = Decimal('0')
        card_sales = Decimal('0')
        
        for payment in payments:
            method_name = payment.payment_method.name.lower() if payment.payment_method else ''
            if 'cash' in method_name or 'espèce' in method_name or 'liquide' in method_name:
                cash_sales += payment.amount
            elif 'mobile' in method_name or 'mpesa' in method_name or 'airtel' in method_name or 'orange' in method_name:
                mobile_money_sales += payment.amount
            elif 'card' in method_name or 'carte' in method_name or 'visa' in method_name:
                card_sales += payment.amount
        
        # Ventes à crédit (montant restant dû)
        credit_sales = sales.filter(amount_due__gt=0).aggregate(
            total=Coalesce(Sum('amount_due'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        # Recouvrements de créances
        debt_collections = movements.filter(
            movement_type='debt_collection'
        ).aggregate(total=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()))['total']
        
        # Dépenses
        expenses_movements = movements.filter(direction='out')
        expenses_total = expenses_movements.aggregate(total=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()))['total']
        expenses_count = expenses_movements.count()
        
        # Flux net
        income = movements.filter(direction='in').aggregate(total=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()))['total']
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
        
        # Liste des mouvements
        movements_list = []
        for m in movements:
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
            'movements': DailyCashMovementSerializer(movements_list, many=True).data,
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
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=start_date,
            sale_date__date__lte=end_date,
            status__in=['completed', 'partially_paid']
        )
        
        total_revenue = sales.aggregate(total=Coalesce(Sum('total'), Decimal('0'), output_field=DecimalField()))['total']
        
        # Coût des marchandises vendues
        sale_items = SaleItem.objects.filter(
            sale__in=sales
        ).select_related('product')
        
        total_cost = Decimal('0')
        for item in sale_items:
            cost_price = item.product.cost_price or Decimal('0')
            total_cost += cost_price * item.quantity
        
        # Bénéfice brut
        gross_profit = total_revenue - total_cost
        gross_margin = (gross_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0')
        
        # Dépenses de la période
        expenses = Expense.objects.filter(
            organization=org,
            expense_date__gte=start_date,
            expense_date__lte=end_date,
            status='paid'
        ).aggregate(total=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()))['total']
        
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
    
    @action(detail=False, methods=['get'])
    def product_profits(self, request):
        """Bénéfices par produit"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        limit = int(request.query_params.get('limit', 20))
        
        sale_items = SaleItem.objects.filter(
            sale__organization=org,
            sale__sale_date__date__gte=start_date,
            sale__sale_date__date__lte=end_date,
            sale__status__in=['completed', 'partially_paid']
        ).select_related('product')
        
        # Grouper par produit
        product_data = {}
        for item in sale_items:
            pid = str(item.product.id)
            if pid not in product_data:
                product_data[pid] = {
                    'product_id': item.product.id,
                    'product_name': item.product.name,
                    'product_sku': item.product.sku,
                    'quantity_sold': 0,
                    'total_revenue': Decimal('0'),
                    'total_cost': Decimal('0'),
                }
            
            cost_price = item.product.cost_price or Decimal('0')
            product_data[pid]['quantity_sold'] += item.quantity
            product_data[pid]['total_revenue'] += item.total
            product_data[pid]['total_cost'] += cost_price * item.quantity
        
        # Calculer profit et marge
        result = []
        for data in product_data.values():
            profit = data['total_revenue'] - data['total_cost']
            margin = (profit / data['total_revenue'] * 100) if data['total_revenue'] > 0 else Decimal('0')
            result.append({
                **data,
                'profit': profit,
                'margin_percentage': round(margin, 2),
            })
        
        # Trier par profit décroissant
        result.sort(key=lambda x: x['profit'], reverse=True)
        
        serializer = ProductProfitSerializer(result[:limit], many=True)
        return Response(serializer.data)
    
    # ========================================================================
    # STOCKS DÉTAILLÉS
    # ========================================================================
    
    @action(detail=False, methods=['get'])
    def stock_details(self, request):
        """Liste détaillée du stock par produit"""
        org = self.get_organization()
        filter_status = request.query_params.get('status')  # low, out, available
        
        stocks = Stock.objects.filter(
            organization=org,
            product__is_active=True
        ).select_related('product', 'product__category')
        
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
            
            result.append({
                'product_id': stock.product.id,
                'product_name': stock.product.name,
                'product_sku': stock.product.sku,
                'category_name': stock.product.category.name if stock.product.category else None,
                'current_stock': qty,
                'reserved_stock': reserved,
                'available_stock': available,
                'min_stock_level': min_level,
                'cost_price': cost,
                'stock_value': qty * cost,
                'status': status,
            })
        
        # Trier par statut (ruptures en premier) puis par nom
        status_order = {'out_of_stock': 0, 'low_stock': 1, 'available': 2}
        result.sort(key=lambda x: (status_order.get(x['status'], 3), x['product_name']))
        
        serializer = StockDetailSerializer(result, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def stock_movements_summary(self, request):
        """Résumé des mouvements de stock par type"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        
        from apps.inventory.models import StockMovement
        
        movements = StockMovement.objects.filter(
            organization=org,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
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
