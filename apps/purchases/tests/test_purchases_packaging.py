"""
Commande et réception fournisseur d'un produit vendu en gros et au détail.

Le marchand commande au carton et le magasinier décharge au carton : les deux
saisissent dans l'unité du fournisseur, le stock reste en unité de détail.
"""
from datetime import date
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Supplier
from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product, Unit
from apps.purchases.models import GoodsReceipt, PurchaseOrder
from apps.purchases.services import GoodsReceiptStockService
from apps.sales.tests._helpers import make_org_with_users


class _PurchasePackagingSetup(APITestCase):
    """Eau 50cl : carton de 12, achetée 4 800 le carton."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.carton = Unit.objects.create(
            organization=self.org, name='carton', symbol='crt'
        )
        self.product = Product.objects.create(
            organization=self.org,
            name='Eau 50cl', slug='eau-50cl', sku='EAU-50',
            unit=self.bottle, packaging_unit=self.carton,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            cost_price=Decimal('400.00'),
            selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            package_cost_price=Decimal('4800.00'),
            track_inventory=True, is_active=True,
        )
        self.supplier = Supplier.objects.create(
            organization=self.org, name='Bralima', code='FRN-BRA',
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _create_po(self, **item):
        payload = {
            'supplier': str(self.supplier.id),
            'warehouse': str(self.warehouse.id),
            'order_date': date.today().isoformat(),
            'currency': 'CDF',
            'items': [{'product': str(self.product.id), **item}],
        }
        return self.client.post(
            '/api/v1/purchase-orders/', payload, format='json', **self._headers
        )

    def _last_po(self):
        return PurchaseOrder.objects.filter(organization=self.org).latest('created_at')

    def _stock(self):
        return Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse
        )


class PurchaseOrderPackagingTests(_PurchasePackagingSetup):

    def test_commande_en_cartons_convertie_en_unites_de_detail(self):
        response = self._create_po(package_quantity='10', package_unit_price='4800.00')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        item = self._last_po().items.first()
        self.assertEqual(item.quantity_ordered, Decimal('120.000'))
        self.assertEqual(item.unit_price, Decimal('400.00'))
        self.assertEqual(item.package_quantity, Decimal('10.000'))
        self.assertEqual(item.packaging_factor, 12)

    def test_montant_calcule_sur_le_prix_du_carton(self):
        """
        4 900 / 12 ne tombe pas juste : le montant doit rester celui que le
        fournisseur facturera, pas la somme d'un quotient arrondi.
        """
        self._create_po(package_quantity='10', package_unit_price='4900.00')

        item = self._last_po().items.first()
        self.assertEqual(item.subtotal, Decimal('49000.00'))

    def test_commande_a_l_unite_toujours_acceptee(self):
        response = self._create_po(quantity_ordered='30.000', unit_price='420.00')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        item = self._last_po().items.first()
        self.assertEqual(item.quantity_ordered, Decimal('30.000'))
        self.assertEqual(item.subtotal, Decimal('12600.00'))


class GoodsReceiptPackagingTests(_PurchasePackagingSetup):

    def setUp(self):
        super().setUp()
        self._create_po(package_quantity='10', package_unit_price='4800.00')
        self.po = self._last_po()
        self.po_item = self.po.items.first()

    def _receive(self, **item):
        payload = {
            'purchase_order': str(self.po.id),
            'warehouse': str(self.warehouse.id),
            'receipt_date': date.today().isoformat(),
            'items': [{
                'purchase_order_item': str(self.po_item.id),
                'product': str(self.product.id),
                **item,
            }],
        }
        return self.client.post(
            '/api/v1/goods-receipts/', payload, format='json', **self._headers
        )

    def _last_grn(self):
        return GoodsReceipt.objects.filter(organization=self.org).latest('created_at')

    def _complete(self, grn):
        return self.client.post(
            f'/api/v1/goods-receipts/{grn.id}/complete/',
            {}, format='json', **self._headers,
        )

    def test_reception_en_cartons_convertie_en_unites_de_detail(self):
        response = self._receive(package_quantity='10', package_unit_cost='4800.00')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        item = self._last_grn().items.first()
        self.assertEqual(item.quantity_received, Decimal('120.000'))
        self.assertEqual(item.quantity_accepted, Decimal('120.000'))
        self.assertEqual(item.unit_cost, Decimal('400.00'))

    def test_les_cartons_entrent_scelles(self):
        self._receive(package_quantity='10', package_unit_cost='4800.00')
        self._complete(self._last_grn())

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('120.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))

    def test_reception_mixte_alimente_le_vrac(self):
        self._receive(
            package_quantity='10', loose_quantity='3', package_unit_cost='4800.00'
        )
        self._complete(self._last_grn())

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('123.000'))
        self.assertEqual(stock.loose_quantity, Decimal('3.000'))

    def test_refus_qualite_replafonne_la_part_scellee(self):
        """10 cartons reçus, 12 bouteilles refusées : 9 cartons entrent scellés."""
        self._receive(
            package_quantity='10',
            package_unit_cost='4800.00',
            quantity_rejected='12.000',
            quantity_accepted='108.000',
        )
        self._complete(self._last_grn())

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('108.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))

    def test_mouvement_conserve_la_saisie(self):
        self._receive(
            package_quantity='10', loose_quantity='3', package_unit_cost='4800.00'
        )
        self._complete(self._last_grn())

        movement = StockMovement.objects.get(movement_type='purchase')
        self.assertEqual(movement.quantity, Decimal('123.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('10.000'))
        self.assertEqual(movement.input_loose_quantity, Decimal('3.000'))
        self.assertEqual(movement.packaging_factor, 12)

    def test_annulation_retire_ce_qui_avait_ete_ajoute(self):
        """
        `revert` n'est pas exposé par l'API (seul un brouillon s'annule) : on
        appelle le service, comme le fait ``test_goods_receipt_stock``.
        """
        self._receive(
            package_quantity='10', loose_quantity='3', package_unit_cost='4800.00'
        )
        grn = self._last_grn()
        self._complete(grn)

        GoodsReceiptStockService.revert(grn, self.owner)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('0.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))

    def test_reception_a_l_unite_toujours_acceptee(self):
        """Non-régression : l'ancien format de réception reste valide."""
        response = self._receive(
            quantity_received='30.000',
            quantity_accepted='30.000',
            unit_cost='400.00',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self._complete(self._last_grn())

        self.assertEqual(self._stock().quantity, Decimal('30.000'))
