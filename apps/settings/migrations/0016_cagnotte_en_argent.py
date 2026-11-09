"""
La fidélité cesse de se compter en points et devient une cagnotte en argent.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CETTE MIGRATION CONVERTIT DES SOLDES : ELLE TOUCHE À DE L'ARGENT.           │
│                                                                              │
│ Un point valait `point_value` unités de devise principale. Laisser les       │
│ soldes tels quels sur une organisation réglée à `point_value = 10` ferait    │
│ passer une cagnotte de 400 à 40 : le client perdrait neuf dixièmes de ce     │
│ qu'il avait gagné, sans qu'aucun écran ne le dise. Tous les soldes, cumuls   │
│ à vie et lignes de registre sont donc MULTIPLIÉS par le `point_value` de     │
│ leur organisation AVANT que la colonne ne disparaisse.                       │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ L'ORDRE DES OPÉRATIONS N'EST PAS INDIFFÉRENT. Les `RenameField` d'abord, la
  conversion ENSUITE (elle lit `legacy_point_value`, qu'elle a besoin de voir
  rempli), et les `RemoveField` en DERNIER - retirer `point_value` avant la
  conversion la priverait de son multiplicateur.

⚠ LA CONVERSION POSE `updated_at` EXPLICITEMENT. `queryset.update()`
  court-circuite `save()`, donc `auto_now` : une ligne dont l'horodatage ne
  bouge pas est INVISIBLE au tirage, définitivement et sur tous les terminaux à
  la fois - le curseur `(updated_at, id)` la place avant le point de reprise.
  C'est le défaut de `CustomerBalance` du lot 6, et
  `test_bulk_write_visibility` EXCLUT les migrations : rien ne rattraperait
  l'oubli.
"""
from decimal import Decimal

import django.core.validators
from django.db import migrations, models
from django.utils import timezone

from apps.settings.cagnotte_migration import (
    convertir_montant, facteur_de_conversion, pourcentage_depuis_bareme,
    pourcentage_depuis_pourcentage,
)

UN = Decimal('1.00')
TACHE_EXPIRATION = 'expire_loyalty_points'


def _taux_par_organisation(LoyaltyProgram):
    """`{organization_id: point_value}`, en un seul aller-retour."""
    return {
        org_id: facteur_de_conversion(valeur)
        for org_id, valeur in LoyaltyProgram.objects.values_list(
            'organization_id', 'point_value',
        )
    }


