"""
Tests du multi-devise à l'encaissement (contexte RDC).

Couverture :
- Régression mono-devise : vente CDF payée CDF → montants et caisse inchangés.
- Facture en USD payée 100 % en USD.
- Facture en USD réglée en DEUX devises (10 USD + 23 000 CDF) → split correct,
  un mouvement de caisse PAR devise réellement remise.
- Monnaie rendue dans la devise choisie par le caissier.
- Fermeture de session : soldes attendus PAR devise + écart si comptage divergent.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale, Payment
from apps.cashbook.models import CashMovement
from apps.settings.models import Currency, OrganizationCurrency

from ._helpers import make_org_with_users, make_cash_payment_method


class _BaseMultiCurrencyTest(APITestCase):
    """Org avec devise principale CDF + devise secondaire USD (1 USD = 2800 CDF)."""

    RATE_USD = Decimal('2800')

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)

        # Org.currency = 'CDF' par défaut (devise principale).
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0},
        )
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar US', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=cdf, is_primary=True,
            exchange_rate=Decimal('1.000000'), is_active=True,
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=False,
            exchange_rate=self.RATE_USD, is_active=True,
        )
        from apps.settings.services import CurrencyService
        CurrencyService.invalidate_cache(self.org)

        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'),
            status='open',
        )
        self.product = Product.objects.create(
            organization=self.org, name='Article', sku='ART-1',
            cost_price=Decimal('1000.00'), selling_price=Decimal('2000.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('1000.000'), avg_cost=Decimal('1000.00'),
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _sale(self, **overrides):
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'items': [{'product': str(self.product.id), 'quantity': '1', 'unit_price': '2000.00'}],
            'payments': [],
        }
        payload.update(overrides)
        self.client.force_authenticate(user=self.cashier_a)
        return self.client.post('/api/v1/sales/', payload, format='json', **self._headers())


class MonoCurrencyRegressionTests(_BaseMultiCurrencyTest):

    def test_cdf_sale_paid_cdf_unchanged(self):
        resp = self._sale(payments=[{
            'payment_method': str(self.payment_method.id), 'amount': '2000.00',
        }])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.data['status'], 'completed')
        self.assertEqual(Decimal(resp.data['amount_paid']), Decimal('2000.00'))

        sale = Sale.objects.get(pk=resp.data['id'])
        payment = sale.payments.get()
        self.assertEqual(payment.currency, 'CDF')
        self.assertEqual(payment.amount, Decimal('2000.00'))
        self.assertEqual(payment.tendered_amount, Decimal('2000.00'))

        mv = CashMovement.objects.get(sale=sale, direction='in')
        self.assertEqual(mv.currency, 'CDF')
        self.assertEqual(mv.amount, Decimal('2000.00'))


class UsdInvoiceTests(_BaseMultiCurrencyTest):

    def test_usd_invoice_paid_usd(self):
        resp = self._sale(
            currency='USD', exchange_rate='2800.000000',
            items=[{'product': str(self.product.id), 'quantity': '1', 'unit_price': '20.00'}],
            payments=[{
                'payment_method': str(self.payment_method.id),
                'tendered_amount': '20.00', 'currency': 'USD',
            }],
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.data['status'], 'completed')
        sale = Sale.objects.get(pk=resp.data['id'])
        self.assertEqual(sale.currency, 'USD')
        self.assertEqual(sale.total, Decimal('20.00'))
        self.assertEqual(sale.amount_paid, Decimal('20.00'))

        mv = CashMovement.objects.get(sale=sale, direction='in')
        self.assertEqual(mv.currency, 'USD')
        self.assertEqual(mv.amount, Decimal('20.00'))

    def test_usd_invoice_split_two_currencies(self):
        """Facture 20 USD réglée 10 USD + 23 000 CDF (1 USD = 2300 CDF ici)."""
        # Ajuste le taux USD à 2300 pour coller à l'exemple métier.
        oc = OrganizationCurrency.objects.get(organization=self.org, currency__code='USD')
        oc.exchange_rate = Decimal('2300')
        oc.save()
        from apps.settings.services import CurrencyService
        CurrencyService.invalidate_cache(self.org)

        resp = self._sale(
            currency='USD', exchange_rate='2300.000000',
            items=[{'product': str(self.product.id), 'quantity': '1', 'unit_price': '20.00'}],
            payments=[
                {'payment_method': str(self.payment_method.id),
                 'tendered_amount': '10.00', 'currency': 'USD'},
                # Pas de taux fourni → conversion serveur via CurrencyService.
                {'payment_method': str(self.payment_method.id),
                 'tendered_amount': '23000.00', 'currency': 'CDF'},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.data['status'], 'completed')

        sale = Sale.objects.get(pk=resp.data['id'])
        self.assertEqual(sale.amount_paid, Decimal('20.00'))
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.assertEqual(sale.payments.count(), 2)

        # Un mouvement d'entrée PAR devise physiquement remise.
        ins = {m.currency: m for m in CashMovement.objects.filter(sale=sale, direction='in')}
        self.assertEqual(ins['USD'].amount, Decimal('10.00'))
        self.assertEqual(ins['CDF'].amount, Decimal('23000.00'))

    def test_change_in_cashier_chosen_currency(self):
        """Facture 15 USD payée 20 USD, monnaie rendue en CDF (1 USD = 2800)."""
        resp = self._sale(
            currency='USD', exchange_rate='2800.000000', change_currency='CDF',
            items=[{'product': str(self.product.id), 'quantity': '1', 'unit_price': '15.00'}],
            payments=[{
                'payment_method': str(self.payment_method.id),
                'tendered_amount': '20.00', 'currency': 'USD',
            }],
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        sale = Sale.objects.get(pk=resp.data['id'])
        self.assertEqual(sale.status, 'completed')
        # 5 USD de surplus → 5 × 2800 = 14 000 CDF rendus.
        self.assertEqual(sale.change_currency, 'CDF')
        self.assertEqual(sale.change_amount, Decimal('14000.00'))
        change_mv = CashMovement.objects.get(sale=sale, movement_type='change')
        self.assertEqual(change_mv.direction, 'out')
        self.assertEqual(change_mv.currency, 'CDF')
        self.assertEqual(change_mv.amount, Decimal('14000.00'))


class SessionCloseByCurrencyTests(_BaseMultiCurrencyTest):

    def test_close_reconciles_each_currency(self):
        # Vente 1 : 20 USD encaissés en USD.
        r1 = self._sale(
            currency='USD', exchange_rate='2800.000000',
            items=[{'product': str(self.product.id), 'quantity': '1', 'unit_price': '20.00'}],
            payments=[{'payment_method': str(self.payment_method.id),
                       'tendered_amount': '20.00', 'currency': 'USD'}],
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED, r1.content)
        # Vente 2 : 40 000 CDF encaissés en CDF.
        r2 = self._sale(
            items=[{'product': str(self.product.id), 'quantity': '1', 'unit_price': '40000.00'}],
            payments=[{'payment_method': str(self.payment_method.id), 'amount': '40000.00'}],
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED, r2.content)

        # Fermeture avec comptage exact par devise → aucun écart.
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/register-sessions/{self.session.id}/close/',
            {'counted_balances': [
                {'currency': 'USD', 'amount': '20.00'},
                {'currency': 'CDF', 'amount': '40000.00'},
            ]},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        balances = {b['currency']: b for b in resp.data['currency_balances']}
        self.assertEqual(Decimal(balances['USD']['expected_balance']), Decimal('20.00'))
        self.assertEqual(Decimal(balances['USD']['difference']), Decimal('0.00'))
        self.assertEqual(Decimal(balances['CDF']['expected_balance']), Decimal('40000.00'))
        self.assertEqual(Decimal(balances['CDF']['difference']), Decimal('0.00'))

    def test_close_flags_shortfall_requires_note(self):
        self._sale(
            currency='USD', exchange_rate='2800.000000',
            items=[{'product': str(self.product.id), 'quantity': '1', 'unit_price': '20.00'}],
            payments=[{'payment_method': str(self.payment_method.id),
                       'tendered_amount': '20.00', 'currency': 'USD'}],
        )
        self.client.force_authenticate(user=self.cashier_a)
        # Comptage USD inférieur à l'attendu, sans note → refus.
        resp = self.client.post(
            f'/api/v1/register-sessions/{self.session.id}/close/',
            {'counted_balances': [{'currency': 'USD', 'amount': '18.00'}]},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('notes', resp.data)
