"""
Celery tasks for notifications and alerts.
"""
from celery import shared_task
from django.db.models import F
from django.utils import timezone
from datetime import timedelta


def _expiring_quantity(batch):
    """
    Restant d'un lot qui périme, en unité de détail nommée.

    Un lot suit une date de péremption et non un emballage : rien n'y indique
    combien de ses unités sont encore scellées. Annoncer des contenants ferait
    donc dire au lot ce qu'il ne sait pas.
    """
    from apps.inventory.packaging import PackagingService

    return PackagingService.format_base_total(batch.product, batch.quantity)


def _lecture_du_rayon(rayon):
    """
    Le stock d'UN rayon dans ses propres termes.

    ⚠ La ventilation n'est rendue que si RIEN n'est réservé. Le total qui
    déclenche l'alerte est un DISPONIBLE (quantité moins réservé) alors que les
    deux compteurs portent la quantité brute : les présenter ensemble
    laisserait croire que 4 casiers sont libres alors qu'un est réservé. Sinon
    on nomme l'unité de détail, ce qui reste lisible sans rien affirmer de faux.
    """
    from apps.inventory.packaging import PackagingService

    produit = rayon.product
    if PackagingService.factor(produit) is None:
        return PackagingService.format_base_total(produit, rayon.quantity)
    if (rayon.reserved_quantity or 0) > 0:
        return PackagingService.format_base_total(produit, rayon.quantity)
    return PackagingService.format_split(
        produit, rayon.package_quantity or 0, rayon.loose_quantity or 0
    )


@shared_task
def check_low_stock_alerts():
    """
    Prévient des rayons sous leur seuil, PAR ENTREPÔT.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ L'ALERTE ÉTAIT AGRÉGÉE SUR TOUTE L'ORGANISATION, DONC INADRESSABLE.     │
    │                                                                          │
    │ Elle sommait les dépôts et ne nommait aucun : impossible, alors, de      │
    │ tenir la règle de périmètre - un gérant doit être alerté pour SES        │
    │ dépôts. Ou bien il recevait des ruptures d'un magasin où il ne met pas   │
    │ les pieds, ou bien il ne recevait rien. Et le chiffre lui-même mentait   │
    │ par omission : « 3 casiers » tous dépôts confondus ne dit pas que la     │
    │ boutique est vide pendant que la réserve est pleine, ce qui est          │
    │ exactement le cas qu'un réassort doit traiter.                           │
    │                                                                          │
    │ L'alerte porte désormais son entrepôt (`data.warehouse_id`), et c'est    │
    │ lui que `notifications.services.destinataires` oppose.                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ **UN RAYON QUI SE VIDE EST UN FAIT NOUVEAU.** La déduplication couvrait
    `LOW_STOCK` et `OUT_OF_STOCK` ensemble - juste pour ne pas répéter la même
    alerte à chaque battement, faux pour une AGGRAVATION : un produit déjà
    signalé « bas » qui tombe à zéro ne produisait plus rien, et le marchand
    n'apprenait jamais la rupture. La déduplication est désormais par (rayon,
    TYPE), et le passage de l'un à l'autre résout l'alerte précédente.
    """
    from django.db.models import F
    from apps.inventory.models import Stock
    from apps.inventory.packaging import PackagingService
    from apps.notifications.models import Alert
    from apps.notifications.services import alerter

    # Le critère est celui de `StockFilter` et de `lignes_proposees` : suivi,
    # seuil réel, quantité au-dessous. Trois conditions, et il faut les trois.
    rayons = list(
        Stock.objects
        .filter(
            product__is_active=True,
            product__is_deleted=False,
            product__track_inventory=True,
            product__reorder_point__gt=0,
            quantity__lte=F('product__reorder_point'),
        )
        .select_related(
            'organization', 'warehouse',
            'product', 'product__unit', 'product__packaging_unit',
        )
    )
    if not rayons:
        return

    def _type(rayon):
        return (
            Alert.AlertType.OUT_OF_STOCK if rayon.quantity <= 0
            else Alert.AlertType.LOW_STOCK
        )

    # Les alertes ACTIVES de ces rayons, en une requête. La clé est le couple
    # (rayon, type) : c'est ce qui laisse passer l'aggravation.
    actives = {}
    for a in Alert.objects.filter(
        alert_type__in=[Alert.AlertType.LOW_STOCK, Alert.AlertType.OUT_OF_STOCK],
        resource_type='stock',
        resource_id__in=[r.id for r in rayons],
        status=Alert.Status.ACTIVE,
    ):
        actives.setdefault(a.resource_id, []).append(a)

    for rayon in rayons:
        genre = _type(rayon)
        deja = actives.get(rayon.id, [])
        if any(a.alert_type == genre for a in deja):
            continue

        # ⚠ L'ANCIENNE ALERTE SE RÉSOUT, elle ne s'empile pas : un rayon vide
        # n'est plus « bas », et laisser les deux actives ferait compter deux
        # fois le même rayon dans tout décompte d'alertes.
        perimees = [a for a in deja if a.alert_type != genre]
        if perimees:
            Alert.objects.filter(id__in=[a.id for a in perimees]).update(
                status=Alert.Status.RESOLVED, resolved_at=timezone.now(),
                updated_at=timezone.now(),
            )

        produit = rayon.product
        stock_text = _lecture_du_rayon(rayon)
        reorder_text = PackagingService.format_base_total(
            produit, produit.reorder_point
        )
        vide = genre == Alert.AlertType.OUT_OF_STOCK

        alerter(
            rayon.organization,
            alert_type=genre,
            severity=Alert.Severity.CRITICAL if vide else Alert.Severity.HIGH,
            title=(
                f"Rupture : {produit.name} ({rayon.warehouse.name})" if vide
                else f"Stock bas : {produit.name} ({rayon.warehouse.name})"
            ),
            message=(
                f"{produit.name} (SKU : {produit.sku}) est à {stock_text} "
                f"dans {rayon.warehouse.name}. "
                f"Le seuil de réapprovisionnement est de {reorder_text}."
            ),
            resource_type='stock',
            resource_id=rayon.id,
            data={
                'product_id': str(produit.id),
                'product_name': produit.name,
                # ⚠ C'EST CE CHAMP QUI REND L'ALERTE ADRESSABLE. Sans lui, le
                # service de livraison ne peut pas la borner à un dépôt.
                'warehouse_id': str(rayon.warehouse_id),
                'warehouse_name': rayon.warehouse.name,
                'current_stock': float(rayon.quantity),
                'current_stock_display': stock_text,
                'reorder_point': float(produit.reorder_point),
            },
            action_url='/dashboard/stock/stock-levels',
            action_label='Voir le rayon',
        )


