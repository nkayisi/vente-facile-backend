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

    reference = models.CharField(max_length=50, unique=True)
    
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

    reference = models.CharField(max_length=50, unique=True)
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
