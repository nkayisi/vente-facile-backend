"""
Le sélecteur et la validation se répondent.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE TEST CROISE LES DEUX SENS, ET C'EST TOUT SON OBJET.                      │
│                                                                              │
│ `build_team_payload` PROPOSE des cibles, `assert_user_allowed_for_membership`│
│ les REFUSE. Écrits séparément, ils avaient divergé : le roster rendait tous  │
│ les membres de l'organisation pendant que la validation en refusait la       │
│ moitié, et le marchand recevait 400 sur un nom que l'application venait de   │
│ lui proposer. Un test qui ne regarderait qu'un sens laisserait rouvrir la    │
│ divergence par l'autre.                                                      │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LE DEMANDEUR EST TOUJOURS UN RÔLE BORNÉ, JAMAIS `owner`. Un propriétaire sort
en amont de toute la logique (`accessible_warehouse_ids` rend `None`) : écrit
depuis lui, ce fichier serait vert sans rien exécuter de ce qu'il prétend
couvrir. C'est la règle du dépôt, et son oubli a déjà caché un défaut un lot
entier durant.
"""
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.core.warehouse_scope import assert_user_allowed_for_membership
from apps.inventory.models import Warehouse
from apps.organizations.models import Branch, Organization, OrganizationMembership
from apps.users.devices import build_team_payload
from apps.users.models import User


def _u(email, prenom):
    return User.objects.create_user(
        email=email, password='pw12345!', first_name=prenom, last_name='T',
    )


