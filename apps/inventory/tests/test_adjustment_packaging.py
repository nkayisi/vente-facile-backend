"""
Ajustement de stock d'un produit vendu en gros et au détail.

Le marchand compte ce qu'il voit dans son dépôt : « 3 paquets + 2 bouteilles ».
Le comptage physique fait autorité sur la part hors emballage.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockAdjustment, StockMovement
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _AdjustmentSetup(APITestCase):
    """Eau 50cl : paquet de 12."""

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
            cost_price=Decimal('400.00'),
            selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            track_inventory=True, is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _supply(self, packages=0, loose=0):
        return self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.product.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'package_quantity': str(packages),
                'loose_quantity': str(loose),
                'unit_cost': '400.00',
            },
            format='json', **self._headers,
        )

    def _create_adjustment(self, **item):
        payload = {
            'warehouse': str(self.warehouse.id),
            'adjustment_type': 'count',
            'reason': 'Comptage mensuel',
            'items': [{
                'product': str(self.product.id),
                'quantity_expected': '24.000',
                'unit_cost': '400.00',
                **item,
            }],
        }
        return self.client.post(
            '/api/v1/stock-adjustments/', payload, format='json', **self._headers
        )

    def _last_adjustment(self):
        return StockAdjustment.objects.filter(organization=self.org).latest('created_at')

    def _stock(self):
        return Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse
        )


class AdjustmentCreationTests(_AdjustmentSetup):

    def test_comptage_en_contenants_recompose_le_total(self):
        response = self._create_adjustment(
            counted_package_quantity='3', counted_loose_quantity='2'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        item = self._last_adjustment().items.first()
        self.assertEqual(item.quantity_counted, Decimal('38.000'))
        self.assertEqual(item.counted_loose_quantity, Decimal('2.000'))
        self.assertEqual(item.packaging_factor, 12)
        self.assertEqual(item.quantity_difference, Decimal('14.000'))

    def test_cout_saisi_au_paquet_converti_a_l_unite(self):
        self._create_adjustment(
            counted_package_quantity='3', package_unit_cost='7200.00'
        )

        item = self._last_adjustment().items.first()
        self.assertEqual(item.unit_cost, Decimal('600.00'))

    def test_comptage_obligatoire(self):
        response = self._create_adjustment()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_produit_simple_refuse_la_saisie_en_contenants(self):
        simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle,
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        response = self.client.post(
            '/api/v1/stock-adjustments/',
            {
                'warehouse': str(self.warehouse.id),
                'adjustment_type': 'count',
                'items': [{
                    'product': str(simple.id),
                    'quantity_expected': '10.000',
                    'counted_package_quantity': '2',
                    'unit_cost': '400.00',
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_comptage_simple_toujours_accepte(self):
        """Non-régression : l'ancien format de comptage reste valide."""
        response = self._create_adjustment(quantity_counted='30.000')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self._last_adjustment().items.first().quantity_counted, Decimal('30.000')
        )


class AdjustmentApprovalTests(_AdjustmentSetup):

    def test_approbation_applique_le_partage_compte(self):
        """2 paquets attendus, 3 paquets + 2 bouteilles comptés."""
        self._supply(packages=2)
        self._create_adjustment(
            counted_package_quantity='3', counted_loose_quantity='2'
        )
        adjustment = self._last_adjustment()

        response = self.client.post(
            f'/api/v1/stock-adjustments/{adjustment.id}/approve/',
            {}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('38.000'))
        self.assertEqual(stock.loose_quantity, Decimal('2.000'))

    def test_ecart_relu_en_contenants_dans_l_historique(self):
        self._supply(packages=2)
        self._create_adjustment(
            counted_package_quantity='3', counted_loose_quantity='2'
        )
        adjustment = self._last_adjustment()
        self.client.post(
            f'/api/v1/stock-adjustments/{adjustment.id}/approve/',
            {}, format='json', **self._headers,
        )

        movement = StockMovement.objects.get(movement_type='adjustment_in')
        self.assertEqual(movement.quantity, Decimal('14.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('1.000'))
        self.assertEqual(movement.input_loose_quantity, Decimal('2.000'))
        self.assertEqual(movement.packaging_factor, 12)

    def test_manquant_constate_reduit_le_stock(self):
        self._supply(packages=3)
        self._create_adjustment(
            counted_package_quantity='2', counted_loose_quantity='0'
        )
        adjustment = self._last_adjustment()
        self.client.post(
            f'/api/v1/stock-adjustments/{adjustment.id}/approve/',
            {}, format='json', **self._headers,
        )

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('24.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))
        self.assertEqual(
            StockMovement.objects.filter(movement_type='adjustment_out').count(), 1
        )

    def test_affichage_du_comptage_dans_le_detail(self):
        self._supply(packages=2)
        self._create_adjustment(
            counted_package_quantity='3', counted_loose_quantity='2'
        )
        adjustment = self._last_adjustment()

        response = self.client.get(
            f'/api/v1/stock-adjustments/{adjustment.id}/', **self._headers
        )
        item = response.data['items'][0]
        self.assertEqual(item['counted_display'], '3 paquets + 2 bouteilles')
        self.assertEqual(item['expected_display'], '2 paquets')
