"""
Serializers DRF pour l'app Contacts (Customers & Suppliers).
"""
from rest_framework import serializers
from decimal import Decimal
from .models import Customer, CustomerTransaction, Supplier, SupplierProduct


# =============================================================================
# CUSTOMER SERIALIZERS
# =============================================================================

class CustomerBalancesMixin(serializers.Serializer):
    """
    Expose la dette RÉELLE, ventilée par devise.

    `current_balance` reste la vue agrégée convertie en devise principale ;
    `balances` porte le détail, qu'on n'additionne jamais entre devises.
    """

    balances = serializers.SerializerMethodField()

    def get_balances(self, obj):
        return [
            {'currency': row.currency, 'amount': str(row.amount)}
            for row in obj.balances.all() if row.amount != 0
        ]


class CustomerListSerializer(CustomerBalancesMixin, serializers.ModelSerializer):
    """Serializer léger pour les listes de clients."""
    
    customer_type_display = serializers.CharField(
        source='get_customer_type_display', read_only=True
    )
    
    class Meta:
        model = Customer
        fields = [
            'id', 'code', 'name', 'company_name',
            'customer_type', 'customer_type_display',
            'email', 'phone', 'address', 'tax_id',
            'credit_limit', 'current_balance', 'balances',
            'is_active'
        ]
        read_only_fields = ['id', 'code']


class CustomerDetailSerializer(CustomerBalancesMixin, serializers.ModelSerializer):
    """Serializer complet pour le détail d'un client."""
    
    customer_type_display = serializers.CharField(
        source='get_customer_type_display', read_only=True
    )
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    available_credit = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True
    )
    total_purchases = serializers.SerializerMethodField()
    recent_sales = serializers.SerializerMethodField()
    
    class Meta:
        model = Customer
        fields = [
            'id', 'code', 'customer_type', 'customer_type_display',
            'name', 'company_name',
            'email', 'phone', 'address', 'tax_id',
            'credit_limit', 'current_balance', 'available_credit', 'balances',
            'notes', 'is_active',
            'created_by', 'created_by_name',
            'total_purchases', 'recent_sales',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'code', 'created_at', 'updated_at']

    def get_total_purchases(self, obj):
        return str(obj.get_total_purchases())

    def get_recent_sales(self, obj):
        """Retourne les 5 dernières ventes."""
        sales = obj.sales.filter(
            status='completed'
        ).order_by('-sale_date')[:5]
        
        return [
            {
                'id': str(sale.id),
                'reference': sale.reference,
                'total': str(sale.total),
                'date': sale.sale_date
            }
            for sale in sales
        ]


class CustomerCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de client."""
    
    class Meta:
        model = Customer
        fields = [
            'id', 'code', 'customer_type', 'name', 'company_name',
            'email', 'phone', 'address', 'tax_id',
            'credit_limit', 'notes', 'is_active',
        ]
        read_only_fields = ['id', 'code']
        extra_kwargs = {
            'name': {'required': True, 'allow_blank': False},
            'phone': {'required': True, 'allow_blank': False},
        }

    def validate(self, attrs):
        customer_type = attrs.get('customer_type', 'individual')
        if customer_type == 'business':
            if not attrs.get('name'):
                raise serializers.ValidationError({'name': "Le nom de l'entreprise est obligatoire."})
        else:
            attrs['company_name'] = ''
            attrs['tax_id'] = ''
        return attrs

    def create(self, validated_data):
        org = validated_data.get('organization')
        
        from apps.core.utils import ReferenceGenerator
        validated_data['code'] = ReferenceGenerator.generate_customer_code(org)
        
        return Customer.objects.create(**validated_data)


class CustomerUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour la mise à jour de client."""
    
    class Meta:
        model = Customer
        fields = [
            'customer_type', 'name', 'company_name',
            'email', 'phone', 'address', 'tax_id',
            'credit_limit', 'notes', 'is_active'
        ]
        extra_kwargs = {
            'name': {'required': True, 'allow_blank': False},
            'phone': {'required': True, 'allow_blank': False},
        }

    def validate(self, attrs):
        instance = self.instance
        customer_type = attrs.get('customer_type', getattr(instance, 'customer_type', 'individual'))
        if customer_type == 'individual':
            attrs['company_name'] = ''
            attrs['tax_id'] = ''
        return attrs


# =============================================================================
# CUSTOMER TRANSACTION SERIALIZERS
# =============================================================================

