"""
Prix d'achat et de vente saisis lors d'un approvisionnement.

Produit de référence : **Eau 50cl**, carton de 12 bouteilles. Le marchand peut
acheter au carton, à la bouteille, ou les deux dans la même livraison ; il peut
aussi reporter ces prix sur la fiche produit, mais seulement s'il le demande.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _MovementPricingSetup(APITestCase):

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
            package_cost_price=Decimal('4800.00'),
            selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            track_inventory=True, is_active=True,
        )
        self.simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle,
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _post(self, **payload):
        body = {
            'product': str(self.product.id),
            'warehouse': str(self.warehouse.id),
            'movement_type': 'purchase',
        }
        body.update(payload)
        return self.client.post(
            '/api/v1/stock-movements/', body, format='json', **self._headers
        )

    def _movement(self):
        return StockMovement.objects.get(organization=self.org, product=self.product)


class BlendedCostTests(_MovementPricingSetup):
    """Le coût du mouvement, c'est ce qui a été payé sur ce qui a été reçu."""

    def test_deux_prix_saisis_donnent_un_cout_moyen_pondere(self):
        """2 cartons à 6 000 et 3 bouteilles à 550 : 13 650 pour 27 bouteilles."""
        response = self._post(
            package_quantity='2', package_unit_cost='6000.00',
            loose_quantity='3', unit_cost='550.00',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        movement = self._movement()
        self.assertEqual(movement.quantity, Decimal('27.000'))
        self.assertEqual(movement.unit_cost, Decimal('505.56'))

    def test_prix_au_conditionnement_seul_reste_la_division(self):
        """Non-régression : un seul prix saisi vaut l'ancien comportement."""
        self._post(package_quantity='2', package_unit_cost='4800.00')

        self.assertEqual(self._movement().unit_cost, Decimal('400.00'))

    def test_prix_au_detail_seul_sur_une_saisie_mixte(self):
        """Le prix du carton est complété par conversion, la moyenne le confirme."""
        self._post(package_quantity='2', loose_quantity='3', unit_cost='500.00')

        self.assertEqual(self._movement().unit_cost, Decimal('500.00'))

    def test_prix_saisis_figes_sur_le_mouvement(self):
        """La pondération est irréversible : l'historique garde la saisie."""
        self._post(
            package_quantity='2', package_unit_cost='6000.00',
            loose_quantity='3', unit_cost='550.00',
        )

        movement = self._movement()
        self.assertEqual(movement.input_package_unit_cost, Decimal('6000.00'))
        self.assertEqual(movement.input_loose_unit_cost, Decimal('550.00'))

    def test_prix_nul_vaut_non_saisi(self):
        """Le formulaire envoie 0 par défaut : ce n'est pas un achat gratuit."""
        self._post(package_quantity='2', unit_cost='0')

        movement = self._movement()
        self.assertIsNone(movement.input_loose_unit_cost)
        self.assertIsNone(movement.input_package_unit_cost)

    def test_avg_cost_du_stock_utilise_le_cout_pondere(self):
        self._post(
            package_quantity='2', package_unit_cost='6000.00',
            loose_quantity='3', unit_cost='550.00',
        )

        stock = Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse
        )
        self.assertEqual(stock.avg_cost, Decimal('505.56'))

    def test_produit_sans_conditionnement_refuse_le_prix_au_contenant(self):
        """Non-régression."""
        response = self._post(
            product=str(self.simple.id), quantity='5', package_unit_cost='4800.00'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('package_unit_cost', response.data)


class ProductPriceUpdateTests(_MovementPricingSetup):
    """Report des prix sur la fiche produit, sur demande explicite."""

    def test_case_decochee_ne_touche_pas_la_fiche(self):
        self._post(
            package_quantity='2', package_unit_cost='5400.00', unit_cost='450.00',
            selling_price='700.00', wholesale_price='7200.00',
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, Decimal('400.00'))
        self.assertEqual(self.product.package_cost_price, Decimal('4800.00'))
        self.assertEqual(self.product.selling_price, Decimal('600.00'))
        self.assertEqual(self.product.wholesale_price, Decimal('6000.00'))

    def test_case_cochee_met_a_jour_les_quatre_prix(self):
        response = self._post(
            package_quantity='2', package_unit_cost='5400.00', unit_cost='450.00',
            selling_price='700.00', wholesale_price='7200.00',
            update_product_prices=True,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, Decimal('450.00'))
        self.assertEqual(self.product.package_cost_price, Decimal('5400.00'))
        self.assertEqual(self.product.selling_price, Decimal('700.00'))
        self.assertEqual(self.product.wholesale_price, Decimal('7200.00'))

    def test_cout_pondere_ne_devient_pas_le_prix_dachat_de_la_fiche(self):
        """
        `cost_price` répond à « combien me coûte une bouteille », pas « combien
        m'a coûté ce mélange de livraison ».
        """
        self._post(
            package_quantity='2', package_unit_cost='6000.00',
            loose_quantity='3', unit_cost='550.00',
            update_product_prices=True,
        )

        self.assertEqual(self._movement().unit_cost, Decimal('505.56'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, Decimal('550.00'))

    def test_prix_dachat_seul_au_contenant_derive_le_prix_unitaire(self):
        self._post(
            package_quantity='2', package_unit_cost='6600.00',
            update_product_prices=True,
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.package_cost_price, Decimal('6600.00'))
        self.assertEqual(self.product.cost_price, Decimal('550.00'))

    def test_prix_de_vente_inferieur_au_prix_dachat_annule_tout(self):
        """Test d'atomicité : ni mouvement, ni stock, ni prix."""
        response = self._post(
            package_quantity='2', unit_cost='550.00', selling_price='300.00',
            update_product_prices=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('selling_price', response.data)

        self.assertFalse(StockMovement.objects.filter(product=self.product).exists())
        self.assertFalse(Stock.objects.filter(product=self.product).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.selling_price, Decimal('600.00'))

    def test_mouvement_sortant_refuse_le_report_des_prix(self):
        response = self._post(
            movement_type='damage', quantity='2', unit_cost='550.00',
            update_product_prices=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('update_product_prices', response.data)

    def test_report_sans_aucun_prix_est_refuse(self):
        response = self._post(package_quantity='2', update_product_prices=True)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('update_product_prices', response.data)

    def test_detail_seul_ne_reecrit_pas_le_prix_de_gros(self):
        """
        En vente au détail seule, `wholesale_price` garde son sens historique de
        prix de gros à la pièce : un approvisionnement ne doit pas le changer.
        """
        self.simple.wholesale_price = Decimal('700.00')
        self.simple.save(update_fields=['wholesale_price'])

        self._post(
            product=str(self.simple.id), quantity='5', unit_cost='450.00',
            selling_price='900.00', wholesale_price='9999.00',
            update_product_prices=True,
        )

        self.simple.refresh_from_db()
        self.assertEqual(self.simple.wholesale_price, Decimal('700.00'))
        self.assertEqual(self.simple.selling_price, Decimal('900.00'))
        self.assertEqual(self.simple.cost_price, Decimal('450.00'))


class ProductPricePermissionTests(_MovementPricingSetup):

    def test_sans_products_edit_le_report_est_refuse(self):
        """
        Un caissier à qui on a accordé la création de mouvements peut
        approvisionner, mais pas retarifer le catalogue.
        """
        membership = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org
        )
        membership.extra_permissions = ['stock_movements.create', 'stock.view']
        membership.save(update_fields=['extra_permissions'])
        membership.assigned_warehouses.add(self.warehouse)
        self.client.force_authenticate(user=self.cashier_a)

        refused = self._post(
            package_quantity='2', unit_cost='450.00', selling_price='700.00',
            update_product_prices=True,
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('update_product_prices', refused.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.selling_price, Decimal('600.00'))

        # Le même mouvement sans report reste autorisé.
        accepted = self._post(package_quantity='2', unit_cost='450.00')
        self.assertEqual(accepted.status_code, status.HTTP_201_CREATED, accepted.data)