class CiblesDuPerimetreTests(TestCase):
    """Deux dépôts disjoints, un propriétaire, et un membre sans affectation."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Org', slug='org')
        branche = Branch.objects.create(
            organization=cls.org, name='Main', code='MAIN', is_main=True,
        )
        cls.depot_a = Warehouse.objects.create(
            organization=cls.org, branch=branche, name='A', code='A', is_default=True,
        )
        cls.depot_b = Warehouse.objects.create(
            organization=cls.org, branch=branche, name='B', code='B',
        )

        def membre(email, prenom, role, depots=()):
            m = OrganizationMembership.objects.create(
                user=_u(email, prenom), organization=cls.org, role=role, is_active=True,
            )
            # ⚠ `set()` ET NON `add()` : un signal donne désormais l'entrepôt
            # principal à tout membre borné qui naît sans affectation, pour que
            # la configuration fautive cesse d'exister. Ajouter par-dessus
            # laisserait ici un membre du dépôt B qui aurait AUSSI le dépôt A,
            # et l'« orphelin » ne serait plus orphelin - la fixture ne
            # décrirait plus ce qu'elle prétend décrire.
            m.assigned_warehouses.set(depots)
            return m

        R = OrganizationMembership.Role
        cls.proprio = membre('o@t.test', 'Proprio', R.OWNER)
        cls.gerant_a = membre('ga@t.test', 'GerantA', R.MANAGER, [cls.depot_a])
        cls.gerant_b = membre('gb@t.test', 'GerantB', R.MANAGER, [cls.depot_b])
        cls.magasinier_a = membre('ma@t.test', 'MagA', R.STOCK_KEEPER, [cls.depot_a])
        cls.caissier_a = membre('ca@t.test', 'CaisseA', R.CASHIER, [cls.depot_a])
        cls.caissier_b = membre('cb@t.test', 'CaisseB', R.CASHIER, [cls.depot_b])
        cls.orphelin = membre('orph@t.test', 'Orphelin', R.MANAGER)

    # -- le défaut d'origine ------------------------------------------------

    def test_un_gerant_peut_viser_le_PROPRIETAIRE(self):
        """
        Le propriétaire n'a AUCUN `assigned_warehouses` - c'est son rôle qui lui
        donne tout - si bien qu'une intersection sur le M2M le rejetait
        systématiquement. Or les deux clients le proposent, et le filtre
        volontaire est de toute façon superposé au périmètre du rôle : le gérant
        ne lit que son activité DANS ses propres dépôts.
        """
        assert_user_allowed_for_membership(self.gerant_a, self.proprio.user_id)

    def test_un_magasinier_aussi(self):
        """`users.view` n'est pas la borne : le magasinier a le filtre sans elle."""
        assert_user_allowed_for_membership(self.magasinier_a, self.proprio.user_id)

    def test_le_ROSTER_propose_le_proprietaire(self):
        noms = {m['name'] for m in build_team_payload(self.org, self.gerant_a)['members']}
        self.assertIn('Proprio T', noms)

    # -- ce qui reste fermé -------------------------------------------------

    def test_un_gerant_ne_vise_pas_un_membre_d_un_AUTRE_depot(self):
        with self.assertRaises(ValidationError):
            assert_user_allowed_for_membership(self.gerant_a, self.caissier_b.user_id)

    def test_le_roster_n_expose_pas_les_membres_d_un_autre_depot(self):
        """
        C'est la moitié la plus coûteuse du défaut : le roster les proposait,
        donc le marchand en choisissait un, et recevait un refus.
        """
        noms = {m['name'] for m in build_team_payload(self.org, self.gerant_a)['members']}
        self.assertNotIn('CaisseB T', noms)
        self.assertNotIn('GerantB T', noms)

    def test_un_caissier_n_a_pas_de_roster(self):
        paquet = build_team_payload(self.org, self.caissier_a)
        self.assertFalse(paquet['visible'])
        self.assertEqual(paquet['members'], [])

    def test_un_caissier_ne_vise_que_lui_meme(self):
        assert_user_allowed_for_membership(self.caissier_a, self.caissier_a.user_id)
        with self.assertRaises(ValidationError):
            assert_user_allowed_for_membership(self.caissier_a, self.gerant_a.user_id)

    # -- le croisement, dans les DEUX sens ----------------------------------

    def test_tout_ce_que_le_roster_propose_est_ACCEPTE(self):
        for demandeur in (self.gerant_a, self.magasinier_a, self.gerant_b):
            paquet = build_team_payload(self.org, demandeur)
            self.assertTrue(paquet['members'], "roster vide : le test ne prouverait rien")
            for membre in paquet['members']:
                with self.subTest(demandeur=demandeur.user.first_name, cible=membre['name']):
                    assert_user_allowed_for_membership(demandeur, membre['user_id'])

    def test_tout_ce_qui_est_ACCEPTE_est_propose_par_le_roster(self):
        """
        Le sens inverse, celui qu'on oublie : une cible recevable et absente du
        sélecteur est une fonction que le marchand ne peut pas atteindre.
        """
        tous = OrganizationMembership.objects.filter(organization=self.org, is_active=True)
        for demandeur in (self.gerant_a, self.magasinier_a, self.gerant_b):
            proposes = {
                m['user_id'] for m in build_team_payload(self.org, demandeur)['members']
            }
            for cible in tous:
                try:
                    assert_user_allowed_for_membership(demandeur, cible.user_id)
                except ValidationError:
                    continue
                with self.subTest(demandeur=demandeur.user.first_name, cible=cible.user.first_name):
                    self.assertIn(str(cible.user_id), proposes)

    # -- les bords ----------------------------------------------------------

    def test_un_membre_sans_affectation_ne_figure_dans_aucun_roster_borne(self):
        """
        Cohérent avec le web, où il n'a de toute façon aucune ligne visible.
        Le propriétaire, lui, le voit : `accessible_warehouse_ids` rend `None`.
        """
        noms = {m['name'] for m in build_team_payload(self.org, self.gerant_a)['members']}
        self.assertNotIn('Orphelin T', noms)
        noms_proprio = {
            m['name'] for m in build_team_payload(self.org, self.proprio)['members']
        }
        self.assertIn('Orphelin T', noms_proprio)

    def test_un_membre_affecte_a_DEUX_de_mes_depots_ne_sort_qu_une_fois(self):
        """
        Le prédicat traverse un M2M : sans `.distinct()`, il sortirait en double
        et compterait deux fois contre le plafond de deux cents.
        """
        self.gerant_a.assigned_warehouses.add(self.depot_b)
        self.gerant_a._accessible_warehouse_ids = None
        polyvalent = OrganizationMembership.objects.create(
            user=_u('poly@t.test', 'Poly'), organization=self.org,
            role=OrganizationMembership.Role.CASHIER, is_active=True,
        )
        polyvalent.assigned_warehouses.add(self.depot_a, self.depot_b)

        ids = [m['user_id'] for m in build_team_payload(self.org, self.gerant_a)['members']]
        self.assertEqual(ids.count(str(polyvalent.user_id)), 1)
