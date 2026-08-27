from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0016_stock_package_quantity_alter_stock_loose_quantity'),
    ]

    operations = [
        migrations.AddField(
            model_name='stockadjustmentitem',
            name='expected_loose_quantity',
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=15, null=True
            ),
        ),
    ]
