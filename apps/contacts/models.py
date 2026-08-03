from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal
from apps.core.models import TenantModel, TenantSoftDeleteModel, TenantSyncableModel
from apps.core.managers import TenantSoftDeleteManager


class Customer(TenantSyncableModel):
    """
    Customer model.
    Particulier: customer_type, name, phone, email, address
    Entreprise: customer_type, name, company_name, phone, email, address, tax_id
    """
    
    class CustomerType(models.TextChoices):
        INDIVIDUAL = 'individual', 'Particulier'
        BUSINESS = 'business', 'Entreprise'

    code = models.CharField(max_length=50, db_index=True)
    
    customer_type = models.CharField(
        max_length=20,
        choices=CustomerType.choices,
        default=CustomerType.INDIVIDUAL
    )
    
    name = models.CharField(max_length=255)
    company_name = models.CharField(max_length=255, blank=True)
    
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    
    address = models.TextField(blank=True)
    
    tax_id = models.CharField(max_length=50, blank=True)
    
    credit_limit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    current_balance = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    notes = models.TextField(blank=True)
    
    is_active = models.BooleanField(default=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_customers'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'customers'
        ordering = ['name']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
            models.Index(fields=['code']),
            models.Index(fields=['phone']),
            # Dashboard « nouveaux clients » : filtre org + created_at sur une plage.
            models.Index(
                fields=['organization', 'created_at'],
                name='customer_org_created_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                condition=models.Q(is_deleted=False),
                name='unique_customer_code_per_org'
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def available_credit(self):
        return self.credit_limit - self.current_balance

    def get_total_purchases(self):
        return self.sales.filter(
            status='completed'
        ).aggregate(
            total=models.Sum('total')
        )['total'] or Decimal('0.00')


class CustomerTransaction(TenantModel):
    """
    Tracks all financial movements for a customer:
    - credit_sale: dette ajoutée via vente à crédit
    - payment: paiement reçu du client (réduit la dette)
    - advance: avance/acompte versé par le client
    - adjustment: ajustement manuel du solde
    - refund: remboursement au client
    """
    
    class TransactionType(models.TextChoices):
        CREDIT_SALE = 'credit_sale', 'Vente à crédit'
        PAYMENT = 'payment', 'Paiement'
        ADVANCE = 'advance', 'Avance / Acompte'
        ADJUSTMENT = 'adjustment', 'Ajustement'
        REFUND = 'refund', 'Remboursement'

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    
    transaction_type = models.CharField(
        max_length=20,
        choices=TransactionType.choices
    )
    
    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    
    balance_before = models.DecimalField(
        max_digits=15,
        decimal_places=2
    )
    balance_after = models.DecimalField(
        max_digits=15,
        decimal_places=2
    )
    
    reference = models.CharField(max_length=100, blank=True)
    
    sale = models.ForeignKey(
        'sales.Sale',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_transactions'
    )
    
    payment_method = models.CharField(max_length=50, blank=True)
    
    notes = models.TextField(blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='customer_transactions'
    )

    class Meta:
        db_table = 'customer_transactions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'transaction_type']),
            models.Index(fields=['customer', 'created_at']),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.amount} ({self.customer.name})"


class Supplier(TenantSyncableModel):
    """
    Supplier/vendor model.
    """
    
    code = models.CharField(max_length=50, db_index=True)
    
    name = models.CharField(max_length=255)
    company_name = models.CharField(max_length=255, blank=True)
    
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    
    address = models.TextField(blank=True)
    
    tax_id = models.CharField(max_length=50, blank=True)
    
    currency = models.CharField(max_length=3, default='USD')
    
    current_balance = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    bank_name = models.CharField(max_length=255, blank=True)
    bank_account = models.CharField(max_length=100, blank=True)
    
    is_active = models.BooleanField(default=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_suppliers'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'suppliers'
        ordering = ['name']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
            models.Index(fields=['code']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                condition=models.Q(is_deleted=False),
                name='unique_supplier_code_per_org'
            ),
        ]

    def __str__(self):
        return self.name

    def get_total_purchases(self):
        return self.purchase_orders.filter(
            status__in=['received', 'partially_received']
        ).aggregate(
            total=models.Sum('total')
        )['total'] or Decimal('0.00')


class SupplierProduct(TenantModel):
    """
    Products supplied by specific suppliers.
    Tracks supplier-specific pricing and codes.
    """
    
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.CASCADE,
        related_name='products'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='suppliers'
    )
    
    supplier_code = models.CharField(max_length=100, blank=True)
    supplier_name = models.CharField(max_length=255, blank=True)
    
    unit_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    min_order_quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal('1.000')
    )
    
    lead_time_days = models.PositiveIntegerField(default=0)
    
    is_preferred = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'supplier_products'
        unique_together = ['supplier', 'product']

    def __str__(self):
        return f"{self.supplier.name} - {self.product.name}"
