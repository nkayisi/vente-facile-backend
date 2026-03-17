from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0005_remove_product_type_and_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='has_expiry_date',
            field=models.BooleanField(
                default=False,
                help_text="Indique si ce produit est périssable et nécessite un suivi des dates d'expiration"
            ),
        ),
    ]
