"""Tests quotas d'abonnement, checkout et paliers."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.organizations.models import Organization, OrganizationMembership
from apps.settings.models import Currency
from apps.subscriptions.models import Plan, Subscription, SubscriptionPayment
from apps.subscriptions.services import SubscriptionService

User = get_user_model()


class SubscriptionQuotaServiceTests(TestCase):
    """Tests unitaires du SubscriptionService (quotas / checkout)."""

    def setUp(self):
        suf = uuid.uuid4().hex[:10]
        self.currency = Currency.objects.filter(code="USD", is_active=True).first()
        if not self.currency:
            self.currency = Currency.objects.create(code="USD", name="US Dollar", symbol="$")

        self.plan_low = Plan.objects.create(
            name="Low Q",
            code=f"quota-low-{suf}",
            description="",
            price_monthly=Decimal("10"),
            price_yearly=Decimal("100"),
            currency=self.currency,
            max_users=5,
            max_branches=2,
            max_products=50,
            tier=2,
            sort_order=1,
        )
        self.plan_high = Plan.objects.create(
            name="High Q",
            code=f"quota-high-{suf}",
            description="",
            price_monthly=Decimal("50"),
            price_yearly=Decimal("500"),
            currency=self.currency,
            max_users=20,
            max_branches=10,
            max_products=500,
            tier=8,
            sort_order=2,
        )

        self.user = User.objects.create_user(email=f"quota-{suf}@test.local", password="pass12345")
        self.org = Organization.objects.create(
            name="Org quota",
            slug=f"org-quota-{suf}",
            business_type="boutique",
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.OWNER,
            is_active=True,
        )

    def test_evaluate_downgrade_blocked_by_floor(self):
        self.org.subscription_floor_tier = 8
        self.org.save(update_fields=["subscription_floor_tier"])
        r = SubscriptionService.evaluate_checkout(self.org, self.plan_low)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "SUBSCRIPTION_DOWNGRADE_FORBIDDEN")

    def test_evaluate_early_renew_same_plan_blocked(self):
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.plan_high,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("50"),
            currency="USD",
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
        )
        r = SubscriptionService.evaluate_checkout(self.org, self.plan_high)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "SUBSCRIPTION_EARLY_RENEW_FORBIDDEN")

    def test_evaluate_upgrade_allowed_while_period_active(self):
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.plan_low,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("10"),
            currency="USD",
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
        )
        r = SubscriptionService.evaluate_checkout(self.org, self.plan_high)
        self.assertTrue(r["allowed"])

    def test_evaluate_same_plan_extend_allowed_while_period_active(self):
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.plan_high,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("50"),
            currency="USD",
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
        )
        r = SubscriptionService.evaluate_checkout(
            self.org,
            self.plan_high,
            mode=SubscriptionService.CHECKOUT_MODE_EXTEND,
        )
        self.assertTrue(r["allowed"])

    def test_complete_pending_moko_payment_legacy_same_plan_infers_extend(self):
        now = timezone.now()
        sub = Subscription.objects.create(
            organization=self.org,
            plan=self.plan_high,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("50"),
            currency="USD",
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
        )
        prev_end = sub.current_period_end

        payment = SubscriptionPayment.objects.create(
            organization=self.org,
            amount=Decimal("50"),
            currency="USD",
            payment_method=SubscriptionPayment.PaymentMethod.MOBILE_MONEY,
            status=SubscriptionPayment.Status.PENDING,
            reference=f"legacy-{uuid.uuid4().hex[:10]}",
            metadata={
                "plan_id": str(self.plan_high.id),
                "billing_cycle": Plan.BillingCycle.MONTHLY,
            },
        )

        result = SubscriptionService.complete_pending_moko_payment(payment)
        payment.refresh_from_db()
        sub.refresh_from_db()

        self.assertFalse(result["already_done"])
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)
        self.assertEqual(payment.metadata.get("checkout_mode"), SubscriptionService.CHECKOUT_MODE_EXTEND)
        self.assertGreater(sub.current_period_end, prev_end)

    def test_complete_pending_moko_payment_legacy_other_plan_keeps_upgrade_rule(self):
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.plan_high,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("50"),
            currency="USD",
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
        )
        payment = SubscriptionPayment.objects.create(
            organization=self.org,
            amount=Decimal("10"),
            currency="USD",
            payment_method=SubscriptionPayment.PaymentMethod.MOBILE_MONEY,
            status=SubscriptionPayment.Status.PENDING,
            reference=f"legacy-low-{uuid.uuid4().hex[:10]}",
            metadata={
                "plan_id": str(self.plan_low.id),
                "billing_cycle": Plan.BillingCycle.MONTHLY,
            },
        )
        with self.assertRaises(ValidationError):
            SubscriptionService.complete_pending_moko_payment(payment)

    def test_activate_subscription_raises_floor_tier(self):
        SubscriptionService.activate_subscription(
            self.org,
            self.plan_high,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            duration_months=1,
        )
        self.org.refresh_from_db()
        self.assertEqual(self.org.subscription_floor_tier, self.plan_high.tier)

    def test_assert_can_add_products_raises_over_limit(self):
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.plan_low,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("10"),
            currency="USD",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        from apps.products.models import Product

        for i in range(self.plan_low.max_products):
            Product.objects.create(
                organization=self.org,
                name=f"P{i}",
                sku=f"SKU{i}",
                slug=f"p{i}",
                selling_price=Decimal("1"),
                cost_price=Decimal("1"),
                created_by=self.user,
            )
        with self.assertRaises(ValidationError):
            SubscriptionService.assert_can_add_products(self.org, 1)

    def test_assert_can_add_warehouse_raises_over_limit(self):
        now = timezone.now()
        plan = Plan.objects.create(
            name="WH cap",
            code=f"quota-wh-{uuid.uuid4().hex[:10]}",
            description="",
            price_monthly=Decimal("10"),
            price_yearly=Decimal("100"),
            currency=self.currency,
            max_users=5,
            max_branches=5,
            max_warehouses=1,
            max_products=50,
            tier=2,
            sort_order=99,
        )
        Subscription.objects.create(
            organization=self.org,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal("10"),
            currency="USD",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        from apps.inventory.models import Warehouse

        Warehouse.objects.create(organization=self.org, name="Entrepôt 1", code="WH1")
        with self.assertRaises(ValidationError):
            SubscriptionService.assert_can_add_warehouse(self.org)
