"""
Data migration to backfill Stock.avg_cost from Product.cost_price
for all existing Stock records where avg_cost is 0.
"""
from decimal import Decimal
from django.db import migrations


def backfill_avg_cost(apps, schema_editor):
    Stock = apps.get_model('inventory', 'Stock')
    stocks = Stock.objects.filter(avg_cost=Decimal('0.00')).select_related('product')
    updated = 0
    for stock in stocks:
        if stock.product.cost_price and stock.product.cost_price > 0:
            stock.avg_cost = stock.product.cost_price
            stock.save(update_fields=['avg_cost'])
            updated += 1
    if updated:
        print(f"\n  Backfilled avg_cost for {updated} stock records")


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0005_alter_warehouse_unique_together_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_avg_cost, reverse_backfill),
    ]
