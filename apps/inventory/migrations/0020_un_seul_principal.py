"""
Un établissement a UN entrepôt principal, actif, et la base le garantit.

┌──────────────────────────────────────────────────────────────────────────────┐
│ PLUSIEURS ENTREPÔTS POUVAIENT SE DIRE « PRINCIPAL », ET AUCUN PARFOIS.       │
│                                                                              │
│ Le principal est celui qu'on donne d'office à un membre nouveau, celui d'une │
│ vente sans caisse, celui d'un devis sans choix. Deux principaux rendaient    │
│ ce choix arbitraire ; aucun le rendait impossible.                           │
│                                                                              │
│ Les données d'abord, la contrainte ensuite :                                 │
│ - plusieurs principaux : on garde le plus ancien ACTIF, les autres perdent   │
│   le titre ;                                                                 │
│ - un principal désactivé : il perd le titre au profit du plus ancien actif ; │
│ - aucun : le plus ancien actif le devient.                                   │
│ `updated_at` est posé à la main : `update()` ne passe pas par `save()`, et   │
│ `warehouses` descend au tirage par ce curseur.                               │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.db import migrations, models
from django.utils import timezone


def un_seul_principal(apps, schema_editor):
    Warehouse = apps.get_model('inventory', 'Warehouse')
    maintenant = timezone.now()
    organisations = (
        Warehouse.objects.filter(is_deleted=False)
        .values_list('organization_id', flat=True).distinct()
    )
    for org in organisations:
        vivants = Warehouse.objects.filter(organization_id=org, is_deleted=False)
        elu = (
            vivants.filter(is_default=True, is_active=True).order_by('created_at', 'id').first()
            or vivants.filter(is_active=True).order_by('created_at', 'id').first()
        )
        retirer = vivants.filter(is_default=True)
        if elu is not None:
            retirer = retirer.exclude(id=elu.id)
        retirer.update(is_default=False, updated_at=maintenant)
        if elu is not None and not elu.is_default:
            Warehouse.objects.filter(id=elu.id).update(is_default=True, updated_at=maintenant)


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0019_horloge_metier'),
    ]

    operations = [
        migrations.RunPython(un_seul_principal, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='warehouse',
            constraint=models.UniqueConstraint(
                condition=models.Q(is_default=True, is_deleted=False),
                fields=('organization',),
                name='un_seul_entrepot_principal_par_org',
            ),
        ),
    ]
