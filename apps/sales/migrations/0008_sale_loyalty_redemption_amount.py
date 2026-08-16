# Generated manually - ajoute le champ Sale.loyalty_redemption_amount.
# Trace la part du discount qui vient des points de fidélité, pour qu'on puisse
# distinguer une remise commerciale d'une déduction loyauté dans les rapports.

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0006_session_constraints_and_counted_balance'),
    ]

    operations = [
        migrations.AddField(
            model_name='sale',
            name='loyalty_redemption_amount',
            field=models.DecimalField(
                max_digits=15,
                decimal_places=2,
                default=Decimal('0.00'),
                help_text=(
                    "Montant déduit du total grâce à l'utilisation de points de fidélité."
                ),
            ),
        ),
    ]
