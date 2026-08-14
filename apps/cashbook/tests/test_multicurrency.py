"""
Tests du multi-devise pour les ENTRÉES et les DÉPENSES du livre de caisse.

Règles vérifiées :
- Le taux est résolu CÔTÉ SERVEUR depuis `OrganizationCurrency` : le client
  n'envoie que la devise (sinon une dépense en USD serait stockée à taux 1).
- Le tiroir est ventilé PAR DEVISE : les soldes USD et CDF sont indépendants
  et ne s'additionnent jamais.
- Les agrégats comptables (stats dépenses, bénéfice) convertissent en devise
  principale via `montant × exchange_rate`.
- Régression mono-devise : une org sans devise secondaire est inchangée.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement, Expense, ExpenseCategory
from apps.settings.models import Currency, OrganizationCurrency
from apps.settings.services import CurrencyService

from apps.sales.tests._helpers import make_org_with_users


class _BaseCashbookCurrencyTest(APITestCase):
    """Org avec devise principale CDF + devise secondaire USD (1 USD = 2800 CDF)."""

    RATE_USD = Decimal('2800')

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        cdf, _ = Currency.objects.get_or_create(
            code='CDF',
            defaults={'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0},
        )
        usd, _ = Currency.objects.get_or_create(
            code='USD',
            defaults={'name': 'Dollar US', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.get_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1.000000'), 'is_active': True},
        )
        self.usd_oc = OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=False,
            exchange_rate=self.RATE_USD, is_active=True,
        )
        CurrencyService.invalidate_cache(self.org)

        self.category = ExpenseCategory.objects.create(
            organization=self.org, name='Transport', is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _create_expense(self, amount, currency=None, **extra):
        payload = {
            'category': str(self.category.id),
            'description': 'Course taxi',
            'amount': str(amount),
            'expense_date': '2026-08-14',
            **extra,
        }
        if currency is not None:
            payload['currency'] = currency
        return self.client.post(
            '/api/v1/expenses/', payload, format='json', **self._headers()
        )

    def _last_expense(self):
        """`ExpenseCreateSerializer` ne renvoie pas d'`id` : on relit en base."""
        return Expense.objects.filter(organization=self.org).order_by('-created_at').first()


