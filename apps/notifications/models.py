from django.db import models
from django.conf import settings
from apps.core.models import TenantModel, TimeStampedModel, UUIDModel


class NotificationTemplate(TenantModel):
    """
    Notification templates for different events.
    """
    
    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'
        SMS = 'sms', 'SMS'
        PUSH = 'push', 'Push'
        IN_APP = 'in_app', 'In-App'

    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100)
    
    channel = models.CharField(
        max_length=20,
        choices=Channel.choices,
        default=Channel.IN_APP
    )
    
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField()
    
    variables = models.JSONField(default=list, blank=True)
    
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'notification_templates'
        unique_together = ['organization', 'code', 'channel']

    def __str__(self):
        return f"{self.name} ({self.channel})"


class Notification(TenantModel):
    """
    User notifications.
    """
    
    class NotificationType(models.TextChoices):
        INFO = 'info', 'Information'
        SUCCESS = 'success', 'Succès'
        WARNING = 'warning', 'Avertissement'
        ERROR = 'error', 'Erreur'
        ALERT = 'alert', 'Alerte'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications'
    )
    
    notification_type = models.CharField(
        max_length=20,
        choices=NotificationType.choices,
        default=NotificationType.INFO
    )
    
    title = models.CharField(max_length=255)
    message = models.TextField()
    
    action_url = models.CharField(max_length=500, blank=True)
    action_label = models.CharField(max_length=100, blank=True)
    
    data = models.JSONField(default=dict, blank=True)
    
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read']),
            models.Index(fields=['organization', 'created_at']),
        ]

    def __str__(self):
        return f"{self.title} - {self.user.email}"

    def mark_as_read(self):
        from django.utils import timezone
        self.is_read = True
        self.read_at = timezone.now()
        self.save(update_fields=['is_read', 'read_at'])


class Alert(TenantModel):
    """
    System alerts for important events.
    E.g., low stock, expiring products, subscription expiry.
    """
    
    class AlertType(models.TextChoices):
        LOW_STOCK = 'low_stock', 'Stock bas'
        OUT_OF_STOCK = 'out_of_stock', 'Rupture de stock'
        EXPIRING_PRODUCT = 'expiring_product', 'Produit bientôt périmé'
        EXPIRED_PRODUCT = 'expired_product', 'Produit périmé'
        PAYMENT_DUE = 'payment_due', 'Paiement dû'
        PAYMENT_OVERDUE = 'payment_overdue', 'Paiement en retard'
        SUBSCRIPTION_EXPIRING = 'subscription_expiring', 'Abonnement expire bientôt'
        SUBSCRIPTION_EXPIRED = 'subscription_expired', 'Abonnement expiré'
        CREDIT_LIMIT = 'credit_limit', 'Limite de crédit atteinte'
        SYSTEM = 'system', 'Système'

    class Severity(models.TextChoices):
        LOW = 'low', 'Bas'
        MEDIUM = 'medium', 'Moyen'
        HIGH = 'high', 'Élevé'
        CRITICAL = 'critical', 'Critique'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Actif'
        ACKNOWLEDGED = 'acknowledged', 'Accusé'
        RESOLVED = 'resolved', 'Résolu'
        DISMISSED = 'dismissed', 'Ignoré'

    alert_type = models.CharField(
        max_length=30,
        choices=AlertType.choices
    )
    severity = models.CharField(
        max_length=20,
        choices=Severity.choices,
        default=Severity.MEDIUM
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )
    
    title = models.CharField(max_length=255)
    message = models.TextField()
    
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.UUIDField(null=True, blank=True)
    
    data = models.JSONField(default=dict, blank=True)
    
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acknowledged_alerts'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolved_alerts'
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'alerts'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['alert_type', 'severity']),
            models.Index(fields=['resource_type', 'resource_id']),
        ]

    def __str__(self):
        return f"{self.alert_type} - {self.title}"


class AlertRule(TenantModel):
    """
    Rules for automatic alert generation.
    """
    
    name = models.CharField(max_length=255)
    
    alert_type = models.CharField(
        max_length=30,
        choices=Alert.AlertType.choices
    )
    
    conditions = models.JSONField(default=dict)
    
    severity = models.CharField(
        max_length=20,
        choices=Alert.Severity.choices,
        default=Alert.Severity.MEDIUM
    )
    
    notify_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name='alert_rules'
    )
    
    notify_channels = models.JSONField(default=list)
    
    is_active = models.BooleanField(default=True)
    
    cooldown_minutes = models.PositiveIntegerField(default=60)
    last_triggered = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'alert_rules'

    def __str__(self):
        return self.name


class EmailLog(TimeStampedModel, UUIDModel):
    """
    Log of sent emails.
    """
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'En attente'
        SENT = 'sent', 'Envoyé'
        DELIVERED = 'delivered', 'Livré'
        FAILED = 'failed', 'Échoué'
        BOUNCED = 'bounced', 'Rebondi'

    organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='email_logs'
    )
    
    to_email = models.EmailField()
    from_email = models.EmailField()
    
    subject = models.CharField(max_length=500)
    body = models.TextField()
    
    template = models.ForeignKey(
        NotificationTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    sent_at = models.DateTimeField(null=True, blank=True)
    
    error_message = models.TextField(blank=True)
    
    external_id = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'email_logs'
        ordering = ['-created_at']

    def __str__(self):
        return f"Email to {self.to_email}: {self.subject}"
