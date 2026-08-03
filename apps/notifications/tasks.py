"""
Celery tasks for notifications and alerts.
"""
from celery import shared_task
from django.utils import timezone
from datetime import timedelta


@shared_task
def check_low_stock_alerts():
    """Check for products with low stock and create alerts."""
    from django.db.models import Sum, F, Value, DecimalField
    from django.db.models.functions import Coalesce
    from apps.products.models import Product
    from apps.notifications.models import Alert

    # Stock disponible total agrégé en base + filtre directement sur les produits
    # sous le seuil (une requête au lieu d'une requête Stock par produit).
    # Un produit sans ligne de stock → total 0 (out-of-stock), comme avant.
    products = list(Product.objects.filter(
        is_active=True,
        is_deleted=False,
        track_inventory=True
    ).select_related('organization').annotate(
        total_available=Coalesce(
            Sum(F('stocks__quantity') - F('stocks__reserved_quantity')),
            Value(0, output_field=DecimalField(max_digits=20, decimal_places=3)),
        )
    ).filter(total_available__lte=F('reorder_point')))

    if not products:
        return

    # Produits ayant déjà une alerte de stock ACTIVE (une seule requête).
    # On couvre LOW_STOCK ET OUT_OF_STOCK : un produit à 0 génère une alerte
    # OUT_OF_STOCK, donc filtrer sur le seul type LOW_STOCK recréerait un
    # doublon à chaque exécution.
    already_alerted = set(
        Alert.objects.filter(
            alert_type__in=[Alert.AlertType.LOW_STOCK, Alert.AlertType.OUT_OF_STOCK],
            resource_type='product',
            resource_id__in=[p.id for p in products],
            status=Alert.Status.ACTIVE,
        ).values_list('resource_id', flat=True)
    )

    for product in products:
        if product.id in already_alerted:
            continue

        total_stock = product.total_available
        severity = Alert.Severity.CRITICAL if total_stock == 0 else Alert.Severity.HIGH
        alert_type = Alert.AlertType.OUT_OF_STOCK if total_stock == 0 else Alert.AlertType.LOW_STOCK

        Alert.objects.create(
            organization=product.organization,
            alert_type=alert_type,
            severity=severity,
            title=f"Stock bas: {product.name}",
            message=f"Le produit {product.name} (SKU: {product.sku}) a un stock de {total_stock}. "
                   f"Le seuil de réapprovisionnement est de {product.reorder_point}.",
            resource_type='product',
            resource_id=product.id,
            data={
                'product_id': str(product.id),
                'product_name': product.name,
                'current_stock': float(total_stock),
                'reorder_point': product.reorder_point
            }
        )


@shared_task
def check_expiring_products():
    """Check for products nearing expiry and create alerts."""
    from django.conf import settings
    from apps.inventory.models import StockBatch
    from apps.notifications.models import Alert
    
    warning_days = settings.VENTE_FACILE.get('EXPIRY_WARNING_DAYS', 30)
    warning_date = timezone.now().date() + timedelta(days=warning_days)
    
    expiring_batches = StockBatch.objects.filter(
        expiry_date__lte=warning_date,
        expiry_date__gte=timezone.now().date(),
        quantity__gt=0
    ).select_related('product', 'organization')
    
    for batch in expiring_batches:
        existing_alert = Alert.objects.filter(
            organization=batch.organization,
            alert_type=Alert.AlertType.EXPIRING_PRODUCT,
            resource_type='stock_batch',
            resource_id=batch.id,
            status=Alert.Status.ACTIVE
        ).exists()
        
        if not existing_alert:
            days_until_expiry = (batch.expiry_date - timezone.now().date()).days
            
            Alert.objects.create(
                organization=batch.organization,
                alert_type=Alert.AlertType.EXPIRING_PRODUCT,
                severity=Alert.Severity.HIGH if days_until_expiry <= 7 else Alert.Severity.MEDIUM,
                title=f"Produit bientôt périmé: {batch.product.name}",
                message=f"Le lot {batch.batch_number} du produit {batch.product.name} "
                       f"expire le {batch.expiry_date}. Quantité: {batch.quantity}",
                resource_type='stock_batch',
                resource_id=batch.id,
                data={
                    'batch_id': str(batch.id),
                    'product_name': batch.product.name,
                    'batch_number': batch.batch_number,
                    'expiry_date': str(batch.expiry_date),
                    'quantity': float(batch.quantity)
                }
            )


