"""
`verifier_deploiement` : ce qu'un déploiement doit avoir laissé derrière lui.

⚠ `test_aucun_modele_n_est_modifie_sans_sa_migration` VAUT `makemigrations
  --check` DANS LA SUITE. Un modèle modifié sans migration ne casse rien au
  déploiement - il casse le suivant, qui découvre une migration à créer sur la
  machine de production.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django_celery_beat.models import PeriodicTask

from apps.core.management.commands.verifier_deploiement import (
    AVERTISSEMENT, ERREUR, OK, TACHE_DE_PURGE, administrateurs, configuration,
    migrations_en_attente, modeles_sans_migration, taches_planifiees,
)
from apps.platform_admin.models import PlatformAdminProfile, PlatformRole

User = get_user_model()


def _niveaux(constats):
    return {c.niveau for c in constats}


class VerifierDeploiementTests(TestCase):
    def _admin(self, email, role=PlatformRole.SUPER_ADMIN, **champs):
        u = User.objects.create_user(email=email, password='x', is_staff=True, **champs)
        PlatformAdminProfile.objects.create(user=u, role=role, is_active=True)
        return u

    def test_la_base_de_test_est_a_jour(self):
        self.assertEqual(_niveaux(migrations_en_attente()), {OK})

    def test_aucun_modele_n_est_modifie_sans_sa_migration(self):
        constats = modeles_sans_migration()
        self.assertEqual(_niveaux(constats), {OK}, constats[0].message)

    def test_un_staff_sans_profil_est_nomme_et_bloque_le_deploiement(self):
        """Il recevrait 403 partout : c'est le défaut silencieux de `platform_admin/0002`."""
        self._admin('chef@vf.test')
        User.objects.create_user(email='oublie@vf.test', password='x', is_staff=True)

        constats = administrateurs()
        erreurs = [c for c in constats if c.niveau == ERREUR]
        self.assertEqual(len(erreurs), 1)
        self.assertIn('oublie@vf.test', erreurs[0].message)

        with self.assertRaises(CommandError):
            call_command('verifier_deploiement', stdout=StringIO())

    def test_un_superutilisateur_n_a_pas_besoin_de_profil(self):
        User.objects.create_superuser(email='racine@vf.test', password='x')
        self.assertEqual(_niveaux(administrateurs()), {OK})

    def test_des_admins_sans_aucun_capable_de_nommer_est_une_erreur(self):
        """Plus personne ne pourrait réparer une erreur de rôle."""
        self._admin('support@vf.test', role=PlatformRole.SUPPORT)
        self.assertIn(ERREUR, _niveaux(administrateurs()))

    def test_une_installation_neuve_n_est_qu_un_avertissement(self):
        self.assertEqual(_niveaux(administrateurs()), {AVERTISSEMENT})

    def test_la_purge_des_journaux_doit_etre_planifiee(self):
        self.assertEqual(_niveaux(taches_planifiees()), {OK})
        PeriodicTask.objects.filter(name=TACHE_DE_PURGE).delete()
        self.assertEqual(_niveaux(taches_planifiees()), {ERREUR})

    @override_settings(DEBUG=False, VF_EDGE_KEY='', TRUSTED_PROXY_COUNT=1)
    def test_sans_cle_de_bordure_le_journal_ment_et_on_le_dit(self):
        constats = configuration()
        self.assertEqual(_niveaux(constats), {AVERTISSEMENT})
        self.assertIn('VF_EDGE_KEY', constats[0].message)

    @override_settings(DEBUG=False, VF_EDGE_KEY='secret', TRUSTED_PROXY_COUNT=1)
    def test_une_production_bien_reglee_passe(self):
        self.assertEqual(_niveaux(configuration()), {OK})

    def test_la_commande_conclut_quand_tout_est_conforme(self):
        self._admin('chef@vf.test')
        sortie = StringIO()
        call_command('verifier_deploiement', stdout=sortie)
        self.assertIn('Déploiement conforme', sortie.getvalue())
