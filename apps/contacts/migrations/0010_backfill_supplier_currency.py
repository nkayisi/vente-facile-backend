"""
Rattache la devise des fournisseurs à celle de l'établissement.

`Supplier.currency` (devise de `current_balance`) avait un défaut codé en dur à
'USD'. Un fournisseur d'une boutique opérant en CDF portait donc un solde
libellé USD alors que les montants étaient en CDF.

Le modèle n'a pas de taux : on ne peut pas distinguer un choix délibéré d'un
défaut hérité. On ne réaligne donc QUE les fournisseurs dont la devise n'est pas
activée dans l'organisation - un cas où la valeur ne peut être qu'un artefact,
puisqu'aucun taux ne permettrait de l'exploiter.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    OrganizationCurrency = apps.get_model('settings', 'OrganizationCurrency')
    Supplier = apps.get_model('contacts', 'Supplier')

    for org in Organization.objects.all().iterator():
        primary = org.currency or 'CDF'
        configured = set(
            OrganizationCurrency.objects.filter(
                organization_id=org.id, is_active=True,
            ).values_list('currency__code', flat=True)
        )
        configured.add(primary)

        Supplier.objects.filter(organization_id=org.id).exclude(
            currency__in=configured
        ).update(currency=primary)
        Supplier.objects.filter(
            organization_id=org.id, currency='',
        ).update(currency=primary)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('contacts', '0009_align_currency_with_organization'),
        ('settings', '0009_ensure_primary_organization_currency'),
        ('organizations', '0008_add_extra_permissions_to_membership'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
