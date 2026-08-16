"""
Tests de la gestion de dette client, de bout en bout.

Couvre les dysfonctionnements signalés en production :
- l'acompte n'était pas pris en compte dans la dette ;
- un règlement depuis la fiche client ne touchait aucune facture, si bien que
  le solde du client et la somme des factures dues divergeaient ;
- un surpaiement créditait le client de la monnaie qu'on venait de lui rendre ;
- une vente au détail partiellement payée n'inscrivait aucune dette ;
- les dettes en USD et en CDF s'additionnaient dans un scalaire sans devise.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts import services as contacts_services
from apps.contacts.models import Customer, CustomerBalance, CustomerTransaction
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale
from apps.settings.models import Currency, OrganizationCurrency

from apps.sales.tests._helpers import make_org_with_users, make_cash_payment_method


class _DebtBaseTest(APITestCase):
    """Org + session ouverte + produit en stock + client avec du crédit."""

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
            organization=self.org, name='Client Test', code='C1', phone='0900000000',
            credit_limit=Decimal('1000000'),
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _create_sale(self, *, sale_type='credit', unit_price='2000.00',
                     payments=None, customer=True):
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': sale_type,
            'is_pos': True,
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': payments or [],
        }
        if customer:
            payload['customer'] = str(self.customer.id)
        resp = self.client.post(
            '/api/v1/sales/', payload, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data['id']

    def _balance(self, currency='CDF'):
        return contacts_services.get_balance(self.customer, currency)


class DownPaymentTests(_DebtBaseTest):
    """L'acompte doit être déduit de la dette dès la création."""

    def test_credit_sale_with_down_payment_records_only_the_remainder(self):
        self._create_sale(payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '500.00',
        }])

        self.customer.refresh_from_db()
        self.assertEqual(self._balance(), Decimal('1500.00'))
        self.assertEqual(self.customer.current_balance, Decimal('1500.00'))

        txn = CustomerTransaction.objects.get(
            customer=self.customer,
            transaction_type=CustomerTransaction.TransactionType.CREDIT_SALE,
        )
        self.assertEqual(txn.amount, Decimal('1500.00'))

    def test_partially_paid_retail_sale_also_records_debt(self):
        """Une vente au détail partiellement payée est une dette réelle."""
        self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '800.00',
        }])

        self.assertEqual(self._balance(), Decimal('1200.00'))


class InvoiceSettlementTests(_DebtBaseTest):
    """Un règlement depuis la fiche client doit faire avancer les factures."""

    def test_record_payment_settles_the_invoice(self):
        sale_id = self._create_sale()
        self.assertEqual(self._balance(), Decimal('2000.00'))

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '2000.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'completed')
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.assertEqual(sale.amount_paid, Decimal('2000.00'))
        self.assertEqual(self._balance(), Decimal('0.00'))
        self.assertEqual(resp.data['settled_invoices'], [sale.reference])

    def test_partial_record_payment_advances_the_invoice(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '750.00'}, format='json', **self._headers(),
        )

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'partially_paid')
        self.assertEqual(sale.amount_due, Decimal('1250.00'))
        self.assertEqual(self._balance(), Decimal('1250.00'))

    def test_oldest_invoice_is_settled_first(self):
        first = self._create_sale()
        second = self._create_sale()
        self.assertEqual(self._balance(), Decimal('4000.00'))

        self.client.force_authenticate(user=self.manager)
        self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '2000.00'}, format='json', **self._headers(),
        )

        self.assertEqual(Sale.objects.get(pk=first).status, 'completed')
        self.assertEqual(Sale.objects.get(pk=second).status, 'pending')
        self.assertEqual(self._balance(), Decimal('2000.00'))

    def test_debt_and_open_invoices_never_diverge(self):
        """L'invariant que la production violait."""
        self._create_sale()
        self._create_sale()

        self.client.force_authenticate(user=self.manager)
        for amount in ('500.00', '1200.00', '900.00'):
            self.client.post(
                f'/api/v1/customers/{self.customer.id}/record-payment/',
                {'amount': amount}, format='json', **self._headers(),
            )

        open_due = sum(
            (s.amount_due for s in Sale.objects.filter(
                customer=self.customer,
                status__in=['pending', 'partially_paid'],
            )),
            Decimal('0.00'),
        )
        self.assertEqual(self._balance(), open_due)


class OverpaymentTests(_DebtBaseTest):
    """La monnaie rendue ne doit pas créditer le client une seconde fois."""

    def test_overpayment_does_not_credit_the_change(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2500.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.assertEqual(self._balance(), Decimal('0.00'))
        self.assertEqual(Sale.objects.get(pk=sale_id).change_amount, Decimal('500.00'))


class AdvanceTests(_DebtBaseTest):
    """Une avance sans facture rend le solde créditeur."""

    def test_advance_without_invoice_makes_balance_negative(self):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-advance/',
            {'amount': '3000.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(self._balance(), Decimal('-3000.00'))

    def test_record_payment_surplus_becomes_an_advance(self):
        self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '3000.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['advance_amount'], '1000.00')
        self.assertEqual(self._balance(), Decimal('-1000.00'))


class MultiCurrencyDebtTests(_DebtBaseTest):
    """Deux devises, deux dettes : jamais de somme entre elles."""

    def setUp(self):
        super().setUp()
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'US Dollar', 'symbol': '$'},
        )
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC'},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1'), 'is_active': True},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=usd,
            defaults={'is_primary': False, 'exchange_rate': Decimal('2800'), 'is_active': True},
        )

    def test_usd_and_cdf_debts_are_tracked_separately(self):
        contacts_services.apply_debt(self.customer, Decimal('50.00'), currency='USD')
        contacts_services.apply_debt(self.customer, Decimal('40000.00'), currency='CDF')

        self.assertEqual(self._balance('USD'), Decimal('50.00'))
        self.assertEqual(self._balance('CDF'), Decimal('40000.00'))
        self.assertEqual(CustomerBalance.objects.filter(customer=self.customer).count(), 2)

    def test_primary_balance_is_the_converted_total(self):
        contacts_services.apply_debt(self.customer, Decimal('50.00'), currency='USD')
        contacts_services.apply_debt(self.customer, Decimal('40000.00'), currency='CDF')

        self.customer.refresh_from_db()
        # 50 USD × 2800 = 140 000 CDF, + 40 000 CDF = 180 000 CDF
        self.assertEqual(self.customer.current_balance, Decimal('180000.00'))

    def test_usd_payment_does_not_settle_a_cdf_debt(self):
        contacts_services.apply_debt(self.customer, Decimal('40000.00'), currency='CDF')
        contacts_services.settle_debt(self.customer, Decimal('50.00'), currency='USD')

        self.assertEqual(self._balance('CDF'), Decimal('40000.00'))
        self.assertEqual(self._balance('USD'), Decimal('-50.00'))


class CancelTests(_DebtBaseTest):
    """L'annulation ramène le solde à sa valeur d'avant la vente."""

    def test_cancel_restores_balance(self):
        sale_id = self._create_sale()
        self.assertEqual(self._balance(), Decimal('2000.00'))

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(self._balance(), Decimal('0.00'))

    def test_cancel_after_partial_payment_restores_balance(self):
        sale_id = self._create_sale(payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '500.00',
        }])
        self.assertEqual(self._balance(), Decimal('1500.00'))

        self.client.force_authenticate(user=self.manager)
        self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )
        self.assertEqual(self._balance(), Decimal('0.00'))
