"""
La commande de semis ne doit pas pouvoir toucher une base de production.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ELLE CRÉE SIX COMPTES, DONT UN PROPRIÉTAIRE, AU MOT DE PASSE PUBLIÉ.        │
│                                                                              │
│ `handle()` n'honorait que `--dry-run` : aucun contrôle de `DEBUG`, aucun     │
│ `--force`, aucune assertion d'environnement. La méprise est facile - les     │
│ deux commandes sont adjacentes dans la documentation, et                     │
│ `assign_default_warehouse` est une normalisation légitime de production.     │
│                                                                              │
│ ⚠ Et `owner@test.vf` est repris PAR COURRIEL, globalement : son mot de passe │
│   est RÉINITIALISÉ s'il diffère. La commande peut donc s'emparer d'un compte │
│   existant, pas seulement en créer un.                                       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.organizations.models import Organization


class VerrouDeLaCommandeTests(TestCase):
    """Deux verrous, comme la remise à zéro d'essai : l'environnement, ou un geste."""

    @override_settings(DEBUG=False)
    def test_elle_REFUSE_hors_developpement(self):
        with self.assertRaises(CommandError) as refus:
            call_command('seed_boutique_test')
        message = str(refus.exception)
        # ⚠ Le refus NOMME la base : « je refuse » sans dire contre quoi ferait
        #   relancer avec `--force` par réflexe.
        self.assertIn('--force', message)
        self.assertIn('PROPRIÉTAIRE', message)
        # ⚠ ET IL REFUSE AVANT D'ÉCRIRE. Sans cette assertion, un verrou retiré
        #   passerait au vert : l'appel est désormais SANS `--dry-run`, donc il
        #   créerait réellement les six comptes.
        self.assertFalse(Organization.objects.filter(name__icontains='Test VF').exists())

    @override_settings(DEBUG=False)
    def test_le_geste_EXPLICITE_la_rouvre(self):
        """Le contrôle : le verrou doit se lever, sinon il n'est pas tenable."""
        call_command('seed_boutique_test', '--dry-run', '--force')

    @override_settings(DEBUG=False)
    def test_l_APERCU_passe_hors_developpement(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ `--dry-run` N'ÉCRIT RIEN, DONC IL N'A RIEN À SE FAIRE REFUSER.      │
        │                                                                      │
        │ Le refus s'exécutait AVANT que l'option ne soit lue : l'aperçu en    │
        │ lecture seule - la façon la plus sûre d'inspecter avant de décider - │
        │ était refusé avec le chemin d'écriture, alors que la docstring du    │
        │ module l'annonce. L'opérateur apprenait à tendre vers `--force` par  │
        │ réflexe, c'est-à-dire exactement ce que ce refus veut rendre         │
        │ délibéré.                                                            │
        └──────────────────────────────────────────────────────────────────────┘
        """
        call_command('seed_boutique_test', '--dry-run')
        self.assertFalse(Organization.objects.filter(name__icontains='Test VF').exists())

    @override_settings(DEBUG=True)
    def test_en_developpement_elle_passe_sans_rien_demander(self):
        """C'est ce qu'elle existe pour faire : éprouver les six rôles en local."""
        call_command('seed_boutique_test', '--dry-run')

    @override_settings(DEBUG=False)
    def test_le_refus_ne_PUBLIE_PAS_le_secret_qu_il_protege(self):
        """
        ⚠ IL INTERPOLAIT `MOT_DE_PASSE`. Quand `VF_SEED_PASSWORD` EST posé - le
        chemin de production, le seul où un opérateur l'aura posé - le vrai
        secret partait vers stdout, stderr, les journaux de CI et l'historique
        du shell. Le refus devenait la fuite la plus sûre de la commande.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ ⚠ IL FAUT RECHARGER LE MODULE, ET SANS CELA CE TEST EST UN FAUX     │
        │   VERT. Mesuré : `MOT_DE_PASSE` est une constante de module, fixée   │
        │   à l'IMPORT. Poser la variable d'environnement après coup ne la     │
        │   change pas, donc un refus qui l'interpolerait publierait le        │
        │   DÉFAUT et non le secret posé - et l'assertion passerait. La        │
        │   mutation a bel et bien été vérifiée VERTE sur cette forme-là.      │
        └──────────────────────────────────────────────────────────────────────┘
        """
        import importlib
        import os

        import apps.organizations.management.commands.seed_boutique_test as mod

        avant = os.environ.get('VF_SEED_PASSWORD')
        os.environ['VF_SEED_PASSWORD'] = 'Secret-Tres-Reel@2026'
        try:
            importlib.reload(mod)
            self.assertEqual(mod.MOT_DE_PASSE, 'Secret-Tres-Reel@2026')

            with self.assertRaises(CommandError) as refus:
                mod.Command().handle(dry_run=False, force=False)
            message = str(refus.exception)
            self.assertNotIn('Secret-Tres-Reel@2026', message)
            # Il nomme la SOURCE, pour que l'opérateur sache quoi poser.
            self.assertIn('VF_SEED_PASSWORD', message)
        finally:
            if avant is None:
                del os.environ['VF_SEED_PASSWORD']
            else:
                os.environ['VF_SEED_PASSWORD'] = avant
            importlib.reload(mod)

    def test_le_mot_de_passe_se_lit_de_l_ENVIRONNEMENT(self):
        """
        Le défaut littéral ne sert qu'en `DEBUG` ; partout ailleurs il se pose
        par `VF_SEED_PASSWORD`. Le retirer tout à fait rendrait la commande
        inutilisable pour son objet.
        """
        import importlib

        import apps.organizations.management.commands.seed_boutique_test as mod

        with self.settings():
            import os

            avant = os.environ.get('VF_SEED_PASSWORD')
            os.environ['VF_SEED_PASSWORD'] = 'Autre@2026'
            try:
                recharge = importlib.reload(mod)
                self.assertEqual(recharge.MOT_DE_PASSE, 'Autre@2026')
            finally:
                if avant is None:
                    del os.environ['VF_SEED_PASSWORD']
                else:
                    os.environ['VF_SEED_PASSWORD'] = avant
                importlib.reload(mod)
