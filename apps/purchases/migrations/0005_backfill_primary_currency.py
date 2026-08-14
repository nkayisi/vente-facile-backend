"""
Rattache commandes et règlements fournisseurs à la devise de l'établissement.

`PurchaseOrder.currency` et `SupplierPayment.currency` avaient un défaut codé
en dur à 'USD', sans aucun rapport avec l'organisation : une boutique opérant en
CDF voyait toutes ses commandes libellées en USD, et ces montants étaient
ensuite sommés bruts dans les totaux fournisseurs.

Même marqueur que pour les ventes et la caisse : une devise DIFFÉRENTE de la
principale avec un `exchange_rate` de 1 est impossible pour une vraie ligne en
devise étrangère (seule la principale vaut 1), donc c'est un défaut hérité.

Aucun montant n'est modifié : à taux 1, la valeur est numériquement identique.
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import Q

ONE = Decimal('1.000000')


def backfill(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    PurchaseOrder = apps.get_model('purchases', 'PurchaseOrder')
    SupplierPayment = apps.get_model('purchases', 'SupplierPayment')

    for org in Organization.objects.all().iterator():
        primary = org.currency or 'CDF'
        for model in (PurchaseOrder, SupplierPayment):
            model.objects.filter(organization_id=org.id).filter(
                Q(currency='') | Q(~Q(currency=primary), exchange_rate=ONE)
            ).update(currency=primary, exchange_rate=ONE)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('purchases', '0004_align_currency_with_organization'),
        ('organizations', '0008_add_extra_permissions_to_membership'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