def convertir(apps, schema_editor):
    LoyaltyProgram = apps.get_model('settings', 'LoyaltyProgram')
    CustomerLoyalty = apps.get_model('settings', 'CustomerLoyalty')
    LoyaltyTransaction = apps.get_model('settings', 'LoyaltyTransaction')

    taux = _taux_par_organisation(LoyaltyProgram)
    maintenant = timezone.now()

    # ⚠ LES DEUX MODES SE CONVERTISSENT, et le second est celui qu'on oublie.
    #   L'un comme l'autre comptaient en POINTS : reprendre leur réglage tel
    #   quel diviserait le gain futur par `point_value` pendant que les SOLDES,
    #   eux, sont multipliés par lui - les deux moitiés du même programme ne
    #   parleraient plus de la même unité.
    #
    #   Le barème par tranches rend en plus une APPROXIMATION, et elle doit être
    #   écrite : « X points pour chaque Y dépensé » ne comptait pas les tranches
    #   entamées, le pourcentage est continu. On retient le taux moyen, qui
    #   donne le même gain sur une tranche pleine et un peu plus sur une tranche
    #   entamée - c'est le bon côté de l'écart : personne ne perd de cagnotte.
    for programme in LoyaltyProgram.objects.all():
        programme.legacy_point_value = facteur_de_conversion(programme.point_value)

        if programme.points_calculation_type == 'fixed_per_amount':
            programme.earn_percent = pourcentage_depuis_bareme(
                programme.points_per_unit,
                programme.point_value,
                programme.amount_per_unit,
            )
        else:
            # ⚠ LE MODE POURCENTAGE SE CONVERTIT LUI AUSSI, et l'oublier était
            #   le défaut de la première écriture : `points_percentage` comptait
            #   en POINTS, pas en argent. Voir
            #   `pourcentage_depuis_pourcentage`.
            programme.earn_percent = pourcentage_depuis_pourcentage(
                programme.points_percentage,
                programme.point_value,
            )

        programme.updated_at = maintenant
        programme.save(update_fields=['legacy_point_value', 'earn_percent', 'updated_at'])

    for cagnotte in CustomerLoyalty.objects.all():
        facteur = taux.get(cagnotte.organization_id, UN)
        if facteur == UN:
            continue
        cagnotte.balance = convertir_montant(cagnotte.balance, facteur)
        cagnotte.total_earned = convertir_montant(cagnotte.total_earned, facteur)
        cagnotte.total_used = convertir_montant(cagnotte.total_used, facteur)
        cagnotte.updated_at = maintenant
        cagnotte.save(update_fields=[
            'balance', 'total_earned', 'total_used', 'updated_at',
        ])

    # Le registre n'est pas tiré sur les terminaux, mais il est LU par la fiche
    # d'un client et par son rapport : une ligne non convertie ferait une
    # colonne de montants dont la somme ne fait pas le solde.
    for ligne in LoyaltyTransaction.objects.all():
        facteur = taux.get(ligne.organization_id, UN)
        if facteur == UN:
            continue
        ligne.amount = convertir_montant(ligne.amount, facteur)
        ligne.balance_after = convertir_montant(ligne.balance_after, facteur)
        ligne.updated_at = maintenant
        ligne.save(update_fields=['amount', 'balance_after', 'updated_at'])


def rien(apps, schema_editor):
    """
    Irréversible en DONNÉES, et le dire vaut mieux que le feindre.

    Le schéma se défait (les `RenameField` sont réversibles), mais les soldes
    multipliés ne peuvent pas être redivisés : `legacy_point_value` servirait,
    sauf que la conversion a pu border un pourcentage, et surtout un gain
    accordé APRÈS la bascule n'a jamais eu de `point_value`. On ne rend donc
    rien plutôt que d'écrire une division qui fausserait les soldes récents.
    """
    return None


def retirer_la_tache(apps, schema_editor):
    """L'expiration disparaît : son battement n'a plus de tâche à appeler."""
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name=TACHE_EXPIRATION).delete()


