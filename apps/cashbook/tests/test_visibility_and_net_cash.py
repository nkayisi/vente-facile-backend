"""
Tests : visibilité par rôle (cashbook) + caisse nette par session +
permissions caissier + reset password admin + rapport par utilisateur.

Règle de visibilité (données financières/opérationnelles) :
- owner → tout ; caissier → ses propres enregistrements ; gérant/magasinier →
  leurs entrepôts assignés.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import Expense, ExpenseCategory, CashMovement
from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.models import RegisterSession

from apps.sales.tests._helpers import make_org_with_users, make_cash_payment_method


def _make_expense(org, category, creator, warehouse, ref):
    return Expense.objects.create(
        organization=org,
        reference=ref,
        category=category,
        description=f"Dépense {ref}",
        amount=Decimal('10.00'),
        expense_date=timezone.now().date(),
        warehouse=warehouse,
        created_by=creator,
        status='approved',
    )


class ExpenseVisibilityTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.cat = ExpenseCategory.objects.create(
            organization=self.org, name='Divers', code='DIV'
        )
        self.wh2 = Warehouse.objects.create(
            organization=self.org, branch=self.branch, name='WH2', code='WH2'
        )
        self.e_a = _make_expense(self.org, self.cat, self.cashier_a, self.warehouse, 'E-A')
        self.e_b = _make_expense(self.org, self.cat, self.cashier_b, self.warehouse, 'E-B')
        self.e_org = _make_expense(self.org, self.cat, self.manager, None, 'E-ORG')
        self.e_wh2 = _make_expense(self.org, self.cat, self.manager, self.wh2, 'E-WH2')

    def _list_ids(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get('/api/v1/expenses/', HTTP_X_ORGANIZATION_ID=str(self.org.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return {e['id'] for e in resp.data['results']}

    def test_owner_sees_all(self):
        self.assertEqual(
            self._list_ids(self.owner),
            {str(self.e_a.id), str(self.e_b.id), str(self.e_org.id), str(self.e_wh2.id)},
        )

    def test_manager_sees_only_assigned_warehouse(self):
        # Gérant assigné à l'entrepôt principal : voit E-A et E-B, pas l'org-level ni WH2.
        self.assertEqual(self._list_ids(self.manager), {str(self.e_a.id), str(self.e_b.id)})

    def test_cashier_sees_only_own(self):
        self.assertEqual(self._list_ids(self.cashier_a), {str(self.e_a.id)})


class CashMovementVisibilityTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        def mk(creator, ref, direction='in', mtype='other_in'):
            return CashMovement.objects.create(
                organization=self.org, reference=ref, direction=direction,
                movement_type=mtype, amount=Decimal('20.00'),
                description=f"Mvt {ref}", movement_date=timezone.now(),
                created_by=creator,
            )

        self.m_a = mk(self.cashier_a, 'M-A')
        self.m_b = mk(self.cashier_b, 'M-B')
        self.m_mgr = mk(self.manager, 'M-MGR')

    def _list_ids(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get('/api/v1/cash-movements/', HTTP_X_ORGANIZATION_ID=str(self.org.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return {m['id'] for m in resp.data['results']}

    def test_owner_sees_all(self):
        self.assertEqual(
            self._list_ids(self.owner),
            {str(self.m_a.id), str(self.m_b.id), str(self.m_mgr.id)},
        )

    def test_cashier_sees_only_own(self):
        self.assertEqual(self._list_ids(self.cashier_a), {str(self.m_a.id)})


class NetCashCloseTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.cash_pm = make_cash_payment_method(self.org)

    def _open_session(self):
        return RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('50.00'),
            status='open',
        )

    def _cash_out(self, session, amount, pm, ref):
        return CashMovement.objects.create(
            organization=self.org, reference=ref, direction='out',
            movement_type='expense', amount=Decimal(amount),
            description='dépense', movement_date=timezone.now(),
            session=session, payment_method=pm, created_by=self.cashier_a,
        )

    def _close(self):
        self.client.force_authenticate(user=self.cashier_a)
        session = RegisterSession.objects.filter(register=self.register, status='open').first()
        return self.client.post(
            f'/api/v1/register-sessions/{session.id}/close/',
            {}, format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def test_cash_expense_deducted_from_expected_balance(self):
        session = self._open_session()
        self._cash_out(session, '30.00', self.cash_pm, 'CM-CASH')
        resp = self._close()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        # 50 (ouverture) − 30 (dépense espèces) = 20
        self.assertEqual(Decimal(resp.data['expected_balance']), Decimal('20.00'))

    def test_non_cash_expense_not_deducted(self):
        from apps.sales.models import PaymentMethod
        mobile = PaymentMethod.objects.create(
            organization=self.org, name='Mobile', code='MM',
            method_type='mobile_money', is_active=True,
        )
        session = self._open_session()
        self._cash_out(session, '40.00', mobile, 'CM-MM')
        resp = self._close()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        # Dépense mobile money : ne touche pas le tiroir → reste 50.
        self.assertEqual(Decimal(resp.data['expected_balance']), Decimal('50.00'))


class CashierPermissionsTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

    def test_cashier_can_create_cash_movement(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/cash-movements/',
            {
                'direction': 'in', 'movement_type': 'other_in',
                'amount': '25.00', 'description': 'Apport',
                'movement_date': timezone.now().isoformat(),
            },
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

    def test_cashier_can_access_daily_report(self):
        self.client.force_authenticate(user=self.cashier_a)
        today = timezone.now().date().strftime('%Y-%m-%d')
        resp = self.client.get(
            f'/api/v1/cash-movements/daily-report/?date={today}',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)


class ResetPasswordTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.cashier_a_membership = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org
        )

    def test_owner_resets_cashier_password(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f'/api/v1/memberships/{self.cashier_a_membership.id}/reset-password/',
            {'new_password': 'Zx9kLm28qP'},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.cashier_a.refresh_from_db()
        self.assertTrue(self.cashier_a.check_password('Zx9kLm28qP'))

    def test_cashier_cannot_reset_password(self):
        self.client.force_authenticate(user=self.cashier_b)
        resp = self.client.post(
            f'/api/v1/memberships/{self.cashier_a_membership.id}/reset-password/',
            {'new_password': 'Zx9kLm28qP'},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.content)


class UserActivityReportTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        CashMovement.objects.create(
            organization=self.org, reference='UA-1', direction='in',
            movement_type='other_in', amount=Decimal('15.00'),
            description='apport', movement_date=timezone.now(),
            created_by=self.cashier_a,
        )

    def test_owner_gets_user_activity(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(
            f'/api/v1/reports/statistics/user_activity/?user={self.cashier_a.id}&period=month',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['user']['id'], str(self.cashier_a.id))
        self.assertEqual(Decimal(str(resp.data['cash']['cash_in'])), Decimal('15.00'))

    def test_user_param_required(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(
            '/api/v1/reports/statistics/user_activity/?period=month',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
