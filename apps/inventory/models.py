from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal
from apps.core.models import TenantModel, TenantSoftDeleteModel
from apps.core.managers import TenantSoftDeleteManager


class Warehouse(TenantSoftDeleteModel):
    """
    Storage locations for inventory.
    Can be linked to branches or standalone.
    """
    
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    
    branch = models.ForeignKey(
        'organizations.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='warehouses'
    )
    
    address = models.TextField(blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_warehouses'
    )
    
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    allow_negative_stock = models.BooleanField(default=False)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'warehouses'
        indexes = [
            models.Index(fields=['organization', 'is_active']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                condition=models.Q(is_deleted=False),
                name='unique_warehouse_code_per_org'
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class StockLocation(TenantModel):
    """
    Specific locations within a warehouse.
    E.g., Aisle A, Shelf 1, Bin 3.
    """
    
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='locations'
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children'
    )
    
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'stock_locations'
        unique_together = ['warehouse', 'code']

    def __str__(self):
        return f"{self.warehouse.name} - {self.name}"


class Stock(TenantModel):
    """
    Current stock levels per product/variant/warehouse.
    This is the source of truth for inventory.
    """
    
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='stocks'
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='stocks'
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='stocks'
    )
    location = models.ForeignKey(
        StockLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stocks'
    )
    
    quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    reserved_quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    
    avg_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    last_counted_at = models.DateTimeField(null=True, blank=True)
    last_movement_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'stocks'
        unique_together = ['product', 'variant', 'warehouse', 'location']
        indexes = [
            models.Index(fields=['organization', 'product']),
            models.Index(fields=['warehouse']),
        ]

    def __str__(self):
        return f"{self.product.name} @ {self.warehouse.name}: {self.quantity}"

    @property
    def available_quantity(self):
        """Quantity available for sale (excluding reserved)."""
        return self.quantity - self.reserved_quantity


class StockBatch(TenantModel):
    """
    Batch/lot tracking for products.
    Tracks expiry dates and batch numbers.
    """
    
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='batches'
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='batches'
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='batches'
    )
    location = models.ForeignKey(
        StockLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='batches'
    )
    
    batch_number = models.CharField(max_length=100)
    
    quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    
    cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    manufacturing_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    
    received_at = models.DateTimeField(auto_now_add=True)
    
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'stock_batches'
        unique_together = ['product', 'variant', 'warehouse', 'batch_number']
        indexes = [
            models.Index(fields=['expiry_date']),
            models.Index(fields=['batch_number']),
        ]

    def __str__(self):
        return f"{self.product.name} - Batch {self.batch_number}"

    @property
    def is_expired(self):
        from django.utils import timezone
        if self.expiry_date:
            return self.expiry_date < timezone.now().date()
        return False


class StockMovement(TenantModel):
    """
    Records all stock movements for audit trail.
    Every change in stock creates a movement record.
    """
    
    class MovementType(models.TextChoices):
        PURCHASE = 'purchase', 'Achat'
        SALE = 'sale', 'Vente'
        RETURN_IN = 'return_in', 'Retour client'
        RETURN_OUT = 'return_out', 'Retour fournisseur'
        TRANSFER_IN = 'transfer_in', 'Transfert entrant'
        TRANSFER_OUT = 'transfer_out', 'Transfert sortant'
        ADJUSTMENT_IN = 'adjustment_in', 'Ajustement positif'
        ADJUSTMENT_OUT = 'adjustment_out', 'Ajustement négatif'
        DAMAGE = 'damage', 'Dommage/Perte'
        EXPIRED = 'expired', 'Périmé'
        INITIAL = 'initial', 'Stock initial'
        PRODUCTION_IN = 'production_in', 'Production entrante'
        PRODUCTION_OUT = 'production_out', 'Production sortante'

    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='movements'
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='movements'
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='movements'
    )
    batch = models.ForeignKey(
        StockBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='movements'
    )
    
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    unit_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    quantity_before = models.DecimalField(max_digits=15, decimal_places=3)
    quantity_after = models.DecimalField(max_digits=15, decimal_places=3)
    
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    
    notes = models.TextField(blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='stock_movements'
    )

    class Meta:
        db_table = 'stock_movements'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'product']),
            models.Index(fields=['movement_type']),
            models.Index(fields=['created_at']),
            models.Index(fields=['reference_type', 'reference_id']),
        ]

    def __str__(self):
        return f"{self.movement_type} - {self.product.name}: {self.quantity}"


