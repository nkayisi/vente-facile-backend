from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from apps.core.models import TenantModel, TenantSoftDeleteModel, TenantSyncableModel, SyncableModel
from apps.core.managers import TenantSoftDeleteManager


class Register(TenantSoftDeleteModel):
    """
    POS cash register/terminal.
    Each register tracks its own cash flow.
    """
    
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    
    branch = models.ForeignKey(
        'organizations.Branch',
        on_delete=models.CASCADE,
        related_name='registers'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.PROTECT,
        related_name='registers'
    )
    
    is_active = models.BooleanField(default=True)
    
    receipt_header = models.TextField(blank=True)
    receipt_footer = models.TextField(blank=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'registers'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                condition=models.Q(is_deleted=False),
                name='unique_register_code_per_org'
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.branch.name})"


class RegisterSession(TenantModel):
    """
    Cash register session (shift).
    Tracks opening/closing balances.
    """
    
    class Status(models.TextChoices):
        OPEN = 'open', 'Ouvert'
        CLOSED = 'closed', 'Fermé'

    register = models.ForeignKey(
        Register,
        on_delete=models.CASCADE,
        related_name='sessions'
    )
    
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='opened_sessions'
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='closed_sessions'
    )
    
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.OPEN
    )
    
    opening_balance = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    closing_balance = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    expected_balance = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    counted_balance = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Montant réel compté en caisse à la fermeture (saisie manuelle optionnelle)."
    )
    difference = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )

    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'register_sessions'
        ordering = ['-opened_at']
        indexes = [
            models.Index(fields=['register', 'status']),
            models.Index(fields=['opened_at']),
            # Ouverture de session : dernière session fermée d'une caisse
            # (filter register+status='closed' order_by -closed_at).
            models.Index(
                fields=['register', 'status', '-closed_at'],
                name='regsess_status_closed_idx',
            ),
        ]
        constraints = [
            # Garantit DB-level qu'une seule session 'open' par caisse à la fois.
            models.UniqueConstraint(
                fields=['register'],
                condition=models.Q(status='open'),
                name='unique_open_session_per_register',
            ),
        ]

    def __str__(self):
        return f"Session {self.register.name} - {self.opened_at.date()}"


class RegisterSessionCurrencyBalance(TenantModel):
    """
    Solde de caisse d'une session, ventilé PAR DEVISE.

    En RDC le tiroir contient physiquement plusieurs devises à la fois (ex. USD
    et CDF). Chaque ligne suit une devise indépendamment : fonds d'ouverture,
    attendu (calculé à la clôture), compté (saisie manuelle) et écart. Les champs
    scalaires de RegisterSession restent renseignés avec la devise principale
    pour compat ascendante.
    """

    session = models.ForeignKey(
        RegisterSession,
        on_delete=models.CASCADE,
        related_name='currency_balances'
    )
    currency = models.CharField(max_length=3)

    opening_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    expected_balance = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    counted_balance = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
        help_text="Montant réel compté dans cette devise à la fermeture."
    )
    difference = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )

    class Meta:
        db_table = 'register_session_currency_balances'
        ordering = ['currency']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'currency'],
                name='unique_session_currency_balance',
            ),
        ]

    def __str__(self):
        return f"{self.session_id} · {self.currency}: {self.expected_balance}"


