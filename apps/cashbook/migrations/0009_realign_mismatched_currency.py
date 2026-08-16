"""
Réaligne les mouvements et dépenses mal estampillés sur la devise principale.

Les migrations 0006 et 0008 ne traitaient que `currency=''`. Restent les lignes
portant une devise DIFFÉRENTE de la principale avec un `exchange_rate` de 1 -
combinaison impossible pour une vraie ligne en devise étrangère (seule la
principale vaut 1), donc signature d'une devise héritée d'un défaut plutôt que
choisie.

Aucun montant n'est modifié : à taux 1, la valeur est numériquement identique,
seul le code devise est corrigé. Les vraies lignes multi-devise (taux ≠ 1) sont
laissées intactes.
"""
from decimal import Decimal

from django.db import migrations

ONE = Decimal('1.000000')


def realign(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    CashMovement = apps.get_model('cashbook', 'CashMovement')
    Expense = apps.get_model('cashbook', 'Expense')

    for org in Organization.objects.all().iterator():
        primary = org.currency or 'CDF'
        for model in (CashMovement, Expense):
            model.objects.filter(
                organization_id=org.id, exchange_rate=ONE,
            ).exclude(currency=primary).update(currency=primary)
            # Filet : les lignes créées après 0008 sans devise.
            model.objects.filter(
                organization_id=org.id, currency='',
            ).update(currency=primary, exchange_rate=ONE)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('cashbook', '0008_backfill_currency_remaining'),
        ('organizations', '0008_add_extra_permissions_to_membership'),
    ]

    operations = [
        migrations.RunPython(realign, noop),
    ]
