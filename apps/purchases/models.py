from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal
from apps.core.models import TenantModel, TenantSoftDeleteModel
from apps.core.managers import TenantSoftDeleteManager
from apps.core.clock import maintenant


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

    # Commande passée en contenants : « 10 cartons à 12 500 ». `quantity_ordered`
    # (en unité de détail) et `unit_price` restent les valeurs qui font foi pour
    # la réception et le stock ; les champs ci-dessous conservent la saisie du
    # marchand, qui est aussi ce que le fournisseur facture.
    package_quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    loose_quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    package_unit_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    packaging_factor = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Nombre d'unités par contenant au moment de la commande."
    )

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

    # Réception constatée en contenants : « 10 cartons + 3 bouteilles ». Les
    # quantités en unité de détail restent seules à piloter le stock, les lots
    # et le coût moyen pondéré ; ce partage sert à savoir ce qui entre scellé.
    package_quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    loose_quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('0.000')
    )
    package_unit_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Prix d'achat d'un contenant entier. `unit_cost` en est déduit et "
            "reste la valeur utilisée pour le coût moyen pondéré."
        )
    )
    packaging_factor = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Nombre d'unités par contenant au moment de la réception."
    )

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


# ==============================================================================
# RÉQUISITIONS DE PRODUITS
# ==============================================================================

class Requisition(TenantSoftDeleteModel):
    """
    Ce qu'il faut acheter : une demande, avant toute commande.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ CE N'EST PAS UNE COMMANDE D'ACHAT, ET C'EST POURQUOI ELLE EXISTE.       │
    │                                                                          │
    │ `PurchaseOrder.supplier` est OBLIGATOIRE : une commande est passée À    │
    │ quelqu'un. Une réquisition dit « il nous manque ceci », et le marchand   │
    │ ne sait pas encore chez qui il l'achètera - c'est même souvent la        │
    │ question qu'il se pose en la lisant. Rendre `supplier` nullable pour     │
    │ l'y faire entrer affaiblirait le modèle de commande sur une table        │
    │ chaude, et ferait porter deux sens à un même statut `draft`.             │
    │                                                                          │
    │ T12 du retour de test.                                                   │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        SUBMITTED = 'submitted', 'Soumise'
        APPROVED = 'approved', 'Approuvée'
        REJECTED = 'rejected', 'Rejetée'
        CANCELLED = 'cancelled', 'Annulée'

    reference = models.CharField(max_length=50, db_index=True)

    # L'entrepôt POUR LEQUEL on demande : c'est lui qui manque de marchandise,
    # et c'est lui qui borne le périmètre de lecture.
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.PROTECT,
        related_name='requisitions',
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    notes = models.TextField(blank=True)

    requested_by = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='requisitions_demandees',
    )
    decided_by = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='requisitions_decidees',
    )
    decided_at = models.DateTimeField(null=True, blank=True, editable=False)
    decision_notes = models.TextField(blank=True)

    # ⚠ `default=maintenant` et non `auto_now_add` : une réquisition saisie au
    # fond d'un dépôt sans réseau porte l'heure de l'ACTE, que le journal
    # repose au rejeu. `auto_now_add` la daterait du jour de sa poussée.
    requested_at = models.DateTimeField(default=maintenant, editable=False)

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ IL MANQUAIT, ET UNE PIÈCE SUPPRIMÉE EN DOUCEUR RESTAIT VISIBLE.         │
    # │                                                                          │
    # │ `Requisition` hérite de `TenantSoftDeleteModel` et gardait le            │
    # │ `models.Manager` automatique - seule des quatre de ce fichier, ses trois │
    # │ sœurs le déclarant toutes. `GET /requisitions/` rendait donc les lignes  │
    # │ supprimées, leur `retrieve` répondait 200, et `_objet_de_lorg` laissait  │
    # │ une transition du journal agir dessus. Pendant ce temps le TIRAGE les    │
    # │ cachait correctement (`soft_delete=True`) : c'était la seule des trois   │
    # │ surfaces à être juste, et les deux autres disaient le contraire.         │
    # └──────────────────────────────────────────────────────────────────────────┘
    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'requisitions'
        ordering = ['-requested_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'reference'],
                name='unique_requisition_reference_per_org',
            )
        ]
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'warehouse']),
        ]

    def __str__(self):
        return f"{self.reference} ({self.get_status_display()})"


class RequisitionItem(TenantModel):
    """
    Une ligne de réquisition : l'article, et la quantité à acheter.

    ⚠ LE STOCK ET LE SEUIL SONT FIGÉS À LA SAISIE, comme l'instantané d'une
    session d'inventaire. Les relire plus tard donnerait le stock d'aujourd'hui,
    qui a bougé depuis : le document imprimé ne dirait plus pourquoi la demande
    a été faite, et son lecteur ne pourrait pas juger si elle tient encore.
    """

    requisition = models.ForeignKey(
        Requisition, on_delete=models.CASCADE, related_name='items'
    )
    product = models.ForeignKey(
        'products.Product', on_delete=models.PROTECT, related_name='requisition_items'
    )

    quantity_requested = models.DecimalField(max_digits=12, decimal_places=3)

    #: Le stock disponible AU MOMENT de la demande, dans cet entrepôt.
    stock_at_request = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('0')
    )
    #: Le seuil de réassort au moment de la demande.
    reorder_point_at_request = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('0')
    )

    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'requisition_items'
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['requisition', 'product'],
                name='unique_requisition_item_per_product',
            )
        ]

    def __str__(self):
        return f"{self.product_id} x{self.quantity_requested}"
