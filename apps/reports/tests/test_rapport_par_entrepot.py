"""
Un rapport financier VENTILÉ par entrepôt, caisse comprise.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE FILTRE EXISTAIT, LA VENTILATION NON.                                     │
│                                                                              │
│ Les huit onglets se filtrent par entrepôt depuis septembre : un gérant peut  │
│ lire un dépôt à la fois. Mais aucune action ne rendait une ligne PAR dépôt,  │
│ si bien que comparer deux boutiques demandait de changer le filtre, de        │
│ relever les chiffres à la main et de les additionner soi-même - avec le      │
│ risque d'additionner deux devises. C'est T6 du retour de test.               │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LE DEMANDEUR EST UN GÉRANT, jamais un propriétaire : `accessible_warehouse_ids`
sort en amont pour un owner, et la moitié du code de périmètre ne serait pas
exécutée. C'est la règle de ce dépôt, et son oubli a laissé passer la moitié des
défauts de périmètre.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement, Expense, ExpenseCategory
from apps.inventory.models import Stock, Warehouse
from apps.products.models import Product
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users


class RapportParEntrepotTests(APITestCase):
    URL = '/api/v1/reports/statistics/by-warehouse/'

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_a = self.d['warehouse']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt Bravo', code='WH-B',
        )

        self.gerant = OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        )
        # Le gérant ne porte que WH-A : c'est l'écart avec le propriétaire qui
        # prouve le périmètre.
        self.gerant.assigned_warehouses.set([self.wh_a])

        self.vente_a = self._vente(self.wh_a, 'VT-A-1', '1000')
        self.vente_b = self._vente(self.wh_b, 'VT-B-1', '700')
        # ⚠ Une vente SANS entrepôt : elle entre dans le TOTAL et dans aucune
        # ligne. C'est l'écart qui dit qu'il existe des opérations
        # d'établissement, et c'est la tolérance que portent les `_scope_*`.
        self.vente_nulle = self._vente(None, 'VT-LEGACY', '50')

        # Du STOCK dans les deux dépôts : c'est sur son total que la fuite
        # mesurée portait, et sans lui le garde-fou ne mordrait pas.
        article = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV-1',
            cost_price=Decimal('10'), selling_price=Decimal('15'),
        )
        Stock.objects.create(
            organization=self.org, product=article, warehouse=self.wh_a,
            quantity=Decimal('100'), avg_cost=Decimal('10'),
        )
        Stock.objects.create(
            organization=self.org, product=article, warehouse=self.wh_b,
            quantity=Decimal('40'), avg_cost=Decimal('10'),
        )

        categorie = ExpenseCategory.objects.create(organization=self.org, name='Loyer')
        self._depense(categorie, self.wh_a, 'DEP-A', '300')
        self._depense(categorie, self.wh_b, 'DEP-B', '120')

        # Un mouvement de caisse n'a PAS d'entrepôt : il se dérive de sa vente.
        CashMovement.objects.create(
            organization=self.org, sale=self.vente_a, direction='in',
            movement_type='sale', amount=Decimal('1000'),
            currency='CDF', exchange_rate=Decimal('1'),
            reference='MVT-A-1',
            description='Encaissement A',
            # ⚠ `movement_date` est NON NULLE et sans défaut : c'est le champ
            # exact que le journal du terminal oubliait, et qui envoyait tout
            # mouvement de caisse saisi au comptoir en quarantaine.
            movement_date=self.vente_a.sale_date,
        )
        CashMovement.objects.create(
            organization=self.org, sale=self.vente_b, direction='in',
            movement_type='sale', amount=Decimal('700'),
            currency='CDF', exchange_rate=Decimal('1'),
            reference='MVT-B-1',
            description='Encaissement B',
            movement_date=self.vente_b.sale_date,
        )

    def _vente(self, wh, ref, total):
        return Sale.objects.create(
            organization=self.org, warehouse=wh, sold_by=self.d['manager'],
            reference=ref, status=Sale.Status.COMPLETED,
            subtotal=Decimal(total), total=Decimal(total),
            amount_paid=Decimal(total), currency='CDF', exchange_rate=Decimal('1'),
        )

    def _depense(self, categorie, wh, ref, montant):
        return Expense.objects.create(
            organization=self.org, category=categorie, warehouse=wh,
            created_by=self.d['manager'], reference=ref, description=ref,
            amount=Decimal(montant), currency='CDF', exchange_rate=Decimal('1'),
            expense_date=self.vente_a.sale_date.date(),
        )

    def _appel(self, qui):
        self.client.force_authenticate(user=qui)
        return self.client.get(
            self.URL, {'period': 'last_30_days'},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def _depots(self, data):
        return {w['warehouse_code']: w for w in data['warehouses']}

    # --- La ventilation ------------------------------------------------------

    def test_une_ligne_par_entrepot(self):
        r = self._appel(self.d['owner'])
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        d = self._depots(r.data)
        self.assertEqual(set(d), {'MAIN-WH', 'WH-B'})
        self.assertEqual(Decimal(d['MAIN-WH']['sales_total']), Decimal('1000.00'))
        self.assertEqual(Decimal(d['WH-B']['sales_total']), Decimal('700.00'))

    def test_les_depenses_suivent_leur_entrepot(self):
        d = self._depots(self._appel(self.d['owner']).data)
        self.assertEqual(Decimal(d['MAIN-WH']['expenses_total']), Decimal('300.00'))
        self.assertEqual(Decimal(d['WH-B']['expenses_total']), Decimal('120.00'))

    def test_la_caisse_se_DERIVE_de_la_vente(self):
        """
        `CashMovement` ne porte aucun entrepôt : les trois chemins et leur ordre
        sont ceux de `filter_cash_movement_queryset`. Un quatrième jeu de règles
        ferait un quatrième solde.
        """
        d = self._depots(self._appel(self.d['owner']).data)
        self.assertEqual(Decimal(d['MAIN-WH']['cash_in']), Decimal('1000.00'))
        self.assertEqual(Decimal(d['WH-B']['cash_in']), Decimal('700.00'))

    def test_le_resultat_est_ventes_moins_depenses(self):
        d = self._depots(self._appel(self.d['owner']).data)
        self.assertEqual(Decimal(d['MAIN-WH']['result']), Decimal('700.00'))
        self.assertEqual(Decimal(d['WH-B']['result']), Decimal('580.00'))

    # --- Le périmètre --------------------------------------------------------

    def test_un_gerant_ne_voit_QUE_ses_depots(self):
        d = self._depots(self._appel(self.d['manager']).data)
        self.assertEqual(set(d), {'MAIN-WH'})

    def test_le_TOTAL_d_un_gerant_ne_porte_que_son_perimetre(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ LA FUITE MESURÉE AVANT CORRECTIF.                                   │
        │                                                                      │
        │ Les lignes par dépôt ne lisent que leur propre clé, donc elles       │
        │ étaient justes ; le TOTAL, lui, sommait un dictionnaire bâti sans    │
        │ périmètre. Relevé sur la boutique de test : un gérant d'un seul      │
        │ dépôt lisait 3 182 000 de stock pour un dépôt qui en porte 2 706 000.│
        └──────────────────────────────────────────────────────────────────────┘
        """
        r = self._appel(self.d['manager'])
        t = r.data['total']
        # 1000 de WH-A, plus 50 de la vente SANS entrepôt que la tolérance de
        # rôle laisse passer. Jamais les 700 de WH-B.
        self.assertEqual(Decimal(t['sales_total']), Decimal('1050.00'))
        self.assertEqual(Decimal(t['expenses_total']), Decimal('300.00'))

        # LA FUITE ELLE-MÊME : 100 unités à 10 dans SON dépôt, jamais les 40 de
        # WH-B. Sans borne, ce total valait 1 400 - la valeur du stock de tout
        # l'établissement, sur le rapport dont le métier est de ne montrer que
        # son périmètre.
        self.assertEqual(Decimal(t['stock_value']), Decimal('1000.00'))

    def test_le_TOTAL_du_proprietaire_porte_tout(self):
        """Le contrôle en sens inverse : sans lui, on ne saurait pas si la
        borne ci-dessus refuse trop."""
        t = self._appel(self.d['owner']).data['total']
        self.assertEqual(Decimal(t['sales_total']), Decimal('1750.00'))
        self.assertEqual(Decimal(t['expenses_total']), Decimal('420.00'))
        self.assertEqual(Decimal(t['stock_value']), Decimal('1400.00'))

    def test_le_stock_de_chaque_depot_est_le_sien(self):
        d = self._depots(self._appel(self.d['owner']).data)
        self.assertEqual(Decimal(d['MAIN-WH']['stock_value']), Decimal('1000.00'))
        self.assertEqual(Decimal(d['WH-B']['stock_value']), Decimal('400.00'))

    def test_une_vente_SANS_entrepot_n_entre_dans_AUCUNE_ligne(self):
        """
        Un apport sans tiroir, une charge d'établissement, une vente ancienne :
        l'imputer à un dépôt gonflerait celui-là, l'imputer à tous ferait que la
        somme des lignes dépasse le total. Elle est donc dans le TOTAL seul, et
        l'écart se lit.
        """
        r = self._appel(self.d['owner'])
        somme = sum(Decimal(w['sales_total']) for w in r.data['warehouses'])
        self.assertEqual(somme, Decimal('1700.00'))
        self.assertEqual(Decimal(r.data['total']['sales_total']), Decimal('1750.00'))
        self.assertLess(somme, Decimal(r.data['total']['sales_total']))

    # --- La forme ------------------------------------------------------------

    def test_la_devise_principale_est_NOMMEE(self):
        """
        Sans elle, le client étiquette des montants convertis avec le symbole
        que son contexte porte, sans savoir s'ils le sont.
        """
        self.assertEqual(self._appel(self.d['owner']).data['currency'], 'CDF')

    def test_les_montants_sont_quantifies(self):
        d = self._depots(self._appel(self.d['owner']).data)
        for w in d.values():
            for cle in ('sales_total', 'expenses_total', 'cash_net', 'stock_value', 'result'):
                _, _, dec = str(w[cle]).partition('.')
                self.assertLessEqual(len(dec), 2, f"{cle}={w[cle]}")

    def test_un_caissier_n_y_a_pas_acces(self):
        """`reports.view` reste au propriétaire et au gérant : un rapport
        d'établissement n'est pas un geste de comptoir. Le caissier a « Ma
        journée », qui est sa contrepartie."""
        r = self._appel(self.d['cashier_a'])
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class ExportDuRapportParEntrepotTests(RapportParEntrepotTests):
    """
    Le document porte le MÊME périmètre que l'écran, et les trois formats.

    ⚠ Un papier voyage sans sa barre de filtres : un fichier ne portant qu'un
    dépôt sur deux doit être indiscernable d'un fichier complet pour personne.
    """
    EXPORT = '/api/v1/reports/statistics/export/'

    def _export(self, qui, fmt='csv'):
        self.client.force_authenticate(user=qui)
        return self.client.get(
            self.EXPORT,
            {'tab': 'by-warehouse', 'export_format': fmt, 'period': 'last_30_days'},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def test_les_trois_formats_sortent(self):
        for fmt in ('pdf', 'xlsx', 'csv'):
            with self.subTest(format=fmt):
                r = self._export(self.d['owner'], fmt)
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                self.assertGreater(len(r.content), 500)

    def test_le_document_d_un_gerant_ne_porte_QUE_ses_depots(self):
        corps = self._export(self.d['manager']).content.decode('utf-8-sig')
        self.assertIn('Main WH', corps)
        self.assertNotIn('Dépôt Bravo', corps)

    def test_le_document_du_proprietaire_porte_les_deux(self):
        corps = self._export(self.d['owner']).content.decode('utf-8-sig')
        self.assertIn('Main WH', corps)
        self.assertIn('Dépôt Bravo', corps)
        # Et l'écart EST nommé : le tableau totalise 1 700, la synthèse 1 750,
        # la différence étant la vente sans entrepôt.
        self.assertIn('TOTAL GÉNÉRAL;1 700', corps)
        self.assertIn('Hors entrepôt (dans le total seul);50', corps)

    def test_l_ecart_hors_entrepot_est_NOMME(self):
        """
        La somme des lignes est inférieure au total dès qu'une pièce n'a pas
        d'entrepôt. Le document le DIT, sinon son lecteur conclut à une erreur
        de calcul et refait l'addition à la main.
        """
        corps = self._export(self.d['owner']).content.decode('utf-8-sig')
        self.assertIn('Hors entrepôt', corps)

    def test_le_document_et_l_ecran_donnent_le_MEME_total(self):
        ecran = self._appel(self.d['manager']).data['total']['expenses_total']
        corps = self._export(self.d['manager']).content.decode('utf-8-sig')
        # Le montant formaté du document, tel que le tableau l'écrit.
        self.assertIn('300', corps)
        self.assertEqual(Decimal(ecran), Decimal('300.00'))
