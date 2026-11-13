"""
`settings/0016` sur de vraies lignes : la conversion points → argent.

┌──────────────────────────────────────────────────────────────────────────────┐
│ C'EST LA MIGRATION QUI A ARRÊTÉ LA PRODUCTION LE 9 NOVEMBRE.                │
│                                                                              │
│ Elle lisait `programme.points_percentage`, que son propre `RenameField`      │
│ venait de renommer : `AttributeError`, `migrate` en échec, Gunicorn jamais   │
│ démarré. La suite était verte - une base de test n'a AUCUN programme de      │
│ fidélité, donc la boucle ne tournait jamais. Et même en développement, les   │
│ programmes étaient au mode par défaut (barème) : la branche POURCENTAGE, la  │
│ fautive, n'avait jamais été exécutée nulle part.                             │
│                                                                              │
│ Ces tests posent un programme de CHAQUE mode, avec un `point_value` qui      │
│ n'est pas un, et lisent ce que la migration a écrit. Un `AttributeError`     │
│ dans le `RunPython` les fait tomber dès `setUp`.                             │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import datetime, timezone as tz
from decimal import Decimal

from apps.core.tests.migrations_harness import MigrationDataTestCase

D = Decimal
ANCIEN = datetime(2026, 1, 1, tzinfo=tz.utc)


class ConversionDeLaCagnotteTests(MigrationDataTestCase):
    migrate_from = ('settings', '0015_horloge_metier')
    migrate_to = ('settings', '0016_cagnotte_en_argent')

    def preparer(self, apps):
        Organization = apps.get_model('organizations', 'Organization')
        Customer = apps.get_model('contacts', 'Customer')
        LoyaltyProgram = apps.get_model('settings', 'LoyaltyProgram')
        CustomerLoyalty = apps.get_model('settings', 'CustomerLoyalty')
        LoyaltyTransaction = apps.get_model('settings', 'LoyaltyTransaction')
        PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
        IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')

        def etablissement(slug, **programme):
            org = Organization.objects.create(name=slug.title(), slug=slug)
            prog = LoyaltyProgram.objects.create(organization=org, **programme)
            client = Customer.objects.create(organization=org, code=f'C-{slug}', name='Awa')
            cagnotte = CustomerLoyalty.objects.create(
                organization=org, customer=client,
                current_points=D('40.00'), total_points_earned=D('60.00'),
                total_points_redeemed=D('20.00'),
            )
            ligne = LoyaltyTransaction.objects.create(
                organization=org, customer_loyalty=cagnotte, transaction_type='earn',
                points=D('40.00'), balance_after=D('40.00'),
            )
            return prog, cagnotte, ligne

        # Le mode POURCENTAGE, celui qui a cassé la production.
        self.prog_pct, self.cag_pct, self.ligne_pct = etablissement(
            'pourcentage', points_calculation_type='percentage',
            points_percentage=D('2.00'), point_value=D('10.00'),
        )
        # Le barème par tranches : 1 point par 1 000, un point vaut 10.
        self.prog_bar, self.cag_bar, _ = etablissement(
            'bareme', points_calculation_type='fixed_per_amount',
            points_per_unit=1, amount_per_unit=D('1000.00'), point_value=D('10.00'),
        )
        # `point_value = 1`, l'ancien défaut : rien à multiplier.
        self.prog_un, self.cag_un, _ = etablissement(
            'unite', points_calculation_type='percentage',
            points_percentage=D('3.00'), point_value=D('1.00'),
        )

        # Des horodatages d'avant, pour voir lesquels la migration a bougés.
        CustomerLoyalty.objects.update(updated_at=ANCIEN)
        LoyaltyTransaction.objects.update(updated_at=ANCIEN)
        LoyaltyProgram.objects.update(updated_at=ANCIEN)

        # Le battement d'expiration, que la migration doit retirer.
        toutes_les_heures, _ = IntervalSchedule.objects.get_or_create(every=1, period='hours')
        PeriodicTask.objects.get_or_create(
            name='expire_loyalty_points',
            defaults={'task': 'apps.settings.tasks.expire', 'interval': toutes_les_heures},
        )

    def _relire(self, modele, objet):
        return self.apps_apres.get_model('settings', modele).objects.get(pk=objet.pk)

    # -------------------------------------------------------------- programmes

    def test_le_mode_pourcentage_se_convertit(self):
        """2 % de points valant 10 chacun, c'est 20 % de la facture en argent."""
        prog = self._relire('LoyaltyProgram', self.prog_pct)
        self.assertEqual(prog.earn_percent, D('20.00'))
        self.assertEqual(prog.legacy_point_value, D('10.00'))

    def test_le_bareme_se_convertit_en_taux_moyen(self):
        """1 point (valant 10) pour 1 000 dépensés, c'est 1 %."""
        self.assertEqual(self._relire('LoyaltyProgram', self.prog_bar).earn_percent, D('1.00'))

    def test_un_point_a_un_se_reprend_tel_quel(self):
        self.assertEqual(self._relire('LoyaltyProgram', self.prog_un).earn_percent, D('3.00'))

    # ------------------------------------------------------------------ soldes

    def test_les_soldes_sont_multiplies_par_la_valeur_du_point(self):
        """40 points à 10, c'est une cagnotte de 400 - pas de 40."""
        cag = self._relire('CustomerLoyalty', self.cag_pct)
        self.assertEqual(
            (cag.balance, cag.total_earned, cag.total_used),
            (D('400.00'), D('600.00'), D('200.00')),
        )

    def test_le_registre_est_converti_comme_le_solde(self):
        """Sinon la colonne des montants ne ferait plus le solde."""
        ligne = self._relire('LoyaltyTransaction', self.ligne_pct)
        self.assertEqual((ligne.amount, ligne.balance_after), (D('400.00'), D('400.00')))

    def test_un_point_a_un_ne_change_aucun_solde(self):
        self.assertEqual(self._relire('CustomerLoyalty', self.cag_un).balance, D('40.00'))

    # ------------------------------------------------------- visibilité au tirage

    def test_toute_ligne_convertie_redescend_aux_terminaux(self):
        """
        ⚠ `updated_at` DOIT AVOIR BOUGÉ sur chaque ligne réécrite : le tirage
        pagine sur ce curseur, et une cagnotte convertie dont l'horodatage
        resterait figé descendrait encore à 40 sur le comptoir, pour toujours.
        """
        for modele, objet in (
            ('CustomerLoyalty', self.cag_pct), ('CustomerLoyalty', self.cag_bar),
            ('LoyaltyTransaction', self.ligne_pct), ('LoyaltyProgram', self.prog_pct),
            ('LoyaltyProgram', self.prog_un),
        ):
            with self.subTest(modele=modele):
                self.assertGreater(self._relire(modele, objet).updated_at, ANCIEN)

    def test_le_battement_d_expiration_disparait(self):
        PeriodicTask = self.apps_apres.get_model('django_celery_beat', 'PeriodicTask')
        self.assertFalse(PeriodicTask.objects.filter(name='expire_loyalty_points').exists())
