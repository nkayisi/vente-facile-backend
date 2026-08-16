"""
Helpers partagés pour les tests de l'app sales.
Évite la duplication entre test_sessions_and_visibility.py et test_payment_flows.py.
"""
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership, Branch
from apps.users.models import User
from apps.inventory.models import Warehouse
from apps.sales.models import Register, PaymentMethod


def make_user(email, first='F', last='L'):
    return User.objects.create_user(
        email=email, password='pw12345!', first_name=first, last_name=last,
    )


def make_org_with_users():
    """Crée une organisation avec owner, manager, et deux cashiers."""
    owner = make_user('owner@vf.test', 'Own', 'Er')
    manager = make_user('manager@vf.test', 'Man', 'Ager')
    cashier_a = make_user('a@vf.test', 'Cash', 'A')
    cashier_b = make_user('b@vf.test', 'Cash', 'B')

    org = Organization.objects.create(name='Test Org', slug='test-org')
    branch = Branch.objects.create(organization=org, name='Main', code='MAIN', is_main=True)
    warehouse = Warehouse.objects.create(
        organization=org, branch=branch,
        name='Main WH', code='MAIN-WH', is_default=True,
    )
    register = Register.objects.create(
        organization=org, branch=branch, warehouse=warehouse,
        name='Caisse 1', code='C1',
    )

    for u, role in [
        (owner, OrganizationMembership.Role.OWNER),
        (manager, OrganizationMembership.Role.MANAGER),
        (cashier_a, OrganizationMembership.Role.CASHIER),
        (cashier_b, OrganizationMembership.Role.CASHIER),
    ]:
        m = OrganizationMembership.objects.create(
            user=u, organization=org, role=role, is_active=True,
        )
        # Donner accès au warehouse principal : sinon WarehouseScopedQuerysetMixin
        # filtre toutes les ventes/sessions (sauf pour les owners qui ont scope global).
        if hasattr(m, 'assigned_warehouses') and role != OrganizationMembership.Role.OWNER:
            m.assigned_warehouses.add(warehouse)

    # Abonnement actif : sinon `HasActiveSubscription` rejette toutes les
    # requêtes en test (DEBUG=False par défaut dans le test runner Django).
    from apps.subscriptions.models import Plan, Subscription
    from apps.settings.models import Currency
    cdf, _ = Currency.objects.get_or_create(
        code='CDF',
        defaults={'name': 'Franc Congolais', 'symbol': 'FC'},
    )
    plan, _ = Plan.objects.get_or_create(
        code='TEST',
        defaults={
            'name': 'Plan Test',
            'price_monthly': Decimal('0'),
            'is_active': True,
            'currency': cdf,
        },
    )
    Subscription.objects.create(
        organization=org,
        plan=plan,
        status=Subscription.Status.ACTIVE,
        current_period_start=timezone.now(),
        current_period_end=timezone.now() + timedelta(days=30),
    )

    return {
        'owner': owner, 'manager': manager,
        'cashier_a': cashier_a, 'cashier_b': cashier_b,
        'org': org, 'branch': branch,
        'warehouse': warehouse, 'register': register,
    }


def make_cash_payment_method(org):
    """Méthode de paiement cash par défaut pour les tests."""
    return PaymentMethod.objects.create(
        organization=org,
        name='Espèces',
        code='CASH',
        method_type='cash',
        is_active=True,
        is_default=True,
    )