class ExpenseCurrencyTests(_BaseCashbookCurrencyTest):

    def test_expense_in_usd_resolves_rate_server_side(self):
        """Le client n'envoie que la devise : le taux vient d'OrganizationCurrency."""
        resp = self._create_expense('100.00', currency='USD')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        expense = self._last_expense()
        self.assertEqual(expense.currency, 'USD')
        self.assertEqual(expense.exchange_rate, self.RATE_USD)

    def test_expense_without_currency_falls_back_to_primary(self):
        """Jamais de `currency=''` persisté (c'était le bug d'avant)."""
        resp = self._create_expense('5000.00')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        expense = self._last_expense()
        self.assertEqual(expense.currency, 'CDF')
        self.assertEqual(expense.exchange_rate, Decimal('1.000000'))

    def test_expense_with_unconfigured_currency_is_rejected(self):
        resp = self._create_expense('10.00', currency='EUR')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('currency', resp.data)

    def test_changing_currency_on_update_recomputes_rate(self):
        self._create_expense('5000.00')
        expense_id = self._last_expense().id

        resp = self.client.patch(
            f'/api/v1/expenses/{expense_id}/',
            {'currency': 'USD', 'amount': '100.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        expense = Expense.objects.get(id=expense_id)
        self.assertEqual(expense.currency, 'USD')
        self.assertEqual(expense.exchange_rate, self.RATE_USD)

    def test_approved_usd_expense_creates_usd_cash_movement(self):
        """La sortie de caisse hérite de la devise ET du taux de la dépense."""
        self._create_expense('100.00', currency='USD')
        expense_id = self._last_expense().id
        self.client.post(
            f'/api/v1/expenses/{expense_id}/submit/', {}, format='json', **self._headers()
        )
        resp = self.client.post(
            f'/api/v1/expenses/{expense_id}/approve/', {}, format='json', **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        movement = CashMovement.objects.get(expense_id=expense_id)
        self.assertEqual(movement.currency, 'USD')
        self.assertEqual(movement.exchange_rate, self.RATE_USD)
        self.assertEqual(movement.amount, Decimal('100.00'))
        # Le solde USD part de 0 : il ne se mélange pas au CDF.
        self.assertEqual(movement.balance_after, Decimal('-100.00'))


class CashMovementCurrencyTests(_BaseCashbookCurrencyTest):

    def _create_movement(self, amount, currency=None, direction='in',
                         movement_type='fund_in'):
        payload = {
            'direction': direction,
            'movement_type': movement_type,
            'amount': str(amount),
            'description': 'Apport',
            'movement_date': '2026-08-14T10:00:00Z',
        }
        if currency is not None:
            payload['currency'] = currency
        return self.client.post(
            '/api/v1/cash-movements/', payload, format='json', **self._headers()
        )

    def test_manual_entry_in_usd_resolves_rate_and_keeps_balances_separate(self):
        cdf_resp = self._create_movement('46000', currency='CDF')
        self.assertEqual(cdf_resp.status_code, status.HTTP_201_CREATED, cdf_resp.content)

        usd_resp = self._create_movement('40.00', currency='USD')
        self.assertEqual(usd_resp.status_code, status.HTTP_201_CREATED, usd_resp.content)

        # `CashMovementCreateSerializer` ne renvoie pas d'`id` : on relit en base.
        usd_movement = CashMovement.objects.get(organization=self.org, currency='USD')
        self.assertEqual(usd_movement.exchange_rate, self.RATE_USD)
        # 40 USD n'est PAS ajouté aux 46 000 CDF : chaque devise a sa chaîne.
        self.assertEqual(usd_movement.balance_after, Decimal('40.00'))

        cdf_movement = CashMovement.objects.get(organization=self.org, currency='CDF')
        self.assertEqual(cdf_movement.balance_after, Decimal('46000.00'))

    def test_manual_entry_with_unconfigured_currency_is_rejected(self):
        resp = self._create_movement('40.00', currency='EUR')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    def test_balance_endpoint_lists_each_currency(self):
        self._create_movement('46000', currency='CDF')
        self._create_movement('40.00', currency='USD')

        resp = self.client.get('/api/v1/cash-movements/balance/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        by_currency = {r['currency']: r for r in resp.data['by_currency']}
        self.assertEqual(set(by_currency), {'CDF', 'USD'})
        self.assertEqual(Decimal(by_currency['USD']['balance']), Decimal('40.00'))
        self.assertEqual(Decimal(by_currency['CDF']['balance']), Decimal('46000.00'))
        # Le scalaire de compat ne reflète que la devise principale.
        self.assertEqual(Decimal(resp.data['balance']), Decimal('46000.00'))


class ExpenseStatsCurrencyTests(_BaseCashbookCurrencyTest):

    def _paid_expense(self, amount, currency):
        self._create_expense(amount, currency=currency)
        expense_id = self._last_expense().id
        self.client.post(
            f'/api/v1/expenses/{expense_id}/pay/', {}, format='json', **self._headers()
        )
        return expense_id

    def test_stats_split_by_currency_and_converted_total(self):
        self._paid_expense('100.00', 'USD')   # = 280 000 CDF
        self._paid_expense('5000.00', 'CDF')

        resp = self.client.get('/api/v1/expenses/stats/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        by_currency = {r['currency']: r for r in resp.data['by_currency']}
        self.assertEqual(set(by_currency), {'CDF', 'USD'})
        self.assertEqual(Decimal(by_currency['USD']['total']), Decimal('100.00'))
        self.assertEqual(Decimal(by_currency['CDF']['total']), Decimal('5000.00'))

        # 100 × 2800 + 5000 = 285 000, jamais 5100.
        self.assertEqual(Decimal(resp.data['total_primary']), Decimal('285000'))
        self.assertEqual(resp.data['currency'], 'CDF')
        self.assertEqual(resp.data['count'], 2)

    def test_category_total_spent_is_converted_to_primary(self):
        self._paid_expense('100.00', 'USD')

        resp = self.client.get('/api/v1/expense-categories/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        row = next(r for r in resp.data['results'] if r['id'] == str(self.category.id))
        self.assertEqual(Decimal(row['total_spent']), Decimal('280000'))


class MonoCurrencyRegressionTests(APITestCase):
    """Une org sans devise secondaire doit produire exactement les mêmes chiffres."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        cdf, _ = Currency.objects.get_or_create(
            code='CDF',
            defaults={'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0},
        )
        OrganizationCurrency.objects.get_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1.000000'), 'is_active': True},
        )
        CurrencyService.invalidate_cache(self.org)
        self.category = ExpenseCategory.objects.create(
            organization=self.org, name='Loyer', is_active=True,
        )
        self.client.force_authenticate(user=self.owner)

    def test_expense_and_stats_unchanged(self):
        headers = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        resp = self.client.post(
            '/api/v1/expenses/',
            {
                'category': str(self.category.id),
                'description': 'Loyer août',
                'amount': '250000.00',
                'expense_date': '2026-08-14',
            },
            format='json', **headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        expense = Expense.objects.filter(organization=self.org).order_by('-created_at').first()
        self.client.post(
            f'/api/v1/expenses/{expense.id}/pay/', {}, format='json', **headers
        )

        stats = self.client.get('/api/v1/expenses/stats/', **headers)
        self.assertEqual(Decimal(stats.data['total']), Decimal('250000'))
        self.assertEqual(len(stats.data['by_currency']), 1)
        self.assertEqual(stats.data['by_currency'][0]['currency'], 'CDF')


class OtherModelsCurrencyDefaultTests(_BaseCashbookCurrencyTest):
    """Achats, fournisseurs et abonnements : plus aucune devise codée en dur.

    `PurchaseOrder`, `SupplierPayment` et `Supplier` avaient un défaut 'USD'
    sans rapport avec l'établissement ; les modèles de facturation SaaS avaient
    un défaut 'USD' au lieu de suivre la devise du plan souscrit.
    """

    def test_purchase_order_without_currency_uses_org_primary(self):
        from apps.contacts.models import Supplier
        from apps.purchases.models import PurchaseOrder

        supplier = Supplier.objects.create(organization=self.org, name='Fournisseur A')
        order = PurchaseOrder.objects.create(
            organization=self.org,
            reference='PO-TEST-0001',
            supplier=supplier,
            warehouse=self.warehouse,
            order_date='2026-08-14',
            total=Decimal('1000.00'),
        )
        self.assertEqual(order.currency, 'CDF')          # principale, pas 'USD'
        self.assertEqual(order.exchange_rate, Decimal('1.000000'))

    def test_purchase_order_in_usd_snapshots_rate(self):
        from apps.contacts.models import Supplier
        from apps.purchases.models import PurchaseOrder

        supplier = Supplier.objects.create(organization=self.org, name='Fournisseur B')
        order = PurchaseOrder.objects.create(
            organization=self.org,
            reference='PO-TEST-0002',
            supplier=supplier,
            warehouse=self.warehouse,
            order_date='2026-08-14',
            currency='USD',
            total=Decimal('10.00'),
        )
        self.assertEqual(order.currency, 'USD')
        self.assertEqual(order.exchange_rate, self.RATE_USD)

    def test_supplier_without_currency_uses_org_primary(self):
        from apps.contacts.models import Supplier

        supplier = Supplier.objects.create(organization=self.org, name='Fournisseur C')
        self.assertEqual(supplier.currency, 'CDF')

    def test_supplier_payment_without_currency_uses_org_primary(self):
        from apps.contacts.models import Supplier
        from apps.purchases.models import SupplierPayment

        supplier = Supplier.objects.create(organization=self.org, name='Fournisseur D')
        payment = SupplierPayment.objects.create(
            organization=self.org,
            reference='SP-TEST-0001',
            supplier=supplier,
            amount=Decimal('500.00'),
            payment_date='2026-08-14',
        )
        self.assertEqual(payment.currency, 'CDF')
        self.assertEqual(payment.exchange_rate, Decimal('1.000000'))

    def test_subscription_follows_plan_currency_not_org_currency(self):
        """La facturation SaaS suit le plan, pas la devise d'exploitation."""
        from apps.settings.models import Currency
        from apps.subscriptions.models import Plan, Subscription
        from django.utils import timezone

        usd = Currency.objects.get(code='USD')
        plan = Plan.objects.create(
            name='Pro', code='pro-test', price_monthly=Decimal('20.00'),
            price_yearly=Decimal('200.00'), currency=usd, tier=2, is_active=True,
        )
        now = timezone.now()
        subscription = Subscription.objects.create(
            organization=self.org, plan=plan,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal('20.00'),
            current_period_start=now, current_period_end=now,
        )
        # L'org vend en CDF, mais le plan est tarifé en USD.
        self.assertEqual(self.org.currency, 'CDF')
        self.assertEqual(subscription.currency, 'USD')
