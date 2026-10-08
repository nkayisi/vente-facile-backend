from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings
from django.utils import timezone
from decimal import Decimal
from apps.core.models import TimeStampedModel, UUIDModel
from apps.core.versions import (
    SEMVER,
    comparer_versions,
    segments_version,
    urls_magasin,
)

#: Les réglages de plateforme, mis en cache. Lus à CHAQUE réveil de CHAQUE
#: terminal : sans cache, chacun paierait un `get_or_create` pour quatre entiers
#: et huit chaînes qui ne changent que quelques fois par an.
CLE_REGLAGES = "vf:global_config:v1"

#: Filet, pas mécanisme : `GlobalConfig.save()` purge la clé, donc une correction
#: agit au réveil suivant. Ce délai ne couvre qu'une écriture hors du modèle
#: (SQL direct), qui n'existe nulle part aujourd'hui.
TTL_REGLAGES = 300


class Plan(TimeStampedModel, UUIDModel):
    """
    Subscription plans available in the SaaS.
    Global model - not tenant-scoped.
    """
    
    class BillingCycle(models.TextChoices):
        MONTHLY = 'monthly', 'Mensuel'
        QUARTERLY = 'quarterly', 'Trimestriel'
        YEARLY = 'yearly', 'Annuel'

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    
    price_monthly = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )
    price_yearly = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )

    currency = models.ForeignKey(
        'settings.Currency',
        on_delete=models.PROTECT,
        related_name='subscription_plans',
    )
    
    max_users = models.PositiveIntegerField(default=1)
    max_branches = models.PositiveIntegerField(default=1)
    max_warehouses = models.PositiveIntegerField(
        default=1,
        help_text="Nombre maximal d'entrepôts (non supprimés) par organisation.",
    )
    max_products = models.PositiveIntegerField(null=True, blank=True)
    max_monthly_transactions = models.PositiveIntegerField(null=True, blank=True)
    
    storage_limit_mb = models.PositiveIntegerField(default=100)
    
    features = models.JSONField(default=dict, blank=True)
    
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    
    trial_days = models.PositiveIntegerField(default=0)
    
    sort_order = models.PositiveIntegerField(default=0)

    #: Palier pour upgrade/downgrade (plus grand = offre plus riche). Anti-downgrade via Organization.subscription_floor_tier.
    tier = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = 'plans'
        ordering = ['sort_order', 'price_monthly']

    def __str__(self):
        return self.name

    def get_price(self, billing_cycle):
        if billing_cycle == self.BillingCycle.YEARLY:
            return self.price_yearly
        return self.price_monthly


class PlanFeature(TimeStampedModel, UUIDModel):
    """Features included in plans."""
    
    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name='plan_features'
    )
    
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    is_enabled = models.BooleanField(default=True)
    limit_value = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'plan_features'
        unique_together = ['plan', 'code']

    def __str__(self):
        return f"{self.plan.name} - {self.name}"


class Subscription(TimeStampedModel, UUIDModel):
    """
    Organization subscriptions.
    Tracks billing cycle, status, and usage.
    """
    
    class Status(models.TextChoices):
        TRIAL = 'trial', 'Essai'
        ACTIVE = 'active', 'Actif'
        PAST_DUE = 'past_due', 'En retard'
        CANCELLED = 'cancelled', 'Annulé'
        EXPIRED = 'expired', 'Expiré'
        SUSPENDED = 'suspended', 'Suspendu'

    organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.CASCADE,
        related_name='subscriptions'
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name='subscriptions'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.TRIAL
    )
    
    billing_cycle = models.CharField(
        max_length=20,
        choices=Plan.BillingCycle.choices,
        default=Plan.BillingCycle.MONTHLY
    )
    
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )
    # Devise de FACTURATION SaaS : celle du plan souscrit, pas la devise
    # d'exploitation du marchand (un plan tarifé en USD se facture en USD,
    # même à une boutique qui vend en CDF). Défaut vide, résolu dans `save()`.
    currency = models.CharField(max_length=3, blank=True, default='')
    
    trial_start = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    
    external_id = models.CharField(max_length=255, blank=True)
    
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'subscriptions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['current_period_end']),
        ]

    def __str__(self):
        return f"{self.organization.name} - {self.plan.name}"

    def save(self, *args, **kwargs):
        """La devise d'un abonnement est celle de son plan, jamais un défaut."""
        if not self.currency and self.plan_id:
            self.currency = self.plan.currency.code
        super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.status in [self.Status.TRIAL, self.Status.ACTIVE]

    @property
    def is_trial(self):
        return self.status == self.Status.TRIAL

    @property
    def days_remaining(self):
        if self.current_period_end:
            delta = self.current_period_end - timezone.now()
            return max(0, delta.days)
        return 0

    def can_add_user(self):
        current_users = self.organization.memberships.filter(is_active=True).count()
        return current_users < self.plan.max_users

    def can_add_branch(self):
        current_branches = self.organization.branches.filter(is_active=True).count()
        return current_branches < self.plan.max_branches

    def can_add_warehouse(self) -> bool:
        from apps.inventory.models import Warehouse

        cap = self.plan.max_warehouses
        n = Warehouse.objects.filter(organization=self.organization, is_deleted=False).count()
        return n < cap

    def can_add_products(self, count: int = 1) -> bool:
        """True si on peut créer encore `count` produits (hors soft-delete)."""
        if count < 1:
            return True
        max_p = self.plan.max_products
        if max_p is None:
            return True
        from apps.products.models import Product

        products_count = Product.objects.filter(
            organization=self.organization,
            is_deleted=False,
        ).count()
        return products_count + count <= max_p


