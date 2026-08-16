"""
Tests de référence pour le cycle de session d'inventaire physique.

Ces tests épinglent le comportement **actuel** de ``InventorySessionViewSet``
(``start`` → ``count`` → ``submit`` → ``validate``) avant l'introduction du
comptage « X paquets + Y pièces ».

Point le plus structurant à ne pas casser : ``validate`` écrit
``stock.quantity = count.quantity_counted`` en **valeur absolue**, pas en delta.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import (
    InventoryCount, InventorySession, Stock, StockMovement,
)
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users


class _InventorySessionSetup(APITestCase):
    """Organisation + un produit disposant de 10 unités en stock."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org,
            name='Savon de Marseille',
            sku='SAV-01',
            cost_price=Decimal('500.00'),
            selling_price=Decimal('800.00'),
            track_inventory=True,
            is_active=True,
        )
        self.stock = Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('10.000'),
            avg_cost=Decimal('500.00'),
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _create_session(self):
        response = self.client.post(
            '/api/v1/inventory-sessions/',
            {'warehouse': str(self.warehouse.id), 'scope_type': 'full'},
            format='json',
            **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # `InventorySessionCreateSerializer` n'expose pas `id` dans ses champs :
        # la réponse de création ne le contient pas. On relit la session créée.
        return str(
            InventorySession.objects.filter(
                organization=self.org, warehouse=self.warehouse
            ).latest('created_at').id
        )

    def _run_until_review(self, counted_quantity):
        """Crée une session, compte `counted_quantity`, et la soumet."""
        session_id = self._create_session()

        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/start/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        count = InventoryCount.objects.get(session_id=session_id, product=self.product)
        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/count/',
            {'counts': [{'id': str(count.id), 'quantity_counted': counted_quantity}]},
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/submit/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return session_id

    def _validate(self, session_id):
        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/validate/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.stock.refresh_from_db()
        return response


class InventorySessionCycleBaselineTests(_InventorySessionSetup):

    def test_start_prend_un_snapshot_du_stock(self):
        session_id = self._create_session()
        self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/start/',
            format='json', **self._headers,
        )

        count = InventoryCount.objects.get(session_id=session_id, product=self.product)
        self.assertEqual(count.quantity_expected, Decimal('10.000'))
        self.assertEqual(count.unit_cost, Decimal('500.00'))
        self.assertFalse(count.is_counted)

    def test_count_calcule_l_ecart(self):
        session_id = self._run_until_review(counted_quantity=8)

        count = InventoryCount.objects.get(session_id=session_id, product=self.product)
        self.assertTrue(count.is_counted)
        self.assertEqual(count.quantity_counted, Decimal('8.000'))
        self.assertEqual(count.quantity_difference, Decimal('-2.000'))
        self.assertEqual(count.difference_value, Decimal('-1000.00'))
        self.assertEqual(count.counted_by, self.owner)

    def test_validate_ecrase_la_quantite_en_absolu(self):
        session_id = self._run_until_review(counted_quantity=8)
        self._validate(session_id)

        self.assertEqual(self.stock.quantity, Decimal('8.000'))
        self.assertIsNotNone(self.stock.last_counted_at)

    def test_validate_cree_un_mouvement_d_ajustement(self):
        session_id = self._run_until_review(counted_quantity=8)
        self._validate(session_id)

        movement = StockMovement.objects.get(
            reference_type='inventory_session',
            reference_id=session_id,
        )
        self.assertEqual(movement.movement_type, 'adjustment_out')
        self.assertEqual(movement.quantity, Decimal('-2.000'))
        self.assertEqual(movement.quantity_before, Decimal('10.000'))
        self.assertEqual(movement.quantity_after, Decimal('8.000'))

    def test_ecart_positif_produit_un_ajustement_entrant(self):
        session_id = self._run_until_review(counted_quantity=14)
        self._validate(session_id)

        self.assertEqual(self.stock.quantity, Decimal('14.000'))
        movement = StockMovement.objects.get(reference_type='inventory_session')
        self.assertEqual(movement.movement_type, 'adjustment_in')
        self.assertEqual(movement.quantity, Decimal('4.000'))

    def test_ecart_nul_ne_cree_aucun_mouvement(self):
        session_id = self._run_until_review(counted_quantity=10)
        self._validate(session_id)

        self.assertEqual(self.stock.quantity, Decimal('10.000'))
        self.assertFalse(
            StockMovement.objects.filter(reference_type='inventory_session').exists()
        )

    def test_validate_deverrouille_le_stock(self):
        session_id = self._run_until_review(counted_quantity=8)
        session = InventorySession.objects.get(id=session_id)
        self.assertTrue(session.is_stock_locked)

        self._validate(session_id)
        session.refresh_from_db()
        self.assertEqual(session.status, 'validated')
        self.assertFalse(session.is_stock_locked)
        self.assertEqual(session.validated_by, self.owner)
