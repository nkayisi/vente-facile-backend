from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal
from apps.core.models import TenantSoftDeleteModel, TenantModel
from apps.core.managers import TenantSoftDeleteManager


class Category(TenantSoftDeleteModel):
    """Product categories with hierarchical support."""
    
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True)
    
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children'
    )
    
    image = models.ImageField(upload_to='categories/', null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'categories'
        verbose_name_plural = 'categories'
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
            models.Index(fields=['parent']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'slug'],
                condition=models.Q(is_deleted=False),
                name='unique_category_slug_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'name'],
                condition=models.Q(is_deleted=False),
                name='unique_category_name_per_org'
            ),
        ]

    def __str__(self):
        return self.name

    def get_ancestors(self):
        """Get all parent categories."""
        ancestors = []
        current = self.parent
        while current:
            ancestors.append(current)
            current = current.parent
        return ancestors[::-1]


class Brand(TenantSoftDeleteModel):
    """Product brands/manufacturers."""
    
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    logo = models.ImageField(upload_to='brands/', null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'brands'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'slug'],
                condition=models.Q(is_deleted=False),
                name='unique_brand_slug_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'name'],
                condition=models.Q(is_deleted=False),
                name='unique_brand_name_per_org'
            ),
        ]

    def __str__(self):
        return self.name


class Unit(TenantModel):
    """Units of measurement for products."""
    
    name = models.CharField(max_length=50)
    symbol = models.CharField(max_length=10)
    
    base_unit = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_units'
    )
    conversion_factor = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('1.0000')
    )

    class Meta:
        db_table = 'units'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'symbol'],
                name='unique_unit_symbol_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'name'],
                name='unique_unit_name_per_org'
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class Product(TenantSoftDeleteModel):
    """
    Main product model.
    Supports variants, multiple prices, and inventory tracking.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    sku = models.CharField(max_length=100, db_index=True)
    barcode = models.CharField(max_length=100, blank=True, db_index=True)
    
    short_description = models.CharField(max_length=500, blank=True)
    
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )
    
    cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    selling_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    wholesale_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    is_taxable = models.BooleanField(default=True)
    
    track_inventory = models.BooleanField(default=True)
    allow_negative_stock = models.BooleanField(default=False)
    min_stock_level = models.PositiveIntegerField(default=0)
    max_stock_level = models.PositiveIntegerField(null=True, blank=True)
    reorder_point = models.PositiveIntegerField(default=0)
    reorder_quantity = models.PositiveIntegerField(default=0)
    
    weight = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True
    )
    dimensions = models.JSONField(default=dict, blank=True)
    
    image = models.ImageField(upload_to='products/', null=True, blank=True)
    
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    is_sellable = models.BooleanField(default=True)
    is_purchasable = models.BooleanField(default=True)
    
    expiry_tracking = models.BooleanField(default=False)
    batch_tracking = models.BooleanField(default=False)
    serial_tracking = models.BooleanField(default=False)
    
    attributes = models.JSONField(default=dict, blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_products'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'products'
        ordering = ['name']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
            models.Index(fields=['organization', 'category']),
            models.Index(fields=['sku']),
            models.Index(fields=['barcode']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'sku'],
                condition=models.Q(is_deleted=False),
                name='unique_product_sku_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'slug'],
                condition=models.Q(is_deleted=False),
                name='unique_product_slug_per_org'
            ),
        ]
        permissions = [
            ('view_cost_price', 'Can view product cost price'),
            ('change_price', 'Can change product prices'),
            ('export_products', 'Can export products'),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def profit_margin(self):
        """Calculate profit margin percentage."""
        if self.cost_price and self.cost_price > 0:
            return ((self.selling_price - self.cost_price) / self.cost_price) * 100
        return Decimal('0.00')

    def get_price_with_tax(self):
        """Get selling price including tax."""
        if self.is_taxable:
            return self.selling_price * (1 + self.tax_rate / 100)
        return self.selling_price


class ProductImage(TenantModel):
    """Additional product images."""
    
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='images'
    )
    image = models.ImageField(upload_to='products/gallery/')
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = 'product_images'
        ordering = ['sort_order']

    def __str__(self):
        return f"Image for {self.product.name}"


class ProductVariant(TenantSoftDeleteModel):
    """
    Product variants (e.g., size, color combinations).
    Each variant can have its own SKU, barcode, and price.
    """
    
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='variants'
    )
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100)
    barcode = models.CharField(max_length=100, blank=True)
    
    cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    selling_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    
    attributes = models.JSONField(default=dict)
    image = models.ImageField(upload_to='products/variants/', null=True, blank=True)
    
    is_active = models.BooleanField(default=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'product_variants'
        indexes = [
            models.Index(fields=['product']),
            models.Index(fields=['sku']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'sku'],
                condition=models.Q(is_deleted=False),
                name='unique_variant_sku_per_org'
            ),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.name}"

    def get_price(self):
        """Get variant price or fallback to product price."""
        return self.selling_price or self.product.selling_price


class PriceList(TenantModel):
    """
    Multiple price lists for different customer types.
    E.g., retail, wholesale, VIP customers.
    """
    
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    description = models.TextField(blank=True)
    
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )

    class Meta:
        db_table = 'price_lists'
        unique_together = ['organization', 'code']

    def __str__(self):
        return self.name


class ProductPrice(TenantModel):
    """Product-specific prices for different price lists."""
    
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='prices'
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='prices'
    )
    price_list = models.ForeignKey(
        PriceList,
        on_delete=models.CASCADE,
        related_name='product_prices'
    )
    
    price = models.DecimalField(max_digits=15, decimal_places=2)
    min_quantity = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = 'product_prices'
        unique_together = ['product', 'variant', 'price_list', 'min_quantity']

    def __str__(self):
        return f"{self.product.name} - {self.price_list.name}: {self.price}"
