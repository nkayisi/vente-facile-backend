"""
Tests des points de fidélité : cumul et utilisation sur facture.

Couvre les dysfonctionnements signalés en production :
- le cumul ne se faisait pas pour chaque client ;
- impossible de régler une facture, partiellement ou totalement, avec les points.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import Payment, RegisterSession, Sale, SaleReturn, SaleReturnItem
from apps.settings.models import (
    Currency, CustomerLoyalty, LoyaltyProgram, LoyaltyTransaction, OrganizationCurrency,
)
from apps.settings.services import LoyaltyService

from ._helpers import make_org_with_users, make_cash_payment_method


class _LoyaltyBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'),
            status='open',
        )
        self.product = Product.objects.create(
            organization=self.org, name='Produit', sku='P1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='0900000000',
            credit_limit=Decimal('1000000'),
        )
        # 1 point par 1 000 (devise principale) ; 1 point = 10.
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, is_active=True,
            points_calculation_type=LoyaltyProgram.PointsCalculationType.FIXED_PER_AMOUNT,
            points_per_unit=1,
            amount_per_unit=Decimal('1000.00'),
            point_value=Decimal('10.00'),
            min_points_to_redeem=10,
        )
        self.loyalty = CustomerLoyalty.objects.create(
            organization=self.org, customer=self.customer, current_points=0,
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _set_redemption_ceiling(self, percent):
        """
        Règle la part de facture réglable en points pour ce programme.

        Il n'existe pas de « sans plafond » : `MAX_REDEMPTION_PERCENT_CEILING`
        borne durement la configuration à 70 %, une facture garde toujours une
        part à encaisser en monnaie. Les tests qui veulent isoler un autre cap
        (le reste dû, le total) montent donc au maximum autorisé, pas à 100.
        """
        self.program.max_redemption_percent = Decimal(percent)
        self.program.save(update_fields=['max_redemption_percent'])

    def _create_sale(self, *, sale_type='credit', unit_price='2000.00', payments=None):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': sale_type,
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': payments or [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data['id']


class AccumulationTests(_LoyaltyBaseTest):
    """Le cumul doit se faire pour chaque client, dans toutes les devises."""

    def test_completed_sale_credits_points(self):
        self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)
        self.assertEqual(self.loyalty.total_points_earned, 2)

    def test_credit_sale_settled_from_customer_page_credits_points(self):
        """
        La cause racine du « pas de cumul » : une vente à crédit soldée depuis
        la fiche client ne passait jamais `completed`, donc n'attribuait aucun
        point. Les deux bugs n'en faisaient qu'un.
        """
        sale_id = self._create_sale()
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 0, "Rien avant le règlement.")

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '2000.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.assertEqual(Sale.objects.get(pk=sale_id).status, 'completed')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)

    def test_points_accumulate_across_sales(self):
        for _ in range(3):
            self._create_sale(sale_type='retail', payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '2000.00',
            }])
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 6)

    def test_award_is_idempotent_per_sale(self):
        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        sale = Sale.objects.get(pk=sale_id)

        LoyaltyService.award_points_for_sale(sale, user=self.cashier_a)
        LoyaltyService.award_points_for_sale(sale, user=self.cashier_a)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=LoyaltyTransaction.TransactionType.EARN,
            ).count(), 1,
        )


class _MultiCurrencyLoyaltyTest(_LoyaltyBaseTest):
    """Organisation en CDF principal, avec l'USD activé à 2 800."""

    def setUp(self):
        super().setUp()
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC'},
        )
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'US Dollar', 'symbol': '$'},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1'), 'is_active': True},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=usd,
            defaults={'is_primary': False, 'exchange_rate': Decimal('2800'), 'is_active': True},
        )


