"""
Tests des corrections du flux de vente cash + crédit.

Couverture :
- Vente retail/wholesale sans paiement → refusée.
- Vente crédit sans customer → refusée.
- Points de fidélité réduisent effectivement le total.
- Points de fidélité cappés par le total de la vente.
- Cancel d'une vente crédit overpayée → solde client correctement restauré.
- add_payment qui passe la vente en completed → stock décrémenté UNE seule fois.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale
from apps.settings.models import LoyaltyProgram, CustomerLoyalty

from ._helpers import make_org_with_users, make_cash_payment_method


class _BaseSaleFlowTest(APITestCase):
    """Setup commun : org + users + session ouverte + produit en stock."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)
        # Session ouverte (sinon validate() refuse les ventes POS)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'),
            status='open',
        )
        # Produit avec stock
        self.product = Product.objects.create(
            organization=self.org,
            name='Coca-Cola 1.5L',
            sku='COCA-15L',
            cost_price=Decimal('1500.00'),
            selling_price=Decimal('2000.00'),
            track_inventory=True,
            allow_negative_stock=False,
            is_active=True,
        )
        Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('100.000'),
            avg_cost=Decimal('1500.00'),
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _base_sale_payload(self, **overrides):
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'payments': [],
        }
        payload.update(overrides)
        return payload


class RetailWithoutPaymentTests(_BaseSaleFlowTest):

    def test_retail_sale_without_payment_is_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(payments=[]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('payments', resp.data)

    def test_wholesale_sale_without_payment_is_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(sale_type='wholesale', payments=[]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('payments', resp.data)

    def test_retail_sale_with_full_payment_is_accepted(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '2000.00',
            }]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.data['status'], 'completed')


