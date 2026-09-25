"""
Le tirage et la vue bornent PAREIL, table par table.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QU'UN TERMINAL DÉTIENT DOIT ÊTRE CE QUE SON PORTEUR A LE DROIT DE VOIR.  │
│                                                                              │
│ `_scope_to_warehouses` était une TROISIÈME écriture du périmètre, et elle    │
│ divergeait sur six tables : les sessions de caisse, les dépenses, les        │
│ mouvements de caisse et les transferts descendaient EN ENTIER ; les ventes   │
│ et les retours perdaient leurs lignes sans entrepôt, que le back-office      │
│ montre.                                                                      │
│                                                                              │
│ Comparer les DÉCLARATIONS ne suffirait pas : deux mécanismes différents      │
│ peuvent porter la même règle. Ce fichier compare donc les ENSEMBLES rendus.  │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Le membre est un GÉRANT borné à UN dépôt sur deux. En propriétaire, les deux
côtés rendent tout et le fichier serait vert sans rien démontrer.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement, Expense, ExpenseCategory
from apps.inventory.models import StockTransfer, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Register, RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.sync.pull import PULL_TABLES, PULL_TABLES_BY_NAME, read_page


class TirageEtVueTests(APITestCase):
    """Six tables réalignées, et le contrôle sur celles qui ne bougent pas."""

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_a = self.d['warehouse']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'], name='B', code='WH-B',
        )
        self.membership = OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        )

        self.reg_b = Register.objects.create(
            organization=self.org, branch=self.d['branch'], warehouse=self.wh_b,
            name='Caisse B', code='CB',
        )
        # Une session dans CHAQUE dépôt : le gérant ne doit voir que la sienne.
        self.sess_a = RegisterSession.objects.create(
            organization=self.org, register=self.d['register'],
            opened_by=self.d['manager'], status='open', opening_balance=Decimal('0'),
        )
        self.sess_b = RegisterSession.objects.create(
            organization=self.org, register=self.reg_b,
            opened_by=self.d['owner'], status='open', opening_balance=Decimal('0'),
        )

        def vente(ref, entrepot):
            return Sale.objects.create(
                organization=self.org, warehouse=entrepot, sold_by=self.d['manager'],
                reference=ref, status=Sale.Status.COMPLETED,
                subtotal=Decimal('10'), total=Decimal('10'),
                amount_paid=Decimal('10'), currency='CDF', exchange_rate=Decimal('1'),
            )
        vente('VT-A', self.wh_a)
        vente('VT-B', self.wh_b)
        # ⚠ La vente ANCIENNE sans entrepôt : c'est elle qui manquait au tirage.
        vente('VT-LEGACY', None)

        cat = ExpenseCategory.objects.create(
            organization=self.org, name='Divers', code='DIV'
        )
        for ref, entrepot in (('D-A', self.wh_a), ('D-B', self.wh_b), ('D-ORG', None)):
            Expense.objects.create(
                organization=self.org, reference=ref, category=cat,
                description=ref, amount=Decimal('5'),
                expense_date=timezone.localdate(), warehouse=entrepot,
                created_by=self.d['manager'], status='approved',
            )

        for ref, session in (('M-A', self.sess_a), ('M-B', self.sess_b), ('M-ORG', None)):
            CashMovement.objects.create(
                organization=self.org, reference=ref, direction='in',
                movement_type='other_in', amount=Decimal('5'), currency='CDF',
                description=ref, movement_date=timezone.now(),
                created_by=self.d['manager'], session=session,
            )

        StockTransfer.objects.create(
            organization=self.org, reference='TR-AB',
            source_warehouse=self.wh_a, destination_warehouse=self.wh_b,
            status='draft', requested_by=self.d['manager'],
        )
        StockTransfer.objects.create(
            organization=self.org, reference='TR-BB',
            source_warehouse=self.wh_b, destination_warehouse=self.wh_b,
            status='draft', requested_by=self.d['owner'],
        )

        # Un référentiel d'organisation, pour le contrôle en sens inverse.
        make_cash_payment_method(self.org)

        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    # -- outillage ----------------------------------------------------------

    def _tirage(self, nom, champ='reference'):
        lignes, _, _ = read_page(
            PULL_TABLES_BY_NAME[nom], self.org, self.membership, None, 500
        )
        return {l[champ] for l in lignes}

    def _vue(self, chemin, champ='reference'):
        r = self.client.get(chemin, {'page_size': 500})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content[:200])
        return {x[champ] for x in r.data['results']}

    # -- le croisement ------------------------------------------------------

    def test_le_balayage_voit_bien_TOUTES_les_tables(self):
        # Un balayage qui ne balaie rien passe au vert : ce dépôt s'est fait
        # prendre cinq fois.
        self.assertGreater(len(PULL_TABLES), 30)

    def test_les_ventes(self):
        """La vente ANCIENNE sans entrepôt doit descendre : le web la montre."""
        tire = self._tirage('sales')
        self.assertEqual(tire, self._vue('/api/v1/sales/'))
        self.assertIn('VT-LEGACY', tire)
        self.assertNotIn('VT-B', tire)

    def test_les_sessions_de_caisse(self):
        """Le tirage les descendait TOUTES."""
        # ⚠ `read_page` SÉRIALISE : les identifiants y sont des chaînes.
        tire = self._tirage('register_sessions', champ='id')
        self.assertNotIn(str(self.sess_b.id), tire)
        self.assertIn(str(self.sess_a.id), tire)

    def test_les_depenses(self):
        tire = self._tirage('expenses')
        self.assertEqual(tire, self._vue('/api/v1/expenses/'))
        self.assertNotIn('D-ORG', tire)
        self.assertNotIn('D-B', tire)

    def test_les_mouvements_de_caisse(self):
        tire = self._tirage('cash_movements')
        self.assertEqual(tire, self._vue('/api/v1/cash-movements/'))
        self.assertNotIn('M-ORG', tire)
        self.assertNotIn('M-B', tire)

    def test_les_transferts_bornent_en_OU(self):
        """Source OU destination : le magasinier de destination doit voir ce
        qu'on lui expédie. Mais « ne pas borner » n'est pas « borner en OU »."""
        tire = self._tirage('stock_transfers')
        self.assertIn('TR-AB', tire)
        self.assertNotIn('TR-BB', tire)

    def test_un_membre_SANS_entrepot_ne_tire_RIEN_de_borne(self):
        """
        ⚠ Le tirage était le SEUL des trois à ne pas borner : un membre mal
        configuré détenait toute l'organisation dans un SQLite non chiffré.
        """
        self.membership.assigned_warehouses.clear()
        self.membership._accessible_warehouse_ids = None
        for nom in ('sales', 'expenses', 'cash_movements', 'stock_transfers'):
            with self.subTest(table=nom):
                self.assertEqual(self._tirage(nom), set())

    def test_le_catalogue_reste_INTACT_pour_un_membre_sans_entrepot(self):
        """Le contrôle : on borne ce qui dépend d'un dépôt, pas le reste."""
        self.membership.assigned_warehouses.clear()
        self.membership._accessible_warehouse_ids = None
        lignes, _, _ = read_page(
            PULL_TABLES_BY_NAME['payment_methods'], self.org, self.membership, None, 500
        )
        self.assertTrue(lignes, "un référentiel d'organisation ne se borne pas")
