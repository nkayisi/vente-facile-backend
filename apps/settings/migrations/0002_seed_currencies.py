"""
Data migration to seed default currencies.
"""
from django.db import migrations


def seed_currencies(apps, schema_editor):
    Currency = apps.get_model('settings', 'Currency')
    
    currencies = [
        {'code': 'CDF', 'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0, 'is_active': True},
        {'code': 'USD', 'name': 'Dollar Américain', 'symbol': '$', 'decimal_places': 2, 'is_active': True},
        {'code': 'EUR', 'name': 'Euro', 'symbol': '€', 'decimal_places': 2, 'is_active': True},
    ]
    
    for currency_data in currencies:
        Currency.objects.get_or_create(
            code=currency_data['code'],
            defaults=currency_data
        )


def reverse_seed(apps, schema_editor):
    Currency = apps.get_model('settings', 'Currency')
    Currency.objects.filter(code__in=['CDF', 'USD', 'EUR']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_currencies, reverse_seed),
    ]
