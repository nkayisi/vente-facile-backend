"""
Remplace le champ texte Plan.currency par une FK vers settings.Currency.
Les plans existants sont mappés par code ISO (ex. USD) ; code inconnu → USD.

Si votre dépôt contient des migrations 0003/0004 après 0002, remplacez la
dépendance `subscriptions` ci-dessous par la dernière migration locale
(ex. 0004_restore_plan_currency_charfield_sqlite_retry) pour garder un graphe linéaire.
"""
import django.db.models.deletion
from django.db import migrations, models


def forwards_fill_plan_currency(apps, schema_editor):
    Plan = apps.get_model("subscriptions", "Plan")
    Currency = apps.get_model("settings", "Currency")
    usd = Currency.objects.filter(code="USD").first()
    if not usd:
        raise RuntimeError(
            "Aucune devise USD en base. Appliquez les migrations de l’app "
            "`settings` (au moins jusqu’à 0002_seed_currencies) avant celle-ci."
        )
    for plan in Plan.objects.all():
        raw = plan.currency
        code = None
        if isinstance(raw, str):
            code = raw.strip().upper()[:3] or None
        cur = Currency.objects.filter(code=code).first() if code else None
        plan.plan_currency_id = (cur or usd).pk
        plan.save(update_fields=["plan_currency_id"])


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("subscriptions", "0002_globalconfig_subscriptionpayment_created_by_and_more"),
        ("settings", "0002_seed_currencies"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="plan_currency",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="settings.currency",
            ),
        ),
        migrations.RunPython(forwards_fill_plan_currency, backwards_noop),
        migrations.RemoveField(
            model_name="plan",
            name="currency",
        ),
        migrations.RenameField(
            model_name="plan",
            old_name="plan_currency",
            new_name="currency",
        ),
        migrations.AlterField(
            model_name="plan",
            name="currency",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="subscription_plans",
                to="settings.currency",
            ),
        ),
    ]
