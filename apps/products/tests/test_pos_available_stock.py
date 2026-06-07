"""
Tests : la liste produits du POS ne renvoie que le stock DISPONIBLE
(quantité − réservé) et est restreinte à l'entrepôt du caissier.

Couverture :
- in_stock=true exclut un produit entièrement réservé (quantité=10, réservé=10 → 0 dispo).
- in_stock=true inclut un produit partiellement réservé, avec stock_quantity = quantité − réservé.
- le caissier ne voit pas les produits dont le stock est dans un autre entrepôt.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, Warehouse
from apps.products.models import Product

from apps.sales.tests._helpers import make_org_with_users as _make_org_with_users


class PosAvailableStockTests(APITestCase):
    def setUp(self):
        ctx = _make_org_with_users()
        self.__dict__.update(ctx)
        # cashier_a est assigné à self.warehouse (cf. _helpers).
        self.product = Product.objects.create(
            organization=self.org,
            name='Produit Test',
            slug='produit-test',
            sku='SKU-001',
            track_inventory=True,
            selling_price=Decimal('1000'),
        )

    def _list_in_stock(self):
        """GET /products/?in_stock=true&warehouse=<entrepôt caissier> en tant que cashier_a."""
        self.client.force_authenticate(user=self.cashier_a)
        return self.client.get(
            '/api/v1/products/',
            {'in_stock': 'true', 'is_active': 'true', 'warehouse': str(self.warehouse.id)},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def test_fully_reserved_product_excluded(self):
        """quantité=10, réservé=10 → disponible 0 → absent de la liste in_stock."""
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('10.000'), reserved_quantity=Decimal('10.000'),
        )
        resp = self._list_in_stock()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        ids = [p['id'] for p in resp.data['results']]
        self.assertNotIn(str(self.product.id), ids)

    def test_partially_reserved_product_included_with_available_qty(self):
        """quantité=10, réservé=3 → présent, stock_quantity = 7 (disponible)."""
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('10.000'), reserved_quantity=Decimal('3.000'),
        )
        resp = self._list_in_stock()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        rows = {p['id']: p for p in resp.data['results']}
        self.assertIn(str(self.product.id), rows)
        self.assertEqual(Decimal(str(rows[str(self.product.id)]['stock_quantity'])), Decimal('7'))

    def test_stock_in_other_warehouse_not_visible_to_cashier(self):
        """Stock présent uniquement dans un autre entrepôt → invisible pour le caissier scopé."""
        other_wh = Warehouse.objects.create(
            organization=self.org, branch=self.branch,
            name='Autre WH', code='OTHER-WH',
        )
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=other_wh,
            quantity=Decimal('50.000'), reserved_quantity=Decimal('0.000'),
        )
        # Sans param warehouse : scope = entrepôt(s) assigné(s) au caissier (self.warehouse).
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get(
            '/api/v1/products/',
            {'in_stock': 'true', 'is_active': 'true'},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        ids = [p['id'] for p in resp.data['results']]
        self.assertNotIn(str(self.product.id), ids)
