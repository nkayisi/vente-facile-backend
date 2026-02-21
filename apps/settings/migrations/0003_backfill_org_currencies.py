"""
Data migration to backfill OrganizationCurrency for existing organizations
that were created before the auto-creation logic was added.
"""
from django.db import migrations


def backfill_org_currencies(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    Currency = apps.get_model('settings', 'Currency')
    OrganizationCurrency = apps.get_model('settings', 'OrganizationCurrency')
    
    for org in Organization.objects.all():
        # Skip if already has a primary currency
        if OrganizationCurrency.objects.filter(organization=org, is_primary=True).exists():
            continue
        
        currency_code = org.currency or 'CDF'
        currency_obj = Currency.objects.filter(code=currency_code).first()
        
        if not currency_obj:
            # Create the currency if it doesn't exist
            defaults = {
                'CDF': ('Franc Congolais', 'FC', 0),
                'USD': ('Dollar Américain', '$', 2),
                'EUR': ('Euro', '€', 2),
            }
            name, symbol, decimals = defaults.get(currency_code, (currency_code, currency_code, 2))
            currency_obj = Currency.objects.create(
                code=currency_code,
                name=name,
                symbol=symbol,
                decimal_places=decimals,
                is_active=True
            )
        
        OrganizationCurrency.objects.create(
            organization=org,
            currency=currency_obj,
            is_primary=True,
            is_active=True,
            exchange_rate='1.000000'
        )


def reverse_backfill(apps, schema_editor):
    pass  # No reverse needed


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0002_seed_currencies'),
        ('organizations', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(backfill_org_currencies, reverse_backfill),
    ]