class CreditSaleValidationTests(_BaseSaleFlowTest):

    def test_credit_sale_without_customer_is_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(sale_type='credit', customer=None, payments=[]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('customer', resp.data)

    def test_credit_sale_with_customer_is_accepted(self):
        customer = Customer.objects.create(
            organization=self.org, code='C-001', name='Test Client',
            credit_limit=Decimal('100000'), current_balance=Decimal('0'),
        )
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(
                sale_type='credit', customer=str(customer.id), payments=[],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.data['status'], 'pending')
        customer.refresh_from_db()
        self.assertEqual(customer.current_balance, Decimal('2000.00'))


class LoyaltyRedemptionTests(_BaseSaleFlowTest):

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            organization=self.org, code='C-LOY', name='Client Fidèle',
            credit_limit=Decimal('100000'), current_balance=Decimal('0'),
        )
        self.program = LoyaltyProgram.objects.create(
            organization=self.org,
            name='Programme test',
            is_active=True,
            point_value=Decimal('10.00'),       # 1 point = 10 CDF
            min_points_to_redeem=10,
            # 0% sur le total - on teste la redemption isolément, sans gain croisé.
            points_calculation_type=LoyaltyProgram.PointsCalculationType.PERCENTAGE,
            points_percentage=Decimal('0.00'),
            only_registered_customers=False,
        )
        self.loyalty = CustomerLoyalty.objects.create(
            organization=self.org, customer=self.customer, current_points=100,
        )

    def test_loyalty_points_reduce_sale_total(self):
        """50 points × 10 CDF = 500 CDF déduits du total 2000 → total final 1500."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(
                customer=str(self.customer.id),
                points_used=50,
                payments=[{
                    'payment_method': str(self.payment_method.id),
                    'amount': '1500.00',
                }],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(Decimal(resp.data['loyalty_redemption_amount']), Decimal('500.00'))
        self.assertEqual(Decimal(resp.data['total']), Decimal('1500.00'))
        self.assertEqual(resp.data['status'], 'completed')
        # Solde points décrémenté
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 50)

    def test_loyalty_points_capped_by_redemption_ceiling(self):
        """1000 points dispo, total 2000, plafond 70 % → 140 points utilisables."""
        self.loyalty.current_points = 1000
        self.loyalty.save()
        # Au maximum autorisé : la borne dure de 70 % est ce qui mord désormais,
        # le total ne peut plus être le cap puisqu'il n'est jamais atteignable.
        self.program.max_redemption_percent = Decimal('70.00')
        self.program.save(update_fields=['max_redemption_percent'])

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(
                customer=str(self.customer.id),
                points_used=1000,  # tentative d'utiliser tous
                payments=[{
                    'payment_method': str(self.payment_method.id),
                    'amount': '0.01',  # juste pour passer la validation paiement
                }],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        # 70 % de 2 000 = 1 400 CDF, soit 140 points à 10 CDF pièce.
        self.assertEqual(Decimal(resp.data['loyalty_redemption_amount']), Decimal('1400.00'))
        self.assertEqual(Decimal(resp.data['total']), Decimal('600.00'))
        self.loyalty.refresh_from_db()
        # 1000 - 140 = 860 points restants
        self.assertEqual(self.loyalty.current_points, 860)


class AddPaymentAndCancelTests(_BaseSaleFlowTest):

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            organization=self.org, code='C-002', name='Client B',
            credit_limit=Decimal('100000'), current_balance=Decimal('0'),
        )

    def _create_credit_sale(self, total='2000.00'):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(
                sale_type='credit', customer=str(self.customer.id),
                items=[{
                    'product': str(self.product.id),
                    'quantity': '1',
                    'unit_price': str(total),
                }],
                payments=[],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data['id']

    def test_add_payment_completes_sale_and_decrements_stock_once(self):
        sale_id = self._create_credit_sale(total='2000.00')
        stock_before = Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['status'], 'completed')

        stock_after = Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity
        self.assertEqual(stock_before - stock_after, Decimal('1.000'))

        # Idempotence : un second add_payment doit être refusé (vente completed)
        resp2 = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '100'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        # Stock inchangé
        stock_final = Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity
        self.assertEqual(stock_final, stock_after)

    def test_cancel_credit_sale_overpaid_restores_balance_correctly(self):
        """
        Surpaiement : le client remet 2500 pour une dette de 2000 et repart avec
        500 de monnaie. Sa dette tombe à 0, pas à -500 : lui créditer la monnaie
        qu'on vient de lui rendre la compterait deux fois.
        """
        sale_id = self._create_credit_sale(total='2000.00')
        # Customer balance = 2000 après création (à crédit)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal('2000.00'))

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2500.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal('0.00'))

        # La monnaie rendue est bien tracée sur la vente.
        self.assertEqual(Sale.objects.get(pk=sale_id).change_amount, Decimal('500.00'))

        # Cancel par le manager (le cashier n'a pas `sales.cancel` par défaut).
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/',
            {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal('0.00'))

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'cancelled')


class SaleListContractTests(_BaseSaleFlowTest):
    """
    Le serializer de LISTE doit accompagner ses montants de leur devise.

    `SaleListSerializer` n'exposait ni `currency`, ni `exchange_rate`, ni
    `due_date`, alors que toutes les surfaces de liste (fiche client, paiements
    en attente, liste des ventes) lisent ces champs pour formater et convertir.
    À l'exécution ils valaient `undefined` : l'interface affichait
    « 50 undefined » et les règlements en devise étrangère n'étaient jamais
    convertis.

    Le type TS `Sale` les déclarant obligatoires, la compilation ne pouvait pas
    attraper l'écart : ce test est le seul garde-fou.
    """

    def _list(self):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get('/api/v1/sales/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data['results']

    def _create_paid_sale(self, **overrides):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(
                payments=[{
                    'payment_method': str(self.payment_method.id),
                    'amount': '2000.00',
                }],
                **overrides,
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data

    def test_list_exposes_the_fields_needed_to_interpret_amounts(self):
        self._create_paid_sale()

        row = self._list()[0]
        for field in ('currency', 'exchange_rate', 'change_currency', 'due_date'):
            self.assertIn(field, row, f"`{field}` manque au serializer de liste")
        self.assertTrue(row['currency'], "la devise ne doit jamais être vide")

    def test_list_currency_matches_the_detail(self):
        detail = self._create_paid_sale()

        self.assertEqual(self._list()[0]['currency'], detail['currency'])


class LoyaltyAllowanceExposureTests(LoyaltyRedemptionTests):
    """
    `loyalty_max_redeemable` doit dire ce que le serveur autorisera vraiment.

    L'écran de règlement d'une facture calculait son « Maximum » sur le seul
    reste à payer : il proposait des points que `resolve_redemption` refusait
    ensuite dès que `max_redemption_percent` était plus serré. Le plafond est
    désormais calculé par la fonction que `apply_payment_to_sale` applique
    lui-même, et exposé tel quel.
    """

    def _credit_sale(self, unit_price='2000.00'):
        """Facture émise et laissée entièrement due."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._base_sale_payload(
                sale_type='credit',
                customer=str(self.customer.id),
                items=[{
                    'product': str(self.product.id),
                    'quantity': '1',
                    'unit_price': unit_price,
                }],
                payments=[],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data

    def _detail(self, sale_id):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get(f'/api/v1/sales/{sale_id}/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data

    def test_allowance_follows_the_program_ceiling(self):
        self.program.max_redemption_percent = Decimal('25.00')
        self.program.save(update_fields=['max_redemption_percent'])

        sale = self._credit_sale()

        # 25 % de 2 000 = 500, bien en deçà du reste à payer (2 000).
        self.assertEqual(
            Decimal(self._detail(sale['id'])['loyalty_max_redeemable']),
            Decimal('500.00'),
        )

    def test_allowance_is_capped_by_what_is_still_due(self):
        self.program.max_redemption_percent = Decimal('100.00')
        self.program.save(update_fields=['max_redemption_percent'])

        sale = self._credit_sale()
        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f"/api/v1/sales/{sale['id']}/add-payment/",
            {'payment_method': str(self.payment_method.id), 'amount': '1500.00'},
            format='json', **self._headers(),
        )

        # Le plafond vaut 2 000, mais il ne reste que 500 à payer.
        self.assertEqual(
            Decimal(self._detail(sale['id'])['loyalty_max_redeemable']),
            Decimal('500.00'),
        )

    def test_allowance_shrinks_after_a_payment_in_points(self):
        self.program.max_redemption_percent = Decimal('50.00')
        self.program.save(update_fields=['max_redemption_percent'])

        sale = self._credit_sale()
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f"/api/v1/sales/{sale['id']}/add-payment/",
            {'points_used': '50'},   # 50 pts × 10 = 500
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        # Le plafond porte sur le TOTAL (50 % de 2 000 = 1 000) et retranche ce
        # qui a déjà été réglé en points : 1 000 - 500 = 500. Sans cette
        # soustraction, deux règlements successifs dépasseraient la part
        # autorisée sans jamais la franchir en apparence.
        self.assertEqual(
            Decimal(self._detail(sale['id'])['loyalty_max_redeemable']),
            Decimal('500.00'),
        )

    def test_a_program_set_above_the_hard_ceiling_is_re_bounded(self):
        """
        `MAX_REDEMPTION_PERCENT_CEILING` (70 %) borne le réglage de
        l'organisation. L'écran doit lire la valeur re-bornée, pas le réglage
        brut : sinon il proposerait 100 % d'une facture que le serveur refuse.
        """
        self.program.max_redemption_percent = Decimal('100.00')
        self.program.save(update_fields=['max_redemption_percent'])

        sale = self._credit_sale()

        # 70 % de 2 000, et non 100 %.
        self.assertEqual(
            Decimal(self._detail(sale['id'])['loyalty_max_redeemable']),
            Decimal('1400.00'),
        )

    def test_the_maximum_offered_is_accepted_by_the_server(self):
        """Le contrat qui manquait : ce qu'on propose doit passer."""
        self.program.max_redemption_percent = Decimal('25.00')
        self.program.save(update_fields=['max_redemption_percent'])

        sale = self._credit_sale()
        allowance = Decimal(self._detail(sale['id'])['loyalty_max_redeemable'])
        points = allowance / self.program.point_value

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f"/api/v1/sales/{sale['id']}/add-payment/",
            {'points_used': str(points)},
            format='json', **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(
            Decimal(self._detail(sale['id'])['loyalty_max_redeemable']),
            Decimal('0.00'),
        )
