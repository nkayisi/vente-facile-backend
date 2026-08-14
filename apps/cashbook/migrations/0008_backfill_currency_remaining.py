"""
Rattrapage du backfill multi-devise (données existantes intactes).

La migration 0006 avait rempli `currency` pour les lignes existantes, mais
`ExpenseViewSet.perform_create` ne renseignait pas le champ : toute dépense
créée depuis est persistée avec `currency=''`. Même filet pour CashMovement.

`exchange_rate` n'est pas touché : ces lignes sont par construction en devise
principale, dont le taux est 1.
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
        ('cashbook', '0007_alter_cashmovement_exchange_rate_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
