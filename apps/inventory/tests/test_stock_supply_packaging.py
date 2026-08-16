"""
Approvisionnement d'un produit vendu en gros et au détail.

Le vendeur saisit une entrée en conditionnements, en unités, ou dans les deux à
la fois ; le stock est toujours stocké en unité de base.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _SupplySetup(APITestCase):
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
        self.simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle,
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _post(self, **payload):
        body = {
            'product': str(self.product.id),
            'warehouse': str(self.warehouse.id),
            'movement_type': 'purchase',
        }
        body.update(payload)
        return self.client.post(
            '/api/v1/stock-movements/', body, format='json', **self._headers
        )

    def _stock(self, product=None):
        return Stock.objects.get(
            organization=self.org,
            product=product or self.product,
            warehouse=self.warehouse,
        )


class SupplyInPackagesTests(_SupplySetup):

    def test_appro_en_paquets_convertit_en_unites_de_base(self):
        response = self._post(package_quantity='2', unit_cost='400.00')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.assertEqual(self._stock().quantity, Decimal('24.000'))

    def test_appro_en_paquets_ne_cree_pas_de_vrac(self):
        """Les emballages arrivent scellés : 2 paquets + 0 bouteille."""
        self._post(package_quantity='2', unit_cost='400.00')

        stock = self._stock()
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))

        response = self.client.get(
            f'/api/v1/stocks/{stock.id}/', **self._headers
        )
        self.assertEqual(response.data['stock_display'], '2 paquets')
        self.assertEqual(response.data['stock_packages'], 2)

    def test_appro_en_pieces_alimente_le_vrac(self):
        self._post(loose_quantity='5', unit_cost='400.00')

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('5.000'))
        self.assertEqual(stock.loose_quantity, Decimal('5.000'))

    def test_appro_mixte(self):
        """« Vous ajoutez 10 paquets + 5 bouteilles = 125 bouteilles. »"""
        self._post(package_quantity='10', loose_quantity='5', unit_cost='400.00')

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('125.000'))
        self.assertEqual(stock.loose_quantity, Decimal('5.000'))

    def test_la_saisie_d_origine_est_conservee(self):
        self._post(package_quantity='10', loose_quantity='5', unit_cost='400.00')

        movement = StockMovement.objects.get(movement_type='purchase')
        self.assertEqual(movement.quantity, Decimal('125.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('10.000'))
        self.assertEqual(movement.input_loose_quantity, Decimal('5.000'))
        self.assertEqual(movement.packaging_factor, 12)

    def test_cout_saisi_au_paquet_converti_a_l_unite(self):
        # 7 200 le paquet de 12 → 600 la bouteille
        self._post(package_quantity='2', package_unit_cost='7200.00')

        self.assertEqual(self._stock().avg_cost, Decimal('600.00'))

    def test_saisie_en_conditionnement_refusee_sur_produit_simple(self):
        response = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.simple.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'package_quantity': '2',
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quantite_obligatoire(self):
        response = self._post(unit_cost='400.00')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class UnpackMovementRejectedTests(_SupplySetup):

    def test_mouvement_unpack_refuse_via_l_api(self):
        """Seul le service sait maintenir le partage scellé/vrac."""
        response = self._post(movement_type='unpack', quantity='0')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('movement_type', response.data)


class ManualUnpackActionTests(_SupplySetup):

    def setUp(self):
        super().setUp()
        self._post(package_quantity='2', unit_cost='400.00')
        self.stock = self._stock()

    def test_ouverture_manuelle_d_un_paquet(self):
        response = self.client.post(
            f'/api/v1/stocks/{self.stock.id}/unpack/',
            {'packages': 1}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['packages_opened'], 1)
        self.assertEqual(response.data['stock_display'], '1 paquet + 12 bouteilles')

        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal('24.000'))
        self.assertEqual(self.stock.loose_quantity, Decimal('12.000'))

    def test_ouverture_manuelle_de_plusieurs_paquets(self):
        response = self.client.post(
            f'/api/v1/stocks/{self.stock.id}/unpack/',
            {'packages': 2}, format='json', **self._headers,
        )
        self.assertEqual(response.data['packages_opened'], 2)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.loose_quantity, Decimal('24.000'))

    def test_ouverture_manuelle_possible_meme_sans_deconditionnement_auto(self):
        self.product.allow_auto_unpacking = False
        self.product.save()

        response = self.client.post(
            f'/api/v1/stocks/{self.stock.id}/unpack/',
            {'packages': 1}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['packages_opened'], 1)

    def test_ouverture_tracee(self):
        self.client.post(
            f'/api/v1/stocks/{self.stock.id}/unpack/',
            {'packages': 1}, format='json', **self._headers,
        )
        movement = StockMovement.objects.get(movement_type='unpack')
        self.assertEqual(movement.quantity, Decimal('0.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('1.000'))
        self.assertEqual(movement.reference_type, 'manual_unpack')
        self.assertEqual(movement.created_by, self.owner)

    def test_refuse_sur_produit_simple(self):
        self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.simple.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'quantity': '10',
            },
            format='json', **self._headers,
        )
        simple_stock = self._stock(product=self.simple)

        response = self.client.post(
            f'/api/v1/stocks/{simple_stock.id}/unpack/',
            {'packages': 1}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_creation_directe_de_stock_toujours_refusee(self):
        """
        Ouvrir `POST` pour l'action `unpack` ne doit pas rendre le stock
        modifiable en direct. La permission tranche avant même le 405.
        """
        response = self.client.post(
            '/api/v1/stocks/',
            {'product': str(self.product.id), 'warehouse': str(self.warehouse.id)},
            format='json', **self._headers,
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )
        self.assertEqual(Stock.objects.filter(product=self.product).count(), 1)


class SimpleProductSupplyRegressionTests(_SupplySetup):
    """Non-régression : l'approvisionnement d'un produit mono-unité est inchangé."""

    def test_appro_quantite_simple(self):
        response = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.simple.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'quantity': '10.000',
                'unit_cost': '400.00',
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        stock = self._stock(product=self.simple)
        self.assertEqual(stock.quantity, Decimal('10.000'))
        self.assertEqual(stock.avg_cost, Decimal('400.00'))

    def test_affichage_sans_paquet(self):
        self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.simple.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'quantity': '10.000',
                'unit_cost': '400.00',
            },
            format='json', **self._headers,
        )
        stock = self._stock(product=self.simple)
        response = self.client.get(f'/api/v1/stocks/{stock.id}/', **self._headers)

        self.assertEqual(response.data['stock_display'], '10 bouteilles')
        self.assertIsNone(response.data['stock_packages'])
