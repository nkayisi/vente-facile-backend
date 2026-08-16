"""
Constants for WatermelonDB sync.

Defines syncable models and their dependency order.
"""

# Models to sync, in dependency order (parents first)
# Format: (table_name, app_label, model_name, select_related_fields)
SYNCABLE_MODELS = [
    # Phase 1: Reference data (read-only on mobile)
    ('categories', 'products', 'Category', ['parent']),
    ('brands', 'products', 'Brand', []),
    ('units', 'products', 'Unit', ['base_unit']),
    ('warehouses', 'inventory', 'Warehouse', ['branch']),
    ('payment_methods', 'sales', 'PaymentMethod', []),
    
    # Phase 2: Core business data
    ('products', 'products', 'Product', ['category', 'brand', 'unit']),
    ('product_variants', 'products', 'ProductVariant', ['product']),
    ('customers', 'contacts', 'Customer', []),
    ('suppliers', 'contacts', 'Supplier', []),
    
    # Phase 3: Transactions
    ('sales', 'sales', 'Sale', ['customer', 'warehouse']),
    ('sale_items', 'sales', 'SaleItem', ['sale', 'product', 'variant']),
    ('payments', 'sales', 'Payment', ['sale', 'payment_method']),
    ('stock_movements', 'inventory', 'StockMovement', ['product', 'warehouse']),
]

# Map table name to model info for quick lookup
SYNCABLE_MODELS_MAP = {
    table_name: {
        'app_label': app_label,
        'model_name': model_name,
        'select_related': select_related,
    }
    for table_name, app_label, model_name, select_related in SYNCABLE_MODELS
}

# Tables that are read-only on mobile (can only be pulled, not pushed).
# Reflète l'intention documentée ligne 10 ("Phase 1: Reference data (read-only
# on mobile)") : ces référentiels sont gérés depuis le dashboard par
# l'owner/manager - le cashier sur mobile sélectionne dans le catalogue
# existant mais ne peut pas créer/modifier ces lignes.
READ_ONLY_TABLES = {
    'categories',
    'brands',
    'units',
    'warehouses',
    'payment_methods',
}

# Tables that require special handling during push
SPECIAL_PUSH_TABLES = {
    'sales': 'handle_sale_push',
    'sale_items': 'handle_sale_item_push',
    'stock_movements': 'handle_stock_movement_push',
}

# Maximum records per table in a single pull response
MAX_RECORDS_PER_TABLE = 1000

# Default schema version for sync responses.
# 2 : ajout de `selling_mode` / `units_per_package` sur les produits.
SCHEMA_VERSION = 2