def remettre_la_tache(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0015_horloge_metier'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        # 1. Les renommages, qui préservent les données en place.
        migrations.RenameField(
            model_name='loyaltyprogram',
            old_name='points_percentage',
            new_name='earn_percent',
        ),
        migrations.RenameField(
            model_name='customerloyalty',
            old_name='current_points',
            new_name='balance',
        ),
        migrations.RenameField(
            model_name='customerloyalty',
            old_name='total_points_earned',
            new_name='total_earned',
        ),
        migrations.RenameField(
            model_name='customerloyalty',
            old_name='total_points_redeemed',
            new_name='total_used',
        ),
        migrations.RenameField(
            model_name='customerloyalty',
            old_name='last_points_earned_at',
            new_name='last_earned_at',
        ),
        migrations.RenameField(
            model_name='customerloyalty',
            old_name='last_points_redeemed_at',
            new_name='last_used_at',
        ),
        migrations.RenameField(
            model_name='loyaltytransaction',
            old_name='points',
            new_name='amount',
        ),
        migrations.RenameField(
            model_name='organizationsettings',
            old_name='show_loyalty_points_on_receipt',
            new_name='show_loyalty_on_receipt',
        ),

        # 2. Le champ transitoire, posé AVANT la conversion qui le remplit.
        migrations.AddField(
            model_name='loyaltyprogram',
            name='legacy_point_value',
            field=models.DecimalField(
                blank=True, decimal_places=2, editable=False, max_digits=10,
                null=True,
                help_text="Valeur d'un point avant la bascule en argent. Transitoire.",
            ),
        ),

        # 3. La conversion des soldes.
        migrations.RunPython(convertir, rien),

        # 4. Les libellés et les aides, qui ne parlent plus de points.
        migrations.AlterField(
            model_name='loyaltyprogram',
            name='earn_percent',
            field=models.DecimalField(
                decimal_places=2, default=Decimal('1.00'), max_digits=5,
                help_text="Part de la facture reversée dans la cagnotte du client (en %)",
                validators=[
                    django.core.validators.MinValueValidator(Decimal('0.01')),
                    django.core.validators.MaxValueValidator(Decimal('100.00')),
                ],
            ),
        ),
        migrations.AlterField(
            model_name='loyaltyprogram',
            name='max_redemption_percent',
            field=models.DecimalField(
                decimal_places=2, default=Decimal('50.00'), max_digits=5,
                help_text="Part maximale d'une facture réglable avec la cagnotte (maximum 70 %)",
                validators=[
                    django.core.validators.MinValueValidator(Decimal('0.01')),
                    django.core.validators.MaxValueValidator(Decimal('70.00')),
                ],
            ),
        ),
        migrations.AlterField(
            model_name='loyaltyprogram',
            name='only_registered_customers',
            field=models.BooleanField(
                default=True,
                help_text='Seuls les clients enregistrés peuvent accumuler une cagnotte',
            ),
        ),
        migrations.AlterField(
            model_name='loyaltytransaction',
            name='amount',
            field=models.DecimalField(
                decimal_places=2, max_digits=15,
                help_text='Positif pour un gain, négatif pour une utilisation',
            ),
        ),
        migrations.AlterField(
            model_name='loyaltytransaction',
            name='balance_after',
            field=models.DecimalField(
                decimal_places=2, max_digits=15,
                help_text='Solde de la cagnotte après le mouvement',
            ),
        ),
        migrations.AlterField(
            model_name='loyaltytransaction',
            name='transaction_type',
            field=models.CharField(
                choices=[
                    ('earn', 'Cagnotte gagnée'),
                    ('redeem', 'Cagnotte utilisée'),
                    ('expire', 'Cagnotte expirée'),
                    ('adjust', 'Ajustement manuel'),
                    ('bonus', 'Bonus'),
                    ('earn_reversal', 'Annulation gain'),
                    ('redeem_reversal', 'Annulation utilisation'),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='organizationsettings',
            name='show_loyalty_on_receipt',
            field=models.BooleanField(
                default=True,
                help_text='Afficher la cagnotte gagnée et le solde du client sur les reçus',
            ),
        ),

        # 5. Ce qui disparaît, en DERNIER.
        migrations.RemoveField(model_name='loyaltyprogram', name='points_calculation_type'),
        migrations.RemoveField(model_name='loyaltyprogram', name='points_per_unit'),
        migrations.RemoveField(model_name='loyaltyprogram', name='amount_per_unit'),
        migrations.RemoveField(model_name='loyaltyprogram', name='point_value'),
        migrations.RemoveField(model_name='loyaltyprogram', name='min_points_to_redeem'),
        migrations.RemoveField(model_name='loyaltyprogram', name='points_expiry_days'),
        migrations.RemoveField(model_name='loyaltytransaction', name='reward'),
        migrations.RemoveField(model_name='loyaltyreward', name='loyalty_program'),
        migrations.RemoveField(model_name='loyaltyreward', name='organization'),
        migrations.RemoveField(model_name='loyaltyreward', name='product'),
        migrations.DeleteModel(name='LoyaltyReward'),

        # 6. Le battement d'expiration, qui n'a plus de tâche.
        migrations.RunPython(retirer_la_tache, remettre_la_tache),
    ]