@shared_task
def check_subscription_expiry():
    """Check for subscriptions nearing expiry."""
    from apps.subscriptions.models import Subscription
    from apps.notifications.models import Alert, Notification
    
    warning_date = timezone.now() + timedelta(days=7)
    
    expiring_subscriptions = Subscription.objects.filter(
        status=Subscription.Status.ACTIVE,
        current_period_end__lte=warning_date,
        current_period_end__gte=timezone.now()
    ).select_related('organization', 'plan')
    
    for subscription in expiring_subscriptions:
        existing_alert = Alert.objects.filter(
            organization=subscription.organization,
            alert_type=Alert.AlertType.SUBSCRIPTION_EXPIRING,
            status=Alert.Status.ACTIVE
        ).exists()
        
        if not existing_alert:
            days_remaining = (subscription.current_period_end - timezone.now()).days
            
            Alert.objects.create(
                organization=subscription.organization,
                alert_type=Alert.AlertType.SUBSCRIPTION_EXPIRING,
                severity=Alert.Severity.CRITICAL if days_remaining <= 3 else Alert.Severity.HIGH,
                title="Abonnement expire bientôt",
                message=f"Votre abonnement {subscription.plan.name} expire dans {days_remaining} jours. "
                       f"Renouvelez pour éviter une interruption de service.",
                data={
                    'subscription_id': str(subscription.id),
                    'plan_name': subscription.plan.name,
                    'expiry_date': str(subscription.current_period_end)
                }
            )
            
            owners = subscription.organization.memberships.filter(
                role='owner',
                is_active=True
            ).select_related('user')
            
            for membership in owners:
                Notification.objects.create(
                    organization=subscription.organization,
                    user=membership.user,
                    notification_type=Notification.NotificationType.WARNING,
                    title="Abonnement expire bientôt",
                    message=f"Votre abonnement expire dans {days_remaining} jours.",
                    action_url='/settings/subscription',
                    action_label='Renouveler'
                )


@shared_task
def send_daily_sales_report():
    """Send daily sales summary to organization admins."""
    from apps.organizations.models import Organization
    from apps.sales.models import Sale
    from apps.notifications.models import Notification
    from django.db.models import Sum, Count
    
    yesterday = timezone.now().date() - timedelta(days=1)
    
    organizations = Organization.objects.filter(is_active=True)
    
    for org in organizations:
        sales = Sale.objects.filter(
            organization=org,
            status='completed',
            sale_date__date=yesterday
        ).aggregate(
            total=Sum('total'),
            count=Count('id')
        )
        
        if sales['count'] and sales['count'] > 0:
            admins = org.memberships.filter(
                role__in=['owner', 'admin', 'manager'],
                is_active=True
            ).select_related('user')
            
            for membership in admins:
                Notification.objects.create(
                    organization=org,
                    user=membership.user,
                    notification_type=Notification.NotificationType.INFO,
                    title=f"Résumé des ventes - {yesterday}",
                    message=f"Nombre de ventes: {sales['count']}, Total: {sales['total'] or 0} CDF",
                    action_url='/reports/sales',
                    action_label='Voir détails'
                )


@shared_task
def cleanup_old_notifications():
    """Delete notifications older than 30 days."""
    from apps.notifications.models import Notification
    
    cutoff_date = timezone.now() - timedelta(days=30)
    
    deleted_count, _ = Notification.objects.filter(
        created_at__lt=cutoff_date,
        is_read=True
    ).delete()
    
    return f"Deleted {deleted_count} old notifications"


@shared_task
def send_email_notification(notification_id):
    """Send email for a notification."""
    from django.core.mail import send_mail
    from django.conf import settings
    from apps.notifications.models import Notification, EmailLog
    
    try:
        notification = Notification.objects.select_related('user').get(id=notification_id)
        
        email_log = EmailLog.objects.create(
            organization=notification.organization,
            to_email=notification.user.email,
            from_email=settings.DEFAULT_FROM_EMAIL,
            subject=notification.title,
            body=notification.message,
            status=EmailLog.Status.PENDING
        )
        
        send_mail(
            subject=notification.title,
            message=notification.message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[notification.user.email],
            fail_silently=False
        )
        
        email_log.status = EmailLog.Status.SENT
        email_log.sent_at = timezone.now()
        email_log.save()
        
    except Exception as e:
        if 'email_log' in locals():
            email_log.status = EmailLog.Status.FAILED
            email_log.error_message = str(e)
            email_log.save()
        raise