class Sale(TenantSyncableModel):
    """
    Main sale/invoice model.
    Represents a complete transaction.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        COMPLETED = 'completed', 'Terminée'
        PARTIALLY_PAID = 'partially_paid', 'Partiellement payée'
        CANCELLED = 'cancelled', 'Annulée'
        REFUNDED = 'refunded', 'Remboursée'

    class SaleType(models.TextChoices):
        RETAIL = 'retail', 'Détail'
        WHOLESALE = 'wholesale', 'Gros'
        CREDIT = 'credit', 'Crédit'

    reference = models.CharField(max_length=50, db_index=True)
    
    session = models.ForeignKey(
        RegisterSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sales'
    )
    register = models.ForeignKey(
        Register,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sales'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sales'
    )
    
    customer = models.ForeignKey(
        'contacts.Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sales'
    )
    
    sale_type = models.CharField(
        max_length=20,
        choices=SaleType.choices,
        default=SaleType.RETAIL
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    price_list = models.ForeignKey(
        'products.PriceList',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
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
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    # Réduction monétaire effectivement appliquée via points de fidélité (déjà
    # incluse dans `discount_amount`). Champ traçant à part la part loyauté.
    loyalty_redemption_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Montant déduit du total grâce à l'utilisation de points de fidélité."
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
    change_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    # Devise dans laquelle la monnaie est rendue (choix du caissier). Peut différer
    # de `currency` : le montant `change_amount` est exprimé dans cette devise.
    change_currency = models.CharField(max_length=3, blank=True, default='')

    # Devise de facture. Défaut VIDE et non 'CDF' : la valeur est résolue dans
    # `save()` vers la devise principale de l'organisation. Un défaut codé en dur
    # estampillait les ventes 'CDF' même dans une org dont la principale est USD.
    currency = models.CharField(max_length=3, blank=True, default='')
    # Taux snapshot : unités de devise principale de l'org pour 1 unité de la
    # devise de facture (`currency`). 12 décimales pour coller à
    # OrganizationCurrency et éviter la perte sur les petits taux réciproques
    # (ex. principale USD, 1 CDF = 0.000434782609 USD).
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000')
    )

    notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    
    sold_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sales'
    )
    
    sale_date = models.DateTimeField(auto_now_add=True, db_index=True)
    due_date = models.DateField(null=True, blank=True)
    
    is_pos = models.BooleanField(default=True)
    receipt_printed = models.BooleanField(default=False)

    # True dès qu'une réservation de stock a été appliquée (typiquement par
    # conversion d'un devis). Sert de garde d'idempotence côté
    # SaleStockService.release_reservation : on ne libère qu'une fois.
    stock_reserved = models.BooleanField(default=False)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'sales'
        ordering = ['-sale_date']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'customer']),
            models.Index(fields=['sale_date']),
            models.Index(fields=['reference']),
            # Filtre cashier : `get_queryset().filter(sold_by=user)` trié par date.
            models.Index(
                fields=['organization', 'sold_by', '-sale_date'],
                name='sales_org_sold_by_date_idx',
            ),
            # Rapports : statistiques journalières / périodiques par org.
            # Évite le full-scan + sort sur la table à chaque dashboard ouvert.
            models.Index(
                fields=['organization', '-sale_date'],
                name='sales_org_date_idx',
            ),
            # Filtre des ventes à crédit non soldées (rapport recouvrement).
            models.Index(
                fields=['organization', 'status', 'amount_due'],
                name='sales_org_status_due_idx',
            ),
            # Stats dashboard/journalières : filtre org + statut='completed' sur
            # une plage de dates. Narrow org+status puis range sur sale_date.
            models.Index(
                fields=['organization', 'status', 'sale_date'],
                name='sales_org_status_date_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'reference'],
                condition=models.Q(is_deleted=False),
                name='unique_sale_reference_per_org'
            ),
        ]
        permissions = [
            ('cancel_sale', 'Can cancel sales'),
            ('refund_sale', 'Can refund sales'),
            ('apply_discount', 'Can apply discounts'),
            ('view_all_sales', 'Can view all sales'),
        ]

    def __str__(self):
        return f"Sale {self.reference}"

    def save(self, *args, **kwargs):
        """Garantit une devise et un taux cohérents, quel que soit l'appelant.

        Deux chemins créent des ventes (le serializer POS et la conversion d'un
        devis) et le second ne passait aucune devise : la vente héritait du
        défaut du modèle. Résoudre ici couvre tous les appelants, présents et à
        venir, plutôt que de dupliquer la logique à chaque site d'écriture.
        """
        if self.organization_id:
            from apps.sales.services import resolve_sale_currency
            self.currency, self.exchange_rate = resolve_sale_currency(
                self.organization_id, self.currency, self.exchange_rate
            )
        # La monnaie est rendue dans la devise de la vente sauf choix explicite.
        if not self.change_currency:
            self.change_currency = self.currency
        super().save(*args, **kwargs)

    def calculate_totals(self):
        """Recalculate sale totals from items."""
        TWO_PLACES = Decimal('0.01')
        items = self.items.all()
        self.subtotal = sum((item.subtotal for item in items), Decimal('0.00')).quantize(TWO_PLACES)
        self.tax_amount = sum((item.tax_amount for item in items), Decimal('0.00')).quantize(TWO_PLACES)
        
        items_discount = sum((item.discount_amount for item in items), Decimal('0.00')).quantize(TWO_PLACES)
        
        global_discount = Decimal('0.00')
        if self.discount_percentage > 0:
            net_after_item_discounts = self.subtotal - items_discount
            global_discount = (net_after_item_discounts * self.discount_percentage / 100).quantize(TWO_PLACES)
        
        self.discount_amount = (items_discount + global_discount).quantize(TWO_PLACES)
        self.total = (self.subtotal - self.discount_amount + self.tax_amount).quantize(TWO_PLACES)
        self.amount_due = (self.total - self.amount_paid).quantize(TWO_PLACES)
        
        if self.amount_paid >= self.total:
            self.change_amount = (self.amount_paid - self.total).quantize(TWO_PLACES)
            self.amount_due = Decimal('0.00')


class SaleItem(TenantModel, SyncableModel):
    """Individual line items in a sale."""
    
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='sale_items'
    )
    variant = models.ForeignKey(
        'products.ProductVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sale_items'
    )
    batch = models.ForeignKey(
        'inventory.StockBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    description = models.CharField(max_length=500, blank=True)
    
    quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))]
    )
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)

    # Vente en gros : part de `quantity` vendue en conditionnements entiers.
    # `quantity` reste la quantité totale en unité de base (détail) : c'est elle
    # qui pilote le stock, le FIFO, le coût et les rapports. La part vendue au
    # détail est dérivée (`loose_quantity`), jamais stockée, ce qui rend
    # impossible une incohérence entre les deux.
    package_quantity = models.DecimalField(
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
        help_text="Nombre d'unités par conditionnement au moment de la vente."
    )
    cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    discount_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[
            MinValueValidator(Decimal('0')),
            MaxValueValidator(Decimal('100')),
        ],
    )
    tax_amount = models.DecimalField(
        max_digits=15,
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
    
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'sale_items'
        indexes = [
            models.Index(fields=['sale']),
            models.Index(fields=['product']),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    @property
    def loose_quantity(self):
        """
        Part de la ligne vendue à l'unité, déduite et jamais stockée.

        Garder un champ séparé exigerait de maintenir l'égalité
        ``paquets × facteur + unités == quantity`` à chaque écriture ; la
        dériver la rend vraie par construction.
        """
        if not self.package_quantity or not self.packaging_factor:
            return self.quantity
        return self.quantity - (self.package_quantity * self.packaging_factor)

    def save(self, *args, **kwargs):
        TWO_PLACES = Decimal('0.01')

        # Vente en gros : le prix du conditionnement n'est pas le prix unitaire
        # multiplié par le nombre d'unités : c'est tout l'intérêt commercial du
        # gros. Une ligne mixte additionne donc les deux tarifs.
        # `packaging_factor` est exigé : sans lui, la part vendue à l'unité
        # serait indéterminée.
        if (
            self.package_quantity
            and self.package_unit_price is not None
            and self.packaging_factor
        ):
            loose = self.loose_quantity
            if loose < 0:
                raise ValueError(
                    "La quantité totale est inférieure au contenu des "
                    "conditionnements vendus."
                )
            self.subtotal = (
                self.package_quantity * self.package_unit_price
                + loose * self.unit_price
            ).quantize(TWO_PLACES)
        else:
            self.subtotal = (self.quantity * self.unit_price).quantize(TWO_PLACES)

        if self.discount_percentage > 0:
            self.discount_amount = (self.subtotal * self.discount_percentage / 100).quantize(TWO_PLACES)
        
        taxable_amount = self.subtotal - self.discount_amount
        self.tax_amount = (taxable_amount * self.tax_rate / 100).quantize(TWO_PLACES)
        self.total = (taxable_amount + self.tax_amount).quantize(TWO_PLACES)
        
        super().save(*args, **kwargs)


class PaymentMethod(TenantModel):
    """Available payment methods."""
    
    class MethodType(models.TextChoices):
        CASH = 'cash', 'Espèces'
        CARD = 'card', 'Carte bancaire'
        MOBILE_MONEY = 'mobile_money', 'Mobile Money'
        BANK_TRANSFER = 'bank_transfer', 'Virement bancaire'
        CHECK = 'check', 'Chèque'
        CREDIT = 'credit', 'Crédit'
        OTHER = 'other', 'Autre'

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    method_type = models.CharField(
        max_length=20,
        choices=MethodType.choices,
        default=MethodType.CASH
    )
    
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    
    requires_reference = models.BooleanField(default=False)
    
    icon = models.CharField(max_length=50, blank=True)

    class Meta:
        db_table = 'payment_methods'
        unique_together = ['organization', 'code']

    def __str__(self):
        return self.name


class Payment(TenantModel, SyncableModel):
    """
    Payment records for sales.
    A sale can have multiple payments (split payment).
    """
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'En attente'
        COMPLETED = 'completed', 'Terminé'
        FAILED = 'failed', 'Échoué'
        REFUNDED = 'refunded', 'Remboursé'

    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name='payments'
    )
    payment_method = models.ForeignKey(
        PaymentMethod,
        on_delete=models.SET_NULL,
        null=True,
        related_name='payments'
    )
    
    # Valeur imputée à la facture, EXPRIMÉE DANS LA DEVISE DE LA VENTE
    # (= tendered_amount × exchange_rate).
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    # Montant réellement remis par le client, exprimé dans `currency` : c'est ce
    # qui entre physiquement dans le tiroir-caisse. Nullable pour compat : les
    # anciennes lignes sont backfillées à `amount` (mono-devise).
    tendered_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
    )

    # Devise du règlement. Défaut VIDE et non 'CDF' : résolue dans `save()` vers
    # la devise de la vente (elle-même résolue vers la principale de l'org).
    currency = models.CharField(max_length=3, blank=True, default='')
    # Taux : unités de devise de la VENTE pour 1 unité de `currency` (devise du
    # règlement) ⇒ amount = tendered_amount × exchange_rate. (=1 si même devise.)
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000')
    )

    reference = models.CharField(max_length=100, blank=True)
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COMPLETED
    )
    
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='received_payments'
    )
    
    paid_at = models.DateTimeField(auto_now_add=True)
    
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'payments'
        ordering = ['-paid_at']
        indexes = [
            models.Index(fields=['sale']),
            models.Index(fields=['paid_at']),
        ]

    def __str__(self):
        return f"Payment {self.amount} for {self.sale.reference}"

    def save(self, *args, **kwargs):
        """Sans devise explicite, un règlement est dans la devise de la vente.

        Le taux n'est pas déduit ici : une conversion vers la devise de vente
        relève de `services.resolve_tender`, seul endroit qui connaît le montant
        remis. Ici on garantit seulement qu'aucun règlement ne reste sans devise.
        """
        if not self.currency and self.sale_id:
            self.currency = self.sale.currency
        super().save(*args, **kwargs)


class SaleReturn(TenantSoftDeleteModel):
    """
    Sales returns/refunds.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        APPROVED = 'approved', 'Approuvé'
        COMPLETED = 'completed', 'Terminé'
        REJECTED = 'rejected', 'Rejeté'

    class ReturnType(models.TextChoices):
        FULL = 'full', 'Retour complet'
        PARTIAL = 'partial', 'Retour partiel'
        EXCHANGE = 'exchange', 'Échange'

    reference = models.CharField(max_length=50, unique=True)
    
    original_sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name='returns'
    )
    
    return_type = models.CharField(
        max_length=20,
        choices=ReturnType.choices,
        default=ReturnType.PARTIAL
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
    refund_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    reason = models.TextField()
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_returns'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_returns'
    )
    
    return_date = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'sale_returns'
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
        ]

    def __str__(self):
        return f"Return {self.reference}"


