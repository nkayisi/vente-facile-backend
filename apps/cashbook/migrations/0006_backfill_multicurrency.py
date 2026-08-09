"""
Backfill multi-devise pour la caisse (données existantes intactes).

CashMovement.currency et Expense.currency ← devise principale de l'organisation
(champ `Organization.currency`, par défaut 'CDF'). exchange_rate garde son défaut
de 1.000000, ce qui laisse les soldes historiques inchangés.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    CashMovement = apps.get_model('cashbook', 'CashMovement')
    Expense = apps.get_model('cashbook', 'Expense')

    for org in Organization.objects.all().iterator():
        primary = org.currency or 'CDF'
        CashMovement.objects.filter(
            organization_id=org.id, currency=''
        ).update(currency=primary)
        Expense.objects.filter(
            organization_id=org.id, currency=''
        ).update(currency=primary)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('cashbook', '0005_cashmovement_currency_cashmovement_exchange_rate_and_more'),
        ('organizations', '0008_add_extra_permissions_to_membership'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
