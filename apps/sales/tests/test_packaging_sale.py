"""
Vente en gros et au détail, avec déconditionnement automatique.

Produit de référence : **Eau 50cl**, paquet de 12 bouteilles,
6 000 CDF le paquet, 600 CDF la bouteille.

Le scénario de recette complet est rejoué par ``AcceptanceScenarioTests``.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product, Unit
from apps.sales.models import RegisterSession
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users


class _PackagingSaleSetup(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)

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
            units_per_package=12, allow_auto_unpacking=True,
            cost_price=Decimal('400.00'),
            selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            tax_rate=Decimal('0.00'), is_taxable=False,
            track_inventory=True, is_active=True,
        )
        # Session ouverte, sinon la validation refuse les ventes POS.
        RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.owner, opening_balance=Decimal('0'),
            status='open',
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _supply(self, packages=0, loose=0):
        response = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.product.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'package_quantity': str(packages),
                'loose_quantity': str(loose),
                'unit_cost': '400.00',
            },
            format='json', **self._headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data

    def _sell(self, packages=0, loose=0, expect=status.HTTP_201_CREATED):
        item = {'product': str(self.product.id), 'unit_price': '600.00'}
        if packages:
            item['package_quantity'] = str(packages)
            item['package_unit_price'] = '6000.00'
        if loose:
            item['loose_quantity'] = str(loose)

        total = packages * 6000 + loose * 600
        response = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail',
                'is_pos': True,
                'items': [item],
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': str(total),
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def _stock(self):
        return Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse
        )


class WholesaleSaleTests(_PackagingSaleSetup):

    def test_vente_en_gros_decremente_le_contenu_complet(self):
        self._supply(packages=2)
        self._sell(packages=1)

        self.assertEqual(self._stock().quantity, Decimal('12.000'))

    def test_vente_en_gros_utilise_le_prix_du_paquet(self):
        self._supply(packages=2)
        response = self._sell(packages=1)

        self.assertEqual(Decimal(response.data['total']), Decimal('6000.00'))

    def test_vente_en_gros_ne_cree_pas_de_vrac(self):
        self._supply(packages=2)
        self._sell(packages=1)

        self.assertEqual(self._stock().loose_quantity, Decimal('0.000'))
        self.assertFalse(
            StockMovement.objects.filter(movement_type='unpack').exists()
        )


class RetailSaleTests(_PackagingSaleSetup):

    def test_vente_au_detail_sur_produit_vendu_en_gros(self):
        self._supply(packages=0, loose=20)
        response = self._sell(loose=3)

        self.assertEqual(Decimal(response.data['total']), Decimal('1800.00'))
        self.assertEqual(self._stock().quantity, Decimal('17.000'))
        self.assertEqual(self._stock().loose_quantity, Decimal('17.000'))


class MixedSaleTests(_PackagingSaleSetup):

    def test_vente_mixte_additionne_les_deux_tarifs(self):
        """1 paquet + 3 bouteilles = 6 000 + 1 800 = 7 800."""
        self._supply(packages=2)
        response = self._sell(packages=1, loose=3)

        self.assertEqual(Decimal(response.data['total']), Decimal('7800.00'))

    def test_vente_mixte_decremente_la_quantite_totale(self):
        self._supply(packages=2)
        self._sell(packages=1, loose=3)

        self.assertEqual(self._stock().quantity, Decimal('9.000'))

    def test_ligne_mixte_conserve_les_deux_parts(self):
        self._supply(packages=2)
        response = self._sell(packages=1, loose=3)

        item = response.data['items'][0]
        self.assertEqual(Decimal(item['package_quantity']), Decimal('1.000'))
        self.assertEqual(Decimal(item['loose_quantity']), Decimal('3.000'))
        self.assertEqual(Decimal(item['quantity']), Decimal('15.000'))
        self.assertEqual(item['packaging_factor'], 12)
        self.assertEqual(item['quantity_display'], '1 paquet + 3 bouteilles')


class AutoUnpackingTests(_PackagingSaleSetup):

    def test_deconditionnement_declenche_par_une_vente_au_detail(self):
        """2 paquets pleins, le client veut 2 bouteilles."""
        self._supply(packages=2)
        self._sell(loose=2)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('22.000'))
        self.assertEqual(stock.loose_quantity, Decimal('10.000'))

    def test_le_deconditionnement_est_trace(self):
        self._supply(packages=2)
        response = self._sell(loose=2)

        movement = StockMovement.objects.get(movement_type='unpack')
        self.assertEqual(movement.quantity, Decimal('0.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('1.000'))
        self.assertEqual(movement.reference_type, 'sale')
        self.assertEqual(str(movement.reference_id), response.data['id'])
        self.assertEqual(movement.created_by, self.owner)
        self.assertIsNotNone(movement.created_at)

    def test_le_vendeur_est_informe_dans_la_reponse(self):
        self._supply(packages=2)
        response = self._sell(loose=2)

        notices = response.data['unpacking_notices']
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['packages_opened'], 1)
        message = notices[0]['message']
        self.assertIn('1 paquet a été ouvert automatiquement', message)
        self.assertIn('12 bouteille', message)
        self.assertIn('1 paquet + 10 bouteilles', message)

    def test_deconditionnement_de_plusieurs_paquets(self):
        self._supply(packages=3)
        self._sell(loose=25)

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('11.000'))
        movement = StockMovement.objects.get(movement_type='unpack')
        self.assertEqual(movement.input_package_quantity, Decimal('3.000'))

    def test_pas_de_deconditionnement_si_le_vrac_suffit(self):
        self._supply(packages=1, loose=5)
        self._sell(loose=3)

        self.assertFalse(
            StockMovement.objects.filter(movement_type='unpack').exists()
        )
        self.assertEqual(self._stock().loose_quantity, Decimal('2.000'))

    def test_deconditionnement_refuse_si_desactive(self):
        self.product.allow_auto_unpacking = False
        self.product.save()
        self._supply(packages=2)

        response = self._sell(loose=2, expect=status.HTTP_400_BAD_REQUEST)
        self.assertIn('Ouvrez un paquet', str(response.data))
        self.assertEqual(self._stock().quantity, Decimal('24.000'))

    def test_vente_possible_sans_deconditionnement_auto_si_vrac_suffisant(self):
        self.product.allow_auto_unpacking = False
        self.product.save()
        self._supply(packages=1, loose=5)

        self._sell(loose=3)
        self.assertEqual(self._stock().quantity, Decimal('14.000'))


class RepackagingRefusedTests(_PackagingSaleSetup):

    def test_vente_en_gros_refusee_sans_paquet_scelle(self):
        """30 bouteilles en vrac ne font pas 2 paquets."""
        self._supply(loose=30)

        response = self._sell(packages=2, expect=status.HTTP_400_BAD_REQUEST)
        message = str(response.data)
        self.assertIn('ne peuvent pas y être remises', message)
        self.assertIn('au détail', message)
        self.assertEqual(self._stock().quantity, Decimal('30.000'))

    def test_stock_total_insuffisant_refuse(self):
        self._supply(packages=1)

        self._sell(packages=2, expect=status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._stock().quantity, Decimal('12.000'))


class PriceGuardTests(_PackagingSaleSetup):

    def test_prix_du_paquet_superieur_au_contenu_refuse(self):
        self._supply(packages=2)

        response = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [{
                    'product': str(self.product.id),
                    'unit_price': '600.00',
                    'package_quantity': '1',
                    'package_unit_price': '99000.00',
                }],
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': '99000.00',
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quantite_fractionnaire_refusee(self):
        self._supply(packages=2)

        response = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [{
                    'product': str(self.product.id),
                    'unit_price': '600.00',
                    'loose_quantity': '1.5',
                }],
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': '900.00',
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('unités entières', str(response.data))

    def test_conditionnement_refuse_sur_produit_simple(self):
        simple = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle, selling_price=Decimal('800.00'),
            cost_price=Decimal('400.00'), is_taxable=False,
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=simple,
            warehouse=self.warehouse, quantity=Decimal('50.000'),
        )

        response = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [{
                    'product': str(simple.id),
                    'unit_price': '800.00',
                    'package_quantity': '2',
                    'package_unit_price': '1500.00',
                }],
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': '1500.00',
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SimpleProductSaleRegressionTests(_PackagingSaleSetup):
    """Non-régression : une vente de produit mono-unité est inchangée."""

    def setUp(self):
        super().setUp()
        self.simple = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle, selling_price=Decimal('800.00'),
            cost_price=Decimal('400.00'), is_taxable=False,
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.simple,
            warehouse=self.warehouse, quantity=Decimal('50.000'),
            avg_cost=Decimal('400.00'),
        )

    def test_vente_simple_totaux_inchanges(self):
        response = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [{
                    'product': str(self.simple.id),
                    'quantity': '3',
                    'unit_price': '800.00',
                }],
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': '2400.00',
                }],
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(Decimal(response.data['total']), Decimal('2400.00'))

        item = response.data['items'][0]
        self.assertEqual(Decimal(item['package_quantity']), Decimal('0.000'))
        self.assertIsNone(item['packaging_factor'])
        self.assertEqual(item['quantity_display'], '3 bouteilles')

        stock = Stock.objects.get(product=self.simple, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal('47.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))
        self.assertEqual(response.data['unpacking_notices'], [])


class AcceptanceScenarioTests(_PackagingSaleSetup):
    """Le scénario de recette de la spécification, joué de bout en bout."""

    def test_scenario_complet(self):
        # 2. Approvisionner 2 paquets → « 2 paquets + 0 bouteille »
        self._supply(packages=2)
        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('24.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))

        # 3. Vendre 2 bouteilles → 1 200, avec déconditionnement signalé
        response = self._sell(loose=2)
        self.assertEqual(Decimal(response.data['total']), Decimal('1200.00'))
        self.assertEqual(len(response.data['unpacking_notices']), 1)
        self.assertEqual(
            response.data['unpacking_notices'][0]['packages_opened'], 1
        )

        # 4. Le stock affiche « 1 paquet + 10 bouteilles »
        stock_response = self.client.get(
            f'/api/v1/stocks/{self._stock().id}/', **self._headers
        )
        self.assertEqual(
            stock_response.data['stock_display'], '1 paquet + 10 bouteilles'
        )

        # 5. Vendre 1 paquet + 3 bouteilles → 7 800, reste « 0 paquet + 7 bouteilles »
        response = self._sell(packages=1, loose=3)
        self.assertEqual(Decimal(response.data['total']), Decimal('7800.00'))
        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('7.000'))
        stock_response = self.client.get(
            f'/api/v1/stocks/{stock.id}/', **self._headers
        )
        self.assertEqual(stock_response.data['stock_display'], '7 bouteilles')
        self.assertEqual(stock_response.data['stock_packages'], 0)

        # 6. Vendre 1 paquet → refusé, avec une issue proposée
        response = self._sell(packages=1, expect=status.HTTP_400_BAD_REQUEST)
        self.assertIn('au détail', str(response.data))

        # 7. Le déconditionnement de l'étape 3 est tracé
        movement = StockMovement.objects.get(movement_type='unpack')
        self.assertEqual(movement.created_by, self.owner)
        self.assertIsNotNone(movement.created_at)
        self.assertEqual(movement.input_package_quantity, Decimal('1.000'))