class SaleReturnItem(TenantModel):
    """Items in a sale return."""
    
    sale_return = models.ForeignKey(
        SaleReturn,
        on_delete=models.CASCADE,
        related_name='items'
    )
    original_item = models.ForeignKey(
        SaleItem,
        on_delete=models.CASCADE,
        related_name='return_items'
    )
    
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    total = models.DecimalField(max_digits=15, decimal_places=2)
    
    reason = models.TextField(blank=True)
    
    restock = models.BooleanField(default=True)

    class Meta:
        db_table = 'sale_return_items'

    def __str__(self):
        return f"Return: {self.original_item.product.name} x {self.quantity}"


class Quotation(TenantSoftDeleteModel):
    """
    Sales quotations/estimates.
    Can be converted to sales.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        SENT = 'sent', 'Envoyé'
        ACCEPTED = 'accepted', 'Accepté'
        REJECTED = 'rejected', 'Rejeté'
        EXPIRED = 'expired', 'Expiré'
        CONVERTED = 'converted', 'Converti'

    reference = models.CharField(max_length=50, unique=True)
    
    customer = models.ForeignKey(
        'contacts.Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quotations'
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
    total = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    valid_until = models.DateField()
    
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='quotations'
    )
    
    converted_sale = models.OneToOneField(
        Sale,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_quotation'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'quotations'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['reference']),
        ]

    def __str__(self):
        return f"Quotation {self.reference}"


class QuotationItem(TenantModel):
    """Items in a quotation."""
    
    quotation = models.ForeignKey(
        Quotation,
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
    
    description = models.CharField(max_length=500, blank=True)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    total = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = 'quotation_items'

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"