@shared_task
def check_expiring_products():
    """Check for products nearing expiry and create alerts."""
    from django.conf import settings
    from apps.inventory.models import StockBatch
    from apps.notifications.models import Alert
    from apps.notifications.services import alerter
    
    warning_days = settings.VENTE_FACILE.get('EXPIRY_WARNING_DAYS', 30)
    warning_date = timezone.localdate() + timedelta(days=warning_days)
    
    expiring_batches = StockBatch.objects.filter(
        expiry_date__lte=warning_date,
        expiry_date__gte=timezone.localdate(),
        quantity__gt=0
    ).select_related('product', 'product__unit', 'organization', 'warehouse')
    
    for batch in expiring_batches:
        existing_alert = Alert.objects.filter(
            organization=batch.organization,
            alert_type=Alert.AlertType.EXPIRING_PRODUCT,
            resource_type='stock_batch',
            resource_id=batch.id,
            status=Alert.Status.ACTIVE
        ).exists()
        
        if not existing_alert:
            days_until_expiry = (batch.expiry_date - timezone.localdate()).days
            
            alerter(
                batch.organization,
                alert_type=Alert.AlertType.EXPIRING_PRODUCT,
                severity=Alert.Severity.HIGH if days_until_expiry <= 7 else Alert.Severity.MEDIUM,
                title=f"Produit bientôt périmé: {batch.product.name}",
                message=f"Le lot {batch.batch_number} du produit {batch.product.name} "
                       f"expire le {batch.expiry_date}. "
                       f"Quantité: {_expiring_quantity(batch)}",
                resource_type='stock_batch',
                resource_id=batch.id,
                data={
                    'batch_id': str(batch.id),
                    'product_name': batch.product.name,
                    'batch_number': batch.batch_number,
                    'expiry_date': str(batch.expiry_date),
                    'quantity': float(batch.quantity),
                    # ⚠ UN LOT PORTE SON DÉPÔT : c'est ce qui rend l'alerte
                    # adressable. Sans ce champ, la péremption d'un lot de la
                    # réserve irait aussi au gérant de la boutique d'en face.
                    'warehouse_id': str(batch.warehouse_id),
                    'warehouse_name': batch.warehouse.name,
                },
                action_url='/dashboard/stock/stock-levels',
                action_label='Voir le lot',
            )


