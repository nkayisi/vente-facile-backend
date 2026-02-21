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
        from apps.settings.models import Currency, OrganizationCurrency
        
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
        
        # Créer la devise principale de l'organisation
        currency_code = organization.currency or 'CDF'
        currency_obj = Currency.objects.filter(code=currency_code).first()
        if not currency_obj:
            # Fallback: créer la devise si elle n'existe pas
            currency_defaults = {
                'CDF': ('Franc Congolais', 'FC', 0),
                'USD': ('Dollar Américain', '$', 2),
                'EUR': ('Euro', '€', 2),
            }
            name_c, symbol, decimals = currency_defaults.get(currency_code, (currency_code, currency_code, 2))
            currency_obj = Currency.objects.create(
                code=currency_code,
                name=name_c,
                symbol=symbol,
                decimal_places=decimals,
                is_active=True
            )
        
        OrganizationCurrency.objects.create(
            organization=organization,
            currency=currency_obj,
            is_primary=True,
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
    """
    Service de gestion des permissions basé sur les rôles.
    
    Chaque permission est une chaîne au format 'module.action', ex: 'products.view'.
    Les permissions sont déterminées par le rôle du membre dans l'organisation.
    Pas besoin de django-guardian pour ce système — le rôle suffit.
    """
    
    # =========================================================================
    # PERMISSIONS GRANULAIRES PAR RÔLE
    # =========================================================================
    
    ROLE_PERMISSIONS = {
        # ADMIN (owner) : toutes les permissions
        OrganizationMembership.Role.OWNER: [
            # Organisation & paramètres
            'organization.view', 'organization.edit', 'organization.settings',
            # Utilisateurs
            'users.view', 'users.create', 'users.edit', 'users.deactivate',
            # Abonnement
            'subscription.view', 'subscription.manage',
            # Produits
            'products.view', 'products.create', 'products.edit', 'products.delete',
            'categories.view', 'categories.create', 'categories.edit', 'categories.delete',
            # Stock
            'stock.view', 'stock.adjust',
            'warehouses.view', 'warehouses.create', 'warehouses.edit', 'warehouses.delete',
            'stock_movements.view', 'stock_movements.create',
            'stock_transfers.view', 'stock_transfers.create', 'stock_transfers.ship',
            'stock_transfers.receive', 'stock_transfers.cancel',
            'stock_adjustments.view', 'stock_adjustments.create', 'stock_adjustments.approve',
            # Inventaire
            'inventory.view', 'inventory.create', 'inventory.start',
            'inventory.count', 'inventory.submit', 'inventory.validate', 'inventory.cancel',
            'inventory.print',
            # Ventes
            'sales.view', 'sales.create', 'sales.cancel', 'sales.discount',
            'sales.view_all',
            'payment_methods.view', 'payment_methods.manage',
            'sale_returns.view', 'sale_returns.create', 'sale_returns.approve',
            # Achats / Fournisseurs
            'purchases.view', 'purchases.create', 'purchases.edit', 'purchases.receive',
            'suppliers.view', 'suppliers.create', 'suppliers.edit', 'suppliers.delete',
            # Clients
            'customers.view', 'customers.create', 'customers.edit', 'customers.delete',
            # Rapports
            'reports.view', 'reports.export',
            # Livre de caisse
            'cashbook.view', 'cashbook.view_reports',
            'cashbook.create_movement', 'cashbook.cancel_movement', 'cashbook.delete_movement',
            'cashbook.create_expense', 'cashbook.approve_expense', 'cashbook.delete_expense',
            'cashbook.manage_categories',
            # Dashboard
            'dashboard.view',
            # Paramètres
            'settings.view', 'settings.manage',
        ],
        
        # GÉRANT (manager) : gestion complète sauf abonnement et paramètres critiques
        OrganizationMembership.Role.MANAGER: [
            # Organisation
            'organization.view',
            # Utilisateurs (peut créer magasiniers et caissiers uniquement)
            'users.view', 'users.create', 'users.edit', 'users.deactivate',
            # Produits
            'products.view', 'products.create', 'products.edit', 'products.delete',
            'categories.view', 'categories.create', 'categories.edit', 'categories.delete',
            # Stock
            'stock.view', 'stock.adjust',
            'warehouses.view', 'warehouses.create', 'warehouses.edit',
            'stock_movements.view', 'stock_movements.create',
            'stock_transfers.view', 'stock_transfers.create', 'stock_transfers.ship',
            'stock_transfers.receive', 'stock_transfers.cancel',
            'stock_adjustments.view', 'stock_adjustments.create', 'stock_adjustments.approve',
            # Inventaire
            'inventory.view', 'inventory.create', 'inventory.start',
            'inventory.count', 'inventory.submit', 'inventory.validate', 'inventory.cancel',
            'inventory.print',
            # Ventes
            'sales.view', 'sales.create', 'sales.cancel', 'sales.discount',
            'sales.view_all',
            'payment_methods.view',
            'sale_returns.view', 'sale_returns.create', 'sale_returns.approve',
            # Achats / Fournisseurs
            'purchases.view', 'purchases.create', 'purchases.edit', 'purchases.receive',
            'suppliers.view', 'suppliers.create', 'suppliers.edit', 'suppliers.delete',
            # Clients
            'customers.view', 'customers.create', 'customers.edit', 'customers.delete',
            # Rapports
            'reports.view', 'reports.export',
            # Livre de caisse
            'cashbook.view', 'cashbook.view_reports',
            'cashbook.create_movement', 'cashbook.cancel_movement',
            'cashbook.create_expense', 'cashbook.approve_expense',
            'cashbook.manage_categories',
            # Dashboard
            'dashboard.view',
            # Paramètres (lecture seule)
            'settings.view',
        ],
        
        # MAGASINIER (stock_keeper) : stock, inventaire, réceptions, fournisseurs
        OrganizationMembership.Role.STOCK_KEEPER: [
            # Organisation
            'organization.view',
            # Produits (lecture + modification stock)
            'products.view', 'products.edit',
            'categories.view',
            # Stock (gestion complète)
            'stock.view', 'stock.adjust',
            'warehouses.view',
            'stock_movements.view', 'stock_movements.create',
            'stock_transfers.view', 'stock_transfers.create', 'stock_transfers.ship',
            'stock_transfers.receive', 'stock_transfers.cancel',
            'stock_adjustments.view', 'stock_adjustments.create',
            # Inventaire (peut compter et soumettre, pas valider)
            'inventory.view', 'inventory.create', 'inventory.start',
            'inventory.count', 'inventory.submit',
            'inventory.print',
            # Achats / Fournisseurs (réceptions)
            'purchases.view', 'purchases.receive',
            'suppliers.view', 'suppliers.create', 'suppliers.edit',
            # Clients (lecture seule)
            'customers.view',
            # Livre de caisse (lecture seule)
            'cashbook.view',
            # Dashboard
            'dashboard.view',
        ],
        
        # CAISSIER (cashier) : ventes, consultation produits/prix, clients basique
        OrganizationMembership.Role.CASHIER: [
            # Organisation
            'organization.view',
            # Produits (lecture seule)
            'products.view',
            'categories.view',
            # Stock (lecture seule)
            'stock.view',
            'warehouses.view',
            # Ventes (créer, voir les siennes)
            'sales.view', 'sales.create',
            'payment_methods.view',
            # Clients (créer + voir)
            'customers.view', 'customers.create',
            # Livre de caisse (lecture seule + créer dépenses)
            'cashbook.view', 'cashbook.create_expense',
            # Dashboard
            'dashboard.view',
        ],
    }

    @classmethod
    def get_role_permissions(cls, role):
        """Retourne la liste des permissions pour un rôle donné."""
        return cls.ROLE_PERMISSIONS.get(role, [])

    @classmethod
    def has_permission(cls, user, organization, permission):
        """
        Vérifie si un utilisateur a une permission spécifique dans une organisation.
        Basé uniquement sur le rôle du membre.
        """
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True
        ).first()
        if not membership:
            return False
        return permission in cls.get_role_permissions(membership.role)

    @classmethod
    def has_any_permission(cls, user, organization, permissions):
        """Vérifie si l'utilisateur a au moins une des permissions listées."""
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True
        ).first()
        if not membership:
            return False
        role_perms = cls.get_role_permissions(membership.role)
        return any(p in role_perms for p in permissions)

    @classmethod
    def get_user_permissions(cls, user, organization):
        """Retourne toutes les permissions d'un utilisateur dans une organisation."""
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True
        ).first()
        if not membership:
            return []
        return cls.get_role_permissions(membership.role)

    @classmethod
    def get_user_role(cls, user, organization):
        """Retourne le rôle d'un utilisateur dans une organisation."""
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True
        ).first()
        return membership.role if membership else None

    @classmethod
    def assign_owner_permissions(cls, user, organization):
        """Assign owner guardian permissions (backward compat)."""
        # Guardian permissions are no longer the primary system,
        # but we keep this for backward compatibility
        try:
            for perm in ['view_organization', 'change_organization', 'delete_organization']:
                assign_perm(perm, user, organization)
        except Exception:
            pass

    @classmethod
    def assign_role_permissions(cls, user, organization, role):
        """Assign guardian permissions for backward compat."""
        try:
            assign_perm('view_organization', user, organization)
            if role in ['owner', 'manager']:
                assign_perm('change_organization', user, organization)
        except Exception:
            pass

    @classmethod
    def remove_all_permissions(cls, user, organization):
        """Remove guardian permissions for backward compat."""
        try:
            for perm in ['view_organization', 'change_organization', 'delete_organization']:
                remove_perm(perm, user, organization)
        except Exception:
            pass


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
