"""
Le jeton de périmètre : ce qui le fait bouger, et ce qui ne doit pas.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN JETON TROP SENSIBLE COÛTE AUSSI CHER QU'UN JETON INERTE.                 │
│                                                                              │
│ Inerte, il laisse une table amputée pour toujours. Trop sensible, il fait    │
│ effacer et re-tirer trente-huit tables à chaque réveil, sur une 2G. Ce       │
│ fichier tient les deux bords.                                                │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users
from apps.sync.pull import PULL_TABLES_BY_NAME, SCOPE_TOKEN_ORG, scope_token


class JetonDePerimetreTests(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'], name='B', code='WH-B',
        )
        self.gerant = OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        )
        self.proprio = OrganizationMembership.objects.get(
            user=self.d['owner'], organization=self.org
        )

    @staticmethod
    def _table(nom):
        return PULL_TABLES_BY_NAME[nom]

    # -- ce qui NE doit pas bouger -----------------------------------------

    def test_une_table_non_bornee_a_un_jeton_CONSTANT(self):
        """
        `products`, `customers`, `categories`… ne dépendent d'aucune
        affectation. Y mêler le rôle ou les entrepôts ferait re-tirer le
        catalogue entier le jour où un magasinier reçoit un second dépôt.
        """
        for nom in ('products', 'customers', 'categories'):
            with self.subTest(table=nom):
                self.assertEqual(
                    scope_token(self._table(nom), self.gerant), SCOPE_TOKEN_ORG
                )

    def test_le_jeton_ne_bouge_pas_sans_raison(self):
        avant = scope_token(self._table('sales'), self.gerant)
        self.gerant._accessible_warehouse_ids = None  # force la relecture
        self.assertEqual(scope_token(self._table('sales'), self.gerant), avant)

    # -- ce qui DOIT bouger -------------------------------------------------

    def test_recevoir_un_second_depot_fait_bouger_le_jeton(self):
        """
        C'est le cas qui perdait des lignes : le périmètre s'élargit, les
        lignes du nouveau dépôt sont derrière le curseur, et la sonde confirme
        « rien de neuf ».
        """
        avant = scope_token(self._table('sales'), self.gerant)
        self.gerant.assigned_warehouses.add(self.wh_b)
        self.gerant._accessible_warehouse_ids = None
        self.assertNotEqual(scope_token(self._table('sales'), self.gerant), avant)

    def test_deux_roles_n_ont_pas_le_meme_jeton(self):
        """Un propriétaire lit sans borne : son contenu n'est pas celui d'un gérant."""
        self.assertNotEqual(
            scope_token(self._table('sales'), self.proprio),
            scope_token(self._table('sales'), self.gerant),
        )

    def test_perdre_son_dernier_depot_fait_bouger_le_jeton(self):
        """
        ⚠ `None` (aucune restriction) et `[]` (aucun accès) sont OPPOSÉS. Les
        écrire pareil donnerait le même jeton au moment exact où le périmètre
        bascule de l'un à l'autre.
        """
        avant = scope_token(self._table('sales'), self.gerant)
        self.gerant.assigned_warehouses.clear()
        self.gerant._accessible_warehouse_ids = None
        apres = scope_token(self._table('sales'), self.gerant)
        self.assertNotEqual(apres, avant)
        self.assertNotEqual(apres, scope_token(self._table('sales'), self.proprio))

    # -- le manifeste le porte ----------------------------------------------

    def test_le_manifeste_porte_UN_jeton_par_table(self):
        self.client.force_authenticate(user=self.d['manager'])
        r = self.client.get(
            '/api/v1/sync/pull/manifest/', HTTP_X_ORGANIZATION_ID=str(self.org.id)
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        tables = r.data['tables']
        self.assertGreater(len(tables), 30, "le balayage ne balaie rien")
        for t in tables:
            with self.subTest(table=t['name']):
                self.assertIn('scope_token', t)
                self.assertTrue(t['scope_token'])

    def test_le_manifeste_d_un_GERANT_differe_de_celui_du_proprietaire(self):
        def jetons(qui):
            self.client.force_authenticate(user=qui)
            r = self.client.get(
                '/api/v1/sync/pull/manifest/', HTTP_X_ORGANIZATION_ID=str(self.org.id)
            )
            return {t['name']: t['scope_token'] for t in r.data['tables']}

        du_gerant = jetons(self.d['manager'])
        du_proprio = jetons(self.d['owner'])
        self.assertNotEqual(du_gerant['sales'], du_proprio['sales'])
        # …mais les tables non bornées, elles, restent identiques.
        self.assertEqual(du_gerant['products'], du_proprio['products'])
