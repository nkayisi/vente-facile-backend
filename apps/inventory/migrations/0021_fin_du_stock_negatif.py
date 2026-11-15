"""
Aucun entrepôt ne vend plus à découvert : le drapeau est forcé à faux.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LA COLONNE RESTE, LE DRAPEAU NE DÉCIDE PLUS RIEN.                            │
│                                                                              │
│ Le serveur ne la lit plus, mais les terminaux, si : le comptoir s'en servait │
│ pour accepter une vente au-delà du stock. On la force donc à faux AVEC       │
│ `updated_at` posé à la main - `update()` ne passe pas par `save()`, et       │
│ `warehouses` descend au tirage par ce curseur : sans lui, un terminal        │
│ garderait `True` et continuerait d'imprimer des tickets que le serveur       │
│ refusera. La colonne quitte le manifeste au lot E10, quand le parc est à     │
│ jour ; `editable=False` la ferme d'ici là à toute écriture.                  │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.db import migrations, models
from django.utils import timezone


def forcer_a_faux(apps, schema_editor):
    Warehouse = apps.get_model('inventory', 'Warehouse')
    Warehouse.objects.filter(allow_negative_stock=True).update(
        allow_negative_stock=False, updated_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0020_un_seul_principal'),
    ]

    operations = [
        migrations.RunPython(forcer_a_faux, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='warehouse',
            name='allow_negative_stock',
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