class StockTransfer(TenantSoftDeleteModel):
    """
    Transfer stock between warehouses.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        IN_TRANSIT = 'in_transit', 'En transit'
        COMPLETED = 'completed', 'Terminé'
        CANCELLED = 'cancelled', 'Annulé'

    reference = models.CharField(max_length=50)
    
    source_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='outgoing_transfers'
    )
    destination_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='incoming_transfers'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    notes = models.TextField(blank=True)
    
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='requested_transfers'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_transfers'
    )
    
    requested_at = models.DateTimeField(auto_now_add=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'stock_transfers'
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['organization', 'reference'], name='unique_transfer_reference_per_org'),
        ]

    def __str__(self):
        return f"Transfer {self.reference}"


class StockTransferItem(TenantModel):
    """Items in a stock transfer."""
    
    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    batch = models.ForeignKey(
        StockBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    quantity_requested = models.DecimalField(max_digits=15, decimal_places=3)
    quantity_shipped = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        null=True,
        blank=True
    )
    quantity_received = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        null=True,
        blank=True
    )
    
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'stock_transfer_items'

    def __str__(self):
        return f"{self.product.name} x {self.quantity_requested}"


class StockAdjustment(TenantSoftDeleteModel):
    """
    Stock adjustments for corrections, damages, etc.
    """
    
    class AdjustmentType(models.TextChoices):
        COUNT = 'count', 'Inventaire'
        DAMAGE = 'damage', 'Dommage'
        THEFT = 'theft', 'Vol'
        EXPIRED = 'expired', 'Périmé'
        CORRECTION = 'correction', 'Correction'
        OTHER = 'other', 'Autre'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        APPROVED = 'approved', 'Approuvé'
        REJECTED = 'rejected', 'Rejeté'

    reference = models.CharField(max_length=50)
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='adjustments'
    )
    
    adjustment_type = models.CharField(
        max_length=20,
        choices=AdjustmentType.choices
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    reason = models.TextField(blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_adjustments'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_adjustments'
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'stock_adjustments'
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['organization', 'reference'], name='unique_adjustment_reference_per_org'),
        ]

    def __str__(self):
        return f"Adjustment {self.reference}"


class StockAdjustmentItem(TenantModel):
    """Items in a stock adjustment."""
    
    adjustment = models.ForeignKey(
        StockAdjustment,
        on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    batch = models.ForeignKey(
        StockBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    quantity_counted = models.DecimalField(max_digits=15, decimal_places=3)
    quantity_expected = models.DecimalField(max_digits=15, decimal_places=3)
    quantity_difference = models.DecimalField(max_digits=15, decimal_places=3)
    
    unit_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'stock_adjustment_items'

    def __str__(self):
        return f"{self.product.name}: {self.quantity_difference:+}"


# =============================================================================
# INVENTORY SESSION & COUNT MODELS
# =============================================================================

class InventorySession(TenantSoftDeleteModel):
    """
    Represents a full inventory count session.
    
    An inventory session can be scoped to:
    - A specific warehouse (required)
    - Specific categories (optional filter)
    - Specific products (optional filter)
    - Full warehouse inventory (no category/product filter)
    
    When a session is started (status=in_progress), the stock of the
    targeted products in the warehouse is locked (movements blocked)
    until the session is completed or cancelled.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        IN_PROGRESS = 'in_progress', 'En cours'
        REVIEW = 'review', 'En révision'
        VALIDATED = 'validated', 'Validé'
        CANCELLED = 'cancelled', 'Annulé'

    class ScopeType(models.TextChoices):
        FULL = 'full', 'Inventaire complet'
        CATEGORY = 'category', 'Par catégorie'
        PRODUCT = 'product', 'Par produit'

    reference = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='inventory_sessions'
    )
    
    scope_type = models.CharField(
        max_length=20,
        choices=ScopeType.choices,
        default=ScopeType.FULL
    )
    
    # Optional scope filters
    categories = models.ManyToManyField(
        'products.Category',
        blank=True,
        related_name='inventory_sessions'
    )
    products = models.ManyToManyField(
        'products.Product',
        blank=True,
        related_name='inventory_sessions'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    notes = models.TextField(blank=True)
    
    # Stock locking: when True, stock movements for targeted products are blocked
    is_stock_locked = models.BooleanField(default=False)
    
    # Dates
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    
    # Users
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_inventory_sessions'
    )
    validated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='validated_inventory_sessions'
    )
    
    # Summary fields (computed on validation)
    total_expected_quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal('0.000')
    )
    total_counted_quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal('0.000')
    )
    total_difference_quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal('0.000')
    )
    total_difference_value = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'inventory_sessions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
            models.Index(fields=['warehouse', 'status']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['organization', 'reference'], name='unique_inventory_reference_per_org'),
        ]

    def __str__(self):
        return f"{self.reference} - {self.name}"

    @property
    def progress_percentage(self):
        """Percentage of items that have been counted."""
        total = self.counts.count()
        if total == 0:
            return 0
        counted = self.counts.filter(is_counted=True).count()
        return round((counted / total) * 100, 1)

    @property
    def items_total(self):
        return self.counts.count()

    @property
    def items_counted(self):
        return self.counts.filter(is_counted=True).count()

    @property
    def items_with_difference(self):
        return self.counts.filter(is_counted=True).exclude(quantity_difference=Decimal('0.000')).count()

    def get_locked_product_ids(self):
        """
        Retourne les IDs des produits bloqués par cette session d'inventaire.
        """
        if not self.is_stock_locked:
            return set()
        
        product_ids = set()
        
        if self.scope_type == self.ScopeType.FULL:
            # Tous les produits de l'entrepôt avec du stock
            from apps.inventory.models import Stock
            product_ids = set(
                Stock.objects.filter(
                    organization=self.organization,
                    warehouse=self.warehouse
                ).values_list('product_id', flat=True)
            )
        elif self.scope_type == self.ScopeType.CATEGORY:
            # Produits des catégories sélectionnées
            from apps.products.models import Product
            category_ids = list(self.categories.values_list('id', flat=True))
            if category_ids:
                product_ids = set(
                    Product.objects.filter(
                        organization=self.organization,
                        category_id__in=category_ids,
                        is_deleted=False
                    ).values_list('id', flat=True)
                )
        elif self.scope_type == self.ScopeType.PRODUCT:
            # Produits spécifiquement sélectionnés
            product_ids = set(self.products.values_list('id', flat=True))
        
        return product_ids

    @classmethod
    def get_all_locked_product_ids(cls, organization):
        """
        Retourne les IDs de tous les produits bloqués par des inventaires en cours
        pour une organisation donnée.
        """
        locked_sessions = cls.objects.filter(
            organization=organization,
            is_stock_locked=True,
            status__in=[cls.Status.IN_PROGRESS, cls.Status.REVIEW],
            is_deleted=False
        )
        
        all_locked_ids = set()
        for session in locked_sessions:
            all_locked_ids.update(session.get_locked_product_ids())
        
        return all_locked_ids

    @classmethod
    def get_locking_sessions_for_products(cls, organization, product_ids):
        """
        Retourne les sessions d'inventaire qui bloquent les produits donnés.
        """
        if not product_ids:
            return []
        
        product_ids = set(product_ids)
        locking_sessions = []
        
        locked_sessions = cls.objects.filter(
            organization=organization,
            is_stock_locked=True,
            status__in=[cls.Status.IN_PROGRESS, cls.Status.REVIEW],
            is_deleted=False
        ).select_related('warehouse')
        
        for session in locked_sessions:
            session_locked_ids = session.get_locked_product_ids()
            if session_locked_ids & product_ids:  # Intersection
                locking_sessions.append(session)
        
        return locking_sessions


