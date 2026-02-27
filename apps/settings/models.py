"""
Settings models for organization configuration.
Includes multi-currency support and loyalty program.
"""
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from apps.core.models import TenantModel, TimeStampedModel, UUIDModel


class Currency(TimeStampedModel, UUIDModel):
    """
    Available currencies in the system.
    Global table, not tenant-specific.
    """
    code = models.CharField(max_length=3, unique=True, primary_key=False)
    name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=10)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'currencies'
        verbose_name_plural = 'Currencies'
        ordering = ['code']
    
    def __str__(self):
        return f"{self.code} - {self.name}"


class OrganizationCurrency(TenantModel):
    """
    Currencies enabled for a specific organization.
    Each organization has one primary currency and can have multiple secondary currencies.
    """
    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name='organization_currencies'
    )
    is_primary = models.BooleanField(default=False)
    exchange_rate = models.DecimalField(
        max_digits=15,
        decimal_places=6,
        default=Decimal('1.000000'),
        validators=[MinValueValidator(Decimal('0.000001'))],
        help_text="Taux de change par rapport à la devise principale"
    )
    is_active = models.BooleanField(default=True)
    last_rate_update = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'organization_currencies'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'currency'],
                name='unique_currency_per_org'
            ),
        ]
        ordering = ['-is_primary', 'currency__code']
    
    def __str__(self):
        return f"{self.organization.name} - {self.currency.code}"
    
    def save(self, *args, **kwargs):
        # Si c'est la devise principale, le taux est toujours 1
        if self.is_primary:
            self.exchange_rate = Decimal('1.000000')
        super().save(*args, **kwargs)


class LoyaltyProgram(TenantModel):
    """
    Loyalty program configuration for an organization.
    Each organization can have one active loyalty program.
    """
    
    class PointsCalculationType(models.TextChoices):
        FIXED_PER_AMOUNT = 'fixed_per_amount', 'Points fixes par montant'
        PERCENTAGE = 'percentage', 'Pourcentage du montant'
    
    name = models.CharField(max_length=255, default="Programme de fidélité")
    is_active = models.BooleanField(default=False)
    
    # Configuration des points
    points_calculation_type = models.CharField(
        max_length=20,
        choices=PointsCalculationType.choices,
        default=PointsCalculationType.FIXED_PER_AMOUNT
    )
    
    # Pour FIXED_PER_AMOUNT: X points pour chaque Y montant dépensé
    points_per_unit = models.PositiveIntegerField(
        default=1,
        help_text="Nombre de points gagnés"
    )
    amount_per_unit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('1000.00'),
        help_text="Montant requis pour gagner les points (en devise principale)"
    )
    
    # Pour PERCENTAGE: X% du montant converti en points
    points_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(Decimal('100.00'))],
        help_text="Pourcentage du montant converti en points"
    )
    
    # Valeur des points
    point_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1.00'),
        help_text="Valeur d'un point en devise principale (pour les récompenses)"
    )
    
    # Seuil minimum pour utiliser les points
    min_points_to_redeem = models.PositiveIntegerField(
        default=100,
        help_text="Nombre minimum de points pour pouvoir les utiliser"
    )
    
    # Expiration des points (0 = jamais)
    points_expiry_days = models.PositiveIntegerField(
        default=0,
        help_text="Nombre de jours avant expiration des points (0 = jamais)"
    )
    
    # Règles d'éligibilité
    only_registered_customers = models.BooleanField(
        default=True,
        help_text="Seuls les clients enregistrés peuvent accumuler des points"
    )
    
    class Meta:
        db_table = 'loyalty_programs'
        constraints = [
            models.UniqueConstraint(
                fields=['organization'],
                name='unique_loyalty_program_per_org'
            ),
        ]
    
    def __str__(self):
        return f"{self.organization.name} - {self.name}"
    
    def calculate_points(self, amount: Decimal) -> int:
        """Calcule le nombre de points pour un montant donné."""
        if not self.is_active:
            return 0
        
        if self.points_calculation_type == self.PointsCalculationType.FIXED_PER_AMOUNT:
            if self.amount_per_unit > 0:
                units = int(amount / self.amount_per_unit)
                return units * self.points_per_unit
            return 0
        else:  # PERCENTAGE
            points = (amount * self.points_percentage / 100)
            return int(points)
    
    def calculate_redemption_value(self, points: int) -> Decimal:
        """Calcule la valeur monétaire des points."""
        return Decimal(points) * self.point_value