@shared_task
def check_customer_payment_due():
    """
    Alerte sur les créances clients : échéance proche, échéance dépassée, et
    limite de crédit bientôt atteinte.

    Les trois types d'alerte (`payment_due`, `payment_overdue`, `credit_limit`)
    étaient déclarés dans `Alert.AlertType` depuis l'origine mais **aucune tâche
    ne les produisait** : `Sale.due_date` était stocké et exposé sans que rien ne
    le lise.

    Idempotence : une alerte ACTIVE existante pour la même ressource n'est pas
    recréée. Une facture passe de `payment_due` à `payment_overdue` quand
    l'échéance tombe ; l'alerte d'échéance proche est alors résolue pour ne pas
    laisser deux alertes concurrentes sur la même facture.
    """
    from django.conf import settings
    from apps.contacts.models import Customer
    from apps.notifications.models import Alert
    from apps.notifications.services import alerter
    from apps.sales.models import Sale

    today = timezone.localdate()
    warning_days = settings.VENTE_FACILE.get('PAYMENT_DUE_WARNING_DAYS', 3)
    warning_date = today + timedelta(days=warning_days)

    open_invoices = Sale.objects.filter(
        status__in=[Sale.Status.PENDING, Sale.Status.PARTIALLY_PAID],
        amount_due__gt=0,
        customer__isnull=False,
        due_date__isnull=False,
    ).select_related('customer', 'organization')

    def _alert_exists(organization, alert_type, resource_id):
        return Alert.objects.filter(
            organization=organization,
            alert_type=alert_type,
            resource_type='sale',
            resource_id=resource_id,
            status=Alert.Status.ACTIVE,
        ).exists()

    for sale in open_invoices.filter(due_date__lt=today):
        days_late = (today - sale.due_date).days

        # L'échéance est passée : l'alerte « bientôt dû » n'a plus d'objet.
        Alert.objects.filter(
            organization=sale.organization,
            alert_type=Alert.AlertType.PAYMENT_DUE,
            resource_type='sale',
            resource_id=sale.id,
            status=Alert.Status.ACTIVE,
        ).update(status=Alert.Status.RESOLVED)

        if _alert_exists(sale.organization, Alert.AlertType.PAYMENT_OVERDUE, sale.id):
            continue

        alerter(
            sale.organization,
            alert_type=Alert.AlertType.PAYMENT_OVERDUE,
            severity=(
                Alert.Severity.HIGH if days_late > 7 else Alert.Severity.MEDIUM
            ),
            title=f"Paiement en retard : {sale.customer.name}",
            message=(
                f"La facture {sale.reference} est échue depuis {days_late} jour(s). "
                f"Reste à payer : {sale.amount_due} {sale.currency}."
            ),
            resource_type='sale',
            resource_id=sale.id,
            data={
                'sale_id': str(sale.id),
                'sale_reference': sale.reference,
                'customer_id': str(sale.customer_id),
                'customer_name': sale.customer.name,
                'due_date': str(sale.due_date),
                'days_late': days_late,
                # Montant ET devise : une créance de 50 USD n'est pas une
                # créance de 50 CDF.
                'amount_due': str(sale.amount_due),
                'currency': sale.currency,
            },
        )

    for sale in open_invoices.filter(due_date__gte=today, due_date__lte=warning_date):
        if _alert_exists(sale.organization, Alert.AlertType.PAYMENT_DUE, sale.id):
            continue

        days_left = (sale.due_date - today).days
        alerter(
            sale.organization,
            alert_type=Alert.AlertType.PAYMENT_DUE,
            severity=Alert.Severity.LOW,
            title=f"Échéance proche : {sale.customer.name}",
            message=(
                f"La facture {sale.reference} arrive à échéance "
                f"{'aujourd’hui' if days_left == 0 else f'dans {days_left} jour(s)'}. "
                f"Reste à payer : {sale.amount_due} {sale.currency}."
            ),
            resource_type='sale',
            resource_id=sale.id,
            data={
                'sale_id': str(sale.id),
                'sale_reference': sale.reference,
                'customer_id': str(sale.customer_id),
                'customer_name': sale.customer.name,
                'due_date': str(sale.due_date),
                'days_left': days_left,
                'amount_due': str(sale.amount_due),
                'currency': sale.currency,
            },
        )

    # Limite de crédit bientôt atteinte. `current_balance` et `credit_limit` sont
    # tous deux exprimés en devise principale : la comparaison est homogène.
    # Une limite à 0 signifie « illimité », il n'y a rien à surveiller.
    threshold_percent = settings.VENTE_FACILE.get('CREDIT_LIMIT_WARNING_PERCENT', 80)
    near_limit = Customer.objects.filter(
        credit_limit__gt=0,
        current_balance__gt=0,
    ).filter(
        current_balance__gte=F('credit_limit') * threshold_percent / 100,
    ).select_related('organization')

    for customer in near_limit:
        if _customer_alert_exists(customer):
            continue

        used_percent = int(customer.current_balance / customer.credit_limit * 100)
        alerter(
            customer.organization,
            alert_type=Alert.AlertType.CREDIT_LIMIT,
            severity=(
                Alert.Severity.HIGH if used_percent >= 100 else Alert.Severity.MEDIUM
            ),
            title=f"Limite de crédit : {customer.name}",
            message=(
                f"{customer.name} utilise {used_percent} % de sa limite de crédit "
                f"({customer.current_balance} sur {customer.credit_limit})."
            ),
            resource_type='customer',
            resource_id=customer.id,
            data={
                'customer_id': str(customer.id),
                'customer_name': customer.name,
                'current_balance': str(customer.current_balance),
                'credit_limit': str(customer.credit_limit),
                'used_percent': used_percent,
            },
        )


