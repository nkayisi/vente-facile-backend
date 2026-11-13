"""
L'essai dure quatorze jours, et son plan est au palier ZÉRO.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DEUX ÉCARTS, TOUS DEUX VISIBLES PAR LE MARCHAND.                            │
│                                                                              │
│ - La page d'accueil promet « 14 jours d'essai » ; la base en accordait 15.  │
│   Une promesse publique ne se tient pas à un jour près dans l'autre sens.    │
│ - Le plan d'essai portait le palier 1, comme le premier plan payant. Le      │
│   palier ordonne les OFFRES : un essai n'en est pas une, et le placer au     │
│   même rang faisait lire « Starter » comme une offre égale à l'essai.        │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ ON NE TOUCHE QUE LA VALEUR D'ORIGINE. Un administrateur qui a déjà réglé la
  durée d'essai à 30 jours l'a DÉCIDÉ : la ramener à 14 défairait sa décision.
  Seul un 15 - le défaut d'origine - devient 14. Même règle pour la description
  du plan : elle n'est réécrite que si elle est encore celle que le service a
  fabriquée.

⚠ LES ESSAIS EN COURS NE BOUGENT PAS. Leur échéance est écrite sur leur
  abonnement (`trial_end`) ; la raccourcir d'un jour sous les pieds d'un
  marchand qui a compté ses jours serait une promesse retirée. Seuls les
  nouveaux essais durent quatorze jours.
"""
from django.db import migrations, models

ANCIENNE_DUREE = 15
NOUVELLE_DUREE = 14


def _description(jours):
    return f"Plan d'essai gratuit de {jours} jours"


def appliquer(apps, schema_editor):
    GlobalConfig = apps.get_model('subscriptions', 'GlobalConfig')
    Plan = apps.get_model('subscriptions', 'Plan')

    GlobalConfig.objects.filter(trial_days=ANCIENNE_DUREE).update(
        trial_days=NOUVELLE_DUREE
    )

    for plan in Plan.objects.filter(code='trial'):
        champs = []
        if plan.tier == 1:
            plan.tier = 0
            champs.append('tier')
        if plan.trial_days == ANCIENNE_DUREE:
            plan.trial_days = NOUVELLE_DUREE
            champs.append('trial_days')
        if plan.description == _description(ANCIENNE_DUREE):
            plan.description = _description(NOUVELLE_DUREE)
            champs.append('description')
        if champs:
            # `updated_at` suit : `auto_now` ne s'applique qu'aux colonnes
            # nommées dans `update_fields`.
            plan.save(update_fields=champs + ['updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0019_version_magasin_obligatoire'),
    ]

    operations = [
        migrations.AlterField(
            model_name='globalconfig',
            name='trial_days',
            field=models.PositiveIntegerField(
                default=14,
                help_text="Nombre de jours de la période d'essai pour les nouveaux comptes",
            ),
        ),
        migrations.AlterField(
            model_name='plan',
            name='tier',
            field=models.PositiveIntegerField(
                default=1,
                help_text=(
                    "Palier de l'offre (plus grand = plus riche), qui fonde "
                    "l'anti-rétrogradation. L'essai est au palier 0 : ce n'est "
                    "pas une offre."
                ),
            ),
        ),
        migrations.RunPython(appliquer, migrations.RunPython.noop),
    ]
