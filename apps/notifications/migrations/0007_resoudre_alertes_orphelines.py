"""
Résout les alertes de stock que plus aucun code ne peut résoudre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CHANGER LA CLÉ DE DÉDUPLICATION ORPHELINE TOUT CE QUI PRÉCÈDE.              │
│                                                                              │
│ `check_low_stock_alerts` écrivait `resource_type='product'` et agrégeait     │
│ tous les dépôts ; elle écrit désormais `resource_type='stock'`, PAR RAYON,   │
│ pour que la règle de périmètre puisse mordre. Elle ne reverra donc JAMAIS    │
│ les anciennes lignes : elles restent `active` pour toujours, sur l'écran de  │
│ chaque marchand, pour des ruptures qui peuvent avoir été réapprovisionnées   │
│ depuis des semaines.                                                         │
│                                                                              │
│ MESURÉ sur la base de développement : 1042 alertes dans ce cas, dont AUCUNE  │
│ ne porte de `warehouse_id`. Elles sont donc en plus montrées à tout le monde │
│ - une alerte sans dépôt n'est bornée à aucun - et c'est exact : on ne peut   │
│ pas rétro-attribuer un entrepôt qui n'a jamais été enregistré.               │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ **ON RÉSOUT, ON NE SUPPRIME PAS.** Une alerte est une trace : elle dit qu'un
rayon a manqué un jour, et les rapports peuvent la lire. `resolved` la retire des
écrans sans effacer l'histoire.

⚠ **ON NE TOUCHE QUE LES ALERTES DE STOCK.** Les créances, les abonnements et
les péremptions gardent leur `resource_type` : leurs tâches les reverront, et les
résoudre ici reviendrait à taire des faits toujours vrais.
"""
from django.db import migrations


def resoudre(apps, schema_editor):
    from django.utils import timezone

    Alert = apps.get_model('notifications', 'Alert')
    maintenant = timezone.now()

    # ⚠ `updated_at` EST POSÉ À LA MAIN. `queryset.update()` court-circuite
    # `save()`, donc `auto_now` : une ligne dont l'horodatage ne bouge pas est
    # invisible au tirage, définitivement et sur tous les terminaux à la fois.
    # Et `test_bulk_write_visibility` EXCLUT les migrations, donc rien ne
    # rattraperait l'oubli - c'est la leçon de la migration de devises.
    Alert.objects.filter(
        resource_type='product',
        status='active',
        alert_type__in=['low_stock', 'out_of_stock'],
    ).update(status='resolved', resolved_at=maintenant, updated_at=maintenant)


def rien(apps, schema_editor):
    """
    Irréversible, et le dire vaut mieux que de mentir.

    Rouvrir ces alertes remettrait sur l'écran du marchand des ruptures qu'on
    sait périmées, et on ne saurait pas distinguer celles qu'un humain avait
    résolues entre-temps.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0006_genres_alerte'),
    ]

    operations = [
        migrations.RunPython(resoudre, rien),
    ]
