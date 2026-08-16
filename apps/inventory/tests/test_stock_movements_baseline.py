"""
Tests de référence pour les mouvements de stock manuels (approvisionnement).

Ces tests épinglent le comportement **actuel** de
``StockMovementViewSet.perform_create`` avant l'introduction de la vente en
gros/détail : quantité, coût moyen pondéré, lots FIFO et traçabilité.

Ils constituent le filet de sécurité de l'étape « approvisionnement » : toute
régression sur les produits mono-unité doit les faire échouer.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockBatch, StockMovement
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users


class _StockMovementSetup(APITestCase):
    """Organisation + produit mono-unité suivi en stock."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org,
            name='Sac de riz 25kg',
            sku='RIZ-25',
            cost_price=Decimal('8000.00'),
            selling_price=Decimal('10000.00'),
            track_inventory=True,
            allow_negative_stock=False,
            is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    def _post_movement(self, **overrides):
        payload = {
            'product': str(self.product.id),
            'warehouse': str(self.warehouse.id),
            'movement_type': 'purchase',
            'quantity': '10.000',
            'unit_cost': '6000.00',
        }
        payload.update(overrides)
        return self.client.post(
            '/api/v1/stock-movements/',
            payload,
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def _stock(self):
        return Stock.objects.get(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )


class StockSupplyBaselineTests(_StockMovementSetup):
    """Entrée de stock : quantité, mouvement, lot."""

    def test_appro_cree_le_stock_et_le_mouvement(self):
        response = self._post_movement()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('10.000'))
        self.assertEqual(stock.reserved_quantity, Decimal('0.000'))
        self.assertIsNotNone(stock.last_movement_at)

        movement = StockMovement.objects.get(
            organization=self.org,
            product=self.product,
            movement_type='purchase',
        )
        self.assertEqual(movement.quantity, Decimal('10.000'))
        self.assertEqual(movement.quantity_before, Decimal('0.000'))
        self.assertEqual(movement.quantity_after, Decimal('10.000'))
        self.assertEqual(movement.created_by, self.owner)

    def test_appro_cree_un_lot_fifo(self):
        self._post_movement()

        batches = StockBatch.objects.filter(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )
        self.assertEqual(batches.count(), 1)
        self.assertEqual(batches.first().quantity, Decimal('10.000'))
        self.assertEqual(batches.first().cost_price, Decimal('6000.00'))

    def test_deux_appros_cumulent_la_quantite(self):
        self._post_movement(quantity='10.000')
        self._post_movement(quantity='5.000')

        self.assertEqual(self._stock().quantity, Decimal('15.000'))
        self.assertEqual(
            StockMovement.objects.filter(product=self.product).count(), 2
        )

    def test_quantity_before_et_after_chainent(self):
        self._post_movement(quantity='10.000')
        self._post_movement(quantity='5.000')

        second = StockMovement.objects.filter(
            product=self.product
        ).order_by('created_at').last()
        self.assertEqual(second.quantity_before, Decimal('10.000'))
        self.assertEqual(second.quantity_after, Decimal('15.000'))


class WeightedAverageCostBaselineTests(_StockMovementSetup):
    """Coût moyen pondéré (PMP) - la formule à ne pas casser."""

    def test_premier_appro_pose_le_cout_unitaire(self):
        self._post_movement(quantity='10.000', unit_cost='6000.00')
        self.assertEqual(self._stock().avg_cost, Decimal('6000.00'))

    def test_second_appro_pondere_le_cout(self):
        # 10 × 6000 + 5 × 8000 = 100 000 pour 15 unités → 6666,67
        self._post_movement(quantity='10.000', unit_cost='6000.00')
        self._post_movement(quantity='5.000', unit_cost='8000.00')

        self.assertEqual(self._stock().avg_cost, Decimal('6666.67'))

    def test_appro_sans_cout_ne_modifie_pas_le_pmp(self):
        self._post_movement(quantity='10.000', unit_cost='6000.00')
        self._post_movement(quantity='5.000', unit_cost='0.00')

        self.assertEqual(self._stock().avg_cost, Decimal('6000.00'))
        self.assertEqual(self._stock().quantity, Decimal('15.000'))


class StockOutgoingBaselineTests(_StockMovementSetup):
    """Sorties de stock : signe imposé et garde du stock négatif."""

    def test_type_sortant_force_la_quantite_en_negatif(self):
        self._post_movement(quantity='10.000', unit_cost='6000.00')

        response = self._post_movement(
            movement_type='damage', quantity='3.000', unit_cost='0.00'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        movement = StockMovement.objects.get(movement_type='damage')
        self.assertEqual(movement.quantity, Decimal('-3.000'))
        self.assertEqual(self._stock().quantity, Decimal('7.000'))

    def test_sortie_superieure_au_stock_refusee(self):
        """
        Le filet de sécurité de ``Stock.save()`` empêche le stock négatif.

        Défaut connu épinglé ici : ``Stock.save()`` lève une
        ``django.core.exceptions.ValidationError`` que DRF ne mappe pas en 400,
        d'où un **500**. Ce qui compte pour la non-régression, c'est que la
        transaction soit annulée et le stock intact - pas le code HTTP, qui est
        un travers préexistant.
        """
        self._post_movement(quantity='2.000', unit_cost='6000.00')

        response = self._post_movement(
            movement_type='damage', quantity='5.000', unit_cost='0.00'
        )
        self.assertEqual(
            response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        self.assertEqual(self._stock().quantity, Decimal('2.000'))
        self.assertFalse(
            StockMovement.objects.filter(movement_type='damage').exists()
        )
