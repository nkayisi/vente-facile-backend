# Generated manually — rattache les caisses sans entrepôt à l’entrepôt par défaut,
# puis rend la FK obligatoire (PROTECT).

import django.db.models.deletion
from django.db import migrations, models


def assign_default_warehouse(apps, schema_editor):
    """
    Pour chaque caisse sans entrepôt : entrepôt is_default=True de la même org,
    sinon premier entrepôt actif par code (déterministe).
    """
    Register = apps.get_model('sales', 'Register')
    Warehouse = apps.get_model('inventory', 'Warehouse')
    qs = Register.objects.filter(warehouse__isnull=True).iterator(chunk_size=200)
    for reg in qs:
        org_id = reg.organization_id
        wh = (
            Warehouse.objects.filter(
                organization_id=org_id, is_default=True, is_deleted=False
            ).order_by('code', 'id').first()
            or Warehouse.objects.filter(
                organization_id=org_id, is_active=True, is_deleted=False
            ).order_by('code', 'id').first()
        )
        if wh is None:
            raise ValueError(
                f'Caisse {reg.pk}: aucun entrepôt disponible pour organisation {org_id}.'
            )
        Register.objects.filter(pk=reg.pk).update(warehouse_id=wh.pk)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0009_stockbatch_location'),
        ('sales', '0004_add_receipt_printed'),
    ]

    operations = [
        migrations.RunPython(assign_default_warehouse, noop_reverse),
        migrations.AlterField(
            model_name='register',
            name='warehouse',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='registers',
                to='inventory.warehouse',
            ),
        ),
    ]