class SecondaryCurrencyAccumulationTests(_MultiCurrencyLoyaltyTest):
    """Le barème est en devise principale : une facture en USD doit être convertie."""

    def test_usd_invoice_awards_converted_points(self):
        """
        50 USD = 140 000 CDF ⇒ 140 points. L'ancien code comparait 50 à un
        barème de 1 000 CDF et attribuait 0 point.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '50.00',
                'currency': 'USD',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 140)


class InvoiceRedemptionTests(_LoyaltyBaseTest):
    """Régler une facture émise avec des points, partiellement ou totalement."""

    def setUp(self):
        super().setUp()
        self.loyalty.current_points = 500
        self.loyalty.save(update_fields=['current_points'])

    def test_points_partially_pay_an_invoice(self):
        """50 points × 10 = 500 imputés sur une facture de 2 000."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 50}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'partially_paid')
        self.assertEqual(sale.amount_paid, Decimal('500.00'))
        self.assertEqual(sale.amount_due, Decimal('1500.00'))
        # Le total de la facture émise ne bouge pas.
        self.assertEqual(sale.total, Decimal('2000.00'))

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 450)

    def test_les_points_ne_soldent_jamais_toute_la_facture(self):
        """
        Même au plafond maximal, une facture garde une part à encaisser.

        Le client a de quoi couvrir les 2 000 en points (200 × 10) : seuls
        1 400 passent, la borne dure de 70 % retenant le reste.
        """
        self._set_redemption_ceiling('70.00')
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 200}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1400.00'))
        self.assertEqual(sale.amount_due, Decimal('600.00'))
        self.assertEqual(sale.status, 'partially_paid')
        self.loyalty.refresh_from_db()
        # 140 points consommés ; la vente n'étant pas soldée, rien n'est gagné.
        self.assertEqual(self.loyalty.current_points, 360)

    def test_points_are_capped_by_amount_due(self):
        """
        On ne peut pas payer plus que ce qui reste dû.

        Il faut d'abord encaisser en monnaie pour que le reste dû (400) passe
        SOUS l'enveloppe de points (1 400) : sinon c'est le plafond qui mord et
        ce test ne mesurerait plus rien.
        """
        self._set_redemption_ceiling('70.00')
        sale_id = self._create_sale(payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '1600.00',
        }])

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 500}, format='json', **self._headers(),
        )

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('2000.00'))
        self.assertEqual(sale.status, 'completed')
        self.loyalty.refresh_from_db()
        # 400 / 10 = 40 points consommés seulement, + 2 gagnés à la clôture.
        self.assertEqual(self.loyalty.current_points, 462)

    def test_two_successive_points_payments_are_allowed(self):
        """Une facture peut être réglée en points en plusieurs fois."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        for _ in range(2):
            resp = self.client.post(
                f'/api/v1/sales/{sale_id}/add-payment/',
                {'points_used': 50}, format='json', **self._headers(),
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 400)
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=LoyaltyTransaction.TransactionType.REDEEM,
            ).count(), 2,
        )

    def test_points_payment_creates_no_cash_movement(self):
        """Les points ne sont pas de l'argent physique : rien au tiroir."""
        from apps.cashbook.models import CashMovement

        sale_id = self._create_sale()
        before = CashMovement.objects.filter(organization=self.org).count()

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 50}, format='json', **self._headers(),
        )

        self.assertEqual(CashMovement.objects.filter(organization=self.org).count(), before)
        self.assertTrue(
            Payment.objects.filter(
                sale_id=sale_id, payment_method__method_type='loyalty',
            ).exists()
        )

    def test_points_payment_reduces_customer_debt(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 50}, format='json', **self._headers(),
        )

        from apps.contacts import services as contacts_services
        self.assertEqual(
            contacts_services.get_balance(self.customer, 'CDF'), Decimal('1500.00'),
        )

    def test_below_minimum_is_rejected(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 5}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class DebtRedemptionTests(_LoyaltyBaseTest):
    """Utiliser les points sur la dette globale du client."""

    def setUp(self):
        super().setUp()
        self.loyalty.current_points = 500
        self.loyalty.save(update_fields=['current_points'])

    def test_points_settle_the_global_debt(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/redeem-points/',
            {'points': 100}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 400)

    def test_customer_without_debt_is_rejected(self):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/redeem-points/',
            {'points': 100}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ReversalCounterTests(_LoyaltyBaseTest):
    """Les compteurs à vie doivent être corrigés du bon côté."""

    def test_cancel_reverses_earned_points_without_inflating_counters(self):
        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.total_points_earned, 2)

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 0)
        self.assertEqual(self.loyalty.total_points_earned, 0)

    def test_cancel_restores_redeemed_points_to_the_right_counter(self):
        """
        Annuler une utilisation doit réduire `total_points_redeemed`, pas
        gonfler `total_points_earned` comme le faisait l'ancien code.
        """
        self.loyalty.current_points = 500
        self.loyalty.save(update_fields=['current_points'])

        sale_id = self._create_sale()
        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 50}, format='json', **self._headers(),
        )
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.total_points_redeemed, 50)
        earned_before = self.loyalty.total_points_earned

        self.client.force_authenticate(user=self.manager)
        self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 500)
        self.assertEqual(self.loyalty.total_points_redeemed, 0)
        self.assertEqual(
            self.loyalty.total_points_earned, earned_before,
            "Annuler une utilisation ne doit pas gonfler les points gagnés.",
        )


