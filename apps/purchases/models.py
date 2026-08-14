from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal
from apps.core.models import TenantModel, TenantSoftDeleteModel
from apps.core.managers import TenantSoftDeleteManager


class PurchaseOrder(TenantSoftDeleteModel):
    """
    Purchase orders to suppliers.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        SENT = 'sent', 'Envoyé'
        CONFIRMED = 'confirmed', 'Confirmé'
        PARTIALLY_RECEIVED = 'partially_received', 'Partiellement reçu'
        RECEIVED = 'received', 'Reçu'
        CANCELLED = 'cancelled', 'Annulé'

    reference = models.CharField(max_length=50, db_index=True)
    
    supplier = models.ForeignKey(
        'contacts.Supplier',
        on_delete=models.CASCADE,
        related_name='purchase_orders'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_orders'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    subtotal = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    tax_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    discount_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    shipping_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    total = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    amount_paid = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    amount_due = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    # Devise de la commande. Défaut VIDE et non 'USD' : résolue dans `save()`
    # vers la devise principale de l'organisation. Un défaut codé en dur, sans
    # rapport avec l'établissement, estampillait toutes les commandes en USD.
    currency = models.CharField(max_length=3, blank=True, default='')
    # Unités de devise principale pour 1 unité de `currency`. 12 décimales pour
    # s'aligner sur OrganizationCurrency : à 4 décimales, un taux réciproque
    # (principale USD, 1 CDF = 0.000434782609) était tronqué à 0.0004.
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000')
    )
    
    order_date = models.DateField()
    expected_date = models.DateField(null=True, blank=True)
    
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_purchase_orders'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_purchase_orders'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'purchase_orders'
        ordering = ['-order_date']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'supplier']),
            models.Index(fields=['reference']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'reference'],
                condition=models.Q(is_deleted=False),
                name='unique_purchase_order_reference_per_org'
            ),
        ]
        permissions = [
            ('approve_purchase_order', 'Can approve purchase orders'),
            ('receive_purchase_order', 'Can receive purchase orders'),
        ]

    def __str__(self):
        return f"PO {self.reference}"

    def save(self, *args, **kwargs):
        """Résout la devise vers celle de l'établissement, quel que soit l'appelant.

        Comme pour `Sale`, la résolution vit dans `save()` et non dans un
        serializer : plusieurs chemins créent ces lignes, et le défaut codé en
        dur les estampillait 'USD' sans rapport avec l'organisation.
        """
        if self.organization_id:
            from apps.settings.services import CurrencyService
            self.currency, self.exchange_rate = CurrencyService.resolve(
                self.organization_id, self.currency, self.exchange_rate
            )
        super().save(*args, **kwargs)


class PurchaseOrderItem(TenantModel):
    """Items in a purchase order."""
    
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='purchase_order_items'
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    description = models.CharField(max_length=500, blank=True)
    
    quantity_ordered = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))]
    )
    quantity_received = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    tax_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    subtotal = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    total = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )

    class Meta:
        db_table = 'purchase_order_items'
        indexes = [
            models.Index(fields=['purchase_order']),
            models.Index(fields=['product']),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity_ordered}"

    @property
    def quantity_pending(self):
        return self.quantity_ordered - self.quantity_received


class GoodsReceipt(TenantSoftDeleteModel):
    """
    Goods receipt note (GRN).
    Records actual receipt of goods from purchase orders.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        COMPLETED = 'completed', 'Terminé'
        CANCELLED = 'cancelled', 'Annulé'

    reference = models.CharField(max_length=50, unique=True)
    
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name='receipts'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.CASCADE,
        related_name='goods_receipts'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    receipt_date = models.DateTimeField()
    
    supplier_invoice = models.CharField(max_length=100, blank=True)
    supplier_delivery_note = models.CharField(max_length=100, blank=True)
    
    notes = models.TextField(blank=True)
    
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='goods_receipts'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'goods_receipts'
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
        ]

    def __str__(self):
        return f"GRN {self.reference}"


