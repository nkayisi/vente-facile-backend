from rest_framework import serializers
from .models import ReportTemplate, SavedReport, ReportExport, Dashboard, DashboardWidget


class ReportTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportTemplate
        fields = [
            'id', 'name', 'code', 'description', 'report_type',
            'query_config', 'columns', 'filters', 'grouping', 'sorting',
            'chart_config', 'is_system', 'is_active', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class SavedReportSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source='template.name', read_only=True)
    
    class Meta:
        model = SavedReport
        fields = [
            'id', 'name', 'template', 'template_name', 'parameters',
            'is_scheduled', 'frequency', 'schedule_time', 'schedule_day',
            'recipients', 'last_run', 'next_run', 'created_at'
        ]
        read_only_fields = ['id', 'last_run', 'next_run', 'created_at']


class ReportExportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportExport
        fields = [
            'id', 'saved_report', 'template', 'export_format', 'status',
            'parameters', 'file', 'file_size', 'error_message',
            'completed_at', 'expires_at', 'created_at'
        ]
        read_only_fields = ['id', 'status', 'file', 'file_size', 'error_message', 'completed_at', 'created_at']


class DashboardWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = DashboardWidget
        fields = [
            'id', 'name', 'widget_type', 'config',
            'position_x', 'position_y', 'width', 'height'
        ]


class DashboardSerializer(serializers.ModelSerializer):
    widgets = DashboardWidgetSerializer(many=True, read_only=True)
    
    class Meta:
        model = Dashboard
        fields = [
            'id', 'name', 'description', 'layout',
            'is_default', 'is_shared', 'widgets', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


# ============================================================================
# STATISTICS SERIALIZERS
# ============================================================================

class SalesStatsSerializer(serializers.Serializer):
    """Statistiques des ventes"""
    total_sales = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_orders = serializers.IntegerField()
    average_order_value = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_items_sold = serializers.IntegerField()
    completed_sales = serializers.IntegerField()
    pending_sales = serializers.IntegerField()
    cancelled_sales = serializers.IntegerField()
    
    # Comparaison période précédente
    sales_growth = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)
    orders_growth = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)


class SalesByPeriodSerializer(serializers.Serializer):
    """Ventes par période (jour/semaine/mois)"""
    period = serializers.CharField()
    total = serializers.DecimalField(max_digits=15, decimal_places=2)
    count = serializers.IntegerField()


class TopProductSerializer(serializers.Serializer):
    """Produits les plus vendus"""
    product_id = serializers.UUIDField()
    product_name = serializers.CharField()
    product_sku = serializers.CharField()
    quantity_sold = serializers.IntegerField()
    total_revenue = serializers.DecimalField(max_digits=15, decimal_places=2)


class TopCustomerSerializer(serializers.Serializer):
    """Meilleurs clients"""
    customer_id = serializers.UUIDField()
    customer_name = serializers.CharField()
    total_purchases = serializers.DecimalField(max_digits=15, decimal_places=2)
    order_count = serializers.IntegerField()
    current_balance = serializers.DecimalField(max_digits=15, decimal_places=2)


class StockStatsSerializer(serializers.Serializer):
    """Statistiques du stock"""
    total_products = serializers.IntegerField()
    total_stock_value = serializers.DecimalField(max_digits=15, decimal_places=2)
    low_stock_count = serializers.IntegerField()
    out_of_stock_count = serializers.IntegerField()
    expiring_soon_count = serializers.IntegerField()


class CashbookStatsSerializer(serializers.Serializer):
    """Statistiques de la caisse"""
    current_balance = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_income = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_expenses = serializers.DecimalField(max_digits=15, decimal_places=2)
    net_flow = serializers.DecimalField(max_digits=15, decimal_places=2)
    pending_expenses = serializers.IntegerField()


class CashFlowByPeriodSerializer(serializers.Serializer):
    """Flux de trésorerie par période"""
    period = serializers.CharField()
    income = serializers.DecimalField(max_digits=15, decimal_places=2)
    expenses = serializers.DecimalField(max_digits=15, decimal_places=2)
    net = serializers.DecimalField(max_digits=15, decimal_places=2)


class CustomerStatsSerializer(serializers.Serializer):
    """Statistiques des clients"""
    total_customers = serializers.IntegerField()
    active_customers = serializers.IntegerField()
    new_customers_period = serializers.IntegerField()
    total_receivables = serializers.DecimalField(max_digits=15, decimal_places=2)
    customers_with_debt = serializers.IntegerField()


class SalesByCategorySerializer(serializers.Serializer):
    """Ventes par catégorie"""
    category_id = serializers.UUIDField(allow_null=True)
    category_name = serializers.CharField()
    total_revenue = serializers.DecimalField(max_digits=15, decimal_places=2)
    quantity_sold = serializers.DecimalField(max_digits=15, decimal_places=2)
    percentage = serializers.DecimalField(max_digits=5, decimal_places=2)


