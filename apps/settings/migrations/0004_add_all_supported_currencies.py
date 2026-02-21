# Generated migration to add all supported currencies

from django.db import migrations


def add_supported_currencies(apps, schema_editor):
    Currency = apps.get_model('settings', 'Currency')
    
    # Liste complète des devises supportées
    currencies = [
        # Afrique
        {'code': 'CDF', 'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0},
        {'code': 'XAF', 'name': 'Franc CFA (CEMAC)', 'symbol': 'FCFA', 'decimal_places': 0},
        {'code': 'XOF', 'name': 'Franc CFA (UEMOA)', 'symbol': 'FCFA', 'decimal_places': 0},
        {'code': 'ZAR', 'name': 'Rand Sud-Africain', 'symbol': 'R', 'decimal_places': 2},
        {'code': 'NGN', 'name': 'Naira Nigérian', 'symbol': '₦', 'decimal_places': 2},
        {'code': 'KES', 'name': 'Shilling Kenyan', 'symbol': 'KSh', 'decimal_places': 2},
        {'code': 'GHS', 'name': 'Cedi Ghanéen', 'symbol': 'GH₵', 'decimal_places': 2},
        {'code': 'TZS', 'name': 'Shilling Tanzanien', 'symbol': 'TSh', 'decimal_places': 2},
        {'code': 'UGX', 'name': 'Shilling Ougandais', 'symbol': 'USh', 'decimal_places': 0},
        {'code': 'RWF', 'name': 'Franc Rwandais', 'symbol': 'FRw', 'decimal_places': 0},
        {'code': 'MAD', 'name': 'Dirham Marocain', 'symbol': 'DH', 'decimal_places': 2},
        {'code': 'EGP', 'name': 'Livre Égyptienne', 'symbol': 'E£', 'decimal_places': 2},
        
        # Devises internationales majeures
        {'code': 'USD', 'name': 'Dollar Américain', 'symbol': '$', 'decimal_places': 2},
        {'code': 'EUR', 'name': 'Euro', 'symbol': '€', 'decimal_places': 2},
        {'code': 'GBP', 'name': 'Livre Sterling', 'symbol': '£', 'decimal_places': 2},
        {'code': 'CHF', 'name': 'Franc Suisse', 'symbol': 'CHF', 'decimal_places': 2},
        {'code': 'CAD', 'name': 'Dollar Canadien', 'symbol': 'C$', 'decimal_places': 2},
        {'code': 'AUD', 'name': 'Dollar Australien', 'symbol': 'A$', 'decimal_places': 2},
        {'code': 'JPY', 'name': 'Yen Japonais', 'symbol': '¥', 'decimal_places': 0},
        {'code': 'CNY', 'name': 'Yuan Chinois', 'symbol': '¥', 'decimal_places': 2},
        {'code': 'INR', 'name': 'Roupie Indienne', 'symbol': '₹', 'decimal_places': 2},
    ]
    
    for currency_data in currencies:
        Currency.objects.get_or_create(
            code=currency_data['code'],
            defaults={
                'name': currency_data['name'],
                'symbol': currency_data['symbol'],
                'decimal_places': currency_data['decimal_places'],
                'is_active': True,
            }
        )


def reverse_add_currencies(apps, schema_editor):
    # Ne rien faire en reverse pour éviter de supprimer des devises utilisées
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0003_backfill_org_currencies'),
    ]

    operations = [
        migrations.RunPython(add_supported_currencies, reverse_add_currencies),
    ]
