"""
Rattache les ventes et règlements historiques à la devise principale de l'org.

`Sale.currency` et `Payment.currency` avaient un défaut codé en dur à 'CDF' :
toute ligne créée sans devise explicite était estampillée 'CDF', même dans une
organisation dont la devise principale est USD. La conversion devis → vente ne
passait aucune devise du tout.

Marqueur de détection : une devise DIFFÉRENTE de la référence avec un
`exchange_rate` de 1. C'est une combinaison impossible pour une vraie ligne en
devise étrangère (la principale est seule à valoir 1), donc sans ambiguïté la
signature d'un défaut jamais renseigné.

L'opération ne change AUCUN montant : seuls le code devise (et le taux, laissé
à 1) sont réalignés. Les vraies lignes multi-devise, qui portent un taux ≠ 1,
sont laissées intactes.
"""
from django.db import migrations
from django.db.models import F, Q
from decimal import Decimal

ONE = Decimal('1.000000')


def backfill(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    Sale = apps.get_model('sales', 'Sale')
    Payment = apps.get_model('sales', 'Payment')

    for org in Organization.objects.all().iterator():
        primary = org.currency or 'CDF'
        org_sales = Sale.objects.filter(organization_id=org.id)

        # Ventes : devise vide, ou mal estampillée (≠ principale au taux 1).
        mis_stamped = org_sales.filter(
            Q(currency='') | Q(~Q(currency=primary), exchange_rate=ONE)
        )
        corrected_ids = list(mis_stamped.values_list('id', flat=True))
        mis_stamped.update(currency=primary, exchange_rate=ONE)

        # Ces ventes sont antérieures au multi-devise : la monnaie y était
        # forcément rendue dans la devise de la vente. La migration 0015 avait
        # recopié l'ancienne devise (fausse) dans `change_currency`.
        Sale.objects.filter(id__in=corrected_ids).update(change_currency=primary)
        org_sales.filter(change_currency='').update(change_currency=F('currency'))

        # Règlements : un `exchange_rate` de 1 signifie « même devise que la
        # vente ». Toute divergence est un défaut hérité, pas un choix.
        for ccy in org_sales.order_by().values_list('currency', flat=True).distinct():
            Payment.objects.filter(
                sale__organization_id=org.id, sale__currency=ccy, exchange_rate=ONE,
            ).exclude(currency=ccy).update(currency=ccy)

        Payment.objects.filter(
            sale__organization_id=org.id, currency='',
        ).update(currency=primary)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0017_alter_currency_defaults'),
        ('organizations', '0008_add_extra_permissions_to_membership'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
