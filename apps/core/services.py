"""
Core services for tenant management and permissions.
"""
from django.db import transaction
from guardian.shortcuts import assign_perm, remove_perm, get_perms
from apps.organizations.models import Organization, OrganizationMembership, Branch


class OrganizationService:
    """Service for organization management."""
    
    @staticmethod
    @transaction.atomic
    def create_organization(user, name, business_type, **kwargs):
        """
        Create a new organization with the user as owner.
        Also creates default branch, warehouse, and trial subscription.
        """
        from django.utils.text import slugify
        from django.utils import timezone
        from datetime import timedelta
        from apps.inventory.models import Warehouse
        from apps.sales.models import PaymentMethod
        from apps.subscriptions.models import Plan, Subscription
        
        slug = slugify(name)
        base_slug = slug
        counter = 1
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        organization = Organization.objects.create(
            name=name,
            slug=slug,
            business_type=business_type,
            **kwargs
        )
        
        OrganizationMembership.objects.create(
            user=user,
            organization=organization,
            role=OrganizationMembership.Role.OWNER,
            is_active=True
        )
        
        user.active_organization = organization
        user.save(update_fields=['active_organization'])
        
        # Créer la subscription trial
        trial_plan = Plan.objects.filter(code='trial').first()
        if not trial_plan:
            # Créer un plan trial par défaut si inexistant
            trial_plan = Plan.objects.create(
                name='Essai Gratuit',
                code='trial',
                description='Plan d\'essai gratuit de 14 jours',
                price_monthly=0,
                price_yearly=0,
                max_users=3,
                max_branches=1,
                max_products=100,
                max_monthly_transactions=500,
                storage_limit_mb=100,
                trial_days=14,
                is_active=True
            )
        
        now = timezone.now()
        trial_days = trial_plan.trial_days or 14
        Subscription.objects.create(
            organization=organization,
            plan=trial_plan,
            status=Subscription.Status.TRIAL,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=0,
            currency=organization.currency or 'USD',
            trial_start=now,
            trial_end=now + timedelta(days=trial_days),
            current_period_start=now,
            current_period_end=now + timedelta(days=trial_days)
        )
        
        branch = Branch.objects.create(
            organization=organization,
            name='Principal',
            code='MAIN',
            is_main=True,
            is_active=True
        )
        
        Warehouse.objects.create(
            organization=organization,
            name='Entrepôt Principal',
            code='WH-MAIN',
            branch=branch,
            is_default=True,
            is_active=True
        )
        
        default_methods = [
            ('Espèces', 'CASH', PaymentMethod.MethodType.CASH, True),
            ('Mobile Money', 'MOMO', PaymentMethod.MethodType.MOBILE_MONEY, False),
            ('Carte Bancaire', 'CARD', PaymentMethod.MethodType.CARD, False),
        ]
        for name, code, method_type, is_default in default_methods:
            PaymentMethod.objects.create(
                organization=organization,
                name=name,
                code=code,
                method_type=method_type,
                is_default=is_default,
                is_active=True
            )
        
        PermissionService.assign_owner_permissions(user, organization)
        
        return organization

    @staticmethod
    def add_member(organization, user, role, invited_by=None):
        """Add a user to an organization with a specific role."""
        membership, created = OrganizationMembership.objects.get_or_create(
            user=user,
            organization=organization,
            defaults={
                'role': role,
                'invited_by': invited_by,
                'is_active': True
            }
        )
        
        if not created:
            membership.role = role
            membership.is_active = True
            membership.save()
        
        PermissionService.assign_role_permissions(user, organization, role)
        
        return membership

    @staticmethod
    def remove_member(organization, user):
        """Remove a user from an organization."""
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization
        ).first()
        
        if membership:
            membership.is_active = False
            membership.save()
            
            PermissionService.remove_all_permissions(user, organization)
            
            if user.active_organization == organization:
                other_membership = user.memberships.filter(
                    is_active=True
                ).exclude(organization=organization).first()
                
                user.active_organization = other_membership.organization if other_membership else None
                user.save(update_fields=['active_organization'])


