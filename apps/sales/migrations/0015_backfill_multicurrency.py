"""
Backfill multi-devise (données existantes intactes).

- Payment.tendered_amount ← amount (les lignes historiques sont mono-devise :
  amount est déjà exprimé dans la devise du règlement, exchange_rate≈1).
- Sale.change_currency ← currency (monnaie rendue dans la devise de la vente).
"""
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    Payment = apps.get_model('sales', 'Payment')
    Sale = apps.get_model('sales', 'Sale')

    Payment.objects.filter(tendered_amount__isnull=True).update(
        tendered_amount=F('amount')
    )
    Sale.objects.filter(change_currency='').update(
        change_currency=F('currency')
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0014_payment_tendered_amount_sale_change_currency_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
