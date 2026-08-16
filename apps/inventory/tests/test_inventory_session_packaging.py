"""
Inventaire physique d'un produit vendu en gros et au détail.

Le comptage se saisit en « X conditionnements + Y unités » et fait autorité :
c'est lui qui constate ce qui est réellement hors emballage.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import (
    InventoryCount, InventorySession, Stock, StockMovement,
)
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _InventoryPackagingSetup(APITestCase):
    """Eau 50cl, paquet de 12 : stock de 1 paquet scellé + 10 bouteilles."""

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
            track_inventory=True, is_active=True,
        )
        self.stock = Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('22.000'), loose_quantity=Decimal('10.000'),
            avg_cost=Decimal('400.00'),
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _start_session(self):
        response = self.client.post(
            '/api/v1/inventory-sessions/',
            {'warehouse': str(self.warehouse.id), 'scope_type': 'full'},
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        session = InventorySession.objects.filter(
            organization=self.org, warehouse=self.warehouse
        ).latest('created_at')

        response = self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/start/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return session

    def _count_and_validate(self, session, packages=None, loose=None, simple=None):
        count = InventoryCount.objects.get(session=session, product=self.product)
        payload = {'id': str(count.id)}
        if simple is not None:
            payload['quantity_counted'] = simple
        else:
            payload['counted_package_quantity'] = packages
            payload['counted_loose_quantity'] = loose
            payload['quantity_counted'] = 0

        response = self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/count/',
            {'counts': [payload]}, format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/submit/',
            format='json', **self._headers,
        )
        response = self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/validate/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.stock.refresh_from_db()
        return InventoryCount.objects.get(session=session, product=self.product)


class InventoryCountPackagingTests(_InventoryPackagingSetup):

    def test_l_attendu_est_affiche_en_paquets_et_unites(self):
        session = self._start_session()
        count = InventoryCount.objects.get(session=session, product=self.product)

        self.assertEqual(count.quantity_expected, Decimal('22.000'))
        self.assertEqual(count.expected_loose_quantity, Decimal('10.000'))
        self.assertEqual(count.packaging_factor, 12)

        response = self.client.get(
            f'/api/v1/inventory-sessions/{session.id}/counts/', **self._headers
        )
        row = response.data['results'][0] if 'results' in response.data else response.data[0]
        self.assertEqual(row['expected_display'], '1 paquet + 10 bouteilles')

    def test_comptage_en_paquets_et_unites_calcule_la_quantite(self):
        session = self._start_session()
        count = self._count_and_validate(session, packages=2, loose=3)

        # 2 × 12 + 3 = 27
        self.assertEqual(count.quantity_counted, Decimal('27.000'))
        self.assertEqual(count.quantity_difference, Decimal('5.000'))

    def test_le_comptage_fait_autorite_sur_le_vrac(self):
        session = self._start_session()
        self._count_and_validate(session, packages=2, loose=3)

        self.assertEqual(self.stock.quantity, Decimal('27.000'))
        self.assertEqual(self.stock.loose_quantity, Decimal('3.000'))

    def test_comptage_sans_ecart_ne_change_rien(self):
        session = self._start_session()
        self._count_and_validate(session, packages=1, loose=10)

        self.assertEqual(self.stock.quantity, Decimal('22.000'))
        self.assertEqual(self.stock.loose_quantity, Decimal('10.000'))
        self.assertFalse(
            StockMovement.objects.filter(reference_type='inventory_session').exists()
        )

    def test_comptage_constatant_moins_de_vrac(self):
        """Le compteur trouve 2 paquets scellés et plus rien en vrac."""
        session = self._start_session()
        self._count_and_validate(session, packages=2, loose=0)

        self.assertEqual(self.stock.quantity, Decimal('24.000'))
        self.assertEqual(self.stock.loose_quantity, Decimal('0.000'))


class InventorySimpleProductRegressionTests(APITestCase):
    """Non-régression : le comptage d'un produit mono-unité est inchangé."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.product = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        self.stock = Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse, quantity=Decimal('10.000'),
            avg_cost=Decimal('400.00'),
        )
        self.client.force_authenticate(user=self.owner)

    def test_comptage_simple_toujours_accepte(self):
        headers = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        self.client.post(
            '/api/v1/inventory-sessions/',
            {'warehouse': str(self.warehouse.id), 'scope_type': 'full'},
            format='json', **headers,
        )
        session = InventorySession.objects.latest('created_at')
        self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/start/',
            format='json', **headers,
        )

        count = InventoryCount.objects.get(session=session, product=self.product)
        self.assertIsNone(count.packaging_factor)

        self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/count/',
            {'counts': [{'id': str(count.id), 'quantity_counted': 8}]},
            format='json', **headers,
        )
        self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/submit/',
            format='json', **headers,
        )
        self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/validate/',
            format='json', **headers,
        )

        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal('8.000'))
        self.assertEqual(self.stock.loose_quantity, Decimal('0.000'))
