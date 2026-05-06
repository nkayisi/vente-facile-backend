# Generated manually for subscription tier ordering

from django.db import migrations, models


def backfill_plan_tier(apps, schema_editor):
    Plan = apps.get_model("subscriptions", "Plan")
    for p in Plan.objects.all():
        # Aligner le palier sur sort_order + 1 pour préserver l’ordre existant (minimum 1)
        t = int(p.sort_order or 0) + 1
        if t < 1:
            t = 1
        Plan.objects.filter(pk=p.pk).update(tier=t)


class Migration(migrations.Migration):

    dependencies = [
        ("subscriptions", "0009_update_moko_poll_interval"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="tier",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(backfill_plan_tier, migrations.RunPython.noop),
    ]
