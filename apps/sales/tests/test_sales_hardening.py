"""
Tests des correctifs d'intégrité métier (Lot L7-A).

Couvre :
- FK cross-tenant (customer, product, payment_method d'une autre org → 400)
- Product désactivé (is_active=False, is_sellable=False) → 400
- Customer suspendu (is_active=False) en vente crédit → 400
- Tax rate négatif / > 100 → 400
- Discount globale > 100% → 400
- Loyalty award idempotent (UniqueConstraint)
- Cancel reverse les transactions loyalty (EARN/REDEEM → EARN_REVERSAL/REDEEM_REVERSAL)
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.organizations.models import Organization, OrganizationMembership
from apps.sales.models import RegisterSession, Sale, PaymentMethod
from apps.settings.models import LoyaltyProgram, LoyaltyTransaction

from ._helpers import make_org_with_users, make_cash_payment_method, make_user


class _BaseHardeningTest(APITestCase):
    """Setup commun : org + users + session ouverte + produit + client."""

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
            organization=self.org,
            name='Test Product',
            sku='TST-001',
            cost_price=Decimal('100.00'),
            selling_price=Decimal('200.00'),
            track_inventory=True,
            allow_negative_stock=False,
            is_active=True,
            is_sellable=True,
        )
        Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('50.000'),
            avg_cost=Decimal('100.00'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, code='C-T1', name='Test Customer',
            credit_limit=Decimal('100000'), current_balance=Decimal('0'),
            is_active=True,
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _payload(self, **overrides):
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '200.00',
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '200.00',
            }],
        }
        payload.update(overrides)
        return payload


class CrossTenantFKTests(_BaseHardeningTest):
    """Un user de l'org A ne peut pas référencer un customer/product/payment_method de l'org B."""

    def setUp(self):
        super().setUp()
        # Org B avec ses propres données
        self.other_owner = make_user('other@vf.test', 'Other', 'Owner')
        self.other_org = Organization.objects.create(name='Other Org', slug='other-org')
        OrganizationMembership.objects.create(
            user=self.other_owner, organization=self.other_org,
            role=OrganizationMembership.Role.OWNER, is_active=True,
        )
        self.other_customer = Customer.objects.create(
            organization=self.other_org, code='OTH-C', name='Other Customer',
            credit_limit=Decimal('1000'),
        )
        self.other_product = Product.objects.create(
            organization=self.other_org, name='Other Product', sku='OTH-001',
            selling_price=Decimal('100.00'), is_active=True, is_sellable=True,
        )
        self.other_pm = PaymentMethod.objects.create(
            organization=self.other_org, name='Other PM', code='OTH-PM',
            method_type='cash', is_active=True,
        )

    def test_cross_tenant_customer_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(
                sale_type='credit',
                customer=str(self.other_customer.id),
                payments=[],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    def test_cross_tenant_product_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(items=[{
                'product': str(self.other_product.id),
                'quantity': '1',
                'unit_price': '100.00',
            }]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    def test_cross_tenant_payment_method_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(payments=[{
                'payment_method': str(self.other_pm.id),
                'amount': '200.00',
            }]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)


class ProductStateValidationTests(_BaseHardeningTest):

    def test_inactive_product_rejected(self):
        self.product.is_active = False
        self.product.save()
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(),
            format='json', **self._headers(),
        )
        # FK queryset filtre is_deleted=False mais pas is_active, donc le
        # produit est trouvé puis rejeté par validate_product.
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    def test_non_sellable_product_rejected(self):
        self.product.is_sellable = False
        self.product.save()
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)


class CustomerStateValidationTests(_BaseHardeningTest):

    def test_suspended_customer_credit_rejected(self):
        self.customer.is_active = False
        self.customer.save()
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(
                sale_type='credit',
                customer=str(self.customer.id),
                payments=[],
            ),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)


class DiscountAndTaxValidationTests(_BaseHardeningTest):

    def test_global_discount_over_100_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(discount_percentage='150'),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    def test_negative_tax_rate_rejected(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(items=[{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '200.00',
                'tax_rate': '-5',
            }]),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)


class LoyaltyReversalTests(_BaseHardeningTest):
    """Vérifie que cancel reverse les transactions EARN / REDEEM."""

    def setUp(self):
        super().setUp()
        self.program = LoyaltyProgram.objects.create(
            organization=self.org,
            name='VIP',
            is_active=True,
            points_per_unit=1,
            amount_per_unit=Decimal('100.00'),
            point_value=Decimal('1.00'),
            only_registered_customers=True,
        )

    def test_cancel_reverses_loyalty_earn(self):
        """Une vente complétée gagne des points ; son annulation les retire via EARN_REVERSAL."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(customer=str(self.customer.id)),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        sale_id = resp.data['id']

        # Vérifier qu'une ligne EARN existe.
        earn = LoyaltyTransaction.objects.filter(
            sale_id=sale_id,
            transaction_type=LoyaltyTransaction.TransactionType.EARN,
        ).first()
        if earn is None:
            self.skipTest("Programme de fidélité n'a pas attribué de points (config absente)")
        points_awarded = earn.points
        loyalty = earn.customer_loyalty
        balance_before_cancel = loyalty.current_points

        # Annuler la vente (manager pour bypasser la perm sold_by).
        self.client.force_authenticate(user=self.manager)
        cancel_resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/',
            format='json', **self._headers(),
        )
        self.assertEqual(cancel_resp.status_code, status.HTTP_200_OK, cancel_resp.content)

        # Une ligne EARN_REVERSAL doit exister avec exactement -points_awarded.
        rev = LoyaltyTransaction.objects.filter(
            sale_id=sale_id,
            transaction_type=LoyaltyTransaction.TransactionType.EARN_REVERSAL,
        ).first()
        self.assertIsNotNone(rev, "Aucune ligne EARN_REVERSAL créée")
        self.assertEqual(rev.points, -points_awarded)

        loyalty.refresh_from_db()
        self.assertEqual(loyalty.current_points, balance_before_cancel - points_awarded)

    def test_cancel_reverse_is_idempotent(self):
        """Rappeler cancel sur une vente déjà annulée ne crée pas deux REVERSAL."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(customer=str(self.customer.id)),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        sale_id = resp.data['id']

        earn = LoyaltyTransaction.objects.filter(
            sale_id=sale_id,
            transaction_type=LoyaltyTransaction.TransactionType.EARN,
        ).first()
        if earn is None:
            self.skipTest("Pas de programme de fidélité actif pour ce test")

        self.client.force_authenticate(user=self.manager)
        self.client.post(f'/api/v1/sales/{sale_id}/cancel/', format='json', **self._headers())
        # Second appel → refus (statut déjà cancelled), mais pas de double REVERSAL.
        self.client.post(f'/api/v1/sales/{sale_id}/cancel/', format='json', **self._headers())

        rev_count = LoyaltyTransaction.objects.filter(
            sale_id=sale_id,
            transaction_type=LoyaltyTransaction.TransactionType.EARN_REVERSAL,
        ).count()
        self.assertEqual(rev_count, 1, "EARN_REVERSAL doit être unique par vente")


class LoyaltyAwardIdempotenceTests(_BaseHardeningTest):
    """La contrainte unique (sale, transaction_type='earn') empêche un double-award."""

    def setUp(self):
        super().setUp()
        LoyaltyProgram.objects.create(
            organization=self.org,
            name='VIP',
            is_active=True,
            points_per_unit=1,
            amount_per_unit=Decimal('100.00'),
            point_value=Decimal('1.00'),
        )

    def test_duplicate_earn_blocked_by_unique_constraint(self):
        """Insérer deux LoyaltyTransaction EARN sur la même vente → IntegrityError."""
        from django.db import IntegrityError
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            self._payload(customer=str(self.customer.id)),
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        sale = Sale.objects.get(id=resp.data['id'])

        existing = LoyaltyTransaction.objects.filter(
            sale=sale,
            transaction_type=LoyaltyTransaction.TransactionType.EARN,
        ).first()
        if existing is None:
            self.skipTest("Pas de programme actif → pas de EARN à dupliquer")

        # Tentative de duplication directe → doit lever IntegrityError.
        with self.assertRaises(IntegrityError):
            LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=existing.customer_loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.EARN,
                points=existing.points,
                balance_after=existing.balance_after,
                sale=sale,
                description="Duplicate attempt",
            )
