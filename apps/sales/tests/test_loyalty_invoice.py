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


class SecondaryCurrencyAccumulationTests(_LoyaltyBaseTest):
    """Le barème est en devise principale : une facture en USD doit être convertie."""

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

    def test_points_fully_pay_an_invoice(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 200}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'completed')
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.loyalty.refresh_from_db()
        # 500 - 200 consommés, + 2 gagnés : la vente soldée attribue ses points
        # sur le total, quel que soit le moyen de règlement.
        self.assertEqual(self.loyalty.current_points, 302)

    def test_points_are_capped_by_amount_due(self):
        """On ne peut pas payer plus que ce qui reste dû."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'points_used': 500}, format='json', **self._headers(),
        )

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('2000.00'))
        self.loyalty.refresh_from_db()
        # 2000 / 10 = 200 points consommés seulement, + 2 gagnés.
        self.assertEqual(self.loyalty.current_points, 302)

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
