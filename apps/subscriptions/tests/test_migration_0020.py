"""
`subscriptions/0020` sur de vraies lignes : l'essai à quatorze jours, au palier 0.

Une migration de données n'est éprouvée que sur des lignes : une base de test
est vide, et la boucle d'un `RunPython` n'y tourne jamais. C'est ce qui a laissé
passer la panne du 9 novembre (voir `apps/core/tests/migrations_harness.py`).
"""
from apps.core.tests.migrations_harness import MigrationDataTestCase


class EssaiQuatorzeJoursTests(MigrationDataTestCase):
    migrate_from = ('subscriptions', '0019_version_magasin_obligatoire')
    migrate_to = ('subscriptions', '0020_essai_quatorze_jours_palier_zero')

    def preparer(self, apps):
        GlobalConfig = apps.get_model('subscriptions', 'GlobalConfig')
        Plan = apps.get_model('subscriptions', 'Plan')
        Currency = apps.get_model('settings', 'Currency')

        # La base du harnais est VIDÉE entre deux tests : la devise que pose
        # `settings/0004` n'y est plus.
        usd = Currency.objects.get_or_create(code='USD', defaults={'name': 'Dollar', 'symbol': '$'})[0]
        GlobalConfig.objects.update_or_create(pk=1, defaults={'trial_days': 15})

        Plan.objects.create(
            code='trial', name='Essai Gratuit', currency=usd, tier=1,
            trial_days=15, description="Plan d'essai gratuit de 15 jours",
        )
        Plan.objects.create(
            code='starter', name='Starter', currency=usd, tier=1,
            price_monthly=5, description='Un plan pour débuter',
        )

    def _plan(self, code):
        return self.apps_apres.get_model('subscriptions', 'Plan').objects.get(code=code)

    def test_la_duree_d_essai_passe_a_quatorze_jours(self):
        GlobalConfig = self.apps_apres.get_model('subscriptions', 'GlobalConfig')
        self.assertEqual(GlobalConfig.objects.get(pk=1).trial_days, 14)

    def test_le_plan_d_essai_descend_au_palier_zero(self):
        essai = self._plan('trial')
        self.assertEqual(essai.tier, 0)
        self.assertEqual(essai.trial_days, 14)
        self.assertEqual(essai.description, "Plan d'essai gratuit de 14 jours")

    def test_un_plan_payant_garde_son_palier(self):
        # Contrôle : la migration ne descend que l'ESSAI.
        self.assertEqual(self._plan('starter').tier, 1)


class DureeDeciderParLAdministrateurTests(MigrationDataTestCase):
    """Une durée réglée à la main n'est PAS la valeur d'origine : elle reste."""

    migrate_from = ('subscriptions', '0019_version_magasin_obligatoire')
    migrate_to = ('subscriptions', '0020_essai_quatorze_jours_palier_zero')

    def preparer(self, apps):
        GlobalConfig = apps.get_model('subscriptions', 'GlobalConfig')
        Plan = apps.get_model('subscriptions', 'Plan')
        Currency = apps.get_model('settings', 'Currency')

        GlobalConfig.objects.update_or_create(pk=1, defaults={'trial_days': 30})
        Plan.objects.create(
            code='trial', name='Essai',
            currency=Currency.objects.get_or_create(code='USD', defaults={'name': 'Dollar', 'symbol': '$'})[0],
            tier=1, trial_days=30, description='Trente jours pour essayer',
        )

    def test_une_duree_choisie_n_est_pas_ramenee_a_quatorze(self):
        GlobalConfig = self.apps_apres.get_model('subscriptions', 'GlobalConfig')
        self.assertEqual(GlobalConfig.objects.get(pk=1).trial_days, 30)

    def test_une_description_reecrite_n_est_pas_touchee(self):
        essai = self.apps_apres.get_model('subscriptions', 'Plan').objects.get(code='trial')
        self.assertEqual(essai.trial_days, 30)
        self.assertEqual(essai.description, 'Trente jours pour essayer')
        # Le palier, lui, descend : ce n'est pas un réglage mais un rang.
        self.assertEqual(essai.tier, 0)
