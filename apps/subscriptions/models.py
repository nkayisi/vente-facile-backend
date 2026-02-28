from django.db import models
from django.conf import settings
from django.utils import timezone
from decimal import Decimal
from apps.core.models import TimeStampedModel, UUIDModel


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
    
    currency = models.CharField(max_length=3, default='USD')
    
    max_users = models.PositiveIntegerField(default=1)
    max_branches = models.PositiveIntegerField(default=1)
    max_products = models.PositiveIntegerField(null=True, blank=True)
    max_monthly_transactions = models.PositiveIntegerField(null=True, blank=True)
    
    storage_limit_mb = models.PositiveIntegerField(default=100)
    
    features = models.JSONField(default=dict, blank=True)
    
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    
    trial_days = models.PositiveIntegerField(default=14)
    
    sort_order = models.PositiveIntegerField(default=0)

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
    currency = models.CharField(max_length=3, default='USD')
    
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
    
    currency = models.CharField(max_length=3, default='USD')
    
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
    currency = models.CharField(max_length=3, default='USD')
    
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    reference = models.CharField(max_length=255, blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    
    paid_at = models.DateTimeField(null=True, blank=True)
    
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='subscription_payments_created'
    )
    
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'subscription_payments'
        ordering = ['-created_at']

    def __str__(self):
        return f"Payment {self.amount} {self.currency} - {self.organization.name}"


class GlobalConfig(models.Model):
    """
    Configuration globale du système (singleton).
    Gérée uniquement via l'admin Django.
    """
    trial_days = models.PositiveIntegerField(
        default=15,
        help_text="Nombre de jours de la période d'essai pour les nouveaux comptes"
    )
    grace_period_days = models.PositiveIntegerField(
        default=3,
        help_text="Nombre de jours de grâce après expiration avant blocage total"
    )
    
    class Meta:
        db_table = 'global_config'
        verbose_name = 'Configuration globale'
        verbose_name_plural = 'Configuration globale'

    def __str__(self):
        return "Configuration globale"

    def save(self, *args, **kwargs):
        # Singleton: force l'id à 1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        """Retourne l'instance unique de configuration, la crée si besoin."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
