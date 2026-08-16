# Generated manually - rattache les caisses sans entrepôt à l’entrepôt par défaut,
# puis rend la FK obligatoire (PROTECT).

import django.db.models.deletion
from django.db import migrations, models


def assign_default_warehouse(apps, schema_editor):
    """
    Pour chaque caisse sans entrepôt : entrepôt is_default=True de la même org,
    sinon premier entrepôt actif, sinon n'importe quel entrepôt non supprimé
    (par code, déterministe).

    Si l'organisation n'a AUCUN entrepôt, on en crée un par défaut
    (« Entrepôt Principal ») plutôt que d'échouer : la FK warehouse devient
    obligatoire (NOT NULL) juste après, donc toute caisse doit en avoir un.
    Le code « MAIN » ne peut pas entrer en collision ici puisqu'on ne crée que
    lorsqu'il n'existe aucun entrepôt non supprimé pour l'org.
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
            or Warehouse.objects.filter(
                organization_id=org_id, is_deleted=False
            ).order_by('code', 'id').first()
        )
        if wh is None:
            # Organisation sans aucun entrepôt : en créer un par défaut.
            # (lecture-après-écriture dans la transaction de migration : une 2e
            # caisse orpheline de la même org réutilisera cet entrepôt via le
            # filtre is_default ci-dessus.)
            wh = Warehouse.objects.create(
                organization_id=org_id,
                name='Entrepôt Principal',
                code='MAIN',
                is_default=True,
                is_active=True,
            )
        Register.objects.filter(pk=reg.pk).update(warehouse_id=wh.pk)

    # Postgres : les FK sont DEFERRABLE INITIALLY DEFERRED, donc les INSERT/UPDATE
    # ci-dessus laissent des « pending trigger events » qui font échouer
    # l'AlterField suivant (« cannot ALTER TABLE ... because it has pending
    # trigger events »). On force la vérification immédiate pour vider la file
    # avant le changement de schéma, dans la même transaction.
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')


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
