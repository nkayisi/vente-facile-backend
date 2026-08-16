"""
Aligne la devise de facturation SaaS sur celle du plan souscrit.

Abonnements, factures et règlements portaient un défaut codé en dur à 'USD', et
`create_trial_subscription` utilisait la devise d'EXPLOITATION du marchand
(`Organization.currency`) alors que `activate_subscription` utilisait celle du
plan : d'où une devise qui basculait à la conversion de l'essai.

La source de vérité est le plan : c'est lui qui porte le prix. Une boutique qui
vend en CDF reste facturée dans la devise de son plan.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Subscription = apps.get_model('subscriptions', 'Subscription')
    Invoice = apps.get_model('subscriptions', 'Invoice')
    SubscriptionPayment = apps.get_model('subscriptions', 'SubscriptionPayment')

    for subscription in Subscription.objects.select_related('plan__currency').iterator():
        plan_currency = getattr(getattr(subscription.plan, 'currency', None), 'code', None)
        if not plan_currency or subscription.currency == plan_currency:
            continue
        Subscription.objects.filter(id=subscription.id).update(currency=plan_currency)
        Invoice.objects.filter(subscription_id=subscription.id).update(
            currency=plan_currency
        )
        SubscriptionPayment.objects.filter(subscription_id=subscription.id).update(
            currency=plan_currency
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0013_align_currency_with_organization'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