class GoodsReceiptItem(TenantModel):
    """Items in a goods receipt."""
    
    goods_receipt = models.ForeignKey(
        GoodsReceipt,
        on_delete=models.CASCADE,
        related_name='items'
    )
    purchase_order_item = models.ForeignKey(
        PurchaseOrderItem,
        on_delete=models.CASCADE,
        related_name='receipt_items'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    quantity_received = models.DecimalField(max_digits=15, decimal_places=3)
    quantity_accepted = models.DecimalField(max_digits=15, decimal_places=3)
    quantity_rejected = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    
    unit_cost = models.DecimalField(max_digits=15, decimal_places=2)
    
    batch_number = models.CharField(max_length=100, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    
    notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)

    class Meta:
        db_table = 'goods_receipt_items'

    def __str__(self):
        return f"{self.product.name} x {self.quantity_received}"


class SupplierPayment(TenantModel):
    """
    Payments to suppliers.
    """
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'En attente'
        COMPLETED = 'completed', 'Terminé'
        CANCELLED = 'cancelled', 'Annulé'

    reference = models.CharField(max_length=50, unique=True)
    
    supplier = models.ForeignKey(
        'contacts.Supplier',
        on_delete=models.CASCADE,
        related_name='payments'
    )
    
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    
    # Devise du règlement fournisseur, résolue dans `save()` (cf. PurchaseOrder).
    currency = models.CharField(max_length=3, blank=True, default='')
    # Unités de devise principale pour 1 unité de `currency` (précision alignée
    # sur OrganizationCurrency).
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000')
    )
    
    payment_method = models.ForeignKey(
        'sales.PaymentMethod',
        on_delete=models.SET_NULL,
        null=True
    )
    
    payment_reference = models.CharField(max_length=100, blank=True)
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    payment_date = models.DateField()
    
    notes = models.TextField(blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='supplier_payments'
    )

    class Meta:
        db_table = 'supplier_payments'
        indexes = [
            models.Index(fields=['organization', 'supplier']),
            models.Index(fields=['reference']),
        ]

    def __str__(self):
        return f"Payment {self.reference} to {self.supplier.name}"

    def save(self, *args, **kwargs):
        """Résout la devise vers celle de l'établissement, quel que soit l'appelant.

        Comme pour `Sale`, la résolution vit dans `save()` et non dans un
        serializer : plusieurs chemins créent ces lignes, et le défaut codé en
        dur les estampillait 'USD' sans rapport avec l'organisation.
        """
        if self.organization_id:
            from apps.settings.services import CurrencyService
            self.currency, self.exchange_rate = CurrencyService.resolve(
                self.organization_id, self.currency, self.exchange_rate
            )
        super().save(*args, **kwargs)


class SupplierPaymentAllocation(TenantModel):
    """
    Allocation of supplier payments to purchase orders.
    """
    
    payment = models.ForeignKey(
        SupplierPayment,
        on_delete=models.CASCADE,
        related_name='allocations'
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name='payment_allocations'
    )
    
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = 'supplier_payment_allocations'
        unique_together = ['payment', 'purchase_order']

    def __str__(self):
        return f"{self.payment.reference} -> {self.purchase_order.reference}"


class PurchaseReturn(TenantSoftDeleteModel):
    """
    Returns to suppliers.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        APPROVED = 'approved', 'Approuvé'
        SHIPPED = 'shipped', 'Expédié'
        COMPLETED = 'completed', 'Terminé'
        CANCELLED = 'cancelled', 'Annulé'

    reference = models.CharField(max_length=50, unique=True)
    
    supplier = models.ForeignKey(
        'contacts.Supplier',
        on_delete=models.CASCADE,
        related_name='returns'
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='returns'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.CASCADE,
        related_name='purchase_returns'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    total_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    reason = models.TextField()
    
    return_date = models.DateField()
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='purchase_returns'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'purchase_returns'
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
        ]

    def __str__(self):
        return f"Return {self.reference}"


class PurchaseReturnItem(TenantModel):
    """Items in a purchase return."""
    
    purchase_return = models.ForeignKey(
        PurchaseReturn,
        on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    batch = models.ForeignKey(
        'inventory.StockBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=15, decimal_places=2)
    total = models.DecimalField(max_digits=15, decimal_places=2)
    
    reason = models.TextField(blank=True)

    class Meta:
        db_table = 'purchase_return_items'

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"
