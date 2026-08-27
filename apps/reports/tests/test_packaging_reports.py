"""
Ventilation du chiffre d'affaires et de la marge entre gros et détail.

La marge diffère selon la forme de vente : c'est une information commerciale de
premier plan pour le marchand. Le point à ne pas rater est la réconciliation :
la somme des deux colonnes doit égaler le chiffre d'affaires de la période.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.products.models import Product, Unit
from apps.sales.models import Sale, SaleItem
from apps.sales.tests._helpers import make_org_with_users


class _PackagingReportSetup(APITestCase):
    """Fixtures communes : Eau 50cl, paquet de 12, vendue en gros et au détail."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.pack = Unit.objects.create(
            organization=self.org, name='paquet', symbol='pqt'
        )
        self.product = Product.objects.create(
            organization=self.org,
            name='Eau 50cl', slug='eau-50cl', sku='EAU-50',
            unit=self.bottle, packaging_unit=self.pack,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            cost_price=Decimal('400.00'), selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            is_taxable=False, track_inventory=True, is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _make_sale(self, reference, *, packages=0, loose=0, discount=Decimal('0.00')):
        factor = 12
        quantity = Decimal(packages * factor + loose)
        subtotal = Decimal(packages * 6000 + loose * 600)

        sale = Sale.objects.create(
            organization=self.org, reference=reference,
            warehouse=self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=subtotal, discount_amount=discount,
            total=subtotal - discount, amount_paid=subtotal - discount,
            sold_by=self.owner, sale_date=timezone.now(),
        )
        SaleItem.objects.create(
            organization=self.org, sale=sale, product=self.product,
            quantity=quantity, unit_price=Decimal('600.00'),
            cost_price=Decimal('400.00'),
            package_quantity=Decimal(packages),
            package_unit_price=Decimal('6000.00') if packages else None,
            packaging_factor=factor if packages else None,
        )
        return sale


class PackagingReportTests(_PackagingReportSetup):

    def _report(self):
        response = self.client.get(
            '/api/v1/reports/statistics/sales-by-packaging/', **self._headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = {r['sale_form']: r for r in response.data['results']}
        return response.data, rows

    def test_vente_en_gros_pure(self):
        self._make_sale('VTE-G-1', packages=2)
        data, rows = self._report()

        self.assertEqual(Decimal(rows['wholesale']['revenue']), Decimal('12000.00'))
        self.assertEqual(Decimal(rows['retail']['revenue']), Decimal('0.00'))
        # 24 unités à 400 de coût = 9 600
        self.assertEqual(Decimal(rows['wholesale']['cost']), Decimal('9600.00'))
        self.assertEqual(Decimal(rows['wholesale']['gross_profit']), Decimal('2400.00'))

    def test_vente_au_detail_pure(self):
        self._make_sale('VTE-D-1', loose=10)
        data, rows = self._report()

        self.assertEqual(Decimal(rows['retail']['revenue']), Decimal('6000.00'))
        self.assertEqual(Decimal(rows['retail']['cost']), Decimal('4000.00'))
        self.assertEqual(Decimal(rows['wholesale']['revenue']), Decimal('0.00'))

    def test_la_marge_differe_entre_gros_et_detail(self):
        """Le fait commercial que ce rapport doit rendre visible."""
        self._make_sale('VTE-G-1', packages=1)   # 12 unités à 6 000
        self._make_sale('VTE-D-1', loose=12)     # 12 unités à 7 200

        _, rows = self._report()
        self.assertLess(
            Decimal(rows['wholesale']['margin_percentage']),
            Decimal(rows['retail']['margin_percentage']),
        )

    def test_ligne_mixte_repartie_au_prorata(self):
        # 1 paquet (12 unités, 6 000) + 3 bouteilles (1 800) = 7 800 pour 15 unités
        self._make_sale('VTE-M-1', packages=1, loose=3)
        _, rows = self._report()

        # 12/15 du CA au gros, 3/15 au détail
        self.assertEqual(Decimal(rows['wholesale']['revenue']), Decimal('6240.00'))
        self.assertEqual(Decimal(rows['retail']['revenue']), Decimal('1560.00'))
        self.assertEqual(Decimal(rows['wholesale']['quantity']), Decimal('12.000'))
        self.assertEqual(Decimal(rows['retail']['quantity']), Decimal('3.000'))

    def test_la_ventilation_se_recolle_au_total(self):
        self._make_sale('VTE-1', packages=2)
        self._make_sale('VTE-2', loose=7)
        self._make_sale('VTE-3', packages=1, loose=3)

        data, rows = self._report()
        somme = Decimal(rows['wholesale']['revenue']) + Decimal(rows['retail']['revenue'])
        self.assertEqual(somme, Decimal(data['total_revenue']))

    def test_remise_globale_correctement_allouee(self):
        """Une remise sur la vente ne doit pas disparaître de la ventilation."""
        self._make_sale('VTE-R-1', packages=1, loose=3, discount=Decimal('800.00'))

        data, rows = self._report()
        somme = Decimal(rows['wholesale']['revenue']) + Decimal(rows['retail']['revenue'])
        self.assertEqual(somme, Decimal('7000.00'))
        self.assertEqual(Decimal(data['total_revenue']), Decimal('7000.00'))

    def test_produit_mono_unite_compte_au_detail(self):
        simple = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            is_taxable=False, track_inventory=True, is_active=True,
        )
        sale = Sale.objects.create(
            organization=self.org, reference='VTE-S-1',
            warehouse=self.warehouse, status='completed', currency='CDF',
            subtotal=Decimal('2400.00'), total=Decimal('2400.00'),
            sale_date=timezone.now(),
        )
        SaleItem.objects.create(
            organization=self.org, sale=sale, product=simple,
            quantity=Decimal('3.000'), unit_price=Decimal('800.00'),
            cost_price=Decimal('400.00'),
        )

        _, rows = self._report()
        self.assertEqual(Decimal(rows['retail']['revenue']), Decimal('2400.00'))
        self.assertEqual(Decimal(rows['wholesale']['revenue']), Decimal('0.00'))

class QuantitesLisiblesTests(_PackagingReportSetup):
    """
    Les rapports rendent les quantités dans les termes de l'opération.

    « 245 » ne dit pas au gérant si ses clients lui ont pris vingt casiers ou
    cinq casiers et un carton de bouteilles à l'unité : ces deux ventes ne se
    réapprovisionnent pas de la même façon, et leur marge n'est pas la même.
    """

    def _stock(self, packages, loose):
        from apps.inventory.models import Stock

        return Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal(packages * 12 + loose),
            package_quantity=Decimal(packages),
            loose_quantity=Decimal(loose),
            avg_cost=Decimal('400.00'),
        )

    def test_top_products_ventile_la_quantite_vendue(self):
        # 5 casiers facturés au paquet, plus 12 bouteilles à la pièce : le
        # total (72) se lirait « 6 casiers » si on le redécoupait au facteur.
        self._make_sale('VTE-G-1', packages=5)
        self._make_sale('VTE-D-1', loose=12)

        response = self.client.get(
            '/api/v1/reports/statistics/top_products/', **self._headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = response.data['results'][0]

        self.assertEqual(row['quantity_display'], '5 paquets + 12 bouteilles')
        self.assertEqual(Decimal(row['packages_sold']), Decimal('5.000'))
        self.assertEqual(Decimal(row['loose_sold']), Decimal('12.000'))
        self.assertEqual(row['packaging_factor'], 12)

    def test_top_products_d_un_produit_simple_reste_en_unites(self):
        simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle,
            cost_price=Decimal('500.00'), selling_price=Decimal('800.00'),
            is_taxable=False, track_inventory=True, is_active=True,
        )
        sale = Sale.objects.create(
            organization=self.org, reference='VTE-S-1',
            warehouse=self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=Decimal('7200.00'), total=Decimal('7200.00'),
            amount_paid=Decimal('7200.00'),
            sold_by=self.owner, sale_date=timezone.now(),
        )
        SaleItem.objects.create(
            organization=self.org, sale=sale, product=simple,
            quantity=Decimal('9.000'), unit_price=Decimal('800.00'),
            cost_price=Decimal('500.00'),
        )

        response = self.client.get(
            '/api/v1/reports/statistics/top_products/', **self._headers
        )
        row = next(
            r for r in response.data['results'] if r['product_sku'] == 'SAV-01'
        )
        self.assertEqual(row['quantity_display'], '9 bouteilles')
        self.assertIsNone(row['packaging_factor'])

    def test_stock_details_lit_les_deux_compteurs(self):
        """
        3 paquets + 27 bouteilles reste tel quel.

        Reconstitué depuis le total (63), il deviendrait « 5 paquets +
        3 bouteilles » : un rayon qui n'a jamais existé.
        """
        self._stock(3, 27)

        response = self.client.get(
            '/api/v1/reports/statistics/stock_details/', **self._headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = response.data['results'][0]

        self.assertEqual(row['stock_display'], '3 paquets + 27 bouteilles')
        self.assertEqual(row['available_display'], '3 paquets + 27 bouteilles')
        self.assertEqual(row['packaging_factor'], 12)
        self.assertEqual(Decimal(row['stock_packages']), Decimal('3.000'))

    def test_stock_details_reserve_ne_devient_pas_des_paquets(self):
        stock = self._stock(3, 7)
        stock.reserved_quantity = Decimal('12.000')
        stock.save(update_fields=['reserved_quantity'])

        response = self.client.get(
            '/api/v1/reports/statistics/stock_details/', **self._headers
        )
        row = response.data['results'][0]

        self.assertEqual(row['reserved_display'], '12 bouteilles')
        self.assertEqual(row['available_display'], '2 paquets + 7 bouteilles')

    def test_product_supplies_rend_la_saisie_de_reception(self):
        from apps.inventory.models import StockMovement

        StockMovement.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.PURCHASE,
            quantity=Decimal('125.000'),
            quantity_before=Decimal('0.000'), quantity_after=Decimal('125.000'),
            input_package_quantity=Decimal('10.000'),
            input_loose_quantity=Decimal('5.000'),
            packaging_factor=12,
        )

        response = self.client.get(
            '/api/v1/reports/statistics/product_supplies/', **self._headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        entry = response.data[str(self.product.id)]

        self.assertEqual(entry['display'], '10 paquets + 5 bouteilles')
        self.assertEqual(entry['quantity'], 125.0)
        self.assertEqual(entry['packages'], 10.0)
        self.assertEqual(entry['loose'], 5.0)

    def test_product_profits_ventile_aussi_la_quantite(self):
        self._make_sale('VTE-G-1', packages=5)
        self._make_sale('VTE-D-1', loose=12)

        response = self.client.get(
            '/api/v1/reports/statistics/product_profits/', **self._headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = response.data['results'][0]

        self.assertEqual(row['quantity_display'], '5 paquets + 12 bouteilles')
        self.assertEqual(row['packaging_factor'], 12)
