"""
Modèles pour le module Livre de Caisse (Entrées/Sorties).

Ce module centralise tous les mouvements financiers de la caisse :
- Entrées : ventes, remboursements fournisseurs, apports de fonds, recouvrements
- Sorties : dépenses, achats, remboursements clients, retraits de fonds
"""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from apps.core.models import TenantModel


# =============================================================================
# CATÉGORIES D'ENTRÉES
# =============================================================================

class IncomeCategory(TenantModel):
    """
    Catégories d'entrées pour classifier les entrées de caisse.
    Exemples : Ventes comptant, Recouvrements, Apports, Autres revenus, etc.
    """

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30, blank=True)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=7, default='#10B981')
    icon = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'income_categories'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'name'],
                name='unique_income_category_name_per_org'
            ),
        ]
        verbose_name = 'Catégorie d\'entrée'
        verbose_name_plural = 'Catégories d\'entrées'

    def __str__(self):
        return self.name


# =============================================================================
# CATÉGORIES DE DÉPENSES
# =============================================================================

class ExpenseCategory(TenantModel):
    """
    Catégories de dépenses pour classifier les sorties de caisse.
    Exemples : Loyer, Salaires, Transport, Fournitures, Électricité, etc.
    """

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30, blank=True)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=7, default='#6B7280')
    icon = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    budget_monthly = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Budget mensuel alloué à cette catégorie (0 = pas de limite)"
    )

    class Meta:
        db_table = 'expense_categories'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'name'],
                name='unique_expense_category_name_per_org'
            ),
        ]
        verbose_name = 'Catégorie de dépense'
        verbose_name_plural = 'Catégories de dépenses'

    def __str__(self):
        return self.name


# =============================================================================
# DÉPENSES
# =============================================================================

