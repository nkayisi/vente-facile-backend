"""
Garantit à chaque établissement une devise principale configurée et cohérente.

Toute la résolution de devise repose sur deux sources qui doivent concorder :
`Organization.currency` (le code de la principale) et la ligne
`OrganizationCurrency` marquée `is_primary` (qui porte le taux). Une org créée
avant le multi-devise, ou par un chemin qui n'appelait pas le service de
création, pouvait n'avoir aucune ligne — auquel cas aucune devise secondaire
n'est résoluble et tout retombe silencieusement sur un taux de 1.

Cette migration est idempotente : elle crée la ligne manquante, réaligne le
drapeau `is_primary` sur `Organization.currency`, et force le taux de la
principale à 1 (invariant du modèle).
"""
from decimal import Decimal

from django.db import migrations

CURRENCY_DEFAULTS = {
    'CDF': ('Franc Congolais', 'FC', 0),
    'USD': ('Dollar Américain', '$', 2),
    'EUR': ('Euro', '€', 2),
}


def ensure_primary(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    Currency = apps.get_model('settings', 'Currency')
    OrganizationCurrency = apps.get_model('settings', 'OrganizationCurrency')

    for org in Organization.objects.all().iterator():
        code = org.currency or 'CDF'

        currency = Currency.objects.filter(code=code).first()
        if currency is None:
            name, symbol, decimals = CURRENCY_DEFAULTS.get(code, (code, code, 2))
            currency = Currency.objects.create(
                code=code, name=name, symbol=symbol,
                decimal_places=decimals, is_active=True,
            )

        org_currency, _ = OrganizationCurrency.objects.get_or_create(
            organization_id=org.id, currency_id=currency.id,
            defaults={
                'is_primary': True,
                'exchange_rate': Decimal('1.000000'),
                'is_active': True,
            },
        )

        # Une seule principale par org, et c'est celle de `Organization.currency`.
        OrganizationCurrency.objects.filter(
            organization_id=org.id, is_primary=True,
        ).exclude(id=org_currency.id).update(is_primary=False)

        OrganizationCurrency.objects.filter(id=org_currency.id).update(
            is_primary=True, is_active=True, exchange_rate=Decimal('1.000000'),
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0008_alter_organizationcurrency_exchange_rate'),
        ('organizations', '0008_add_extra_permissions_to_membership'),
    ]

    operations = [
        migrations.RunPython(ensure_primary, noop),
    ]
