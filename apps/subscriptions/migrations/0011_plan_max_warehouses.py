# Plan max_warehouses + backfill from max_branches

from django.db import migrations, models


def backfill_max_warehouses(apps, schema_editor):
    Plan = apps.get_model("subscriptions", "Plan")
    for p in Plan.objects.all():
        p.max_warehouses = p.max_branches
        p.save(update_fields=["max_warehouses"])


class Migration(migrations.Migration):

    dependencies = [
        ("subscriptions", "0010_plan_tier"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="max_warehouses",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Nombre maximal d'entrepôts (non supprimés) par organisation.",
            ),
        ),
        migrations.RunPython(backfill_max_warehouses, migrations.RunPython.noop),
    ]