class Expense(TenantModel):
    """
    Dépense détaillée avec catégorie, bénéficiaire, et justificatif.
    Chaque dépense génère automatiquement un CashMovement de type 'out'.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        APPROVED = 'approved', 'Approuvée'
        PAID = 'paid', 'Payée'
        REJECTED = 'rejected', 'Rejetée'
        CANCELLED = 'cancelled', 'Annulée'

    reference = models.CharField(max_length=50, db_index=True)
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name='expenses'
    )

    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
        help_text="Entrepôt rattaché à la dépense (filtrage par périmètre membre)"
    )

    description = models.CharField(max_length=500)
    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    # Devise de la dépense (défaut = devise principale de l'org). Permet de
    # régler une dépense en devise étrangère ; le CashMovement généré en hérite.
    currency = models.CharField(max_length=3, blank=True, default='')
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000'),
        help_text="Unités de devise principale pour 1 unité de `currency`."
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )

    beneficiary = models.CharField(
        max_length=255,
        blank=True,
        help_text="Nom du bénéficiaire de la dépense"
    )
    payment_method = models.ForeignKey(
        'sales.PaymentMethod',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses'
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text="Référence du paiement (n° chèque, transaction mobile, etc.)"
    )

    expense_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    paid_date = models.DateField(null=True, blank=True)

    is_recurring = models.BooleanField(
        default=False,
        help_text="Dépense récurrente (loyer, salaires, etc.)"
    )
    recurrence_period = models.CharField(
        max_length=20,
        blank=True,
        choices=[
            ('daily', 'Quotidien'),
            ('weekly', 'Hebdomadaire'),
            ('monthly', 'Mensuel'),
            ('quarterly', 'Trimestriel'),
            ('yearly', 'Annuel'),
        ]
    )

    notes = models.TextField(blank=True)
    attachment = models.FileField(
        upload_to='expenses/attachments/%Y/%m/',
        null=True,
        blank=True,
        help_text="Pièce justificative (facture, reçu, etc.)"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_expenses'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_expenses'
    )

    class Meta:
        db_table = 'expenses'
        ordering = ['-expense_date', '-created_at']
        indexes = [
            # Index du tirage de synchronisation. L'ordre `(organisation,
            # updated_at, id)` est celui de la pagination par curseur : sans lui,
            # chaque page fait un balayage complet suivi d'un tri.
            models.Index(fields=['organization', 'updated_at', 'id']),
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'category']),
            models.Index(fields=['expense_date']),
            models.Index(fields=['reference']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'reference'],
                name='unique_expense_reference_per_org'
            ),
        ]
        verbose_name = 'Dépense'
        verbose_name_plural = 'Dépenses'

    def __str__(self):
        return f"{self.reference} - {self.description[:50]}"


# =============================================================================
# MOUVEMENTS DE CAISSE
# =============================================================================

class CashMovement(TenantModel):
    """
    Mouvement d'entrée ou de sortie de caisse.
    
    Chaque mouvement est soit une entrée (cash_in) soit une sortie (cash_out).
    Il peut être lié à une vente, une dépense, ou être un mouvement manuel.
    
    Types de mouvements :
    - sale : Entrée de vente
    - sale_return : Sortie de remboursement client
    - expense : Sortie de dépense
    - purchase : Sortie d'achat fournisseur
    - supplier_refund : Entrée de remboursement fournisseur
    - debt_collection : Entrée de recouvrement de dette client
    - fund_in : Apport de fonds en caisse
    - fund_out : Retrait de fonds de la caisse
    - adjustment : Ajustement de caisse (écart)
    - other_in : Autre entrée
    - other_out : Autre sortie
    """

    class Direction(models.TextChoices):
        IN = 'in', 'Entrée'
        OUT = 'out', 'Sortie'

    class MovementType(models.TextChoices):
        SALE = 'sale', 'Vente'
        SALE_RETURN = 'sale_return', 'Remboursement client'
        EXPENSE = 'expense', 'Dépense'
        PURCHASE = 'purchase', 'Achat fournisseur'
        SUPPLIER_REFUND = 'supplier_refund', 'Remboursement fournisseur'
        DEBT_COLLECTION = 'debt_collection', 'Recouvrement dette'
        FUND_IN = 'fund_in', 'Apport de fonds'
        FUND_OUT = 'fund_out', 'Retrait de fonds'
        ADJUSTMENT = 'adjustment', 'Ajustement de caisse'
        CHANGE = 'change', 'Monnaie rendue'
        OTHER_IN = 'other_in', 'Autre entrée'
        OTHER_OUT = 'other_out', 'Autre sortie'

    reference = models.CharField(max_length=50, db_index=True)

    direction = models.CharField(
        max_length=3,
        choices=Direction.choices
    )
    movement_type = models.CharField(
        max_length=30,
        choices=MovementType.choices
    )

    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    # Devise du mouvement (celle de `amount`). Défaut backfillé à la devise
    # principale de l'org. Le solde `balance_after` est suivi PAR devise.
    currency = models.CharField(max_length=3, blank=True, default='')
    exchange_rate = models.DecimalField(
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000'),
        help_text="Unités de devise principale pour 1 unité de `currency`."
    )
    description = models.CharField(max_length=500)

    payment_method = models.ForeignKey(
        'sales.PaymentMethod',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )

    # Catégories optionnelles
    income_category = models.ForeignKey(
        IncomeCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements',
        help_text="Catégorie pour les entrées de caisse"
    )
    expense_category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='direct_cash_movements',
        help_text="Catégorie pour les sorties de caisse (hors dépenses formelles)"
    )

    # Liens optionnels vers les documents source
    sale = models.ForeignKey(
        'sales.Sale',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )
    expense = models.ForeignKey(
        Expense,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )
    purchase_order = models.ForeignKey(
        'purchases.PurchaseOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )
    customer = models.ForeignKey(
        'contacts.Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )
    supplier = models.ForeignKey(
        'contacts.Supplier',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )

    # Session de caisse rattachée (apports/retraits/dépenses saisis pendant
    # une session ouverte). Permet de calculer la caisse nette d'une session
    # (ouverture + entrées espèces − sorties espèces) et de scoper les
    # mouvements manuels par l'entrepôt de la caisse.
    session = models.ForeignKey(
        'sales.RegisterSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements'
    )

    # Solde de caisse après ce mouvement
    balance_after = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Solde de caisse après ce mouvement"
    )

    movement_date = models.DateTimeField(db_index=True)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_cash_movements'
    )

    is_cancelled = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_cash_movements'
    )
    cancel_reason = models.TextField(blank=True)

    class Meta:
        db_table = 'cash_movements'
        ordering = ['-movement_date', '-created_at']
        indexes = [
            # Index du tirage de synchronisation. L'ordre `(organisation,
            # updated_at, id)` est celui de la pagination par curseur : sans lui,
            # chaque page fait un balayage complet suivi d'un tri.
            models.Index(fields=['organization', 'updated_at', 'id']),
            models.Index(fields=['organization', 'direction']),
            models.Index(fields=['organization', 'movement_type']),
            models.Index(fields=['organization', 'movement_date']),
            models.Index(fields=['reference']),
            models.Index(fields=['organization', 'is_cancelled']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'reference'],
                name='unique_cash_movement_reference_per_org'
            ),
        ]
        verbose_name = 'Mouvement de caisse'
        verbose_name_plural = 'Mouvements de caisse'

    def __str__(self):
        direction_label = "+" if self.direction == 'in' else "-"
        return f"{self.reference} {direction_label}{self.amount}"

    @property
    def signed_amount(self):
        """Montant signé : positif pour entrée, négatif pour sortie."""
        return self.amount if self.direction == 'in' else -self.amount