class SubscriptionUsage(TimeStampedModel, UUIDModel):
    """
    Tracks usage metrics for subscriptions.
    Used for usage-based billing and limit enforcement.
    """
    
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name='usage_records'
    )
    
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    
    users_count = models.PositiveIntegerField(default=0)
    branches_count = models.PositiveIntegerField(default=0)
    products_count = models.PositiveIntegerField(default=0)
    transactions_count = models.PositiveIntegerField(default=0)
    storage_used_mb = models.PositiveIntegerField(default=0)
    
    api_calls = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'subscription_usage'
        ordering = ['-period_start']

    def __str__(self):
        return f"{self.subscription.organization.name} - {self.period_start.date()}"


class Invoice(TimeStampedModel, UUIDModel):
    """
    Subscription invoices.
    """
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Brouillon'
        PENDING = 'pending', 'En attente'
        PAID = 'paid', 'Payé'
        OVERDUE = 'overdue', 'En retard'
        CANCELLED = 'cancelled', 'Annulé'
        REFUNDED = 'refunded', 'Remboursé'

    organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.CASCADE,
        related_name='invoices'
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices'
    )
    
    invoice_number = models.CharField(max_length=50, unique=True)
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    
    subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )
    tax_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )
    total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    # Devise de FACTURATION SaaS : celle du plan souscrit, pas la devise
    # d'exploitation du marchand (un plan tarifé en USD se facture en USD,
    # même à une boutique qui vend en CDF). Défaut vide, résolu dans `save()`.
    currency = models.CharField(max_length=3, blank=True, default='')
    
    issue_date = models.DateField()
    due_date = models.DateField()
    paid_date = models.DateField(null=True, blank=True)
    
    period_start = models.DateField()
    period_end = models.DateField()
    
    notes = models.TextField(blank=True)
    
    external_id = models.CharField(max_length=255, blank=True)
    
    pdf_url = models.URLField(blank=True)

    class Meta:
        db_table = 'invoices'
        ordering = ['-issue_date']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['invoice_number']),
        ]

    def __str__(self):
        return f"Invoice {self.invoice_number}"

    def save(self, *args, **kwargs):
        """Une facture est libellée dans la devise de l'abonnement facturé."""
        if not self.currency and self.subscription_id:
            self.currency = self.subscription.currency
        super().save(*args, **kwargs)


class InvoiceItem(TimeStampedModel, UUIDModel):
    """Invoice line items."""
    
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='items'
    )
    
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1.00')
    )
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = 'invoice_items'

    def __str__(self):
        return self.description