class LoyaltyReward(TenantModel):
    """
    Predefined rewards that customers can redeem with their points.
    """
    
    class RewardType(models.TextChoices):
        DISCOUNT_AMOUNT = 'discount_amount', 'Réduction montant fixe'
        DISCOUNT_PERCENTAGE = 'discount_percentage', 'Réduction pourcentage'
        FREE_PRODUCT = 'free_product', 'Produit gratuit'
        CUSTOM = 'custom', 'Récompense personnalisée'
    
    loyalty_program = models.ForeignKey(
        LoyaltyProgram,
        on_delete=models.CASCADE,
        related_name='rewards'
    )
    
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    
    reward_type = models.CharField(
        max_length=20,
        choices=RewardType.choices,
        default=RewardType.DISCOUNT_AMOUNT
    )
    
    points_required = models.PositiveIntegerField(
        help_text="Nombre de points requis pour cette récompense"
    )
    
    # Pour DISCOUNT_AMOUNT
    discount_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    # Pour DISCOUNT_PERCENTAGE
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))]
    )
    
    # Pour FREE_PRODUCT
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loyalty_rewards'
    )
    
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'loyalty_rewards'
        ordering = ['points_required']
    
    def __str__(self):
        return f"{self.name} ({self.points_required} pts)"


class CustomerLoyalty(TenantModel):
    """
    Tracks loyalty points for each customer.
    """
    customer = models.OneToOneField(
        'contacts.Customer',
        on_delete=models.CASCADE,
        related_name='loyalty'
    )
    
    total_points_earned = models.PositiveIntegerField(default=0)
    total_points_redeemed = models.PositiveIntegerField(default=0)
    current_points = models.IntegerField(default=0)
    
    tier = models.CharField(max_length=50, blank=True, default='')
    
    last_points_earned_at = models.DateTimeField(null=True, blank=True)
    last_points_redeemed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        db_table = 'customer_loyalty'
        verbose_name_plural = 'Customer loyalties'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'customer'],
                name='unique_loyalty_per_customer'
            ),
        ]
    
    def __str__(self):
        return f"{self.customer.name} - {self.current_points} pts"
    
    def add_points(self, points: int, save: bool = True):
        """Ajoute des points au compte du client."""
        from django.utils import timezone
        self.current_points += points
        self.total_points_earned += points
        self.last_points_earned_at = timezone.now()
        if save:
            self.save()
    
    def redeem_points(self, points: int, save: bool = True):
        """Utilise des points du compte du client."""
        from django.utils import timezone
        if points > self.current_points:
            raise ValueError("Points insuffisants")
        self.current_points -= points
        self.total_points_redeemed += points
        self.last_points_redeemed_at = timezone.now()
        if save:
            self.save()


class LoyaltyTransaction(TenantModel):
    """
    History of all loyalty point transactions.
    """
    
    class TransactionType(models.TextChoices):
        EARN = 'earn', 'Points gagnés'
        REDEEM = 'redeem', 'Points utilisés'
        EXPIRE = 'expire', 'Points expirés'
        ADJUST = 'adjust', 'Ajustement manuel'
        BONUS = 'bonus', 'Bonus'
    
    customer_loyalty = models.ForeignKey(
        CustomerLoyalty,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    
    transaction_type = models.CharField(
        max_length=20,
        choices=TransactionType.choices
    )
    
    points = models.IntegerField(
        help_text="Positif pour gain, négatif pour utilisation"
    )
    
    balance_after = models.IntegerField(
        help_text="Solde de points après la transaction"
    )
    
    # Référence optionnelle à une vente
    sale = models.ForeignKey(
        'sales.Sale',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loyalty_transactions'
    )
    
    # Référence optionnelle à une récompense
    reward = models.ForeignKey(
        LoyaltyReward,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transactions'
    )
    
    description = models.CharField(max_length=255, blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='loyalty_transactions_created'
    )
    
    class Meta:
        db_table = 'loyalty_transactions'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.customer_loyalty.customer.name} - {self.points:+d} pts"


class OrganizationSettings(TenantModel):
    """
    General settings for an organization.
    Singleton per organization.
    """
    
    # Paramètres généraux des reçus
    receipt_header = models.TextField(blank=True, help_text="En-tête des reçus")
    receipt_footer = models.TextField(blank=True, help_text="Pied de page des reçus")
    
    # Paramètres de fidélité
    show_loyalty_points_on_receipt = models.BooleanField(
        default=True,
        help_text="Afficher les points gagnés et le solde du client sur les reçus"
    )
    
    # Notifications
    low_stock_threshold = models.PositiveIntegerField(
        default=10,
        help_text="Seuil d'alerte de stock bas"
    )
    
    class Meta:
        db_table = 'organization_settings'
        verbose_name_plural = 'Organization settings'
        constraints = [
            models.UniqueConstraint(
                fields=['organization'],
                name='unique_settings_per_org'
            ),
        ]
    
    def __str__(self):
        return f"Settings - {self.organization.name}"
