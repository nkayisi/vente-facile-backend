"""
Management command to setup default currencies.
"""
from django.core.management.base import BaseCommand
from apps.settings.models import Currency


class Command(BaseCommand):
    help = 'Setup default currencies for the system'

    def handle(self, *args, **options):
        currencies = [
            {'code': 'CDF', 'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 2},
            {'code': 'USD', 'name': 'Dollar Américain', 'symbol': '$', 'decimal_places': 2},
            {'code': 'EUR', 'name': 'Euro', 'symbol': '€', 'decimal_places': 2},
            {'code': 'XAF', 'name': 'Franc CFA', 'symbol': 'FCFA', 'decimal_places': 0},
            {'code': 'ZAR', 'name': 'Rand Sud-Africain', 'symbol': 'R', 'decimal_places': 2},
            {'code': 'KES', 'name': 'Shilling Kenyan', 'symbol': 'KSh', 'decimal_places': 2},
            {'code': 'UGX', 'name': 'Shilling Ougandais', 'symbol': 'USh', 'decimal_places': 0},
            {'code': 'TZS', 'name': 'Shilling Tanzanien', 'symbol': 'TSh', 'decimal_places': 0},
            {'code': 'RWF', 'name': 'Franc Rwandais', 'symbol': 'FRw', 'decimal_places': 0},
            {'code': 'BIF', 'name': 'Franc Burundais', 'symbol': 'FBu', 'decimal_places': 0},
            {'code': 'AOA', 'name': 'Kwanza Angolais', 'symbol': 'Kz', 'decimal_places': 2},
            {'code': 'ZMW', 'name': 'Kwacha Zambien', 'symbol': 'ZK', 'decimal_places': 2},
        ]
        
        created_count = 0
        for currency_data in currencies:
            currency, created = Currency.objects.get_or_create(
                code=currency_data['code'],
                defaults={
                    'name': currency_data['name'],
                    'symbol': currency_data['symbol'],
                    'decimal_places': currency_data['decimal_places'],
                    'is_active': True
                }
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"Created currency: {currency.code} - {currency.name}")
                )
            else:
                self.stdout.write(f"Currency already exists: {currency.code}")
        
        self.stdout.write(
            self.style.SUCCESS(f"\nDone! Created {created_count} new currencies.")
        )
