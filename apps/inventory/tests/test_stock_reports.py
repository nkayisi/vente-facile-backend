"""
Rapports et exports de la gestion de stock.

Couvre ce qui, sans filet, se casse en silence : une catégorie filtrée sans sa
descendance, une borne de date décalée par le fuseau, une valorisation qui
diverge de l'écran, un total d'approvisionnement gonflé par des lignes sans coût.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.filters import day_bounds
from apps.inventory.models import Stock, StockMovement
from apps.inventory.reports import build_stock_levels_report, build_supplies_report
from apps.organizations.models import OrganizationMembership
from apps.products.models import Category, Product, Unit
from apps.purchases.models import GoodsReceipt, PurchaseOrder
from apps.contacts.models import Supplier
from apps.sales.tests._helpers import make_org_with_users


class _StockReportSetup(APITestCase):
    """Deux catégories, dont une avec sous-catégorie, et deux entrepôts."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.unit = Unit.objects.create(
            organization=self.org, name='pièce', symbol='pc',
        )
        self.boissons = Category.objects.create(
            organization=self.org, name='Boissons', slug='boissons',
        )
        self.sodas = Category.objects.create(
            organization=self.org, name='Sodas', slug='sodas', parent=self.boissons,
        )
        self.hygiene = Category.objects.create(
            organization=self.org, name='Hygiène', slug='hygiene',
        )

        self.eau = self._product('Eau 50cl', 'EAU-50', self.boissons, 400, 600)
        self.soda = self._product('Soda', 'SOD-01', self.sodas, 500, 900)
        self.savon = self._product('Savon', 'SAV-01', self.hygiene, 300, 700)

        self.client.force_authenticate(user=self.owner)

    def _product(self, name, sku, category, cost, price, reorder=5):
        return Product.objects.create(
            organization=self.org, name=name, slug=sku.lower(), sku=sku,
            unit=self.unit, category=category,
            cost_price=Decimal(cost), selling_price=Decimal(price),
            reorder_point=reorder, track_inventory=True, is_active=True,
        )

    def _stock(self, product, quantity, avg_cost=None, warehouse=None):
        return Stock.objects.create(
            organization=self.org, product=product,
            warehouse=warehouse or self.warehouse,
            quantity=Decimal(quantity),
            avg_cost=Decimal(avg_cost) if avg_cost is not None else Decimal('0.00'),
        )

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}


