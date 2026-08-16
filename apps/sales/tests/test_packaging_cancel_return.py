"""
Annulation et retour d'une vente portant sur un produit vendu en gros.

Règle centrale : les unités rendues reviennent **en vrac**. Un client qui rend
2 bouteilles ne reconstitue pas un emballage scellé.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement
from apps.inventory.packaging import PackagingService
from apps.products.models import Product, Unit
from apps.sales.models import RegisterSession, SaleReturn
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users


class _CancelReturnSetup(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)

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
        RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.owner, opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.owner)

        # 2 paquets scellés en stock
        self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.product.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'package_quantity': '2', 'unit_cost': '400.00',
            },
            format='json', **self._headers,
        )

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _sell(self, packages=0, loose=0):
        item = {'product': str(self.product.id), 'unit_price': '600.00'}
        if packages:
            item['package_quantity'] = str(packages)
            item['package_unit_price'] = '6000.00'
        if loose:
            item['loose_quantity'] = str(loose)
        total = packages * 6000 + loose * 600

        response = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [item],
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': str(total),
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def _stock(self):
        return Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse
        )


class CancelPackagedSaleTests(_CancelReturnSetup):

    def test_annulation_dune_vente_en_gros_recredite_en_vrac(self):
        """
        Le paquet vendu revient en bouteilles : le carton est parti chez le
        client, on ne peut pas le déclarer scellé en rayon.
        """
        sale = self._sell(packages=1)
        self.assertEqual(self._stock().quantity, Decimal('12.000'))

        response = self.client.post(
            f'/api/v1/sales/{sale["id"]}/cancel/',
            {'reason': 'Erreur de saisie'}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('24.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))

        sealed, loose = PackagingService.split(
            stock.quantity, stock.loose_quantity, 12
        )
        self.assertEqual((sealed, loose), (1, Decimal('12.000')))

    def test_annulation_apres_deconditionnement(self):
        sale = self._sell(loose=2)
        stock = self._stock()
        self.assertEqual(stock.loose_quantity, Decimal('10.000'))

        self.client.post(
            f'/api/v1/sales/{sale["id"]}/cancel/',
            {'reason': 'Erreur'}, format='json', **self._headers,
        )

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('24.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))

    def test_le_deconditionnement_n_est_pas_annule(self):
        """Un emballage ouvert le reste : on ne re-scelle jamais."""
        sale = self._sell(loose=2)
        self.client.post(
            f'/api/v1/sales/{sale["id"]}/cancel/',
            {'reason': 'Erreur'}, format='json', **self._headers,
        )

        self.assertEqual(
            StockMovement.objects.filter(movement_type='unpack').count(), 1
        )

    def test_annulation_idempotente(self):
        sale = self._sell(packages=1)
        self.client.post(
            f'/api/v1/sales/{sale["id"]}/cancel/',
            {'reason': 'Erreur'}, format='json', **self._headers,
        )
        self.client.post(
            f'/api/v1/sales/{sale["id"]}/cancel/',
            {'reason': 'Encore'}, format='json', **self._headers,
        )

        self.assertEqual(self._stock().quantity, Decimal('24.000'))


class ReturnPackagedSaleTests(_CancelReturnSetup):

    def _create_and_approve_return(self, sale, quantity):
        item_id = sale['items'][0]['id']
        response = self.client.post(
            '/api/v1/sale-returns/',
            {
                'original_sale': sale['id'],
                'return_type': 'partial',
                'reason': 'Client insatisfait',
                'items': [{
                    'original_item': item_id,
                    'quantity': str(quantity),
                    'unit_price': '600.00',
                    'total': '0.00',
                    'restock': True,
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        sale_return = SaleReturn.objects.get(original_sale_id=sale['id'])
        response = self.client.post(
            f'/api/v1/sale-returns/{sale_return.id}/approve/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return sale_return

    def test_retour_partiel_dune_ligne_en_gros_revient_en_vrac(self):
        sale = self._sell(packages=1)
        self._create_and_approve_return(sale, quantity=2)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('14.000'))
        self.assertEqual(stock.loose_quantity, Decimal('2.000'))

        sealed, loose = PackagingService.split(
            stock.quantity, stock.loose_quantity, 12
        )
        self.assertEqual(sealed, 1)
        self.assertEqual(loose, Decimal('2.000'))

    def test_retour_trace_la_part_en_vrac(self):
        sale = self._sell(packages=1)
        sale_return = self._create_and_approve_return(sale, quantity=2)

        movement = StockMovement.objects.get(
            reference_type='sale_return', reference_id=sale_return.id
        )
        self.assertEqual(movement.quantity, Decimal('2.000'))
        self.assertEqual(movement.input_loose_quantity, Decimal('2.000'))
        self.assertEqual(movement.packaging_factor, 12)

    def test_retour_dun_paquet_complet_ne_rescelle_pas(self):
        sale = self._sell(packages=1)
        self._create_and_approve_return(sale, quantity=12)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('24.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))

        sealed, _ = PackagingService.split(stock.quantity, stock.loose_quantity, 12)
        self.assertEqual(sealed, 1, "le paquet rendu ne redevient pas scellé")
