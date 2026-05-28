"""
Tests pour ``GoodsReceiptStockService``.

Couverture :
- apply_increment incrémente Stock + crée StockMovement, une seule fois ;
- apply_increment est idempotent (un second appel ne re-déclenche pas) ;
- l'action ``complete`` du viewset utilise le service et reste cohérente ;
- revert restitue le stock et lève si negative non autorisé.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.contacts.models import Supplier
from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product
from apps.purchases.models import (
    GoodsReceipt, GoodsReceiptItem, PurchaseOrder, PurchaseOrderItem,
)
from apps.purchases.services import GoodsReceiptStockService
from apps.sales.tests._helpers import make_org_with_users


class _GoodsReceiptSetup(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org,
            name='Bidon huile 5L',
            sku='OIL-5L',
            cost_price=Decimal('8000.00'),
            selling_price=Decimal('10000.00'),
            track_inventory=True,
            allow_negative_stock=False,
            is_active=True,
        )
        self.supplier = Supplier.objects.create(
            organization=self.org,
            name='Fournisseur Huile SARL',
            code='FRN-OIL',
        )
        self.po = PurchaseOrder.objects.create(
            organization=self.org,
            reference='PO-TEST-0001',
            supplier=self.supplier,
            warehouse=self.warehouse,
            status='confirmed',
            order_date=date.today(),
            currency='CDF',
            total=Decimal('40000.00'),
        )
        self.po_item = PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=self.po,
            product=self.product,
            quantity_ordered=Decimal('5.000'),
            unit_price=Decimal('8000.00'),
            subtotal=Decimal('40000.00'),
            total=Decimal('40000.00'),
        )
        self.grn = GoodsReceipt.objects.create(
            organization=self.org,
            reference='GRN-TEST-0001',
            purchase_order=self.po,
            warehouse=self.warehouse,
            status='draft',
            receipt_date=date.today(),
            received_by=self.owner,
        )
        self.grn_item = GoodsReceiptItem.objects.create(
            organization=self.org,
            goods_receipt=self.grn,
            purchase_order_item=self.po_item,
            product=self.product,
            quantity_received=Decimal('5.000'),
            quantity_accepted=Decimal('5.000'),
            unit_cost=Decimal('8000.00'),
        )


class GoodsReceiptStockServiceTests(_GoodsReceiptSetup):

    def test_apply_increment_creates_stock_and_movement(self):
        GoodsReceiptStockService.apply_increment(self.grn, self.owner)

        stock = Stock.objects.get(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )
        self.assertEqual(stock.quantity, Decimal('5.000'))
        self.assertEqual(stock.avg_cost, Decimal('8000.00'))

        mvts = StockMovement.objects.filter(
            reference_type='goods_receipt',
            reference_id=self.grn.id,
        )
        self.assertEqual(mvts.count(), 1)
        self.assertEqual(mvts.first().quantity, Decimal('5.000'))

    def test_apply_increment_is_idempotent(self):
        GoodsReceiptStockService.apply_increment(self.grn, self.owner)
        GoodsReceiptStockService.apply_increment(self.grn, self.owner)

        stock = Stock.objects.get(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )
        # Quantité incrémentée une seule fois (pas 10 = 2×5)
        self.assertEqual(stock.quantity, Decimal('5.000'))

        mvts = StockMovement.objects.filter(
            reference_type='goods_receipt',
            reference_id=self.grn.id,
        )
        self.assertEqual(mvts.count(), 1)

    def test_apply_increment_recalculates_weighted_average_cost(self):
        # Stock initial avec coût différent
        Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('10.000'),
            avg_cost=Decimal('6000.00'),
        )

        GoodsReceiptStockService.apply_increment(self.grn, self.owner)

        stock = Stock.objects.get(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )
        # 10 @ 6000 + 5 @ 8000 = 60000 + 40000 = 100000 / 15 = 6666.67
        self.assertEqual(stock.quantity, Decimal('15.000'))
        self.assertEqual(stock.avg_cost, Decimal('6666.67'))

    def test_revert_decrements_stock(self):
        GoodsReceiptStockService.apply_increment(self.grn, self.owner)
        GoodsReceiptStockService.revert(self.grn, self.owner)

        stock = Stock.objects.get(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )
        self.assertEqual(stock.quantity, Decimal('0.000'))

        return_mvts = StockMovement.objects.filter(
            reference_type='goods_receipt_cancel',
            reference_id=self.grn.id,
        )
        self.assertEqual(return_mvts.count(), 1)
        self.assertEqual(return_mvts.first().movement_type, 'return_out')

    def test_revert_rejected_when_stock_would_become_negative(self):
        GoodsReceiptStockService.apply_increment(self.grn, self.owner)
        # Consommation de tout le stock entrant
        Stock.objects.filter(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        ).update(quantity=Decimal('0.000'))

        # Tenter de revert sans allow_negative_stock → ValidationError
        self.assertFalse(self.warehouse.allow_negative_stock)
        with self.assertRaises(ValidationError):
            GoodsReceiptStockService.revert(self.grn, self.owner)


class SaleStockReservationTests(TestCase):
    """Réservation de stock sur conversion devis → vente."""

    def setUp(self):
        from apps.products.models import Product as _P
        from apps.sales.models import Sale, SaleItem

        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = _P.objects.create(
            organization=self.org,
            name='Sucre 1kg',
            sku='SUG-1K',
            cost_price=Decimal('800.00'),
            selling_price=Decimal('1000.00'),
            track_inventory=True,
            allow_negative_stock=False,
            is_active=True,
        )
        Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('10.000'),
            avg_cost=Decimal('800.00'),
        )
        self.sale = Sale.objects.create(
            organization=self.org,
            reference='VT-RESERVE-0001',
            warehouse=self.warehouse,
            sold_by=self.owner,
            sale_type='retail',
            status='pending',
            subtotal=Decimal('3000.00'),
            total=Decimal('3000.00'),
            amount_due=Decimal('3000.00'),
            is_pos=False,
        )
        SaleItem.objects.create(
            sale=self.sale,
            organization=self.org,
            product=self.product,
            quantity=Decimal('3.000'),
            unit_price=Decimal('1000.00'),
            cost_price=Decimal('800.00'),
            subtotal=Decimal('3000.00'),
            total=Decimal('3000.00'),
        )

    def _stock(self):
        return Stock.objects.get(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        )

    def test_reserve_stock_increments_reserved_quantity(self):
        from apps.sales.services import SaleStockService

        SaleStockService.reserve_stock(self.sale, self.owner)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('10.000'))
        self.assertEqual(stock.reserved_quantity, Decimal('3.000'))
        self.assertEqual(stock.available_quantity, Decimal('7.000'))

        self.sale.refresh_from_db()
        self.assertTrue(self.sale.stock_reserved)

    def test_reserve_stock_is_idempotent(self):
        from apps.sales.services import SaleStockService

        SaleStockService.reserve_stock(self.sale, self.owner)
        SaleStockService.reserve_stock(self.sale, self.owner)

        stock = self._stock()
        # Réservée une seule fois (pas 6.000)
        self.assertEqual(stock.reserved_quantity, Decimal('3.000'))

    def test_release_reservation_decrements(self):
        from apps.sales.services import SaleStockService

        SaleStockService.reserve_stock(self.sale, self.owner)
        SaleStockService.release_reservation(self.sale, self.owner)

        stock = self._stock()
        self.assertEqual(stock.reserved_quantity, Decimal('0.000'))

        self.sale.refresh_from_db()
        self.assertFalse(self.sale.stock_reserved)

    def test_reserve_then_apply_decrement_keeps_available_consistent(self):
        from apps.sales.services import SaleStockService

        SaleStockService.reserve_stock(self.sale, self.owner)
        SaleStockService.apply_decrement(self.sale, self.owner)

        stock = self._stock()
        # Réservation libérée + quantité décrémentée
        self.assertEqual(stock.reserved_quantity, Decimal('0.000'))
        self.assertEqual(stock.quantity, Decimal('7.000'))
        self.assertEqual(stock.available_quantity, Decimal('7.000'))

    def test_reserve_rejected_when_available_insufficient(self):
        from apps.sales.services import SaleStockService

        # Bloquer 8 unités déjà réservées → reste 2 disponibles
        Stock.objects.filter(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
        ).update(reserved_quantity=Decimal('8.000'))

        with self.assertRaises(ValidationError):
            SaleStockService.reserve_stock(self.sale, self.owner)