class SalesByPaymentMethodSerializer(serializers.Serializer):
    """Ventes par méthode de paiement"""
    payment_method = serializers.CharField()
    payment_method_name = serializers.CharField()
    total = serializers.DecimalField(max_digits=15, decimal_places=2)
    count = serializers.IntegerField()
    percentage = serializers.DecimalField(max_digits=5, decimal_places=2)


class DashboardSummarySerializer(serializers.Serializer):
    """Résumé global du dashboard"""
    sales = SalesStatsSerializer()
    stock = StockStatsSerializer()
    cashbook = CashbookStatsSerializer()
    customers = CustomerStatsSerializer()


class DailyCashReportSerializer(serializers.Serializer):
    """Rapport journalier de caisse"""
    date = serializers.DateField()
    opening_balance = serializers.DecimalField(max_digits=15, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_sales = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_sales_count = serializers.IntegerField()
    cash_sales = serializers.DecimalField(max_digits=15, decimal_places=2)
    mobile_money_sales = serializers.DecimalField(max_digits=15, decimal_places=2)
    card_sales = serializers.DecimalField(max_digits=15, decimal_places=2)
    credit_sales = serializers.DecimalField(max_digits=15, decimal_places=2)
    debt_collections = serializers.DecimalField(max_digits=15, decimal_places=2)
    expenses = serializers.DecimalField(max_digits=15, decimal_places=2)
    expenses_count = serializers.IntegerField()
    net_cash_flow = serializers.DecimalField(max_digits=15, decimal_places=2)


class DailyCashMovementSerializer(serializers.Serializer):
    """Mouvement de caisse pour rapport journalier"""
    id = serializers.UUIDField()
    time = serializers.TimeField()
    type = serializers.CharField()
    type_display = serializers.CharField()
    description = serializers.CharField()
    reference = serializers.CharField(allow_null=True)
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    direction = serializers.CharField()
    balance_after = serializers.DecimalField(max_digits=15, decimal_places=2)


class ProfitMarginSerializer(serializers.Serializer):
    """Bénéfices et marges"""
    total_revenue = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_cost = serializers.DecimalField(max_digits=15, decimal_places=2)
    gross_profit = serializers.DecimalField(max_digits=15, decimal_places=2)
    gross_margin_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)
    total_expenses = serializers.DecimalField(max_digits=15, decimal_places=2)
    net_profit = serializers.DecimalField(max_digits=15, decimal_places=2)
    net_margin_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)


class ProductProfitSerializer(serializers.Serializer):
    """Bénéfice par produit"""
    product_id = serializers.UUIDField()
    product_name = serializers.CharField()
    product_sku = serializers.CharField()
    quantity_sold = serializers.DecimalField(max_digits=15, decimal_places=3)
    total_revenue = serializers.DecimalField(max_digits=15, decimal_places=2)
    total_cost = serializers.DecimalField(max_digits=15, decimal_places=2)
    profit = serializers.DecimalField(max_digits=15, decimal_places=2)
    margin_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)


class StockDetailSerializer(serializers.Serializer):
    """Détail du stock par produit"""
    product_id = serializers.UUIDField()
    product_name = serializers.CharField()
    product_sku = serializers.CharField()
    category_name = serializers.CharField(allow_null=True)
    current_stock = serializers.DecimalField(max_digits=15, decimal_places=3)
    reserved_stock = serializers.DecimalField(max_digits=15, decimal_places=3)
    available_stock = serializers.DecimalField(max_digits=15, decimal_places=3)
    min_stock_level = serializers.DecimalField(max_digits=15, decimal_places=3, allow_null=True)
    cost_price = serializers.DecimalField(max_digits=15, decimal_places=2, allow_null=True)
    stock_value = serializers.DecimalField(max_digits=15, decimal_places=2)
    status = serializers.CharField()


class StockMovementSummarySerializer(serializers.Serializer):
    """Résumé des mouvements de stock"""
    total_in = serializers.DecimalField(max_digits=15, decimal_places=3)
    total_out = serializers.DecimalField(max_digits=15, decimal_places=3)
    sales_out = serializers.DecimalField(max_digits=15, decimal_places=3)
    adjustments_in = serializers.DecimalField(max_digits=15, decimal_places=3)
    adjustments_out = serializers.DecimalField(max_digits=15, decimal_places=3)
    transfers_in = serializers.DecimalField(max_digits=15, decimal_places=3)
    transfers_out = serializers.DecimalField(max_digits=15, decimal_places=3)
    returns_in = serializers.DecimalField(max_digits=15, decimal_places=3)