class SubscriptionPayment(TimeStampedModel, UUIDModel):
    """
    Payments for subscriptions.
    """
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'En attente'
        COMPLETED = 'completed', 'Terminé'
        FAILED = 'failed', 'Échoué'
        REFUNDED = 'refunded', 'Remboursé'

    class PaymentMethod(models.TextChoices):
        CARD = 'card', 'Carte bancaire'
        MOBILE_MONEY = 'mobile_money', 'Mobile Money'
        BANK_TRANSFER = 'bank_transfer', 'Virement bancaire'
        MANUAL = 'manual', 'Manuel'

    organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.CASCADE,
        related_name='subscription_payments'
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments'
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments'
    )
    
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    # Devise de FACTURATION SaaS : celle du plan souscrit, pas la devise
    # d'exploitation du marchand (un plan tarifé en USD se facture en USD,
    # même à une boutique qui vend en CDF). Défaut vide, résolu dans `save()`.
    currency = models.CharField(max_length=3, blank=True, default='')
    
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    reference = models.CharField(max_length=255, blank=True, db_index=True)
    external_id = models.CharField(max_length=255, blank=True)
    external_transaction_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text='Identifiant transaction PayDRC / MOKO (paydrc_reference)',
    )
    
    paid_at = models.DateTimeField(null=True, blank=True)
    
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='subscription_payments_created'
    )
    
    metadata = models.JSONField(default=dict, blank=True)

    # Dernier passage de `reconcile_moko_payments` sur ce règlement. Sert
    # uniquement à espacer les réinterrogations : la réconciliation repart de la
    # BASE et balayait donc, toutes les 30 minutes, TOUS les paiements MOBILE
    # MONEY `pending` ou `failed` des 30 derniers jours, à raison d'un appel
    # HTTP MOKO par paiement. Un échec définitif restait ainsi réinterrogé
    # 1 440 fois avant de sortir de la fenêtre. `null` signifie « jamais
    # réconcilié », et passe donc en tête de file.
    last_reconciled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reconcile_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'subscription_payments'
        ordering = ['-created_at']

    def __str__(self):
        return f"Payment {self.amount} {self.currency} - {self.organization.name}"

    def save(self, *args, **kwargs):
        """Un règlement d'abonnement est dans la devise de l'abonnement réglé."""
        if not self.currency and self.subscription_id:
            self.currency = self.subscription.currency
        super().save(*args, **kwargs)