class ReturnReversalTests(_LoyaltyBaseTest):
    """Un retour total doit reprendre les points, comme une annulation."""

    def test_full_return_reverses_points(self):
        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        sale = Sale.objects.get(pk=sale_id)
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post('/api/v1/sale-returns/', {
            'original_sale': str(sale.id),
            'return_type': 'full',
            'reason': 'Produit abîmé',
            'items': [{
                'original_item': str(sale.items.first().id),
                'quantity': '1',
                'unit_price': '2000.00',
                'total': '2000.00',
                'restock': True,
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        sale_return = SaleReturn.objects.get(original_sale=sale)
        resp = self.client.post(
            f'/api/v1/sale-returns/{sale_return.id}/approve/',
            {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 0)


class ReceiptExposureTests(_LoyaltyBaseTest):
    """
    Ce que le reçu doit lire. Le POS rejouait le barème avec la seule formule
    « pourcentage » codée en dur : sur un programme `fixed_per_amount`, qui est
    le défaut, il annonçait dix fois les points réellement crédités. Le serveur
    est désormais la seule source.
    """

    def _detail(self, sale_id):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get(f'/api/v1/sales/{sale_id}/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data

    def test_points_gagnes_correspondent_au_registre(self):
        sale_id = self._create_sale(sale_type='retail', unit_price='5000.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '5000.00',
        }])

        data = self._detail(sale_id)
        self.loyalty.refresh_from_db()

        # 1 point par 1 000 : 5 points, pas 50 (ce que donnait le 1 % du POS).
        self.assertEqual(data['loyalty_points_earned'], 5)
        self.assertEqual(data['loyalty_points_earned'], self.loyalty.total_points_earned)
        self.assertEqual(data['loyalty_points_balance'], self.loyalty.current_points)
        self.assertTrue(data['loyalty_program_active'])

    def test_bareme_en_pourcentage(self):
        self.program.points_calculation_type = (
            LoyaltyProgram.PointsCalculationType.PERCENTAGE
        )
        self.program.points_percentage = Decimal('1.00')
        self.program.save()

        sale_id = self._create_sale(sale_type='retail', unit_price='5000.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '5000.00',
        }])

        self.loyalty.refresh_from_db()
        self.assertEqual(self._detail(sale_id)['loyalty_points_earned'], 50)
        self.assertEqual(self.loyalty.current_points, 50)

    def test_points_utilises_exposes(self):
        self.loyalty.current_points = 100
        self.loyalty.total_points_earned = 100
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'points_used': 50,
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '1500.00',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # 50 points x 10 = 500 de remise sur 2 000.
        self.assertEqual(Decimal(resp.data['loyalty_redemption_amount']), Decimal('500.00'))
        self.assertEqual(resp.data['loyalty_points_used'], 50)

        data = self._detail(resp.data['id'])
        self.assertEqual(data['loyalty_points_used'], 50)
        # La fiche de vente et le reçu réimprimé isolent la remise fidélité de
        # la remise commerciale : sans ce montant sur le détail, la déduction
        # se fondrait dans `discount_amount` et disparaîtrait de la facture.
        self.assertEqual(
            Decimal(data['loyalty_redemption_amount']), Decimal('500.00')
        )
        self.assertEqual(Decimal(data['discount_amount']), Decimal('500.00'))

    def test_la_remise_fidelite_se_distingue_de_la_remise_commerciale(self):
        """
        `discount_amount` cumule les deux : la facture doit pouvoir les séparer.

        Sans `loyalty_redemption_amount` exposé à part, l'écran et le reçu
        afficheraient « Remise 800 » sans dire que 500 viennent des points.
        """
        self.loyalty.current_points = 100
        self.loyalty.total_points_earned = 100
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'global_discount_amount': '300.00',
            'points_used': 50,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        data = self._detail(resp.data['id'])
        loyalty = Decimal(data['loyalty_redemption_amount'])
        total_discount = Decimal(data['discount_amount'])
        self.assertEqual(loyalty, Decimal('500.00'))
        self.assertEqual(total_discount, Decimal('800.00'))
        # Ce que la facture affiche sur la ligne « Remise » commerciale.
        self.assertEqual(total_discount - loyalty, Decimal('300.00'))
        self.assertEqual(Decimal(data['total']), Decimal('1200.00'))

    def test_annulation_remet_les_compteurs_du_recu_a_zero(self):
        sale_id = self._create_sale(sale_type='retail', unit_price='5000.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '5000.00',
        }])
        self.assertEqual(self._detail(sale_id)['loyalty_points_earned'], 5)

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/',
            {'reason': 'test'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        # Un reçu réimprimé après annulation ne doit plus annoncer de points.
        self.assertEqual(self._detail(sale_id)['loyalty_points_earned'], 0)

    def test_sans_programme_actif_le_recu_le_sait(self):
        self.program.is_active = False
        self.program.save()

        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])

        data = self._detail(sale_id)
        self.assertFalse(data['loyalty_program_active'])
        self.assertEqual(data['loyalty_points_earned'], 0)


