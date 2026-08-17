"""
Settings models for organization configuration.
Includes multi-currency support and loyalty program.
"""
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from apps.core.models import TenantModel, TimeStampedModel, UUIDModel

# Les points s'accumulent au centième : c'est la finesse qu'exige un barème en
# pourcentage sur une devise forte (1 % de 58 USD = 0,58 point). Au delà, on
# afficherait un bruit que le marchand ne saurait pas lire.
POINT_PRECISION = Decimal('0.01')

# Borne dure du plafond de rédemption : quelle que soit la configuration, une
# facture garde toujours au moins 30 % à encaisser en monnaie. Le réglage de
# l'organisation ne peut que durcir ce plafond, jamais le desserrer.
MAX_REDEMPTION_PERCENT_CEILING = Decimal('70.00')


def _as_points(value) -> Decimal:
    """
    Normalise une quantité de points au centième.

    Les appelants passent indifféremment un ``int``, un ``float`` ou un
    ``Decimal`` ; sans normalisation, additionner un ``float`` à un ``Decimal``
    lève, et un ``Decimal`` non quantifié ferait dériver le compteur.
    """
    return Decimal(str(value or 0)).quantize(POINT_PRECISION)


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
        max_digits=20,
        decimal_places=12,
        default=Decimal('1.000000'),
        validators=[MinValueValidator(Decimal('0.000000000001'))],
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

    # Plafond de rédemption : part d'une facture réglable en points
    max_redemption_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('50.00'),
        validators=[
            MinValueValidator(Decimal('0.01')),
            MaxValueValidator(MAX_REDEMPTION_PERCENT_CEILING),
        ],
        help_text=(
            "Part maximale d'une facture réglable en points "
            f"(maximum {MAX_REDEMPTION_PERCENT_CEILING:.0f} %)"
        )
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
    
    def calculate_points(self, amount: Decimal) -> Decimal:
        """
        Points gagnés pour un montant donné, en **devise principale**.

        Retourne un ``Decimal`` à deux décimales. Le calcul tronquait autrefois
        à l'entier : sur une organisation dont la devise principale a une valeur
        unitaire élevée, 1 % d'un panier de 58 USD valait 0,58 point, ramené à
        **zéro** sans le moindre signal. Un panier de 396 USD perdait de même
        0,96 point. La fidélité y devenait inerte.

        Les deux modes gardent leur sémantique propre :

        - ``PERCENTAGE`` est continu par nature (« X % du montant ») : le
          résultat est exact ;
        - ``FIXED_PER_AMOUNT`` reste un barème par tranches (« X points pour
          **chaque** Y dépensé ») : une tranche entamée ne compte pas. C'est ce
          que le libellé promet au marchand, et le changer relèverait tous les
          barèmes existants sans qu'il l'ait demandé.
        """
        if not self.is_active:
            return Decimal('0.00')

        if self.points_calculation_type == self.PointsCalculationType.FIXED_PER_AMOUNT:
            if self.amount_per_unit > 0:
                units = (amount / self.amount_per_unit).to_integral_value(
                    rounding=ROUND_FLOOR
                )
                return (units * self.points_per_unit).quantize(POINT_PRECISION)
            return Decimal('0.00')

        points = amount * self.points_percentage / 100
        return points.quantize(POINT_PRECISION, rounding=ROUND_HALF_UP)

    def calculate_redemption_value(self, points) -> Decimal:
        """Calcule la valeur monétaire des points."""
        return (Decimal(points) * self.point_value).quantize(Decimal('0.01'))

    def max_redeemable_amount(self, amount) -> Decimal:
        """
        Part de ``amount`` réglable en points, dans la devise de ``amount``.

        Seule source du plafond : les points ne soldent jamais toute une
        facture, il reste toujours un montant à encaisser en monnaie. Le
        réglage de l'organisation est re-borné ici par
        ``MAX_REDEMPTION_PERCENT_CEILING`` : la garantie tient même sur une
        ligne écrite hors validation (SQL direct, fixture ancienne).
        """
        amount = Decimal(amount or 0)
        if amount <= 0:
            return Decimal('0.00')
        percent = min(self.max_redemption_percent, MAX_REDEMPTION_PERCENT_CEILING)
        return (amount * percent / 100).quantize(Decimal('0.01'))


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
    
    # Points fractionnaires : un barème en pourcentage sur une devise forte
    # produit des fractions de point (1 % de 58 USD = 0,58). Les tronquer les
    # faisait disparaître, et le compteur d'un marchand en USD ne bougeait
    # jamais. Les contraintes « positif » sont retirées : `reverse_points`
    # borne déjà les cumuls à vie, et un solde courant peut légitimement passer
    # sous zéro le temps d'une correction.
    total_points_earned = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    total_points_redeemed = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    current_points = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )

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

    def add_points(self, points, save: bool = True):
        """
        Ajoute des points au compte du client.

        Atomique en base via ``F()`` expressions : deux ventes simultanées
        du même client cumulent leurs points sans race condition.
        """
        from django.db.models import F
        from django.utils import timezone

        points = _as_points(points)

        if not save:
            # Modif in-memory uniquement (rare ; caller orchestre le save)
            self.current_points += points
            self.total_points_earned += points
            self.last_points_earned_at = timezone.now()
            return

        now = timezone.now()
        type(self).objects.filter(pk=self.pk).update(
            current_points=F('current_points') + points,
            total_points_earned=F('total_points_earned') + points,
            last_points_earned_at=now,
        )
        # Resynchroniser l'instance en mémoire (nécessaire si le caller lit
        # current_points après l'appel, ex. pour balance_after en log).
        self.refresh_from_db(
            fields=['current_points', 'total_points_earned', 'last_points_earned_at']
        )

    def redeem_points(self, points, save: bool = True):
        """
        Utilise des points du compte du client.

        Sérialise les utilisations concurrentes via ``select_for_update`` :
        le check ``points > current_points`` est verrouillé pour éviter
        qu'un client dépense ses points deux fois en parallèle.
        """
        from django.db import transaction
        from django.utils import timezone

        points = _as_points(points)

        if not save:
            if points > self.current_points:
                raise ValueError("Points insuffisants")
            self.current_points -= points
            self.total_points_redeemed += points
            self.last_points_redeemed_at = timezone.now()
            return

        with transaction.atomic():
            fresh = type(self).objects.select_for_update().get(pk=self.pk)
            if points > fresh.current_points:
                raise ValueError("Points insuffisants")
            fresh.current_points -= points
            fresh.total_points_redeemed += points
            fresh.last_points_redeemed_at = timezone.now()
            fresh.save(update_fields=[
                'current_points', 'total_points_redeemed', 'last_points_redeemed_at'
            ])
            # Synchroniser l'instance qui a appelé la méthode
            self.current_points = fresh.current_points
            self.total_points_redeemed = fresh.total_points_redeemed
            self.last_points_redeemed_at = fresh.last_points_redeemed_at

    def expire_points(self, points) -> Decimal:
        """
        Retire des points périmés et renvoie le nombre réellement retiré.

        Ne touche NI ``total_points_earned`` (ces points ont bien été gagnés),
        NI ``total_points_redeemed`` (ils n'ont pas été utilisés) : seul le solde
        courant diminue. Borné au solde disponible, relu sous verrou pour ne pas
        passer sous zéro si le client vient de dépenser en parallèle.
        """
        from django.db import transaction

        points = abs(_as_points(points))
        if points == 0:
            return Decimal('0.00')

        with transaction.atomic():
            fresh = type(self).objects.select_for_update().get(pk=self.pk)
            amount = min(points, max(Decimal('0.00'), fresh.current_points))
            if amount == 0:
                return Decimal('0.00')
            fresh.current_points -= amount
            fresh.save(update_fields=['current_points'])
            self.current_points = fresh.current_points
            return amount

    def reverse_points(self, points, kind: str):
        """
        Inverse un mouvement de points en corrigeant le BON compteur à vie.

        Détourner ``add_points(-points)`` pour annuler une utilisation gonflait
        ``total_points_earned`` au lieu de réduire ``total_points_redeemed`` ;
        inversement, annuler un gain pouvait faire passer
        ``total_points_earned`` sous zéro ; le bornage à zéro reste, même si le
        champ n'est plus contraint au positif en base.

        ``kind`` vaut ``'earn'`` (on annule un gain : les points partent) ou
        ``'redeem'`` (on annule une utilisation : les points reviennent).
        ``points`` est toujours une magnitude positive.
        """
        from django.db import transaction
        from django.utils import timezone

        points = abs(_as_points(points))
        if points == 0:
            return

        with transaction.atomic():
            fresh = type(self).objects.select_for_update().get(pk=self.pk)
            now = timezone.now()

            if kind == 'earn':
                fresh.current_points -= points
                # Bornage à 0 : le cumul à vie ne peut pas être négatif.
                fresh.total_points_earned = max(
                    Decimal('0.00'), fresh.total_points_earned - points
                )
                fresh.last_points_earned_at = now
                fields = [
                    'current_points', 'total_points_earned', 'last_points_earned_at',
                ]
            else:
                fresh.current_points += points
                fresh.total_points_redeemed = max(
                    Decimal('0.00'), fresh.total_points_redeemed - points
                )
                fresh.last_points_redeemed_at = now
                fields = [
                    'current_points', 'total_points_redeemed', 'last_points_redeemed_at',
                ]

            fresh.save(update_fields=fields)
            for field in fields:
                setattr(self, field, getattr(fresh, field))


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
        # Inversion explicite suite à l'annulation d'une vente. Distincts de
        # EARN/REDEEM pour que l'historique client (et les rapports) montrent
        # « Points retirés - annulation vente X » sans ambiguïté.
        EARN_REVERSAL = 'earn_reversal', 'Annulation gain'
        REDEEM_REVERSAL = 'redeem_reversal', 'Annulation utilisation'
    
    customer_loyalty = models.ForeignKey(
        CustomerLoyalty,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    
    transaction_type = models.CharField(
        max_length=20,
        choices=TransactionType.choices
    )
    
    points = models.DecimalField(
        max_digits=15, decimal_places=2,
        help_text="Positif pour gain, négatif pour utilisation"
    )

    balance_after = models.DecimalField(
        max_digits=15, decimal_places=2,
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
    
    # Règlement associé quand les points servent à payer une facture DÉJÀ émise
    # (via add-payment). Porte l'idempotence des REDEEM, puisqu'une même vente
    # peut être réglée en plusieurs fois, donc consommer des points plusieurs fois.
    payment = models.ForeignKey(
        'sales.Payment',
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
        constraints = [
            # Idempotence du GAIN : une vente ne donne ses points qu'une fois.
            # Une seconde tentative lève IntegrityError, attrapée côté service.
            # Les reversals sont uniques par vente pour la même raison.
            models.UniqueConstraint(
                fields=['sale', 'transaction_type'],
                condition=models.Q(transaction_type__in=['earn', 'earn_reversal', 'redeem_reversal']),
                name='loyalty_tx_unique_per_sale_type',
            ),
            # Idempotence de la CONSOMMATION à la création de vente : un seul
            # REDEEM sans règlement associé par vente (remise avant émission).
            models.UniqueConstraint(
                fields=['sale'],
                condition=models.Q(transaction_type='redeem', payment__isnull=True),
                name='loyalty_tx_unique_sale_redeem_at_creation',
            ),
            # Idempotence des règlements en points sur facture émise : une
            # facture peut être réglée en plusieurs fois, donc consommer des
            # points plusieurs fois, mais jamais deux fois pour le même règlement.
            models.UniqueConstraint(
                fields=['payment'],
                condition=models.Q(transaction_type='redeem', payment__isnull=False),
                name='loyalty_tx_unique_payment_redeem',
            ),
        ]

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
    receipt_paper_width = models.PositiveIntegerField(
        default=58,
        help_text="Largeur du papier du ticket en mm (58 ou 80)"
    )
    
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
