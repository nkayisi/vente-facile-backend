"""
Le drapeau de découvert d'un ARTICLE est forcé à faux, comme celui des entrepôts.

`convert_quotation` le lisait encore : un devis sur un article qui le portait se
convertissait sans contrôle de stock. Les deux horodatages sont posés :
`updated_at` porte le curseur du tirage, `sync_updated_at` la résolution de
conflit (voir `apps.core.bulk`). La colonne quitte le manifeste au lot E10.
"""
from django.db import migrations, models
from django.utils import timezone


def forcer_a_faux(apps, schema_editor):
    Product = apps.get_model('products', 'Product')
    maintenant = timezone.now()
    Product.objects.filter(allow_negative_stock=True).update(
        allow_negative_stock=False, updated_at=maintenant, sync_updated_at=maintenant,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0014_unite_suppression_douce'),
    ]

    operations = [
        migrations.RunPython(forcer_a_faux, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='product',
            name='allow_negative_stock',
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
