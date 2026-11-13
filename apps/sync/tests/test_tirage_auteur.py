"""
Le tirage borne les ventes et les devis d'un caissier à ceux qu'il a faits.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE TERMINAL D'UN CAISSIER PORTAIT LES VENTES DE TOUS SES COLLÈGUES.         │
│                                                                              │
│ `SaleViewSet` borne un membre sans `sales.view_all` à ses propres ventes     │
│ (`sold_by`), `QuotationViewSet` à ses propres devis (`created_by`). Le       │
│ tirage, lui, ne bornait que par entrepôt : chaque terminal de caissier       │
│ détenait, dans un SQLite non chiffré, les ventes de tout le dépôt - clients, │
│ montants, dettes - que le back-office lui refusait une à une.                │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LA RÈGLE EST CELLE DE LA PERMISSION, pas du rôle : un caissier à qui le
  marchand accorde `sales.view_all` voit les ventes de son dépôt, comme au web.

⚠ LE JETON NE BOUGE QUE POUR QUI EST BORNÉ : le gérant et le propriétaire
  gardent le leur, sans quoi tout le parc re-tirerait ses ventes pour rien.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.models import Quotation, Sale
from apps.sales.tests._helpers import make_org_with_users
from apps.sync.pull import PULL_TABLES_BY_NAME, SCOPE_TOKEN_ORG, scope_token

PULL = '/api/v1/sync/pull/'


class _Base(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        for ref, auteur in (('VT-A', 'cashier_a'), ('VT-B', 'cashier_b')):
            Sale.objects.create(
                organization=self.org, warehouse=self.d['warehouse'],
                sold_by=self.d[auteur], reference=ref, status=Sale.Status.COMPLETED,
                subtotal=Decimal('10'), total=Decimal('10'), amount_paid=Decimal('10'),
                currency='CDF', exchange_rate=Decimal('1'),
            )
            Quotation.objects.create(
                organization=self.org, reference=ref.replace('VT', 'DEV'),
                created_by=self.d[auteur], status=Quotation.Status.DRAFT,
                subtotal=Decimal('10'), total=Decimal('10'), valid_until='2030-01-01',
            )

    def _tirer(self, qui, table):
        self.client.force_authenticate(user=self.d[qui])
        r = self.client.get(
            PULL, {'table': table, 'limit': 500},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        return sorted(row['reference'] for row in r.data['rows'])

    def _adhesion(self, qui):
        return self.d[qui].memberships.get(organization=self.org)


class BorneParAuteurTests(_Base):
    def test_un_caissier_ne_tire_que_ses_ventes(self):
        self.assertEqual(self._tirer('cashier_a', 'sales'), ['VT-A'])

    def test_un_caissier_ne_tire_que_ses_devis(self):
        self.assertEqual(self._tirer('cashier_a', 'quotations'), ['DEV-A'])

    def test_le_gerant_tire_tout_le_depot(self):
        # Contrôle : la borne ne touche que celui qui n'a pas `sales.view_all`.
        self.assertEqual(self._tirer('manager', 'sales'), ['VT-A', 'VT-B'])
        self.assertEqual(self._tirer('manager', 'quotations'), ['DEV-A', 'DEV-B'])

    def test_la_permission_accordee_ouvre_le_depot(self):
        adhesion = self._adhesion('cashier_a')
        adhesion.extra_permissions = ['sales.view_all']
        adhesion.save(update_fields=['extra_permissions'])
        self.assertEqual(self._tirer('cashier_a', 'sales'), ['VT-A', 'VT-B'])


class JetonTests(_Base):
    def _jeton(self, qui, table):
        return scope_token(PULL_TABLES_BY_NAME[table], self._adhesion(qui))

    def test_le_jeton_d_un_caissier_borne_differe(self):
        # C'est ce qui fait effacer puis re-tirer, une fois, les ventes des
        # collègues qu'un terminal de caissier détenait déjà.
        self.assertNotEqual(self._jeton('cashier_a', 'sales'), self._jeton('manager', 'sales'))
        self.assertNotEqual(self._jeton('cashier_a', 'quotations'), SCOPE_TOKEN_ORG)

    def test_deux_caissiers_n_ont_pas_le_meme_jeton(self):
        # Le jeton nomme l'AUTEUR : un terminal partagé qui change de caissier
        # ne doit pas garder les ventes du précédent.
        self.assertNotEqual(self._jeton('cashier_a', 'sales'), self._jeton('cashier_b', 'sales'))

    def test_qui_n_est_pas_borne_garde_son_jeton(self):
        # Les devis ne sont bornés par aucun entrepôt : sans borne d'auteur,
        # leur jeton reste celui d'une table d'organisation, donc INCHANGÉ.
        self.assertEqual(self._jeton('manager', 'quotations'), SCOPE_TOKEN_ORG)
        self.assertEqual(self._jeton('owner', 'quotations'), SCOPE_TOKEN_ORG)
