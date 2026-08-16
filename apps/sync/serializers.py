"""
Flat serializers for WatermelonDB sync.

These serializers are optimized for sync:
- No nested objects (flat structure)
- Foreign keys serialized as UUID strings
- Decimal fields serialized as strings
- Timestamps in ISO format
"""
from rest_framework import serializers
from apps.products.models import Category, Brand, Unit, Product, ProductVariant
from apps.contacts.models import Customer, Supplier
from apps.inventory.models import Warehouse, StockMovement
from apps.sales.models import PaymentMethod, Sale, SaleItem, Payment


class BaseSyncSerializer(serializers.ModelSerializer):
    """Base serializer for sync with common field handling."""
    
    def to_representation(self, instance):
        """Convert model instance to sync-compatible dict."""
        data = super().to_representation(instance)
        
        # Convert UUID fields to strings
        if 'id' in data and data['id']:
            data['id'] = str(data['id'])
        if 'organization' in data and data['organization']:
            data['organization'] = str(data['organization'])
            
        return data


class CategorySyncSerializer(BaseSyncSerializer):
    """Flat serializer for Category model."""
    
    parent_id = serializers.UUIDField(source='parent.id', allow_null=True, read_only=True)
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'slug', 'description', 'parent_id',
            'sort_order', 'is_active', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class BrandSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Brand model."""
    
    class Meta:
        model = Brand
        fields = [
            'id', 'name', 'slug', 'is_active', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class UnitSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Unit model."""
    
    base_unit_id = serializers.UUIDField(source='base_unit.id', allow_null=True, read_only=True)
    conversion_factor = serializers.DecimalField(max_digits=10, decimal_places=4, coerce_to_string=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'symbol', 'base_unit_id', 'conversion_factor',
            'created_at', 'updated_at',
        ]


class WarehouseSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Warehouse model."""
    
    branch_id = serializers.UUIDField(source='branch.id', allow_null=True, read_only=True)
    manager_id = serializers.UUIDField(source='manager.id', allow_null=True, read_only=True)
    
    class Meta:
        model = Warehouse
        fields = [
            'id', 'name', 'code', 'branch_id', 'address', 'manager_id',
            'is_default', 'is_active', 'allow_negative_stock', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class PaymentMethodSyncSerializer(BaseSyncSerializer):
    """Flat serializer for PaymentMethod model."""
    
    class Meta:
        model = PaymentMethod
        fields = [
            'id', 'name', 'code', 'method_type',
            'is_active', 'is_default', 'requires_reference', 'icon',
            'created_at', 'updated_at',
        ]


class ProductSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Product model."""
    
    category_id = serializers.UUIDField(source='category.id', allow_null=True, read_only=True)
    brand_id = serializers.UUIDField(source='brand.id', allow_null=True, read_only=True)
    unit_id = serializers.UUIDField(source='unit.id', allow_null=True, read_only=True)
    
    cost_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    selling_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    wholesale_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True, allow_null=True)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, coerce_to_string=True)
    
    class Meta:
        model = Product
        fields = [
            'id', 'name', 'slug', 'sku', 'barcode', 'short_description',
            'category_id', 'brand_id', 'unit_id',
            # Conditionnement : exposé dès maintenant pour que le mobile puisse
            # reconnaître ces produits et refuser de les vendre tant qu'il ne
            # sait pas les traiter. WatermelonDB ignore les colonnes inconnues.
            'selling_mode', 'units_per_package',
            'cost_price', 'selling_price', 'wholesale_price',
            'tax_rate', 'is_taxable',
            'track_inventory', 'allow_negative_stock', 'has_expiry_date',
            'min_stock_level', 'max_stock_level', 'reorder_point', 'reorder_quantity',
            'is_active', 'is_featured', 'is_sellable', 'is_purchasable',
            'is_deleted', 'created_at', 'updated_at', 'deleted_at',
        ]


