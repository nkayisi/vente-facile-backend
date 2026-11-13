"""
Le fonds de caisse PAR DEVISE descend avec sa session (lot L13).

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE TERMINAL NE SAVAIT RIEN DU FONDS D'UNE DEVISE SECONDAIRE.               │
│                                                                              │
│ `register_session_currency_balances` n'était pas au manifeste : le terminal │
│ ne pouvait ni proposer le fonds hérité de la clôture précédente pour le     │
│ dollar, ni compter ce fonds dans l'attendu de clôture - il comptait ZÉRO,   │
│ et le Z annonçait un écart égal au fonds, chaque soir, sur la devise        │
│ secondaire.                                                                 │
└──────────────────────────────────────────────────────────────────────────────┘

Rôle BORNÉ : le gérant, affecté à l'entrepôt de la caisse.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.register_sessions import open_register_session
from apps.sales.tests._helpers import make_org_with_users
from apps.sync.pull import PULL_TABLES_BY_NAME, _jeton_entrepot, scope_token

PULL = '/api/v1/sync/pull/'


class FondsParDeviseTests(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.session = open_register_session(
            self.org, self.d['register'].id, self.d['manager'],
            opening_balances=[{'currency': 'CDF', 'amount': '50000'}, {'currency': 'USD', 'amount': '20'}],
        )
        self.client.force_authenticate(user=self.d['manager'])

    def test_la_session_descend_avec_ses_fonds_par_devise(self):
        r = self.client.get(
            PULL, {'table': 'register_sessions', 'limit': 500},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        ligne = next(l for l in r.data['rows'] if l['id'] == str(self.session.id))
        fonds = {b['currency']: Decimal(b['opening_balance']) for b in ligne['currency_balances']}
        self.assertEqual(fonds, {'CDF': Decimal('50000'), 'USD': Decimal('20')})

    def test_le_manifeste_annonce_la_table_enfant(self):
        r = self.client.get(
            '/api/v1/sync/pull/manifest/', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertIn('register_session_currency_balances', str(r.data))


class RevisionDeJetonTests(APITestCase):
    """
    ⚠ La révision ne touche QUE sa table. Monter `SCOPE_RULE_VERSION` aurait
    fait re-tirer tout le parc, ventes comprises, pour un enfant ajouté.
    """

    def setUp(self):
        self.d = make_org_with_users()
        self.adhesion = self.d['manager'].memberships.get(organization=self.d['org'])

    def test_la_revision_change_le_jeton_des_sessions(self):
        table = PULL_TABLES_BY_NAME['register_sessions']
        self.assertTrue(table.revision)
        self.assertNotEqual(
            scope_token(table, self.adhesion), _jeton_entrepot(table, self.adhesion),
        )

    def test_les_autres_tables_gardent_leur_jeton(self):
        for nom in ('sales', 'stocks', 'registers', 'cash_movements', 'products'):
            with self.subTest(table=nom):
                table = PULL_TABLES_BY_NAME[nom]
                self.assertFalse(table.revision)
                self.assertEqual(
                    scope_token(table, self.adhesion), _jeton_entrepot(table, self.adhesion),
                )
