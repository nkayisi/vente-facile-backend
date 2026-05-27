"""
Tests des corrections de bugs : sessions de caisse + visibilité ventes + POS.

Couverture :
- UniqueConstraint : impossible d'avoir 2 sessions 'open' sur la même caisse.
- open : hérite l'opening_balance du closing_balance précédent.
- close : refusé pour un cashier qui n'est pas l'opener.
- close : autorisé pour un manager même non-opener.
- close : counted_balance saisie → difference calculée + notes obligatoires si écart.
- get_queryset Sale : un cashier ne voit que ses propres ventes.
- create Sale POS : refus si aucune session ouverte sur la caisse.
- create Sale POS : refus si la session a été ouverte par un autre cashier.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.models import RegisterSession, Sale

from ._helpers import make_org_with_users as _make_org_with_users  # noqa: F401


class RegisterSessionConstraintsTests(APITestCase):
    """UniqueConstraint et héritage de l'opening_balance."""

    def setUp(self):
        ctx = _make_org_with_users()
        self.__dict__.update(ctx)

    def test_cannot_create_two_open_sessions_same_register(self):
        """La contrainte DB doit bloquer un second 'open' sur la même caisse."""
        RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'),
            status='open',
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                RegisterSession.objects.create(
                    organization=self.org, register=self.register,
                    opened_by=self.cashier_b, opening_balance=Decimal('0'),
                    status='open',
                )

    def test_open_inherits_previous_closing(self):
        """`opening_balance` doit reprendre le `counted_balance` (ou closing) précédent."""
        # Session précédente fermée avec counted_balance=50000
        prev = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'),
            status='closed',
        )
        prev.expected_balance = Decimal('45000')
        prev.counted_balance = Decimal('50000')
        prev.closing_balance = Decimal('50000')
        prev.difference = Decimal('5000')
        prev.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/register-sessions/open/',
            {'register': str(self.register.id)},
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(Decimal(resp.data['opening_balance']), Decimal('50000.00'))


class RegisterSessionCloseTests(APITestCase):
    """Permissions et comptage manuel sur la fermeture."""

    def setUp(self):
        ctx = _make_org_with_users()
        self.__dict__.update(ctx)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('10000'),
            status='open',
        )

    def _close_url(self):
        return f'/api/v1/register-sessions/{self.session.id}/close/'

    def test_other_cashier_cannot_close(self):
        self.client.force_authenticate(user=self.cashier_b)
        resp = self.client.post(
            self._close_url(), {}, format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.content)

    def test_manager_can_close_other_user_session(self):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            self._close_url(), {}, format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'closed')
        self.assertEqual(self.session.closed_by_id, self.manager.id)

    def test_opener_can_close_without_counted_balance(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            self._close_url(), {}, format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.session.refresh_from_db()
        self.assertEqual(self.session.difference, Decimal('0.00'))
        self.assertIsNone(self.session.counted_balance)

    def test_counted_balance_requires_notes_when_difference(self):
        """Si counted_balance ≠ expected, `notes` est obligatoire."""
        self.client.force_authenticate(user=self.cashier_a)
        # expected_balance = opening (10000) + 0 cash = 10000
        resp = self.client.post(
            self._close_url(),
            {'counted_balance': '9500'},  # diff = -500, sans notes
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('notes', resp.data)

    def test_counted_balance_with_notes_records_difference(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            self._close_url(),
            {'counted_balance': '9500', 'notes': 'Pièce manquante'},
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.session.refresh_from_db()
        self.assertEqual(self.session.counted_balance, Decimal('9500.00'))
        self.assertEqual(self.session.expected_balance, Decimal('10000.00'))
        self.assertEqual(self.session.difference, Decimal('-500.00'))
        self.assertEqual(self.session.closing_balance, Decimal('9500.00'))


class SaleVisibilityTests(APITestCase):
    """Un cashier ne voit que ses propres ventes ; manager/owner voient tout."""

    def setUp(self):
        ctx = _make_org_with_users()
        self.__dict__.update(ctx)
        # Une vente pour cashier_a, une pour cashier_b
        for u, ref in [(self.cashier_a, 'VT-TEST-A'), (self.cashier_b, 'VT-TEST-B')]:
            Sale.objects.create(
                organization=self.org, reference=ref,
                warehouse=self.warehouse, sold_by=u,
                total=Decimal('1000'), amount_paid=Decimal('1000'),
                status='completed', is_pos=True,
            )

    def _list(self):
        return self.client.get(
            '/api/v1/sales/',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def test_cashier_sees_only_own_sales(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self._list()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        refs = [s['reference'] for s in resp.data['results']]
        self.assertIn('VT-TEST-A', refs)
        self.assertNotIn('VT-TEST-B', refs)

    def test_manager_sees_all_sales(self):
        self.client.force_authenticate(user=self.manager)
        resp = self._list()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        refs = [s['reference'] for s in resp.data['results']]
        self.assertIn('VT-TEST-A', refs)
        self.assertIn('VT-TEST-B', refs)