class ProductVariantSyncSerializer(BaseSyncSerializer):
    """Flat serializer for ProductVariant model."""
    
    product_id = serializers.UUIDField(source='product.id', read_only=True)
    cost_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True, allow_null=True)
    selling_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True, allow_null=True)
    
    class Meta:
        model = ProductVariant
        fields = [
            'id', 'product_id', 'name', 'sku', 'barcode',
            'cost_price', 'selling_price', 'attributes',
            'is_active', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class CustomerSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Customer model."""
    
    credit_limit = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    current_balance = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    
    class Meta:
        model = Customer
        fields = [
            'id', 'code', 'customer_type', 'name', 'company_name',
            'email', 'phone', 'address', 'tax_id',
            'credit_limit', 'current_balance', 'notes',
            'is_active', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class SupplierSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Supplier model."""
    
    current_balance = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    
    class Meta:
        model = Supplier
        fields = [
            'id', 'code', 'name', 'company_name', 'contact_person',
            'email', 'phone', 'website', 'address', 'tax_id',
            'currency', 'current_balance',
            'bank_name', 'bank_account', 'notes',
            'is_active', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class SaleSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Sale model."""
    
    session_id = serializers.UUIDField(source='session.id', allow_null=True, read_only=True)
    register_id = serializers.UUIDField(source='register.id', allow_null=True, read_only=True)
    warehouse_id = serializers.UUIDField(source='warehouse.id', allow_null=True, read_only=True)
    customer_id = serializers.UUIDField(source='customer.id', allow_null=True, read_only=True)
    sold_by_id = serializers.UUIDField(source='sold_by.id', allow_null=True, read_only=True)
    
    subtotal = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    tax_amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    discount_amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    discount_percentage = serializers.DecimalField(max_digits=5, decimal_places=2, coerce_to_string=True)
    total = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    amount_paid = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    amount_due = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    change_amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    exchange_rate = serializers.DecimalField(max_digits=15, decimal_places=6, coerce_to_string=True)

    class Meta:
        model = Sale
        fields = [
            'id', 'reference', 'session_id', 'register_id', 'warehouse_id', 'customer_id',
            'sale_type', 'status',
            'subtotal', 'tax_amount', 'discount_amount', 'discount_percentage',
            'total', 'amount_paid', 'amount_due', 'change_amount', 'change_currency',
            'currency', 'exchange_rate', 'notes',
            'sold_by_id', 'sale_date', 'due_date',
            'is_pos', 'receipt_printed', 'is_deleted',
            'created_at', 'updated_at', 'deleted_at',
        ]


class SaleItemSyncSerializer(BaseSyncSerializer):
    """Flat serializer for SaleItem model."""
    
    sale_id = serializers.UUIDField(source='sale.id', read_only=True)
    product_id = serializers.UUIDField(source='product.id', read_only=True)
    variant_id = serializers.UUIDField(source='variant.id', allow_null=True, read_only=True)
    batch_id = serializers.UUIDField(source='batch.id', allow_null=True, read_only=True)
    
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, coerce_to_string=True)
    unit_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    cost_price = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    discount_amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    discount_percentage = serializers.DecimalField(max_digits=5, decimal_places=2, coerce_to_string=True)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, coerce_to_string=True)
    tax_amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    subtotal = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    total = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    
    class Meta:
        model = SaleItem
        fields = [
            'id', 'sale_id', 'product_id', 'variant_id', 'batch_id',
            'description', 'quantity', 'unit_price', 'cost_price',
            'discount_amount', 'discount_percentage',
            'tax_rate', 'tax_amount', 'subtotal', 'total', 'notes',
            'created_at', 'updated_at',
        ]


class PaymentSyncSerializer(BaseSyncSerializer):
    """Flat serializer for Payment model."""
    
    sale_id = serializers.UUIDField(source='sale.id', read_only=True)
    payment_method_id = serializers.UUIDField(source='payment_method.id', allow_null=True, read_only=True)
    received_by_id = serializers.UUIDField(source='received_by.id', allow_null=True, read_only=True)
    
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    tendered_amount = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True, allow_null=True)
    exchange_rate = serializers.DecimalField(max_digits=15, decimal_places=6, coerce_to_string=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'sale_id', 'payment_method_id', 'amount', 'tendered_amount',
            'currency', 'exchange_rate', 'reference', 'status',
            'received_by_id', 'paid_at', 'notes',
            'created_at', 'updated_at',
        ]


class StockMovementSyncSerializer(BaseSyncSerializer):
    """Flat serializer for StockMovement model."""
    
    product_id = serializers.UUIDField(source='product.id', read_only=True)
    variant_id = serializers.UUIDField(source='variant.id', allow_null=True, read_only=True)
    warehouse_id = serializers.UUIDField(source='warehouse.id', read_only=True)
    batch_id = serializers.UUIDField(source='batch.id', allow_null=True, read_only=True)
    created_by_id = serializers.UUIDField(source='created_by.id', allow_null=True, read_only=True)
    
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, coerce_to_string=True)
    unit_cost = serializers.DecimalField(max_digits=15, decimal_places=2, coerce_to_string=True)
    quantity_before = serializers.DecimalField(max_digits=15, decimal_places=3, coerce_to_string=True)
    quantity_after = serializers.DecimalField(max_digits=15, decimal_places=3, coerce_to_string=True)
    
    class Meta:
        model = StockMovement
        fields = [
            'id', 'product_id', 'variant_id', 'warehouse_id', 'batch_id',
            'movement_type', 'quantity', 'unit_cost',
            'quantity_before', 'quantity_after',
            'reference_type', 'reference_id', 'notes',
            'created_by_id', 'created_at', 'updated_at',
        ]


# Map table names to serializer classes
SYNC_SERIALIZERS = {
    'categories': CategorySyncSerializer,
    'brands': BrandSyncSerializer,
    'units': UnitSyncSerializer,
    'warehouses': WarehouseSyncSerializer,
    'payment_methods': PaymentMethodSyncSerializer,
    'products': ProductSyncSerializer,
    'product_variants': ProductVariantSyncSerializer,
    'customers': CustomerSyncSerializer,
    'suppliers': SupplierSyncSerializer,
    'sales': SaleSyncSerializer,
    'sale_items': SaleItemSyncSerializer,
    'payments': PaymentSyncSerializer,
    'stock_movements': StockMovementSyncSerializer,
}


def get_serializer_for_table(table_name):
    """Get the serializer class for a given table name."""
    return SYNC_SERIALIZERS.get(table_name)
