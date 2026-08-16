"""
Tests de référence pour la restitution du stock (annulation et retour client).

Ces tests épinglent le comportement numérique **actuel** de deux chemins qui
recréditent le stock :

- ``SaleStockService.revert`` (annulation de vente) ;
- ``SaleReturnViewSet.approve`` (retour client), qui recrédite **en ligne dans
  la vue** et duplique le recalcul de coût moyen pondéré du service.

Ils doivent rester verts après l'extraction du recrédit de retour vers
``SaleStockService`` : c'est leur raison d'être.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem
from apps.sales.services import SaleStockService
from apps.sales.tests._helpers import make_org_with_users


class _StockRestitutionSetup(APITestCase):
    """
    Une vente de 4 unités déjà décrémentée du stock.

    État de départ : 10 unités à 500 de coût moyen.
    Après décrément : 6 unités, coût moyen inchangé à 500.
    Le coût de la ligne vendue est fixé à 600 pour que le recalcul du coût
    moyen à la restitution produise des valeurs vérifiables à la main.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org,
            name='Bidon huile 5L',
            sku='OIL-5L',
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

        self.sale = Sale.objects.create(
            organization=self.org,
            reference='VTE-BASELINE-0001',
            warehouse=self.warehouse,
            register=self.register,
            status='completed',
            currency='CDF',
            subtotal=Decimal('3200.00'),
            total=Decimal('3200.00'),
            amount_paid=Decimal('3200.00'),
            sold_by=self.owner,
            sale_date=timezone.now(),
        )
        self.item = SaleItem.objects.create(
            organization=self.org,
            sale=self.sale,
            product=self.product,
            quantity=Decimal('4.000'),
            unit_price=Decimal('800.00'),
            cost_price=Decimal('600.00'),
        )

        SaleStockService.apply_decrement(self.sale, self.owner)
        self.stock.refresh_from_db()

        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}


class SaleDecrementBaselineTests(_StockRestitutionSetup):
    """Point de départ : le décrément lui-même ne touche pas au coût moyen."""

    def test_decrement_retire_la_quantite_vendue(self):
        self.assertEqual(self.stock.quantity, Decimal('6.000'))

    def test_decrement_ne_modifie_pas_le_cout_moyen(self):
        self.assertEqual(self.stock.avg_cost, Decimal('500.00'))

    def test_decrement_trace_un_mouvement_de_vente(self):
        movement = StockMovement.objects.get(
            reference_type='sale', reference_id=self.sale.id
        )
        self.assertEqual(movement.movement_type, 'sale')
        self.assertEqual(movement.quantity, Decimal('-4.000'))
        self.assertEqual(movement.quantity_before, Decimal('10.000'))
        self.assertEqual(movement.quantity_after, Decimal('6.000'))


class SaleCancelBaselineTests(_StockRestitutionSetup):
    """Annulation de vente → ``SaleStockService.revert``."""

    def _cancel(self):
        response = self.client.post(
            f'/api/v1/sales/{self.sale.id}/cancel/',
            {'reason': 'Test'},
            format='json',
            **self._headers,
        )
        self.stock.refresh_from_db()
        return response

    def test_annulation_recredite_la_quantite(self):
        response = self._cancel()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.stock.quantity, Decimal('10.000'))

    def test_annulation_repondere_le_cout_moyen(self):
        # (6 × 500 + 4 × 600) / 10 = 5400 / 10 = 540,00
        self._cancel()
        self.assertEqual(self.stock.avg_cost, Decimal('540.00'))

    def test_annulation_trace_un_mouvement_de_retour(self):
        self._cancel()
        movement = StockMovement.objects.get(
            reference_type='sale_cancel', reference_id=self.sale.id
        )
        self.assertEqual(movement.movement_type, 'return_in')
        self.assertEqual(movement.quantity, Decimal('4.000'))
        self.assertEqual(movement.quantity_before, Decimal('6.000'))
        self.assertEqual(movement.quantity_after, Decimal('10.000'))

    def test_annulation_passe_la_vente_en_cancelled(self):
        self._cancel()
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, 'cancelled')


class SaleReturnBaselineTests(_StockRestitutionSetup):
    """
    Retour client → recrédit inline dans ``SaleReturnViewSet.approve``.

    C'est ce comportement numérique que l'extraction vers le service devra
    reproduire à l'identique.
    """

    def _create_and_approve_return(self, quantity=2, restock=True):
        response = self.client.post(
            '/api/v1/sale-returns/',
            {
                'original_sale': str(self.sale.id),
                'return_type': 'partial',
                'reason': 'Produit abîmé',
                # `unit_price` et `total` sont requis par le serializer alors
                # que `create()` les recalcule depuis la ligne d'origine : on
                # envoie des valeurs volontairement fausses pour épingler
                # qu'elles sont bien ignorées.
                'items': [{
                    'original_item': str(self.item.id),
                    'quantity': str(quantity),
                    'unit_price': '1.00',
                    'total': '1.00',
                    'restock': restock,
                }],
            },
            format='json',
            **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        from apps.sales.models import SaleReturn
        sale_return = SaleReturn.objects.get(original_sale=self.sale)

        response = self.client.post(
            f'/api/v1/sale-returns/{sale_return.id}/approve/',
            format='json',
            **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.stock.refresh_from_db()
        return sale_return

    def test_retour_recredite_la_quantite(self):
        self._create_and_approve_return(quantity=2)
        self.assertEqual(self.stock.quantity, Decimal('8.000'))

    def test_retour_repondere_le_cout_moyen(self):
        # (6 × 500 + 2 × 600) / 8 = 4200 / 8 = 525,00
        self._create_and_approve_return(quantity=2)
        self.assertEqual(self.stock.avg_cost, Decimal('525.00'))

    def test_retour_trace_un_mouvement_de_retour(self):
        sale_return = self._create_and_approve_return(quantity=2)
        movement = StockMovement.objects.get(
            reference_type='sale_return', reference_id=sale_return.id
        )
        self.assertEqual(movement.movement_type, 'return_in')
        self.assertEqual(movement.quantity, Decimal('2.000'))
        self.assertEqual(movement.quantity_before, Decimal('6.000'))
        self.assertEqual(movement.quantity_after, Decimal('8.000'))
        self.assertEqual(movement.unit_cost, Decimal('600.00'))

    def test_retour_sans_restock_ne_touche_pas_au_stock(self):
        self._create_and_approve_return(quantity=2, restock=False)
        self.assertEqual(self.stock.quantity, Decimal('6.000'))
        self.assertEqual(self.stock.avg_cost, Decimal('500.00'))
        self.assertFalse(
            StockMovement.objects.filter(reference_type='sale_return').exists()
        )

    def test_retour_passe_en_completed(self):
        sale_return = self._create_and_approve_return(quantity=2)
        sale_return.refresh_from_db()
        self.assertEqual(sale_return.status, 'completed')
        self.assertEqual(sale_return.approved_by, self.owner)

    def test_montants_recalcules_depuis_la_ligne_d_origine(self):
        """Les `unit_price` / `total` envoyés par le client sont ignorés."""
        sale_return = self._create_and_approve_return(quantity=2)

        returned_item = sale_return.items.get()
        self.assertEqual(returned_item.unit_price, Decimal('800.00'))
        self.assertEqual(returned_item.total, Decimal('1600.00'))
        self.assertEqual(sale_return.total_amount, Decimal('1600.00'))
        self.assertEqual(sale_return.refund_amount, Decimal('1600.00'))