def _customer_alert_exists(customer):
    """Alerte de limite de crédit déjà active pour ce client."""
    from apps.notifications.models import Alert

    return Alert.objects.filter(
        organization=customer.organization,
        alert_type=Alert.AlertType.CREDIT_LIMIT,
        resource_type='customer',
        resource_id=customer.id,
        status=Alert.Status.ACTIVE,
    ).exists()


@shared_task
def check_subscription_expiry():
    """Check for subscriptions nearing expiry."""
    from apps.subscriptions.models import Subscription
    from apps.notifications.models import Alert
    from apps.notifications.services import alerter
    
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
            
            alerter(
                subscription.organization,
                alert_type=Alert.AlertType.SUBSCRIPTION_EXPIRING,
                severity=Alert.Severity.CRITICAL if days_remaining <= 3 else Alert.Severity.HIGH,
                title="Abonnement expire bientôt",
                message=f"Votre abonnement {subscription.plan.name} expire dans {days_remaining} jours. "
                       f"Renouvelez pour éviter une interruption de service.",
                data={
                    'subscription_id': str(subscription.id),
                    'plan_name': subscription.plan.name,
                    'expiry_date': str(subscription.current_period_end)
                },
                # ⚠ LA BOUCLE ÉCRITE À LA MAIN DISPARAÎT, et c'est le sens de
                # ce lot. Elle cherchait `role='owner'` : le fan-out rend la
                # même chose en opposant `subscription.view`, que le catalogue
                # n'accorde qu'au propriétaire. La différence est qu'un marchand
                # qui accorde ce droit par `extra_permissions` est désormais
                # servi, et que c'était ici la SEULE alerte livrée sur sept.
                action_url='/dashboard/subscription',
                action_label='Renouveler',
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