class CategorySubtreeTests(_StockReportSetup):
    """Filtrer « Boissons » doit ramener les sodas, sinon le rapport ment."""

    def test_subtree_ids_inclut_les_descendants(self):
        ids = Category.subtree_ids([self.boissons.id])
        self.assertIn(self.boissons.id, ids)
        self.assertIn(self.sodas.id, ids)
        self.assertNotIn(self.hygiene.id, ids)

    def test_filtre_categorie_ramene_la_sous_categorie(self):
        self._stock(self.eau, 10)
        self._stock(self.soda, 5)
        self._stock(self.savon, 3)

        resp = self.client.get(
            f'/api/v1/stocks/?category={self.boissons.id}', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        results = resp.data['results'] if 'results' in resp.data else resp.data
        skus = {row['product_sku'] for row in results}
        self.assertEqual(skus, {'EAU-50', 'SOD-01'})

    def test_categorie_feuille_ne_ramene_quelle_meme(self):
        self._stock(self.eau, 10)
        self._stock(self.soda, 5)

        resp = self.client.get(
            f'/api/v1/stocks/?category={self.sodas.id}', **self._headers,
        )
        results = resp.data['results'] if 'results' in resp.data else resp.data
        self.assertEqual({row['product_sku'] for row in results}, {'SOD-01'})


class DateBoundaryTests(_StockReportSetup):
    """Bornes inclusives et calées sur Africa/Kinshasa, pas sur UTC."""

    def _movement(self, when, quantity=1):
        movement = StockMovement.objects.create(
            organization=self.org, product=self.eau, warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.PURCHASE,
            quantity=Decimal(quantity), unit_cost=Decimal('400.00'),
            quantity_before=Decimal('0'), quantity_after=Decimal(quantity),
        )
        # `created_at` est auto_now_add : il faut le réécrire après coup.
        StockMovement.objects.filter(pk=movement.pk).update(created_at=when)
        return movement

    def test_bornes_du_jour_couvrent_minuit_a_minuit(self):
        today = timezone.localdate()
        start, end = day_bounds(today)
        self.assertEqual(timezone.localtime(start).hour, 0)
        self.assertEqual(timezone.localtime(end).hour, 23)
        self.assertEqual(timezone.localtime(start).date(), today)
        self.assertEqual(timezone.localtime(end).date(), today)

    def test_mouvement_de_23h30_reste_dans_sa_journee_locale(self):
        """
        Le piège du fuseau : à 23h30 à Kinshasa (UTC+1), l'horodatage UTC est
        déjà le lendemain. Comparé à une date nue, le mouvement disparaîtrait
        du rapport du jour où il a réellement eu lieu.
        """
        day = timezone.localdate()
        late = timezone.make_aware(
            datetime.combine(day, time(23, 30)), timezone.get_current_timezone(),
        )
        self._movement(late)

        resp = self.client.get(
            f'/api/v1/stock-movements/?date_from={day}&date_to={day}',
            **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['count'], 1)

    def test_date_to_est_inclusive(self):
        day = timezone.localdate() - timedelta(days=3)
        self._movement(timezone.make_aware(
            datetime.combine(day, time(14, 0)), timezone.get_current_timezone(),
        ))

        resp = self.client.get(
            f'/api/v1/stock-movements/?date_from={day}&date_to={day}',
            **self._headers,
        )
        self.assertEqual(resp.data['count'], 1, "« au 3 » doit comprendre le 3")

    def test_filtre_mois_borne_le_mois_entier(self):
        day = timezone.localdate().replace(day=1)
        self._movement(timezone.make_aware(
            datetime.combine(day, time(9, 0)), timezone.get_current_timezone(),
        ))
        veille = timezone.make_aware(
            datetime.combine(day - timedelta(days=1), time(9, 0)),
            timezone.get_current_timezone(),
        )
        self._movement(veille)

        resp = self.client.get(
            f'/api/v1/stock-movements/?month={day.strftime("%Y-%m")}',
            **self._headers,
        )
        self.assertEqual(resp.data['count'], 1, "le mois précédent est exclu")

    def test_mois_malforme_ne_leve_pas(self):
        resp = self.client.get(
            '/api/v1/stock-movements/?month=pas-un-mois', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 0)


class ValuationTests(_StockReportSetup):
    """Le coût moyen pondéré fait foi ; le catalogue n'est qu'un repli."""

    def test_avg_cost_prime_sur_le_prix_catalogue(self):
        stock = self._stock(self.eau, 10, avg_cost=450)
        self.assertEqual(stock.effective_cost, Decimal('450.00'))
        self.assertEqual(stock.stock_value, Decimal('4500.00'))

    def test_repli_sur_le_prix_catalogue_si_jamais_approvisionne(self):
        stock = self._stock(self.eau, 10, avg_cost=0)
        self.assertEqual(stock.effective_cost, Decimal('400.00'))

    def test_rapport_valorise_au_cout_et_a_la_vente(self):
        self._stock(self.eau, 10, avg_cost=450)
        self._stock(self.savon, 4, avg_cost=300)

        spec = build_stock_levels_report(
            Stock.objects.filter(organization=self.org), self.org, currency='CDF',
        )
        rows = list(spec.rows)
        self.assertEqual(len(rows), 2)

        eau = next(r for r in rows if r['sku'] == 'EAU-50')
        self.assertEqual(eau['stock_value'], Decimal('4500.00'))
        self.assertEqual(eau['sale_value'], Decimal('6000.00'))

        summary = dict(spec.summary)
        self.assertEqual(summary['Valeur au coût'], '5 700')
        self.assertEqual(summary['Valeur de vente'], '8 800')

    def test_statut_distingue_rupture_et_stock_bas(self):
        self._stock(self.eau, 0)
        self._stock(self.soda, 3)      # reorder_point = 5
        self._stock(self.savon, 100)

        spec = build_stock_levels_report(
            Stock.objects.filter(organization=self.org), self.org, currency='CDF',
        )
        statuses = {row['sku']: row['status'] for row in spec.rows}
        self.assertEqual(statuses['EAU-50'], 'Rupture')
        self.assertEqual(statuses['SOD-01'], 'Stock bas')
        self.assertEqual(statuses['SAV-01'], 'En stock')


class SuppliesReportTests(_StockReportSetup):
    """Valeur d'achat, fournisseur, et lignes sans coût."""

    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(
            organization=self.org, name='Bralima', code='SUP-1',
        )

    def _receipt(self, quantity, unit_cost, product=None):
        order = PurchaseOrder.objects.create(
            organization=self.org, reference=f'PO-{timezone.now().timestamp()}',
            supplier=self.supplier, warehouse=self.warehouse, currency='CDF',
            order_date=timezone.localdate(),
        )
        receipt = GoodsReceipt.objects.create(
            organization=self.org, reference=f'GRN-{timezone.now().timestamp()}',
            purchase_order=order, warehouse=self.warehouse,
            receipt_date=timezone.now(),
        )
        StockMovement.objects.create(
            organization=self.org, product=product or self.eau,
            warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.PURCHASE,
            quantity=Decimal(quantity), unit_cost=Decimal(unit_cost),
            quantity_before=Decimal('0'), quantity_after=Decimal(quantity),
            reference_type='goods_receipt', reference_id=receipt.id,
        )
        return receipt

    def _receipt_in_currency(self, quantity, unit_cost, code):
        order = PurchaseOrder.objects.create(
            organization=self.org, reference=f'PO-{code}-{timezone.now().timestamp()}',
            supplier=self.supplier, warehouse=self.warehouse, currency=code,
            order_date=timezone.localdate(),
        )
        receipt = GoodsReceipt.objects.create(
            organization=self.org, reference=f'GRN-{code}-{timezone.now().timestamp()}',
            purchase_order=order, warehouse=self.warehouse,
            receipt_date=timezone.now(),
        )
        StockMovement.objects.create(
            organization=self.org, product=self.eau, warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.PURCHASE,
            quantity=Decimal(quantity), unit_cost=Decimal(unit_cost),
            quantity_before=Decimal('0'), quantity_after=Decimal(quantity),
            reference_type='goods_receipt', reference_id=receipt.id,
        )
        return receipt

    def test_devises_multiples_ventilees_et_signalees(self):
        """
        `unit_cost` est stocké sans conversion : un achat en dollars laisse un
        coût en dollars. Le rapport ventile par devise au lieu d'additionner, et
        le document porte un avertissement pour que le total du tableau, lui
        aussi mélangé, ne trompe personne.
        """
        self._receipt_in_currency(10, 400, 'CDF')
        self._receipt_in_currency(2, 15, 'USD')

        spec = build_supplies_report(
            StockMovement.objects.filter(organization=self.org), self.org,
            currency='CDF', group_by='product',
        )
        summary = dict(spec.summary)

        self.assertNotIn("Valeur d'achat totale", summary)
        self.assertEqual(summary["Valeur d'achat (CDF)"], '4 000')
        self.assertEqual(summary["Valeur d'achat (USD)"], '30,00')
        self.assertIn('plusieurs devises', spec.subtitle)

    def test_devise_unique_garde_un_total_simple(self):
        self._receipt_in_currency(10, 400, 'CDF')

        spec = build_supplies_report(
            StockMovement.objects.filter(organization=self.org), self.org,
            currency='CDF', group_by='product',
        )
        summary = dict(spec.summary)
        self.assertEqual(summary["Valeur d'achat totale"], '4 000')
        self.assertNotIn('plusieurs devises', spec.subtitle)

    def _adjustment(self, quantity):
        """Entrée sans coût saisi, comme un ajustement positif."""
        return StockMovement.objects.create(
            organization=self.org, product=self.eau, warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
            quantity=Decimal(quantity), unit_cost=Decimal('0'),
            quantity_before=Decimal('0'), quantity_after=Decimal(quantity),
        )

    def test_valeur_achat_par_produit(self):
        self._receipt(10, 400)
        self._receipt(5, 500)

        spec = build_supplies_report(
            StockMovement.objects.filter(organization=self.org), self.org,
            currency='CDF', group_by='product',
        )
        rows = list(spec.rows)
        self.assertEqual(len(rows), 1)
        # 10 x 400 + 5 x 500 = 6 500
        self.assertEqual(rows[0]['purchase_value'], Decimal('6500'))
        self.assertEqual(rows[0]['quantity'], Decimal('15'))
        self.assertEqual(rows[0]['entries'], 2)

    def test_ligne_sans_cout_exclue_du_total(self):
        """
        Compter une entrée sans coût comme zéro sous-estimerait la valeur
        d'achat sans que rien ne le signale. Elle est écartée et dénombrée.
        """
        self._receipt(10, 400)
        self._adjustment(50)

        spec = build_supplies_report(
            StockMovement.objects.filter(organization=self.org), self.org,
            currency='CDF', group_by='product',
        )
        summary = dict(spec.summary)
        self.assertEqual(summary["Valeur d'achat totale"], '4 000')
        self.assertEqual(summary['Entrées sans coût saisi'], '1')

        row = list(spec.rows)[0]
        # La quantité totale intègre l'ajustement, la valeur non.
        self.assertEqual(row['quantity'], Decimal('60'))
        self.assertEqual(row['purchase_value'], Decimal('4000'))
        # Le coût moyen ne porte que sur les quantités valorisées.
        self.assertEqual(row['avg_unit_cost'], Decimal('400'))

    def test_detail_chronologique_porte_le_fournisseur(self):
        self._receipt(10, 400)

        spec = build_supplies_report(
            StockMovement.objects.filter(organization=self.org), self.org,
            currency='CDF', group_by='movement',
        )
        row = list(spec.rows)[0]
        self.assertEqual(row['supplier'], 'Bralima')
        self.assertEqual(row['purchase_value'], Decimal('4000'))

    def _count_report_queries(self):
        movements = StockMovement.objects.filter(organization=self.org)
        with CaptureQueriesContext(connection) as captured:
            spec = build_supplies_report(
                movements, self.org, currency='CDF', group_by='movement',
            )
            list(spec.rows)
        return len(captured)

    def test_fournisseurs_resolus_sans_requete_par_ligne(self):
        """
        `reference_id` est un UUID nu et non une clé étrangère : résolu ligne à
        ligne, un rapport de 500 réceptions déclencherait 500 requêtes. On vérifie
        l'invariant qui compte, à savoir que le nombre de requêtes ne dépend PAS
        du volume, plutôt qu'un total absolu que le moindre `select_related`
        rendrait caduc.
        """
        for _ in range(2):
            self._receipt(1, 100)
        with_two = self._count_report_queries()

        for _ in range(10):
            self._receipt(1, 100)
        with_twelve = self._count_report_queries()

        self.assertEqual(
            with_two, with_twelve,
            f"{with_two} requêtes pour 2 réceptions, {with_twelve} pour 12 : "
            "le compte croît avec le volume",
        )

    def test_source_receipts_ecarte_les_autres_entrees(self):
        self._receipt(10, 400)
        self._adjustment(50)

        resp = self.client.get(
            '/api/v1/stock-movements/supplies-export/'
            '?export_format=xlsx&source=receipts',
            **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertIn('spreadsheetml', resp['Content-Type'])


class ExportEndpointTests(_StockReportSetup):
    """Contrat HTTP des trois endpoints d'export."""

    def test_export_stock_pdf(self):
        self._stock(self.eau, 10, avg_cost=450)
        resp = self.client.get(
            '/api/v1/stocks/export/?export_format=pdf', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertIn('niveau_de_stock', resp['Content-Disposition'])
        self.assertTrue(resp.content.startswith(b'%PDF'))

    def test_export_stock_xlsx(self):
        self._stock(self.eau, 10, avg_cost=450)
        resp = self.client.get(
            '/api/v1/stocks/export/?export_format=xlsx', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        # Un .xlsx est une archive ZIP.
        self.assertTrue(resp.content.startswith(b'PK'))

    def test_alias_excel_accepte(self):
        resp = self.client.get(
            '/api/v1/stocks/export/?export_format=excel', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('spreadsheetml', resp['Content-Type'])

    def test_format_invalide_repond_400(self):
        resp = self.client.get(
            '/api/v1/stocks/export/?export_format=docx', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_export_mouvements_et_approvisionnement(self):
        for path, basename in [
            ('/api/v1/stock-movements/export/', 'mouvements_de_stock'),
            ('/api/v1/stock-movements/supplies-export/', 'approvisionnement'),
        ]:
            resp = self.client.get(f'{path}?export_format=pdf', **self._headers)
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
            self.assertIn(basename, resp['Content-Disposition'])

    def test_export_vide_reste_un_fichier_valide(self):
        """Un périmètre sans ligne doit produire un document, pas une erreur."""
        resp = self.client.get(
            '/api/v1/stocks/export/?export_format=pdf'
            f'&category={self.hygiene.id}',
            **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.content.startswith(b'%PDF'))

    def test_magasinier_n_exporte_que_ses_entrepots(self):
        """Le scoping d'entrepôt s'applique à l'export comme à la liste."""
        from apps.inventory.models import Warehouse

        other = Warehouse.objects.create(
            organization=self.org, name='Dépôt Nord', code='NORD',
        )
        self._stock(self.eau, 10, avg_cost=450)
        self._stock(self.savon, 7, avg_cost=300, warehouse=other)

        keeper = self.cashier_a
        membership = OrganizationMembership.objects.get(
            user=keeper, organization=self.org,
        )
        membership.role = OrganizationMembership.Role.STOCK_KEEPER
        membership.save(update_fields=['role'])
        membership.assigned_warehouses.set([self.warehouse])

        self.client.force_authenticate(user=keeper)
        spec_resp = self.client.get(
            '/api/v1/stocks/?warehouse=' + str(other.id), **self._headers,
        )
        results = (
            spec_resp.data['results'] if 'results' in spec_resp.data else spec_resp.data
        )
        self.assertEqual(len(results), 0, "l'entrepôt non assigné doit rester invisible")

        resp = self.client.get(
            '/api/v1/stocks/export/?export_format=pdf', **self._headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
