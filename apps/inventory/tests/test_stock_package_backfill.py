"""
Reprise des stocks existants au passage aux deux compteurs.

La migration ``inventory/0016`` matérialise le nombre de conditionnements
scellés, jusqu'ici dérivé de ``(quantity - loose_quantity) // facteur``. Ce test
rejoue le backfill sur des lignes réelles : c'est le seul moment où le nombre de
paquets est reconstitué, une erreur ici fausserait tout le stock d'un marchand
sans qu'aucun écran ne le signale.
"""
import importlib
from decimal import Decimal

from django.test import TestCase

from apps.inventory.models import Stock
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


def _backfill():
    """Charge la fonction de la migration, sans la dupliquer ici."""
    module = importlib.import_module(
        'apps.inventory.migrations.0016_stock_package_quantity_alter_stock_loose_quantity'
    )
    return module.backfill_package_quantity


class _FakeApps:
    """Registre minimal : la migration ne lit qu'un seul modèle."""

    def get_model(self, app_label, model_name):
        assert (app_label, model_name) == ('inventory', 'Stock')
        return Stock


class StockPackageBackfillTests(TestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.crate = Unit.objects.create(
            organization=self.org, name='casier', symbol='cs'
        )
        self.beer = Product.objects.create(
            organization=self.org, name='Bière', slug='biere', sku='BIE-01',
            unit=self.bottle, packaging_unit=self.crate,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            track_inventory=True, is_active=True,
        )
        self.soap = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle, selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )

    def _raw_stock(self, product, quantity, loose):
        """
        Écrit une ligne dans l'état d'avant migration : total + part vrac, sans
        compteur de paquets. On contourne ``save()``, qui réconcilierait.
        """
        stock = Stock.objects.create(
            organization=self.org, product=product, warehouse=self.warehouse,
        )
        Stock.objects.filter(pk=stock.pk).update(
            quantity=Decimal(quantity),
            loose_quantity=Decimal(loose),
            package_quantity=Decimal('0.000'),
        )
        return stock

    def test_partage_reconstitue(self):
        """48 bouteilles dont 12 en vrac : 3 casiers + 12 bouteilles."""
        stock = self._raw_stock(self.beer, '48.000', '12.000')

        _backfill()(_FakeApps(), None)

        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('3.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))
        self.assertEqual(stock.quantity, Decimal('48.000'))

    def test_orphelin_reste_au_vrac(self):
        """37 bouteilles sans vrac déclaré : 3 casiers + 1 bouteille."""
        stock = self._raw_stock(self.beer, '37.000', '0.000')

        _backfill()(_FakeApps(), None)

        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('3.000'))
        self.assertEqual(stock.loose_quantity, Decimal('1.000'))

    def test_produit_au_detail_seul_reste_a_zero(self):
        stock = self._raw_stock(self.soap, '25.000', '0.000')

        _backfill()(_FakeApps(), None)

        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('0.000'))
        self.assertEqual(stock.quantity, Decimal('25.000'))

    def test_le_total_ne_bouge_jamais(self):
        """Le backfill redistribue, il ne crée ni ne détruit de marchandise."""
        cases = [('48.000', '12.000'), ('37.000', '0.000'), ('5.000', '5.000')]
        stocks = []
        for quantity, loose in cases:
            product = Product.objects.create(
                organization=self.org, name=f'Bière {quantity}',
                slug=f'biere-{quantity}', sku=f'BIE-{quantity}',
                unit=self.bottle, packaging_unit=self.crate,
                selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
                units_per_package=12, selling_price=Decimal('600.00'),
                track_inventory=True, is_active=True,
            )
            stocks.append((self._raw_stock(product, quantity, loose), Decimal(quantity)))

        _backfill()(_FakeApps(), None)

        for stock, expected in stocks:
            stock.refresh_from_db()
            self.assertEqual(stock.quantity, expected)
            self.assertEqual(
                stock.package_quantity * 12 + stock.loose_quantity, expected,
            )
