"""
Tests des échéances de crédit : filtre « en retard », alertes Celery et
balance âgée des créances.

Avant ce lot, `Sale.due_date` était un champ mort - stocké et exposé, jamais
saisi ni interrogé - et les trois types d'alerte `payment_due`,
`payment_overdue` et `credit_limit` étaient déclarés sans qu'aucune tâche ne
les produise.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status

from apps.notifications.models import Alert
from apps.notifications.tasks import check_customer_payment_due
from apps.sales.models import Sale

from .test_debt_flows import _DebtBaseTest


class _DueDateBaseTest(_DebtBaseTest):
    def _set_due_date(self, sale_id, days_from_today):
        sale = Sale.objects.get(pk=sale_id)
        sale.due_date = timezone.now().date() + timedelta(days=days_from_today)
        sale.save(update_fields=['due_date'])
        return sale


class OverdueFilterTests(_DueDateBaseTest):
    def test_overdue_filter_returns_only_late_invoices(self):
        late = self._create_sale()
        on_time = self._create_sale()
        self._set_due_date(late, -5)
        self._set_due_date(on_time, 10)

        self.client.force_authenticate(user=self.manager)
        resp = self.client.get(
            '/api/v1/sales/?overdue=true', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        references = {row['id'] for row in resp.data['results']}
        self.assertIn(str(late), references)
        self.assertNotIn(str(on_time), references)

    def test_settled_invoice_is_never_overdue(self):
        sale_id = self._create_sale()
        self._set_due_date(sale_id, -5)

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )

        self.client.force_authenticate(user=self.manager)
        resp = self.client.get('/api/v1/sales/?overdue=true', **self._headers())
        self.assertEqual(resp.data['count'], 0)


class PaymentDueAlertTests(_DueDateBaseTest):
    def test_overdue_invoice_raises_an_alert(self):
        sale_id = self._create_sale()
        self._set_due_date(sale_id, -10)

        check_customer_payment_due()

        alert = Alert.objects.get(
            organization=self.org,
            alert_type=Alert.AlertType.PAYMENT_OVERDUE,
            resource_id=sale_id,
        )
        self.assertEqual(alert.data['days_late'], 10)
        # Montant ET devise : jamais un montant nu.
        self.assertEqual(alert.data['amount_due'], '2000.00')
        self.assertEqual(alert.data['currency'], 'CDF')

    def test_upcoming_due_date_raises_a_low_severity_alert(self):
        sale_id = self._create_sale()
        self._set_due_date(sale_id, 1)

        check_customer_payment_due()

        alert = Alert.objects.get(
            organization=self.org,
            alert_type=Alert.AlertType.PAYMENT_DUE,
            resource_id=sale_id,
        )
        self.assertEqual(alert.severity, Alert.Severity.LOW)

    def test_task_is_idempotent(self):
        sale_id = self._create_sale()
        self._set_due_date(sale_id, -3)

        check_customer_payment_due()
        check_customer_payment_due()

        self.assertEqual(
            Alert.objects.filter(
                alert_type=Alert.AlertType.PAYMENT_OVERDUE, resource_id=sale_id,
            ).count(),
            1,
        )

    def test_due_alert_is_resolved_once_the_invoice_falls_due(self):
        """Pas deux alertes concurrentes sur la même facture."""
        sale_id = self._create_sale()
        self._set_due_date(sale_id, 1)
        check_customer_payment_due()

        self._set_due_date(sale_id, -1)
        check_customer_payment_due()

        due_alert = Alert.objects.get(
            alert_type=Alert.AlertType.PAYMENT_DUE, resource_id=sale_id,
        )
        self.assertEqual(due_alert.status, Alert.Status.RESOLVED)
        self.assertTrue(
            Alert.objects.filter(
                alert_type=Alert.AlertType.PAYMENT_OVERDUE,
                resource_id=sale_id,
                status=Alert.Status.ACTIVE,
            ).exists()
        )

    def test_invoice_without_due_date_raises_nothing(self):
        self._create_sale()

        check_customer_payment_due()

        self.assertFalse(
            Alert.objects.filter(
                alert_type__in=[
                    Alert.AlertType.PAYMENT_DUE, Alert.AlertType.PAYMENT_OVERDUE,
                ],
            ).exists()
        )


class CreditLimitAlertTests(_DueDateBaseTest):
    def test_customer_near_the_limit_raises_an_alert(self):
        self.customer.credit_limit = Decimal('2200.00')
        self.customer.save(update_fields=['credit_limit'])
        self._create_sale()  # 2000 sur 2200 = 90 %

        check_customer_payment_due()

        alert = Alert.objects.get(
            alert_type=Alert.AlertType.CREDIT_LIMIT, resource_id=self.customer.id,
        )
        self.assertEqual(alert.data['used_percent'], 90)

    def test_unlimited_customer_raises_nothing(self):
        """`credit_limit = 0` signifie illimité : rien à surveiller."""
        self.customer.credit_limit = Decimal('0.00')
        self.customer.save(update_fields=['credit_limit'])
        self._create_sale()

        check_customer_payment_due()

        self.assertFalse(
            Alert.objects.filter(alert_type=Alert.AlertType.CREDIT_LIMIT).exists()
        )


class ReceivablesReportTests(_DueDateBaseTest):
    def _report(self):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get(
            '/api/v1/reports/statistics/receivables/', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data

    def test_invoices_land_in_the_right_aging_bucket(self):
        self._set_due_date(self._create_sale(), 10)     # pas encore échue
        self._set_due_date(self._create_sale(), -15)    # 1-30 j
        self._set_due_date(self._create_sale(), -100)   # 90+ j

        data = self._report()
        cdf = next(row for row in data['by_currency'] if row['currency'] == 'CDF')

        self.assertEqual(Decimal(cdf['current']), Decimal('2000.00'))
        self.assertEqual(Decimal(cdf['d1_30']), Decimal('2000.00'))
        self.assertEqual(Decimal(cdf['d90_plus']), Decimal('2000.00'))
        self.assertEqual(Decimal(cdf['total']), Decimal('6000.00'))

    def test_currencies_are_never_added_together(self):
        self._create_sale()
        usd_sale = Sale.objects.filter(customer=self.customer).first()
        usd_sale.currency = 'USD'
        usd_sale.save(update_fields=['currency'])

        data = self._report()

        currencies = {row['currency'] for row in data['by_currency']}
        self.assertIn('USD', currencies)
        # Chaque devise a sa propre ligne, jamais un total mélangé.
        for row in data['by_currency']:
            self.assertEqual(
                Decimal(row['total']),
                sum(
                    Decimal(row[b]) for b in data['buckets']
                ),
            )

    def test_debtors_are_listed_with_their_oldest_invoice(self):
        self._set_due_date(self._create_sale(), -40)

        data = self._report()

        self.assertEqual(data['debtor_count'], 1)
        debtor = data['by_customer'][0]
        self.assertEqual(debtor['customer_name'], 'Client Test')
        self.assertEqual(debtor['oldest_days'], 40)
        self.assertEqual(Decimal(debtor['overdue_amount']), Decimal('2000.00'))

    def test_settled_and_cancelled_invoices_are_excluded(self):
        settled = self._create_sale()
        cancelled = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{settled}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )
        self.client.force_authenticate(user=self.manager)
        self.client.post(
            f'/api/v1/sales/{cancelled}/cancel/', {}, format='json', **self._headers(),
        )

        data = self._report()

        self.assertEqual(data['invoice_count'], 0)
        self.assertEqual(Decimal(data['total_primary']), Decimal('0.00'))
