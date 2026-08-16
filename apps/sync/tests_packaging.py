"""
Garde-fous de synchronisation pour les produits vendus en gros.

L'application mobile calcule ses ventes et son stock hors ligne en supposant une
unité unique. Tant qu'elle ne sait pas traiter le conditionnement, le serveur
refuse ses écritures sur ces produits et protège leurs prix.
"""
from decimal import Decimal

from django.test import TestCase

from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users
from apps.sync.services import SyncPushService


class _SyncPackagingSetup(TestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.pack = Unit.objects.create(
            organization=self.org, name='paquet', symbol='pqt'
        )
        self.packaged = Product.objects.create(
            organization=self.org,
            name='Eau 50cl', slug='eau-50cl', sku='EAU-50',
            unit=self.bottle, packaging_unit=self.pack,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            cost_price=Decimal('400.00'), selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            track_inventory=True, is_active=True,
        )
        self.simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            wholesale_price=Decimal('700.00'),
            track_inventory=True, is_active=True,
        )
        self.service = SyncPushService(self.org, self.owner)

    def _push(self, changes):
        """Applique un lot de changements poussé par le mobile."""
        return self.service.apply_changes(changes, None)


class WholesalePriceProtectionTests(_SyncPackagingSetup):
    """
    Le mobile envoie toujours `wholesale_price`, à `null` quand il l'ignore.

    Ce champ étant nullable, l'écriture l'effaçait : ce qui, sur un produit
    vendu en gros, supprime le prix du conditionnement.
    """

    def _push_product(self, product, **fields):
        payload = {
            'id': str(product.id),
            'name': product.name,
            'sku': product.sku,
            'selling_price': str(product.selling_price),
            **fields,
        }
        self._push({
            'products': {'created': [], 'updated': [payload], 'deleted': []}
        })
        product.refresh_from_db()
        return product

    def test_le_mobile_ne_peut_pas_effacer_le_prix_du_conditionnement(self):
        self._push_product(self.packaged, wholesale_price=None)

        self.assertEqual(self.packaged.wholesale_price, Decimal('6000.00'))

    def test_le_mobile_ne_peut_pas_changer_le_conditionnement(self):
        self._push_product(
            self.packaged, units_per_package=99, selling_mode='retail_only'
        )

        self.assertEqual(self.packaged.units_per_package, 12)
        self.assertEqual(self.packaged.selling_mode, 'wholesale_and_retail')

    def test_le_mobile_modifie_toujours_les_produits_a_l_unite(self):
        """Non-régression : rien ne change pour les produits mono-unité."""
        self._push_product(self.simple, wholesale_price='950.00')

        self.assertEqual(self.simple.wholesale_price, Decimal('950.00'))

    def test_le_mobile_modifie_les_autres_champs_dun_produit_en_gros(self):
        self._push_product(self.packaged, name='Eau 50cl (promo)')

        self.assertEqual(self.packaged.name, 'Eau 50cl (promo)')


class PackagedProductPushRejectedTests(_SyncPackagingSetup):

    def test_ligne_de_vente_sur_produit_en_gros_rejetee(self):
        result = self._push({
            'sale_items': {
                'created': [{
                    'id': '11111111-1111-4111-8111-111111111111',
                    'product_id': str(self.packaged.id),
                    'quantity': '2.000',
                    'unit_price': '600.00',
                }],
                'updated': [], 'deleted': [],
            }
        })

        self.assertTrue(result['errors'])
        self.assertIn('conditionnement', str(result['errors']))

    def test_mouvement_de_stock_sur_produit_en_gros_rejete(self):
        from apps.inventory.models import StockMovement

        self._push({
            'stock_movements': {
                'created': [{
                    'id': '22222222-2222-4222-8222-222222222222',
                    'product_id': str(self.packaged.id),
                    'warehouse_id': str(self.warehouse.id),
                    'movement_type': 'sale',
                    'quantity': '-2.000',
                    'quantity_before': '10.000',
                    'quantity_after': '8.000',
                }],
                'updated': [], 'deleted': [],
            }
        })

        self.assertFalse(
            StockMovement.objects.filter(product=self.packaged).exists()
        )

    def test_produit_a_l_unite_toujours_accepte(self):
        from apps.inventory.models import StockMovement

        self._push({
            'stock_movements': {
                'created': [{
                    'id': '33333333-3333-4333-8333-333333333333',
                    'product_id': str(self.simple.id),
                    'warehouse_id': str(self.warehouse.id),
                    'movement_type': 'purchase',
                    'quantity': '10.000',
                    'quantity_before': '0.000',
                    'quantity_after': '10.000',
                }],
                'updated': [], 'deleted': [],
            }
        })

        self.assertTrue(
            StockMovement.objects.filter(product=self.simple).exists()
        )


class PullExposesPackagingTests(_SyncPackagingSetup):

    def test_le_pull_expose_le_mode_de_vente(self):
        from apps.sync.serializers import ProductSyncSerializer

        data = ProductSyncSerializer(self.packaged).data
        self.assertEqual(data['selling_mode'], 'wholesale_and_retail')
        self.assertEqual(data['units_per_package'], 12)
