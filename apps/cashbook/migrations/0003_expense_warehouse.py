"""Ajoute un champ warehouse (nullable) au modèle Expense + backfill vers
l'entrepôt par défaut de chaque organisation."""
import django.db.models.deletion
from django.db import migrations, models


def backfill_expense_warehouse(apps, schema_editor):
    """Affecte chaque dépense existante à l'entrepôt par défaut de son organisation
    (ou au premier entrepôt actif si aucun n'est marqué par défaut)."""
    Expense = apps.get_model('cashbook', 'Expense')
    Warehouse = apps.get_model('inventory', 'Warehouse')

    org_to_warehouse: dict = {}
    for expense in Expense.objects.filter(warehouse__isnull=True).iterator():
        org_id = expense.organization_id
        if org_id not in org_to_warehouse:
            default_wh = (
                Warehouse.objects.filter(
                    organization_id=org_id,
                    is_deleted=False,
                    is_default=True,
                ).first()
                or Warehouse.objects.filter(
                    organization_id=org_id,
                    is_deleted=False,
                    is_active=True,
                ).order_by('created_at').first()
            )
            org_to_warehouse[org_id] = default_wh
        target = org_to_warehouse[org_id]
        if target is not None:
            expense.warehouse_id = target.id
            expense.save(update_fields=['warehouse'])


def reverse_clear(apps, schema_editor):
    """Pas de rollback métier : on conserve les valeurs (pas de perte de données)."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('cashbook', '0002_cashmovement_expense_category_incomecategory_and_more'),
        ('inventory', '0009_stockbatch_location'),
    ]

    operations = [
        migrations.AddField(
            model_name='expense',
            name='warehouse',
            field=models.ForeignKey(
                blank=True,
                help_text='Entrepôt rattaché à la dépense (filtrage par périmètre membre)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='expenses',
                to='inventory.warehouse',
            ),
        ),
        migrations.RunPython(backfill_expense_warehouse, reverse_clear),
    ]