class CustomerTransactionSerializer(serializers.ModelSerializer):
    """Serializer pour l'affichage des transactions client."""
    
    transaction_type_display = serializers.CharField(
        source='get_transaction_type_display', read_only=True
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )
    sale_reference = serializers.CharField(
        source='sale.reference', read_only=True, default=''
    )
    
    class Meta:
        model = CustomerTransaction
        fields = [
            'id', 'transaction_type', 'transaction_type_display',
            'amount', 'currency', 'exchange_rate',
            'balance_before', 'balance_after',
            'reference', 'sale', 'sale_reference',
            'payment_method', 'notes',
            'created_by', 'created_by_name',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class RecordPaymentSerializer(serializers.Serializer):
    """
    Serializer pour enregistrer un paiement ou une avance client.

    ``currency`` est la devise réellement remise : un règlement en USD ne solde
    que des factures libellées en USD, et n'entre au tiroir qu'en USD.
    """

    amount = serializers.DecimalField(
        max_digits=15, decimal_places=2,
        min_value=Decimal('0.01')
    )
    currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=12, required=False
    )
    payment_method = serializers.CharField(required=False, default='cash')
    reference = serializers.CharField(required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class AdjustBalanceSerializer(serializers.Serializer):
    """Serializer pour ajuster manuellement le solde client."""

    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=12, required=False
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class RedeemPointsToDebtSerializer(serializers.Serializer):
    """Utilisation des points de fidélité pour éponger la dette d'un client."""

    points = serializers.IntegerField(min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


# =============================================================================
# SUPPLIER PRODUCT SERIALIZERS
# =============================================================================

class SupplierProductSerializer(serializers.ModelSerializer):
    """Serializer pour les produits fournisseur."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    
    class Meta:
        model = SupplierProduct
        fields = [
            'id', 'product', 'product_name', 'product_sku',
            'supplier_code', 'supplier_name', 'unit_price',
            'min_order_quantity', 'lead_time_days',
            'is_preferred', 'is_active'
        ]
        read_only_fields = ['id']


# =============================================================================
# SUPPLIER SERIALIZERS
# =============================================================================

class SupplierListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de fournisseurs."""
    
    class Meta:
        model = Supplier
        fields = [
            'id', 'code', 'name', 'company_name',
            'contact_person', 'email', 'phone',
            'website', 'address', 'tax_id',
            'currency', 'current_balance',
            'bank_name', 'bank_account',
            'is_active'
        ]
        read_only_fields = ['id', 'code']


class SupplierDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un fournisseur."""
    
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    products = SupplierProductSerializer(many=True, read_only=True)
    total_purchases = serializers.SerializerMethodField()
    recent_orders = serializers.SerializerMethodField()
    
    class Meta:
        model = Supplier
        fields = [
            'id', 'code', 'name', 'company_name',
            'contact_person', 'email', 'phone', 'website',
            'address', 'tax_id',
            'currency', 'current_balance',
            'bank_name', 'bank_account',
            'is_active',
            'created_by', 'created_by_name',
            'products', 'total_purchases', 'recent_orders',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'code', 'created_at', 'updated_at']

    def get_total_purchases(self, obj):
        return str(obj.get_total_purchases())

    def get_recent_orders(self, obj):
        """Retourne les 5 dernières commandes."""
        orders = obj.purchase_orders.filter(
            status__in=['received', 'partially_received', 'sent', 'confirmed']
        ).order_by('-order_date')[:5]
        
        return [
            {
                'id': str(order.id),
                'reference': order.reference,
                'total': str(order.total),
                'status': order.status,
                'date': order.order_date
            }
            for order in orders
        ]


class SupplierCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de fournisseur."""
    
    class Meta:
        model = Supplier
        fields = [
            'id', 'code', 'name', 'company_name', 'contact_person',
            'email', 'phone', 'website',
            'address', 'tax_id',
            'currency',
            'bank_name', 'bank_account',
            'is_active'
        ]
        read_only_fields = ['id', 'code']
        extra_kwargs = {
            'name': {'required': True, 'allow_blank': False},
            'phone': {'required': True, 'allow_blank': False},
        }

    def create(self, validated_data):
        org = validated_data.get('organization')
        
        from apps.core.utils import ReferenceGenerator
        validated_data['code'] = ReferenceGenerator.generate_supplier_code(org)
        
        return Supplier.objects.create(**validated_data)


class SupplierUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour la mise à jour de fournisseur."""
    
    class Meta:
        model = Supplier
        fields = [
            'name', 'company_name', 'contact_person',
            'email', 'phone', 'website',
            'address', 'tax_id',
            'currency',
            'bank_name', 'bank_account',
            'is_active'
        ]
        extra_kwargs = {
            'name': {'required': True, 'allow_blank': False},
            'phone': {'required': True, 'allow_blank': False},
        }
