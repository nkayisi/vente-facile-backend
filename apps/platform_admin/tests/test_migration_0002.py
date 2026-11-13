"""
`platform_admin/0002` sur de vraies lignes : personne n'est enfermé dehors.

┌──────────────────────────────────────────────────────────────────────────────┐
│ SI CETTE MIGRATION RATE SON COUP, L'ÉQUIPE ENTIÈRE PERD L'ADMINISTRATION.   │
│                                                                              │
│ `IsPlatformAdmin` lit le PROFIL, plus `is_staff`. Un compte `is_staff` sans │
│ profil reçoit 403 sur chaque écran de `/admin`, et son jeton annonce         │
│ `is_staff: false` au back-office : il croit son compte cassé. Rien ne lève, │
│ la migration est « passée ». C'est le défaut silencieux qu'on éprouve ici,  │
│ jusqu'à la connexion elle-même.                                              │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.core.tests.migrations_harness import MigrationDataTestCase

MOT_DE_PASSE = 'Repetition@2026'


class StaffExistantsDeviennentSuperAdminTests(MigrationDataTestCase):
    migrate_from = ('platform_admin', '0001_profils_et_journal')
    migrate_to = ('platform_admin', '0002_staff_existants_super_admin')

    def preparer(self, apps):
        User = apps.get_model('users', 'User')
        Profil = apps.get_model('platform_admin', 'PlatformAdminProfile')

        def compte(email, **champs):
            return User.objects.create(
                email=email, password=make_password(MOT_DE_PASSE), **champs,
            )

        self.staff = compte('staff@repetition.vf', is_staff=True)
        self.staff_inactif = compte('ancien@repetition.vf', is_staff=True, is_active=False)
        self.marchand = compte('marchand@repetition.vf')
        # Un compte déjà nommé AVANT la migration (relancée, ou nommé à la main) :
        # elle ne doit pas le promouvoir au-dessus de ce qu'on a décidé pour lui.
        self.support = compte('support@repetition.vf', is_staff=True)
        Profil.objects.create(user_id=self.support.pk, role='support', is_active=True)

    def _profil(self, compte):
        Profil = self.apps_apres.get_model('platform_admin', 'PlatformAdminProfile')
        return Profil.objects.filter(user_id=compte.pk).first()

    def test_chaque_staff_recoit_un_profil_de_super_admin(self):
        profil = self._profil(self.staff)
        self.assertIsNotNone(profil, "un is_staff est resté sans profil : enfermé dehors")
        self.assertEqual(profil.role, 'super_admin')
        self.assertTrue(profil.is_active)

    def test_un_compte_non_staff_n_est_pas_promu(self):
        self.assertIsNone(self._profil(self.marchand))

    def test_un_profil_existant_n_est_pas_retouche(self):
        self.assertEqual(self._profil(self.support).role, 'support')

    def test_un_profil_par_staff_et_pas_un_de_plus(self):
        Profil = self.apps_apres.get_model('platform_admin', 'PlatformAdminProfile')
        User = self.apps_apres.get_model('users', 'User')
        self.assertEqual(
            Profil.objects.count(), User.objects.filter(is_staff=True).count(),
        )

    def test_chaque_nomination_laisse_sa_trace_au_journal(self):
        Journal = self.apps_apres.get_model('platform_admin', 'AdminActionLog')
        traces = set(
            Journal.objects.filter(action='grant_role').values_list('target_id', flat=True)
        )
        self.assertEqual(traces, {str(self.staff.pk), str(self.staff_inactif.pk)})

    def test_le_staff_se_connecte_et_son_jeton_le_dit_administrateur(self):
        """
        La preuve de bout en bout : la porte que le back-office emprunte.

        ⚠ Le jeton porte `is_staff` = « administre la plateforme », calculé sur
        le PROFIL. C'est cette valeur qui ouvre `/admin` côté web.
        """
        # Remettre la base à sa feuille : la connexion lit les modèles COURANTS.
        executeur = self._executeur()
        executeur.migrate(executeur.loader.graph.leaf_nodes())

        cache.clear()  # le verrou de connexion et les limites vivent dans Redis
        reponse = APIClient().post(
            '/api/v1/auth/token/',
            {'email': self.staff.email, 'password': MOT_DE_PASSE},
            format='json',
        )
        self.assertEqual(reponse.status_code, 200, reponse.content)
        self.assertTrue(reponse.data['user']['is_staff'])
