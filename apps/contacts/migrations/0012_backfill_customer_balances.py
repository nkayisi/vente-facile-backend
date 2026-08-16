"""
Rattache la dette client existante à une devise.

Avant ce lot, `Customer.current_balance` était un scalaire sans devise : une
dette de 50 USD et une dette de 50 CDF s'additionnaient. On crée désormais une
ligne `CustomerBalance` par client, dans la devise principale de son
organisation, et on estampille les `CustomerTransaction` de la même façon.

**Aucun montant n'est réécrit** : seul le code devise est renseigné, exactement
comme les migrations de réalignement `sales/0018` et `cashbook/0009`.
"""
from decimal import Decimal

from django.db import migrations


def forwards(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    Customer = apps.get_model('contacts', 'Customer')
    CustomerBalance = apps.get_model('contacts', 'CustomerBalance')
    CustomerTransaction = apps.get_model('contacts', 'CustomerTransaction')

    primary_by_org = {
        org_id: (code or 'CDF')
        for org_id, code in Organization.objects.values_list('id', 'currency')
    }

    balances = []
    for customer in Customer.objects.all().only(
        'id', 'organization_id', 'current_balance'
    ).iterator():
        primary = primary_by_org.get(customer.organization_id, 'CDF')
        balances.append(CustomerBalance(
            organization_id=customer.organization_id,
            customer_id=customer.id,
            currency=primary,
            amount=customer.current_balance or Decimal('0.00'),
        ))
        if len(balances) >= 500:
            CustomerBalance.objects.bulk_create(balances, ignore_conflicts=True)
            balances = []
    if balances:
        CustomerBalance.objects.bulk_create(balances, ignore_conflicts=True)

    # Les transactions historiques sont, par construction, dans la devise
    # principale : c'était la seule devise que le code savait écrire.
    for org_id, primary in primary_by_org.items():
        CustomerTransaction.objects.filter(
            organization_id=org_id, currency='',
        ).update(currency=primary, exchange_rate=Decimal('1'))


def backwards(apps, schema_editor):
    CustomerBalance = apps.get_model('contacts', 'CustomerBalance')
    CustomerTransaction = apps.get_model('contacts', 'CustomerTransaction')
    CustomerBalance.objects.all().delete()
    CustomerTransaction.objects.all().update(currency='')


class Migration(migrations.Migration):

    dependencies = [
        ('contacts', '0011_customertransaction_currency_and_more'),
        ('organizations', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
