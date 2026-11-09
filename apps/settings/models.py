"""
Settings models for organization configuration.
Includes multi-currency support and loyalty program.
"""
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal, ROUND_HALF_UP
from apps.core.models import TenantModel, TimeStampedModel, UUIDModel

# La cagnotte se compte au centième, comme tout montant du projet.
MONEY_PRECISION = Decimal('0.01')

# Borne dure du plafond de rédemption : quelle que soit la configuration, une
# facture garde toujours au moins 30 % à encaisser en monnaie. Le réglage de
# l'organisation ne peut que durcir ce plafond, jamais le desserrer.
MAX_REDEMPTION_PERCENT_CEILING = Decimal('70.00')


def _as_money(value) -> Decimal:
    """
    Normalise un montant de cagnotte au centième.

    Les appelants passent indifféremment un ``int``, un ``float`` ou un
    ``Decimal`` ; sans normalisation, additionner un ``float`` à un ``Decimal``
    lève, et un ``Decimal`` non quantifié ferait dériver le compteur.
    """
    return Decimal(str(value or 0)).quantize(MONEY_PRECISION)


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
    La cagnotte de fidélité d'un établissement : trois nombres, et c'est tout.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LA FIDÉLITÉ SE COMPTE EN ARGENT, PLUS EN POINTS.                        │
    │                                                                          │
    │ Le programme portait HUIT réglages - mode de calcul, points par tranche, │
    │ montant de la tranche, pourcentage, valeur d'un point, minimum           │
    │ d'utilisation, plafond, expiration - plus un second mécanisme de         │
    │ récompenses. Personne ne pouvait dire ce qu'un point valait sans         │
    │ croiser trois champs, et le client lisait « 372,65 points » sur son      │
    │ ticket sans savoir ce qu'il pouvait en faire.                            │
    │                                                                          │
    │ Il n'en reste QUE ce qui se dit en une phrase : « le client récupère     │
    │ X % de chaque facture dans sa cagnotte, et peut en régler jusqu'à Y %    │
    │ d'une facture ». La cagnotte est un MONTANT, en devise principale.       │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    name = models.CharField(max_length=255, default="Programme de fidélité")
    is_active = models.BooleanField(default=False)

    # Ce que la facture rend au client, en pourcentage de son total.
    earn_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(Decimal('100.00'))],
        help_text="Part de la facture reversée dans la cagnotte du client (en %)"
    )

    # Plafond de rédemption : part d'une facture réglable avec la cagnotte.
    max_redemption_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('50.00'),
        validators=[
            MinValueValidator(Decimal('0.01')),
            MaxValueValidator(MAX_REDEMPTION_PERCENT_CEILING),
        ],
        help_text=(
            "Part maximale d'une facture réglable avec la cagnotte "
            f"(maximum {MAX_REDEMPTION_PERCENT_CEILING:.0f} %)"
        )
    )

    # Règles d'éligibilité
    only_registered_customers = models.BooleanField(
        default=True,
        help_text="Seuls les clients enregistrés peuvent accumuler une cagnotte"
    )

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ `legacy_point_value` EST TRANSITOIRE, ET IL FERME UNE CLASSE D'ERREUR.  │
    # │                                                                          │
    # │ Une opération `sale.create` ou `sale.add_payment` mise en file AVANT la │
    # │ bascule porte `points_used` en POINTS. Rejouée après, la traiter comme  │
    # │ un montant la sous-évaluerait d'un facteur `point_value` - et le client │
    # │ aurait payé en cagnotte bien moins que ce que le caissier lui a         │
    # │ annoncé. Le serializer lit l'alias legacy et multiplie par cette        │
    # │ valeur ; voir `apps/sales/serializers.py`.                              │
    # │                                                                          │
    # │ Droppable dès que le parc est à jour.                                    │
    # └──────────────────────────────────────────────────────────────────────────┘
    legacy_point_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, editable=False,
        help_text="Valeur d'un point avant la bascule en argent. Transitoire."
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

    def credit_for(self, amount) -> Decimal:
        """
        Cagnotte gagnée pour un montant donné, en **devise principale**.

        Le pourcentage est CONTINU par nature : on ne tronque pas. Le barème
        par tranches (« X points pour chaque Y dépensé ») a disparu avec les
        points - c'était le réglage dont le résultat ne se devinait pas, et
        c'est lui qui faisait qu'un petit panier ne rapportait rien.
        """
        if not self.is_active:
            return Decimal('0.00')

        amount = Decimal(amount or 0)
        if amount <= 0:
            return Decimal('0.00')

        return (amount * self.earn_percent / 100).quantize(
            MONEY_PRECISION, rounding=ROUND_HALF_UP
        )

    def max_redeemable_amount(self, amount) -> Decimal:
        """
        Part de ``amount`` réglable avec la cagnotte, dans la devise de ``amount``.

        Seule source du plafond : la cagnotte ne solde jamais toute une
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


class CustomerLoyalty(TenantModel):
    """
    La cagnotte d'un client : un solde en devise principale, et ses deux cumuls.
    """
    customer = models.OneToOneField(
        'contacts.Customer',
        on_delete=models.CASCADE,
        related_name='loyalty'
    )

    # Montants au centième, en devise PRINCIPALE. Les contraintes « positif »
    # sont retirées : `reverse` borne déjà les cumuls à vie, et un solde
    # courant peut légitimement passer sous zéro le temps d'une correction.
    total_earned = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    total_used = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )
    balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00')
    )

    tier = models.CharField(max_length=50, blank=True, default='')

    last_earned_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

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
        return f"{self.customer.name} - {self.balance}"

    def credit(self, amount, save: bool = True):
        """
        Crédite la cagnotte du client.

        Atomique en base via ``F()`` expressions : deux ventes simultanées
        du même client cumulent sans race condition.
        """
        from django.db.models import F
        from django.utils import timezone

        amount = _as_money(amount)

        if not save:
            # Modif in-memory uniquement (rare ; caller orchestre le save)
            self.balance += amount
            self.total_earned += amount
            self.last_earned_at = timezone.now()
            return

        from apps.core.bulk import bulk_update_rows

        now = timezone.now()
        # `bulk_update_rows` et non `update()` : un `queryset.update()` ne passe
        # pas par `save()`, donc `updated_at` ne bouge pas, donc la ligne devient
        # INVISIBLE au tirage. Relevé en base : 704,13 côté serveur, 372,65 sur
        # le terminal, avec le même horodatage. Le `F()` reste : c'est lui qui
        # sérialise deux ventes simultanées du même client.
        bulk_update_rows(
            type(self).objects.filter(pk=self.pk),
            balance=F('balance') + amount,
            total_earned=F('total_earned') + amount,
            last_earned_at=now,
        )
        # Resynchroniser l'instance en mémoire (nécessaire si le caller lit
        # `balance` après l'appel, ex. pour `balance_after` en registre).
        self.refresh_from_db(fields=['balance', 'total_earned', 'last_earned_at'])

    def debit(self, amount, save: bool = True):
        """
        Consomme une part de la cagnotte.

        Sérialise les utilisations concurrentes via ``select_for_update`` :
        le contrôle ``amount > balance`` est verrouillé pour éviter qu'un
        client dépense sa cagnotte deux fois en parallèle.
        """
        from django.db import transaction
        from django.utils import timezone

        from apps.core.exceptions import RefusMetier

        amount = _as_money(amount)

        if not save:
            if amount > self.balance:
                raise RefusMetier("Cagnotte insuffisante")
            self.balance -= amount
            self.total_used += amount
            self.last_used_at = timezone.now()
            return

        with transaction.atomic():
            fresh = type(self).objects.select_for_update().get(pk=self.pk)
            if amount > fresh.balance:
                raise RefusMetier("Cagnotte insuffisante")
            fresh.balance -= amount
            fresh.total_used += amount
            fresh.last_used_at = timezone.now()
            fresh.save(update_fields=['balance', 'total_used', 'last_used_at'])
            # Synchroniser l'instance qui a appelé la méthode
            self.balance = fresh.balance
            self.total_used = fresh.total_used
            self.last_used_at = fresh.last_used_at

    def reverse(self, amount, kind: str):
        """
        Inverse un mouvement de cagnotte en corrigeant le BON compteur à vie.

        Détourner ``credit(-amount)`` pour annuler une utilisation gonflait
        ``total_earned`` au lieu de réduire ``total_used`` ; inversement,
        annuler un gain pouvait faire passer ``total_earned`` sous zéro ; le
        bornage à zéro reste, même si le champ n'est plus contraint au positif.

        ``kind`` vaut ``'earn'`` (on annule un gain : la cagnotte baisse) ou
        ``'redeem'`` (on annule une utilisation : la cagnotte revient).
        ``amount`` est toujours une magnitude positive.
        """
        from django.db import transaction
        from django.utils import timezone

        amount = abs(_as_money(amount))
        if amount == 0:
            return

        with transaction.atomic():
            fresh = type(self).objects.select_for_update().get(pk=self.pk)
            now = timezone.now()

            if kind == 'earn':
                fresh.balance -= amount
                # Bornage à 0 : le cumul à vie ne peut pas être négatif.
                fresh.total_earned = max(
                    Decimal('0.00'), fresh.total_earned - amount
                )
                fresh.last_earned_at = now
                fields = ['balance', 'total_earned', 'last_earned_at']
            else:
                fresh.balance += amount
                fresh.total_used = max(
                    Decimal('0.00'), fresh.total_used - amount
                )
                fresh.last_used_at = now
                fields = ['balance', 'total_used', 'last_used_at']

            fresh.save(update_fields=fields)
            for field in fields:
                setattr(self, field, getattr(fresh, field))


class LoyaltyTransaction(TenantModel):
    """
    Registre de tous les mouvements de cagnotte.
    """

    class TransactionType(models.TextChoices):
        EARN = 'earn', 'Cagnotte gagnée'
        REDEEM = 'redeem', 'Cagnotte utilisée'
        # ⚠ `EXPIRE` N'A PLUS AUCUN PRODUCTEUR, et il RESTE dans l'énumération :
        # l'expiration a été retirée avec les points, mais des lignes
        # historiques le portent, et une écriture passée est une trace. La
        # retirer ferait sortir ces lignes sans libellé.
        EXPIRE = 'expire', 'Cagnotte expirée'
        ADJUST = 'adjust', 'Ajustement manuel'
        BONUS = 'bonus', 'Bonus'
        # Inversion explicite suite à l'annulation d'une vente. Distincts de
        # EARN/REDEEM pour que l'historique client (et les rapports) montrent
        # « Cagnotte retirée - annulation vente X » sans ambiguïté.
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

    amount = models.DecimalField(
        max_digits=15, decimal_places=2,
        help_text="Positif pour un gain, négatif pour une utilisation"
    )

    balance_after = models.DecimalField(
        max_digits=15, decimal_places=2,
        help_text="Solde de la cagnotte après le mouvement"
    )

    # Référence optionnelle à une vente
    sale = models.ForeignKey(
        'sales.Sale',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loyalty_transactions'
    )

    # Règlement associé quand la cagnotte sert à payer une facture DÉJÀ émise
    # (via add-payment). Porte l'idempotence des REDEEM, puisqu'une même vente
    # peut être réglée en plusieurs fois, donc puiser plusieurs fois.
    payment = models.ForeignKey(
        'sales.Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='loyalty_transactions'
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
            # Idempotence du GAIN : une vente ne donne sa cagnotte qu'une fois.
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
            # Idempotence des règlements en cagnotte sur facture émise : une
            # facture peut être réglée en plusieurs fois, donc puiser plusieurs
            # fois, mais jamais deux fois pour le même règlement.
            models.UniqueConstraint(
                fields=['payment'],
                condition=models.Q(transaction_type='redeem', payment__isnull=False),
                name='loyalty_tx_unique_payment_redeem',
            ),
        ]

    def __str__(self):
        # ⚠ `:+d` LEVAIT SUR UN `Decimal` : le champ est décimal depuis que les
        # barèmes en pourcentage ont produit des fractions, et `__str__` n'avait
        # pas suivi - toute trace d'une ligne de registre (admin, journal,
        # message d'erreur) plantait.
        return f"{self.customer_loyalty.customer.name} - {self.amount:+.2f}"


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
    show_loyalty_on_receipt = models.BooleanField(
        default=True,
        help_text="Afficher la cagnotte gagnée et le solde du client sur les reçus"
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