class PermissionService:
    """Service for managing django-guardian permissions."""
    
    ROLE_PERMISSIONS = {
        OrganizationMembership.Role.OWNER: [
            'view_organization', 'change_organization', 'delete_organization',
            'manage_members', 'manage_subscription', 'view_reports',
            'manage_products', 'manage_inventory', 'manage_sales',
            'manage_purchases', 'manage_contacts', 'manage_settings',
        ],
        OrganizationMembership.Role.ADMIN: [
            'view_organization', 'change_organization',
            'manage_members', 'view_reports',
            'manage_products', 'manage_inventory', 'manage_sales',
            'manage_purchases', 'manage_contacts', 'manage_settings',
        ],
        OrganizationMembership.Role.MANAGER: [
            'view_organization', 'view_reports',
            'manage_products', 'manage_inventory', 'manage_sales',
            'manage_purchases', 'manage_contacts',
        ],
        OrganizationMembership.Role.CASHIER: [
            'view_organization',
            'view_products', 'create_sales', 'view_sales',
            'view_contacts',
        ],
        OrganizationMembership.Role.STOCK_KEEPER: [
            'view_organization',
            'view_products', 'manage_inventory',
            'view_purchases',
        ],
        OrganizationMembership.Role.ACCOUNTANT: [
            'view_organization', 'view_reports',
            'view_products', 'view_inventory', 'view_sales',
            'view_purchases', 'view_contacts',
        ],
        OrganizationMembership.Role.VIEWER: [
            'view_organization',
            'view_products', 'view_inventory', 'view_sales',
            'view_purchases', 'view_contacts',
        ],
    }

    @classmethod
    def assign_owner_permissions(cls, user, organization):
        """Assign all owner permissions to a user."""
        cls.assign_role_permissions(
            user, organization, OrganizationMembership.Role.OWNER
        )

    @classmethod
    def assign_role_permissions(cls, user, organization, role):
        """Assign permissions based on role."""
        from django.contrib.auth.models import Permission
        permissions = cls.ROLE_PERMISSIONS.get(role, [])
        for perm in permissions:
            try:
                assign_perm(perm, user, organization)
            except Permission.DoesNotExist:
                # Permission n'existe pas encore, on l'ignore en développement
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Permission '{perm}' does not exist for {organization._meta.model_name}")

    @classmethod
    def remove_all_permissions(cls, user, organization):
        """Remove all permissions from a user for an organization."""
        all_perms = set()
        for perms in cls.ROLE_PERMISSIONS.values():
            all_perms.update(perms)
        
        for perm in all_perms:
            remove_perm(perm, user, organization)

    @staticmethod
    def has_permission(user, permission, organization):
        """Check if user has a specific permission on organization."""
        return permission in get_perms(user, organization)

    @staticmethod
    def get_user_permissions(user, organization):
        """Get all permissions a user has on an organization."""
        return get_perms(user, organization)


class SubscriptionService:
    """Service for subscription management."""
    
    @staticmethod
    def check_limits(organization):
        """Check if organization is within subscription limits."""
        subscription = organization.get_active_subscription()
        if not subscription:
            return {'valid': False, 'reason': 'No active subscription'}
        
        plan = subscription.plan
        
        users_count = organization.memberships.filter(is_active=True).count()
        if users_count > plan.max_users:
            return {'valid': False, 'reason': 'User limit exceeded'}
        
        branches_count = organization.branches.filter(is_active=True).count()
        if branches_count > plan.max_branches:
            return {'valid': False, 'reason': 'Branch limit exceeded'}
        
        if plan.max_products:
            from apps.products.models import Product
            products_count = Product.objects.filter(
                organization=organization,
                is_deleted=False
            ).count()
            if products_count > plan.max_products:
                return {'valid': False, 'reason': 'Product limit exceeded'}
        
        return {'valid': True}

    @staticmethod
    def can_add_user(organization):
        """Check if organization can add more users."""
        subscription = organization.get_active_subscription()
        if not subscription:
            return False
        return subscription.can_add_user()

    @staticmethod
    def can_add_branch(organization):
        """Check if organization can add more branches."""
        subscription = organization.get_active_subscription()
        if not subscription:
            return False
        return subscription.can_add_branch()
