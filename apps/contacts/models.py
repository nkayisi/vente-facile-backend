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
    
    # Autorisation d'acheter à crédit, indépendante du montant : `credit_limit`
    # à 0 signifie « pas de plafond », pas « pas de crédit ». Les deux règles
    # sont distinctes et se cumulent - un client autorisé peut avoir un plafond,
    # un client non autorisé ne peut rien prendre à crédit quel que soit le sien.
    # Défaut à True : les clients existants gardent leur comportement.
    allow_credit = models.BooleanField(default=True)

    credit_limit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    # Dette totale CONVERTIE en devise principale de l'organisation. La dette
    # réelle, devise par devise, vit dans `CustomerBalance` : un client peut
    # devoir 50 USD ET 40 000 CDF, deux montants qu'on n'additionne jamais tels
    # quels. Ce champ scalaire reste la vue comptable agrégée (limite de crédit,
    # tri, rapports, sync mobile), sur le même principe que les champs scalaires
    # de `RegisterSession` face à `RegisterSessionCurrencyBalance`.
    #
    # Ne jamais l'écrire directement : passer par `apps.contacts.services`.
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
        """
        Total acheté, converti en devise principale.

        Un `Sum('total')` brut additionnait 50 USD et 40 000 CDF comme des
        nombres nus. `Supplier.get_total_purchases` convertissait déjà ; les deux
        côtés du carnet d'adresses suivent maintenant la même règle.
        """
        from apps.cashbook.services import primary_sum

        return self.sales.filter(status='completed').aggregate(
            total=primary_sum('total')
        )['total'] or Decimal('0.00')


class CustomerBalance(TenantModel):
    """
    Dette d'un client pour UNE devise.

    En RDC un même client peut devoir en CDF et en USD ; ces montants ne
    s'additionnent pas. Chaque ligne suit une devise indépendamment. Le champ
    scalaire `Customer.current_balance` reste la somme convertie en devise
    principale, pour la limite de crédit et les rapports comptables.

    Convention de signe : positif = le client doit de l'argent (dette),
    négatif = le client est créditeur (avance non consommée).

    Écriture exclusivement via `apps.contacts.services`.
    """

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='balances'
    )
    currency = models.CharField(max_length=3)

    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )

    class Meta:
        db_table = 'customer_balances'
        ordering = ['currency']
        constraints = [
            models.UniqueConstraint(
                fields=['customer', 'currency'],
                name='unique_customer_balance_per_currency'
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'currency']),
        ]

    def __str__(self):
        return f"{self.customer.name} - {self.amount} {self.currency}"


class CustomerTransaction(TenantModel):
    """
    Tracks all financial movements for a customer:
    - credit_sale: dette ajoutée via vente à crédit
    - payment: paiement reçu du client (réduit la dette)
    - advance: avance/acompte versé par le client
    - adjustment: ajustement manuel du solde
    - refund: remboursement au client

    ``amount``, ``balance_before`` et ``balance_after`` sont exprimés dans
    ``currency`` (la devise du mouvement), et non en devise principale.
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

    # Devise du mouvement. Défaut VIDE, résolu dans `save()` vers la devise
    # principale de l'organisation, comme Sale, Payment, Expense, CashMovement.
    currency = models.CharField(max_length=3, blank=True, default='')
    # Unités de devise principale pour 1 unité de `currency` (principale = 1).
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1')
    )

    balance_before = models.DecimalField(
        max_digits=15,
        decimal_places=2
    )
    balance_after = models.DecimalField(
        max_digits=15,
        decimal_places=2
    )

    # Référence EXTERNE saisie par le caissier (numéro de transaction mobile
    # money, de chèque). À ne pas confondre avec `receipt_number`.
    reference = models.CharField(max_length=100, blank=True)

    # Numéro de la pièce remise au client, alloué par `core.numbering` :
    # RGL- pour un règlement, AVC- pour une avance, AJU- pour un ajustement.
    # Stable d'une réimpression à l'autre. Vide pour les mouvements antérieurs.
    receipt_number = models.CharField(max_length=32, blank=True, db_index=True)

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

    def save(self, *args, **kwargs):
        # Résolution centralisée : deux chemins créent des transactions client
        # (les ventes et la fiche client), le modèle est le seul endroit sûr.
        if self.organization_id:
            from apps.settings.services import CurrencyService
            self.currency, self.exchange_rate = CurrencyService.resolve(
                self.organization, self.currency, self.exchange_rate,
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.amount} {self.currency} ({self.customer.name})"


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
    
    # Devise de facturation du fournisseur (celle de `current_balance`). Défaut
    # VIDE et non 'USD' : résolue dans `save()` vers la devise principale de
    # l'organisation, comme partout ailleurs.
    currency = models.CharField(max_length=3, blank=True, default='')
    
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

    def save(self, *args, **kwargs):
        """Résout la devise vers celle de l'établissement, quel que soit l'appelant."""
        if self.organization_id:
            from apps.settings.services import CurrencyService
            self.currency, _rate = CurrencyService.resolve(
                self.organization_id, self.currency
            )
        super().save(*args, **kwargs)

    def get_total_purchases(self):
        """Total acheté, CONVERTI en devise principale (montant × taux).

        Les commandes peuvent être libellées dans des devises différentes : les
        sommer brutes additionnerait des USD et des CDF.
        """
        from apps.cashbook.services import primary_sum

        return self.purchase_orders.filter(
            status__in=['received', 'partially_received']
        ).aggregate(total=primary_sum('total'))['total'] or Decimal('0.00')


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