class InventoryCount(TenantModel):
    """
    Individual product count within an inventory session.
    
    Each row represents one product (optionally one variant) to be counted.
    The expected quantity is captured at session start (snapshot of current stock).
    The counted quantity is entered by the user during the count.
    """
    
    session = models.ForeignKey(
        InventorySession,
        on_delete=models.CASCADE,
        related_name='counts'
    )
    
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='inventory_counts'
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='inventory_counts'
    )
    
    # Stock snapshot at session start
    quantity_expected = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal('0.000')
    )
    
    # User-entered count
    quantity_counted = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal('0.000')
    )
    
    # Computed difference (counted - expected)
    quantity_difference = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal('0.000')
    )
    
    # Cost for value calculation
    unit_cost = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    
    # Difference in value
    difference_value = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    
    is_counted = models.BooleanField(default=False)
    
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_counts'
    )
    counted_at = models.DateTimeField(null=True, blank=True)
    
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_counts'
        ordering = ['product__name']
        unique_together = ['session', 'product', 'variant']
        indexes = [
            models.Index(fields=['session', 'is_counted']),
        ]

    def __str__(self):
        name = self.product.name
        if self.variant:
            name += f" ({self.variant.name})"
        return f"{name}: attendu={self.quantity_expected}, compté={self.quantity_counted}"

    def save(self, *args, **kwargs):
        if self.is_counted:
            self.quantity_difference = self.quantity_counted - self.quantity_expected
            self.difference_value = self.quantity_difference * self.unit_cost
        super().save(*args, **kwargs)
