"""
Le moyen de paiement « Points de fidélité » devient « Cagnotte fidélité ».

Ce libellé n'est pas décoratif : il s'imprime sur le ticket du client, à côté
du montant pris sur sa cagnotte, et il nomme une ligne de la répartition par
moyen de paiement dans les rapports. Le laisser parler de points après la
bascule ferait lire « Points de fidélité : 2 500 FC » - une unité et un montant
qui ne vont pas ensemble.

⚠ SEULES LES LIGNES RESTÉES AU LIBELLÉ PAR DÉFAUT SONT TOUCHÉES. Un marchand a
  pu renommer le sien ; écraser son libellé serait lui reprendre un réglage
  qu'il a posé.

⚠ `bulk_update_rows` ET NON `update()` : `payment_methods` est au manifeste de
  tirage, et un `queryset.update()` ne passe pas par `save()`, donc `updated_at`
  ne bouge pas, donc la ligne devient invisible au tirage - le terminal
  continuerait d'imprimer l'ancien libellé pour toujours.
"""
from django.db import migrations

ANCIEN = 'Points de fidélité'
NOUVEAU = 'Cagnotte fidélité'


def renommer(apps, schema_editor):
    from apps.core.bulk import bulk_update_rows

    PaymentMethod = apps.get_model('sales', 'PaymentMethod')
    bulk_update_rows(
        PaymentMethod.objects.filter(method_type='loyalty', name=ANCIEN),
        name=NOUVEAU,
    )


def defaire(apps, schema_editor):
    from apps.core.bulk import bulk_update_rows

    PaymentMethod = apps.get_model('sales', 'PaymentMethod')
    bulk_update_rows(
        PaymentMethod.objects.filter(method_type='loyalty', name=NOUVEAU),
        name=ANCIEN,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0025_devise_par_ligne_de_vente'),
    ]

    operations = [
        migrations.RunPython(renommer, defaire),
    ]