class GlobalConfig(models.Model):
    """
    Configuration globale du système (singleton).

    Deux familles de réglages y cohabitent : la durée d'essai et la grâce
    d'abonnement, et la PORTE DE MISE À JOUR du terminal mobile. Elles n'ont
    rien à voir l'une avec l'autre, mais elles partagent ce qui compte ici : ce
    sont des décisions de PLATEFORME, pas d'organisation, et elles doivent
    pouvoir changer sans redéploiement.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LES SEUILS DE VERSION NAISSENT VIDES, ET C'EST LA RÈGLE LA PLUS          │
    │ IMPORTANTE DE CE MODÈLE.                                                 │
    │                                                                          │
    │ Une valeur par défaut, fût-elle « 1.0.0 », bloquerait TOUT LE PARC à     │
    │ l'instant de la migration, avant même que quiconque ait ouvert l'admin.  │
    │ La chaîne vide veut dire « aucune exigence », et c'est aussi la SORTIE   │
    │ D'URGENCE : vider le champ rouvre la porte au réveil suivant.            │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ **Un seuil n'agit que sur les binaires qui portent le mécanisme.** Les
    versions antérieures l'ignorent, quoi qu'on publie : on livre d'abord la
    version qui sait se bloquer, on attend qu'elle soit diffusée, et seulement
    ensuite on pose un seuil.
    """

    _AIDE_MIN = (
        "Version minimale exigée, en trois segments (1.3.0). Laissez VIDE pour "
        "n'exiger aucune mise à jour. Ne posez un seuil qu'APRÈS que cette "
        "version soit réellement disponible dans le magasin : en dessous, le "
        "terminal est inutilisable. Vider ce champ rouvre la porte au réveil "
        "suivant."
    )
    _AIDE_COURANTE = (
        "Version servie aujourd'hui par le magasin, en trois segments. "
        "OBLIGATOIRE dès qu'un seuil est posé : c'est elle, et elle seule, qui "
        "interdit d'exiger une version qu'aucun terminal ne pourrait installer. "
        "Elle est aussi NOMMÉE sur l'écran de blocage, pour que le marchand "
        "sache quoi réclamer."
    )
    _AIDE_URL = (
        "Adresse de la fiche de l'application. Une fiche Play "
        "(play.google.com/store/apps/details?id=...) ouvre directement "
        "l'application du magasin. Toute autre adresse est servie telle quelle : "
        "revendeur, galerie tierce, ou lien d'APK direct pour les terminaux de "
        "comptoir sans magasin."
    )
    _AIDE_MESSAGE = (
        "Ce que le marchand lit sur l'écran de blocage, à la place du texte par "
        "défaut. C'est la SEULE issue d'un terminal sans magasin : « Appelez le "
        "0XX pour recevoir la nouvelle version »."
    )

    trial_days = models.PositiveIntegerField(
        default=15,
        help_text="Nombre de jours de la période d'essai pour les nouveaux comptes"
    )
    grace_period_days = models.PositiveIntegerField(
        default=3,
        help_text="Nombre de jours de grâce après expiration avant blocage total"
    )

    android_minimum_version = models.CharField(
        max_length=40, blank=True, default='', help_text=_AIDE_MIN
    )
    android_current_version = models.CharField(
        max_length=40, blank=True, default='', help_text=_AIDE_COURANTE
    )
    android_store_url = models.URLField(
        max_length=300, blank=True, default='', help_text=_AIDE_URL
    )
    android_update_message = models.TextField(
        blank=True, default='', help_text=_AIDE_MESSAGE
    )

    ios_minimum_version = models.CharField(
        max_length=40, blank=True, default='', help_text=_AIDE_MIN
    )
    ios_current_version = models.CharField(
        max_length=40, blank=True, default='', help_text=_AIDE_COURANTE
    )
    ios_store_url = models.URLField(
        max_length=300, blank=True, default='',
        help_text=(
            "Adresse de la fiche App Store. LAISSEZ VIDE tant que l'application "
            "n'y est pas publiée : iOS n'autorise aucune installation manuelle, "
            "donc un terminal iOS bloqué sans lien n'a littéralement aucune issue."
        ),
    )
    ios_update_message = models.TextField(
        blank=True, default='', help_text=_AIDE_MESSAGE
    )

    class Meta:
        db_table = 'global_config'
        verbose_name = 'Configuration globale'
        verbose_name_plural = 'Configuration globale'

    def __str__(self):
        return "Configuration globale"

    def clean(self):
        """
        Les trois règles qu'un formulaire doit opposer AVANT qu'un terminal ne les
        découvre. Partagées par l'admin Django et par l'API d'administration : les
        écrire deux fois, c'est les faire diverger.
        """
        super().clean()
        erreurs = {}
        for plateforme in ('android', 'ios'):
            erreurs.update(self._valider_porte(plateforme))
        if erreurs:
            raise ValidationError(erreurs)

    def _valider_porte(self, plateforme):
        minimum = (getattr(self, f'{plateforme}_minimum_version') or '').strip()
        courante = (getattr(self, f'{plateforme}_current_version') or '').strip()
        url = (getattr(self, f'{plateforme}_store_url') or '').strip()
        message = (getattr(self, f'{plateforme}_update_message') or '').strip()

        erreurs = {}
        for champ, valeur in (
            (f'{plateforme}_minimum_version', minimum),
            (f'{plateforme}_current_version', courante),
        ):
            if not valeur:
                continue
            # ⚠ LES DEUX CONTRÔLES, ET IL FAUT LES DEUX. `SEMVER` accepte vingt
            # chiffres par segment ; `segments_version` refuse au-delà de
            # `MAX_SEGMENT`, où le MIROIR JavaScript perdrait la précision. Sans
            # ce second contrôle, `comparer_versions` rendait `None`, la garde
            # anti-brique ne disait rien, et le seuil descendait sur des
            # terminaux qui le rejettent pour la même raison : la porte
            # paraissait armée et était INERTE.
            if not SEMVER.match(valeur) or segments_version(valeur) is None:
                erreurs[champ] = (
                    "Trois segments numériques attendus, par exemple 1.3.0."
                )

        # Une forme fautive rend les deux règles suivantes ininterprétables :
        # les annoncer en plus ferait chercher trois défauts là où il y en a un.
        if erreurs or not minimum:
            return erreurs

        # ┌──────────────────────────────────────────────────────────────────┐
        # │ LA VERSION DU MAGASIN EST OBLIGATOIRE DÈS QU'UN SEUIL EST POSÉ.  │
        # │                                                                  │
        # │ C'est elle, et elle seule, qui rend la garde anti-brique          │
        # │ opérante. Tant qu'elle était facultative, l'ordre naturel - poser │
        # │ le seuil d'abord, la nommer ensuite - sautait la garde            │
        # │ ENTIÈREMENT, et une faute de frappe (`1.30.0` pour `1.3.0`)       │
        # │ bloquait tout le parc sans qu'aucun terminal ne puisse s'y        │
        # │ conformer. Un champ dont l'absence désarme la seule protection du │
        # │ parc n'est pas un champ facultatif.                               │
        # └──────────────────────────────────────────────────────────────────┘
        if not courante:
            erreurs[f'{plateforme}_current_version'] = (
                "Nommez la version servie aujourd'hui par le magasin : c'est "
                "elle qui interdit de poser un seuil qu'aucun terminal ne peut "
                "atteindre."
            )
            # Sans elle, la règle suivante n'a rien à comparer. La tenter
            # ajouterait un second message pour un seul défaut.
            return erreurs

        # ⚠ LA RÈGLE QUI EMPÊCHE DE BRIQUER LE PARC. Un seuil au-dessus de ce
        # que le magasin sert bloque tout le monde sans qu'aucun terminal ne
        # puisse s'y conformer, et la seule réparation est de revenir ici.
        #
        # ⚠ On exige « seuil ≤ magasin », on ne refuse pas seulement le « > ».
        # Les deux numéros sont désormais lisibles, donc `None` n'est plus
        # atteignable - mais une garde qui traite l'illisible comme un accord
        # est celle qui revient le jour où une borne bouge.
        if comparer_versions(minimum, courante) not in (-1, 0):
            erreurs[f'{plateforme}_minimum_version'] = (
                "Ce seuil dépasse la version disponible dans le magasin "
                f"({courante}) : aucun terminal ne pourrait s'y conformer."
            )

        # Un mur sans rien dessus est le seul défaut irréparable DEPUIS le
        # terminal : le marchand lit « mettez à jour » et n'a nulle part où aller.
        if not url and not message:
            erreurs[f'{plateforme}_store_url'] = (
                "Un seuil exige une adresse de mise à jour ou, à défaut, un "
                "message qui dise au marchand comment obtenir la nouvelle version."
            )

        return erreurs

    def save(self, *args, **kwargs):
        # Singleton: force l'id à 1
        self.pk = 1
        super().save(*args, **kwargs)
        # ⚠ LA PURGE SUIT L'ÉCRITURE, JAMAIS L'INVERSE : purger d'abord
        # laisserait une lecture concurrente remettre l'ANCIENNE valeur en cache,
        # et la sortie d'urgence attendrait alors la fin du TTL.
        cache.delete(CLE_REGLAGES)

    @classmethod
    def get(cls):
        """Retourne l'instance unique de configuration, la crée si besoin."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def reglages(cls):
        """
        Les réglages de plateforme, en DICT PLAT et caché.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ JAMAIS L'INSTANCE, TOUJOURS DES PRIMITIFS.                           │
        │                                                                      │
        │ Un modèle Django se sérialise mal en travers d'un déploiement qui    │
        │ ajoute une colonne : le cache rendrait un objet à qui il manque un   │
        │ attribut, et l'erreur tomberait sur le chemin de RÉVEIL d'un         │
        │ terminal. C'est la règle déjà posée par `get_cached_block_state`.    │
        └──────────────────────────────────────────────────────────────────────┘

        Le cache n'est pas un confort : ce chemin est parcouru à chaque réveil de
        chaque terminal, et `get()` est un `get_or_create`. `save()` le purge,
        donc une correction faite en administration agit au réveil SUIVANT.
        """
        caches = cache.get(CLE_REGLAGES)
        if caches is not None:
            return caches

        obj = cls.get()
        valeurs = {
            'trial_days': obj.trial_days,
            'grace_period_days': obj.grace_period_days,
            'android': obj._porte('android'),
            'ios': obj._porte('ios'),
        }
        cache.set(CLE_REGLAGES, valeurs, timeout=TTL_REGLAGES)
        return valeurs

    def _porte(self, plateforme):
        """La porte de mise à jour d'une plateforme, prête à descendre sur le fil."""
        minimum = (getattr(self, f'{plateforme}_minimum_version') or '').strip()
        courante = (getattr(self, f'{plateforme}_current_version') or '').strip()
        message = (getattr(self, f'{plateforme}_update_message') or '').strip()
        url = (getattr(self, f'{plateforme}_store_url') or '').strip()
        return {
            # `None` et non la chaîne vide : côté terminal, « absent » et « vide »
            # doivent se lire de la même façon, et `null` ne se confond avec rien.
            'minimum_version': minimum or None,
            'current_version': courante or None,
            'store_urls': urls_magasin(plateforme, url),
            'message': message or None,
        }