class CustomerLoyaltyEndpointTests(_LoyaltyBaseTest):
    """
    Le POS lit le solde via `?customer=`. L'endpoint était paginé alors que le
    client attendait un tableau : il recevait donc toujours `null`, ce qui
    masquait tout le panneau fidélité et affichait « 0 pts » sur la fiche.
    """

    def test_reponse_non_paginee(self):
        self.loyalty.current_points = 42
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get(
            f'/api/v1/settings/customer-loyalty/?customer={self.customer.id}',
            **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]['current_points'], 42)


class TenderCurrencyIndependenceTests(_MultiCurrencyLoyaltyTest):
    """
    Les points se calculent sur la FACTURE, jamais sur ce qui est remis.

    Le montant perçu peut être encaissé dans une autre devise (une facture en
    CDF réglée en dollars, la monnaie rendue dans une troisième). Rien de tout
    cela ne change ce que le client a acheté : le barème s'applique au total de
    la facture, ramené en devise principale.
    """

    def _sell(self, *, unit_price, payments, currency=None, change_currency=None):
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': payments,
        }
        if currency:
            payload['currency'] = currency
        if change_currency:
            payload['change_currency'] = change_currency
        resp = self.client.post('/api/v1/sales/', payload, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp

    def test_facture_en_principale_reglee_en_devise_etrangere(self):
        """2 800 CDF encaissés en 1 USD : 2 points, comme un règlement en CDF."""
        resp = self._sell(unit_price='2800.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '1.00',
            'currency': 'USD',
        }])

        self.assertEqual(resp.data['currency'], 'CDF')
        self.assertEqual(Decimal(resp.data['total']), Decimal('2800.00'))
        self.assertEqual(resp.data['status'], 'completed')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)
        self.assertEqual(resp.data['loyalty_points_earned'], 2)

    def test_reglement_fractionne_deux_devises(self):
        """1 400 CDF + 0,5 USD sur une facture de 2 800 CDF : toujours 2 points."""
        resp = self._sell(unit_price='2800.00', payments=[
            {'payment_method': str(self.payment_method.id), 'amount': '1400.00'},
            {
                'payment_method': str(self.payment_method.id),
                'amount': '0.50',
                'currency': 'USD',
            },
        ])

        self.assertEqual(resp.data['status'], 'completed')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)

    def test_monnaie_rendue_en_devise_etrangere_ne_change_rien(self):
        """
        Surpaiement rendu en USD : la monnaie sort du tiroir, elle ne retire
        aucun point. Le client a bien acheté pour 2 800 CDF.
        """
        resp = self._sell(
            unit_price='2800.00',
            payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '5000.00',
            }],
            change_currency='USD',
        )

        self.assertEqual(resp.data['change_currency'], 'USD')
        self.assertGreater(Decimal(resp.data['change_amount']), Decimal('0'))

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)

    def test_facture_a_credit_soldee_plus_tard_en_devise_etrangere(self):
        """
        Le règlement différé emprunte le même chemin : la facture reste
        l'assiette, quelle que soit la devise du versement.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2800.00',
            }],
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        sale_id = resp.data['id']

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 0, "Rien avant le règlement.")

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {
                'payment_method': str(self.payment_method.id),
                'amount': '1.00',
                'currency': 'USD',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['status'], 'completed')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 2)
        self.assertEqual(resp.data['loyalty_points_earned'], 2)

    def test_facture_en_usd_reglee_en_cdf(self):
        """
        Symétrique du cas précédent : facture de 50 USD réglée en 140 000 CDF.
        L'assiette reste la facture ramenée en principale, soit 140 points.
        """
        resp = self._sell(
            unit_price='50.00',
            currency='USD',
            payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '140000.00',
                'currency': 'CDF',
            }],
        )

        self.assertEqual(resp.data['currency'], 'USD')
        self.assertEqual(resp.data['status'], 'completed')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 140)


class RedemptionCurrencyTests(_MultiCurrencyLoyaltyTest):
    """
    L'utilisation des points traverse les devises sans se perdre.

    `point_value` est libellé en devise principale ; sur une facture en devise
    secondaire, la remise doit être reconvertie. Le POS restreignait
    l'utilisation aux factures en principale : les points disparaissaient en
    silence, sans message au caissier.
    """

    def test_remise_convertie_dans_la_devise_de_facture(self):
        self.loyalty.current_points = 280
        self.loyalty.total_points_earned = 280
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            # 280 points x 10 CDF = 2 800 CDF = 1 USD de remise.
            'points_used': 280,
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '49.00',
                'currency': 'USD',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('1.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('49.00'))
        self.assertEqual(resp.data['status'], 'completed')
        self.assertEqual(resp.data['loyalty_points_used'], 280)

        self.loyalty.refresh_from_db()
        # 280 consommés, puis 49 USD = 137 200 CDF ⇒ 137 points gagnés.
        self.assertEqual(self.loyalty.current_points, 137)


class RedemptionCeilingTests(_LoyaltyBaseTest):
    """
    Les points ne soldent jamais toute une facture.

    `max_redemption_percent` borne la part réglable en points pour qu'il reste
    toujours un montant à encaisser en monnaie. Le plafond porte sur le TOTAL de
    la facture et non sur le reste à payer : sinon des règlements successifs
    grignoteraient la part autorisée sans jamais la franchir en apparence.
    """

    def setUp(self):
        super().setUp()
        # De quoi solder trois fois la facture si rien ne l'en empêchait.
        self.loyalty.current_points = 1000
        self.loyalty.save(update_fields=['current_points'])

    def test_remise_a_la_creation_plafonnee_a_la_moitie(self):
        """Facture 2 000, plafond 50 % : au plus 1 000 de remise fidélité."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'points_used': 1000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('1000.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('1000.00'))
        # Il reste à encaisser : c'est tout l'objet du plafond.
        self.assertGreater(Decimal(resp.data['amount_due']), Decimal('0.00'))

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 900)

    def test_au_plafond_maximal_il_reste_toujours_a_encaisser(self):
        """
        Même réglé au maximum autorisé, le plafond laisse 30 % à payer.

        La borne dure ne se desserre par aucune configuration : c'est la
        garantie qui remplace l'ancien « 100 % = pas de plafond ».
        """
        self._set_redemption_ceiling('70.00')

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'points_used': 1000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('1400.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('600.00'))
        self.assertEqual(Decimal(resp.data['amount_due']), Decimal('600.00'))

    def test_une_valeur_au_dela_de_la_borne_est_ramenee(self):
        """
        Une ligne écrite hors validation ne desserre pas le plafond.

        Le validator ne protège que les écritures API ; `max_redeemable_amount`
        re-borne, sinon un `UPDATE` direct ou une vieille fixture rendrait la
        garantie caduque.
        """
        LoyaltyProgram.objects.filter(pk=self.program.pk).update(
            max_redemption_percent=Decimal('90.00'),
        )
        self.program.refresh_from_db()

        self.assertEqual(
            self.program.max_redeemable_amount(Decimal('2000.00')),
            Decimal('1400.00'),
        )

    def test_l_api_refuse_un_plafond_au_dela_de_la_borne(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.patch(
            f'/api/v1/settings/loyalty-program/{self.program.id}/',
            {'max_redemption_percent': '90.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('max_redemption_percent', resp.data)

    def test_reglement_en_points_sur_facture_emise_est_plafonne(self):
        """Même règle depuis l'écran des règlements, sinon le POS se contourne."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 1000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))
        self.assertEqual(sale.amount_due, Decimal('1000.00'))
        self.assertEqual(sale.status, 'partially_paid')

    def test_la_remise_de_creation_consomme_l_enveloppe_du_reglement(self):
        """
        Une facture déjà réduite de 50 % en points n'accepte plus de points.

        C'est le contournement que le plafond doit fermer : créer la vente avec
        la remise maximale, puis solder le reste en points depuis la facture.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'points_used': 1000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        sale_id = resp.data['id']

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 100}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('0.00'))
        self.loyalty.refresh_from_db()
        # Seuls les 100 points de la remise de création ont été consommés.
        self.assertEqual(self.loyalty.current_points, 900)

    def test_reglements_successifs_ne_depassent_pas_le_plafond(self):
        """Deux fois 50 points passent (= 50 %), le troisième est refusé."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        for _ in range(2):
            resp = self.client.post(
                f'/api/v1/sales/{sale_id}/add-payment/',
                {'points_used': 50}, format='json', **self._headers(),
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 50}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))

    def test_le_plafond_ne_touche_pas_l_epongement_de_dette(self):
        """
        La dette globale n'est pas une facture : le plafond n'y a pas cours.

        Un client qui a déjà consommé sa marchandise peut solder ce qu'il doit
        avec ses points ; il n'y a plus rien à encaisser en échange.
        """
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/redeem-points/',
            {'points': 200}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('2000.00'))
        self.assertEqual(sale.amount_due, Decimal('0.00'))


class SecondaryCurrencyCeilingTests(_MultiCurrencyLoyaltyTest):
    """Le plafond se compose avec la conversion de devise sans dériver."""

    def test_plafond_applique_dans_la_devise_de_facture(self):
        self.loyalty.current_points = 10000
        self.loyalty.total_points_earned = 10000
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'points_used': 10000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # 50 % de 50 USD = 25 USD = 70 000 CDF, soit 7 000 points à 10 CDF.
        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('25.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('25.00'))
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 3000)


class DegenerateExchangeRateTests(_MultiCurrencyLoyaltyTest):
    """
    Un taux nul sur la vente ne doit pas faire disparaître les points en silence.

    `Sale.save()` passe par `CurrencyService.resolve` et ne peut pas en produire,
    mais une reprise de données ou une écriture SQL directe le peut. Multiplier
    par zéro donnerait 0 point sans aucun signal.
    """

    def test_taux_nul_retombe_sur_le_taux_de_l_organisation(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        sale = Sale.objects.get(pk=resp.data['id'])
        # Écriture directe : on contourne `save()`, comme le ferait une reprise.
        Sale.objects.filter(pk=sale.pk).update(exchange_rate=Decimal('0'))
        sale.refresh_from_db()

        points = LoyaltyService.points_for_sale(sale, self.program)

        self.assertEqual(points, 140, "50 USD valent 140 000 CDF, soit 140 points.")


class FractionalPointsTests(_MultiCurrencyLoyaltyTest):
    """
    Les fractions de point ne se perdent plus.

    Cas réel rencontré en production : organisation en USD principal, barème à
    1 %. Une vente de 58 USD valait 0,58 point, tronqué à zéro ; une vente de
    396 USD en valait 3,96, tronqué à 3. Le compteur d'un marchand en devise
    forte ne bougeait donc presque jamais, et il fallait dépenser 100 USD pour
    voir un seul point apparaître.
    """

    def setUp(self):
        super().setUp()
        # Devise principale USD, barème à 1 % - la configuration du terrain.
        self.org.currency = 'USD'
        self.org.save(update_fields=['currency'])
        OrganizationCurrency.objects.filter(organization=self.org).update(is_primary=False)
        OrganizationCurrency.objects.filter(
            organization=self.org, currency__code='USD',
        ).update(is_primary=True, exchange_rate=Decimal('1'))
        OrganizationCurrency.objects.filter(
            organization=self.org, currency__code='CDF',
        ).update(exchange_rate=Decimal('0.000434782609'))

        from django.core.cache import cache
        cache.clear()

        self.program.points_calculation_type = (
            LoyaltyProgram.PointsCalculationType.PERCENTAGE
        )
        self.program.points_percentage = Decimal('1.00')
        self.program.point_value = Decimal('1.00')
        self.program.min_points_to_redeem = 1
        self.program.save()

    def _sell(self, unit_price):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': unit_price,
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp

    def test_58_usd_credite_zero_virgule_cinquante_huit(self):
        resp = self._sell('58.00')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, Decimal('0.58'))
        self.assertEqual(resp.data['loyalty_points_earned'], 0.58)

    def test_396_usd_ne_perd_plus_la_fraction(self):
        resp = self._sell('396.00')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, Decimal('3.96'))
        self.assertEqual(resp.data['loyalty_points_earned'], 3.96)

    def test_les_fractions_s_additionnent(self):
        for _ in range(4):
            self._sell('58.00')

        self.loyalty.refresh_from_db()
        # 4 x 0,58 = 2,32 - là où l'ancien calcul laissait le compteur à zéro.
        self.assertEqual(self.loyalty.current_points, Decimal('2.32'))
        self.assertEqual(self.loyalty.total_points_earned, Decimal('2.32'))

    def test_un_solde_entier_reste_entier_a_l_affichage(self):
        resp = self._sell('100.00')

        self.assertEqual(resp.data['loyalty_points_earned'], 1)
        self.assertIsInstance(resp.data['loyalty_points_earned'], int)

    def test_le_solde_fractionnaire_est_utilisable(self):
        """Ce qui est gagné doit pouvoir être dépensé, fraction comprise."""
        self._sell('396.00')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, Decimal('3.96'))

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'points_used': '3.96',
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '46.04',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # 3,96 points x 1 USD = 3,96 USD de remise sur 50.
        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('3.96')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('46.04'))
        self.assertEqual(resp.data['loyalty_points_used'], 3.96)

    def test_le_bareme_par_tranches_garde_ses_paliers(self):
        """
        `FIXED_PER_AMOUNT` dit « X points pour CHAQUE Y dépensé » : une tranche
        entamée ne compte pas. Le rendre continu relèverait tous les barèmes
        existants sans que le marchand l'ait demandé.
        """
        self.program.points_calculation_type = (
            LoyaltyProgram.PointsCalculationType.FIXED_PER_AMOUNT
        )
        self.program.points_per_unit = 1
        self.program.amount_per_unit = Decimal('10.00')
        self.program.save()

        self._sell('25.00')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, Decimal('2.00'))
