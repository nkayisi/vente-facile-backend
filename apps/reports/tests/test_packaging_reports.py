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


class PackagingReportTests(APITestCase):

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
