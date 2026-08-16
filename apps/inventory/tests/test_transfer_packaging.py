"""
Transfert d'un produit vendu en gros et au détail.

Un transfert se prépare en contenants et se charge tel quel : ce qui part
scellé arrive scellé, et l'entrepôt source ne peut pas expédier un contenant
qu'il n'a plus.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement, StockTransfer, Warehouse
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _TransferSetup(APITestCase):
    """Eau 50cl : paquet de 12, transférée du dépôt principal vers l'annexe."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.destination = Warehouse.objects.create(
            organization=self.org, branch=self.branch,
            name='Annexe', code='ANNEX',
        )

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
        """Approvisionne l'entrepôt source par l'API, comme le ferait l'écran."""
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

    def _create_transfer(self, **item):
        payload = {
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.destination.id),
            'items': [{'product': str(self.product.id), **item}],
        }
        return self.client.post(
            '/api/v1/stock-transfers/', payload, format='json', **self._headers
        )

    def _last_transfer(self):
        """
        Le serializer de création ne renvoie pas l'identifiant : on le relit en
        base plutôt que d'élargir le contrat de l'API pour les tests.
        """
        return StockTransfer.objects.filter(organization=self.org).latest('created_at')

    def _stock(self, warehouse):
        return Stock.objects.get(
            organization=self.org, product=self.product, warehouse=warehouse
        )


class TransferCreationTests(_TransferSetup):

    def test_demande_en_contenants_convertie_en_unites_de_base(self):
        response = self._create_transfer(package_quantity='2', loose_quantity='3')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        item = response.data['items'][0]
        self.assertEqual(Decimal(item['quantity_requested']), Decimal('27.000'))
        self.assertEqual(item['packaging_factor'], 12)
        self.assertEqual(item['requested_display'], '2 paquets + 3 bouteilles')

    def test_produit_simple_refuse_la_saisie_en_contenants(self):
        simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle,
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        response = self.client.post(
            '/api/v1/stock-transfers/',
            {
                'source_warehouse': str(self.warehouse.id),
                'destination_warehouse': str(self.destination.id),
                'items': [{'product': str(simple.id), 'package_quantity': '2'}],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quantite_simple_toujours_acceptee(self):
        """Non-régression : l'ancien format de demande reste valide."""
        response = self._create_transfer(quantity_requested='10.000')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            Decimal(response.data['items'][0]['quantity_requested']), Decimal('10.000')
        )


class TransferShipTests(_TransferSetup):

    def test_expedition_de_paquets_ne_touche_pas_au_vrac(self):
        self._supply(packages=5)
        self._create_transfer(package_quantity='2')
        transfer = self._last_transfer()

        response = self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/ship/",
            {}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        source = self._stock(self.warehouse)
        self.assertEqual(source.quantity, Decimal('36.000'))
        self.assertEqual(source.loose_quantity, Decimal('0.000'))

    def test_expedition_au_detail_ouvre_un_paquet(self):
        """Il ne reste que des paquets scellés : en sortir 3 bouteilles en ouvre un."""
        self._supply(packages=2)
        self._create_transfer(loose_quantity='3')
        transfer = self._last_transfer()

        self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/ship/",
            {}, format='json', **self._headers,
        )

        source = self._stock(self.warehouse)
        self.assertEqual(source.quantity, Decimal('21.000'))
        # Un paquet ouvert (12), moins les 3 bouteilles parties.
        self.assertEqual(source.loose_quantity, Decimal('9.000'))
        self.assertTrue(
            StockMovement.objects.filter(
                movement_type='unpack', reference_type='stock_transfer'
            ).exists()
        )

    def test_expedition_refusee_quand_les_paquets_scelles_manquent(self):
        """2 paquets en stock dont un déjà ouvert : on ne peut pas en expédier 2."""
        self._supply(packages=2)
        stock = self._stock(self.warehouse)
        self.client.post(
            f'/api/v1/stocks/{stock.id}/unpack/',
            {'packages': 1}, format='json', **self._headers,
        )
        self._create_transfer(package_quantity='2')
        transfer = self._last_transfer()

        response = self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/ship/",
            {}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, Decimal('24.000'))

    def test_mouvement_de_sortie_conserve_la_saisie(self):
        self._supply(packages=5)
        self._create_transfer(package_quantity='2', loose_quantity='1')
        transfer = self._last_transfer()

        self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/ship/",
            {}, format='json', **self._headers,
        )

        movement = StockMovement.objects.get(movement_type='transfer_out')
        self.assertEqual(movement.quantity, Decimal('-25.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('2.000'))
        self.assertEqual(movement.input_loose_quantity, Decimal('1.000'))
        self.assertEqual(movement.packaging_factor, 12)


class TransferReceiveTests(_TransferSetup):

    def _shipped_transfer(self, **item):
        self._supply(packages=5)
        self._create_transfer(**item)
        transfer = self._last_transfer()
        self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/ship/",
            {}, format='json', **self._headers,
        )
        return transfer

    def test_reception_ajoute_des_paquets_scelles(self):
        transfer = self._shipped_transfer(package_quantity='2')

        response = self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/receive/",
            {}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        destination = self._stock(self.destination)
        self.assertEqual(destination.quantity, Decimal('24.000'))
        self.assertEqual(destination.loose_quantity, Decimal('0.000'))

    def test_reception_comptee_en_contenants(self):
        """Le magasinier ne décharge qu'un paquet sur les deux annoncés."""
        transfer = self._shipped_transfer(package_quantity='2')
        item_id = str(transfer.items.first().id)

        self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/receive/",
            {'items': [{'id': item_id, 'package_quantity': 1, 'loose_quantity': 0}]},
            format='json', **self._headers,
        )

        destination = self._stock(self.destination)
        self.assertEqual(destination.quantity, Decimal('12.000'))
        self.assertEqual(destination.loose_quantity, Decimal('0.000'))

    def test_reception_partielle_au_detail_alimente_le_vrac(self):
        transfer = self._shipped_transfer(package_quantity='1', loose_quantity='5')
        item_id = str(transfer.items.first().id)

        self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/receive/",
            {'items': [{'id': item_id, 'package_quantity': 1, 'loose_quantity': 5}]},
            format='json', **self._headers,
        )

        destination = self._stock(self.destination)
        self.assertEqual(destination.quantity, Decimal('17.000'))
        self.assertEqual(destination.loose_quantity, Decimal('5.000'))


class TransferCancelTests(_TransferSetup):

    def test_annulation_restitue_les_paquets_scelles(self):
        self._supply(packages=5)
        self._create_transfer(package_quantity='2')
        transfer = self._last_transfer()
        self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/ship/",
            {}, format='json', **self._headers,
        )

        response = self.client.post(
            f"/api/v1/stock-transfers/{transfer.id}/cancel/",
            {}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        source = self._stock(self.warehouse)
        self.assertEqual(source.quantity, Decimal('60.000'))
        self.assertEqual(source.loose_quantity, Decimal('0.000'))
