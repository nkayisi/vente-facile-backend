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
        Also creates default branch, warehouse, register, payment methods,
        cashbook categories, currency, and trial subscription — l'utilisateur
        peut immédiatement encaisser sans étape de configuration manuelle.
        """
        from django.utils.text import slugify
        from apps.inventory.models import Warehouse
        from apps.sales.models import PaymentMethod, Register
        from apps.cashbook.models import IncomeCategory, ExpenseCategory
        from apps.subscriptions.services import SubscriptionService
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

        # Créer la subscription trial via le service centralisé
        SubscriptionService.create_trial(organization)

        branch = Branch.objects.create(
            organization=organization,
            name='Principal',
            code='MAIN',
            is_main=True,
            is_active=True
        )

        warehouse = Warehouse.objects.create(
            organization=organization,
            name='Entrepôt Principal',
            code='WH-MAIN',
            branch=branch,
            is_default=True,
            is_active=True
        )

        # Caisse par défaut : sans ce Register, un POS débutant ne peut pas
        # ouvrir de session ni encaisser (Sale.is_pos exige register/session).
        Register.objects.create(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            name='Caisse Principale',
            code='REG-MAIN',
            is_active=True,
        )

        default_methods = [
            ('Espèces', 'CASH', PaymentMethod.MethodType.CASH, True),
            ('Mobile Money', 'MOMO', PaymentMethod.MethodType.MOBILE_MONEY, False),
            ('Carte Bancaire', 'CARD', PaymentMethod.MethodType.CARD, False),
        ]
        for pm_name, code, method_type, is_default in default_methods:
            PaymentMethod.objects.create(
                organization=organization,
                name=pm_name,
                code=code,
                method_type=method_type,
                is_default=is_default,
                is_active=True
            )

        # Catégories d'entrées et de dépenses pour le livre de caisse — sans
        # ces lignes, le user doit aller dans Paramètres > Cashbook avant de
        # pouvoir enregistrer une dépense.
        default_income = [
            ('Vente comptant', 'SALE_CASH'),
            ('Recouvrement crédit', 'CREDIT_COLLECT'),
            ('Apport en capital', 'CAPITAL'),
            ('Autres revenus', 'OTHER_INCOME'),
        ]
        for inc_name, inc_code in default_income:
            IncomeCategory.objects.create(
                organization=organization,
                name=inc_name,
                code=inc_code,
                is_active=True,
            )

        default_expense = [
            ('Loyer', 'RENT'),
            ('Salaires', 'SALARY'),
            ('Transport', 'TRANSPORT'),
            ('Fournitures', 'SUPPLIES'),
            ('Électricité & Eau', 'UTILITIES'),
            ('Autres dépenses', 'OTHER_EXPENSE'),
        ]
        for exp_name, exp_code in default_expense:
            ExpenseCategory.objects.create(
                organization=organization,
                name=exp_name,
                code=exp_code,
                is_active=True,
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
    def sync_membership_assigned_warehouses(membership, warehouse_ids, organization=None):
        """Assigne les entrepôtes M2M selon le rôle (owner → aucune ligne)."""
        from apps.organizations.membership_warehouse_validation import (
            validate_membership_warehouse_ids,
        )

        org = organization or membership.organization
        validated = validate_membership_warehouse_ids(
            membership.role, warehouse_ids, org
        )
        if membership.role == OrganizationMembership.Role.OWNER:
            membership.assigned_warehouses.clear()
        else:
            membership.assigned_warehouses.set(validated)

    @staticmethod
    def add_member(
        organization,
        user,
        role,
        invited_by=None,
        warehouse_ids=None,
    ):
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

        if role == OrganizationMembership.Role.OWNER:
            membership.assigned_warehouses.clear()
        elif warehouse_ids is not None:
            OrganizationService.sync_membership_assigned_warehouses(
                membership, warehouse_ids, organization
            )
        
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
            'sales.view', 'sales.create', 'sales.manage_registers',
            'sales.cancel', 'sales.discount',
            'sales.view_all',
            'payment_methods.view', 'payment_methods.manage',
            'sale_returns.view', 'sale_returns.create', 'sale_returns.approve',
            # Achats / Fournisseurs
            'purchases.view', 'purchases.create', 'purchases.edit', 'purchases.receive',
            'suppliers.view', 'suppliers.create', 'suppliers.edit', 'suppliers.delete',
            # Clients
            'customers.view', 'customers.create', 'customers.edit', 'customers.delete',
            # Rapports
            'reports.view', 'reports.export', 'reports.create', 'reports.delete',
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
            'sales.view', 'sales.create', 'sales.manage_registers',
            'sales.cancel', 'sales.discount',
            'sales.view_all',
            'payment_methods.view',
            'sale_returns.view', 'sale_returns.create', 'sale_returns.approve',
            # Achats / Fournisseurs
            'purchases.view', 'purchases.create', 'purchases.edit', 'purchases.receive',
            'suppliers.view', 'suppliers.create', 'suppliers.edit', 'suppliers.delete',
            # Clients
            'customers.view', 'customers.create', 'customers.edit', 'customers.delete',
            # Rapports
            'reports.view', 'reports.export', 'reports.create', 'reports.delete',
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
        
        # CAISSIER (cashier) : ventes, livre de caisse, clients basique, dashboard
        # Note: pas d'accès aux produits (products.view) ni au stock (stock.view) par défaut
        # Ces permissions peuvent être accordées individuellement via extra_permissions
        OrganizationMembership.Role.CASHIER: [
            # Organisation
            'organization.view',
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

    ALL_PERMISSIONS = None

    @classmethod
    def get_role_permissions(cls, role):
        """Retourne la liste des permissions pour un rôle donné."""
        return cls.ROLE_PERMISSIONS.get(role, [])

    @classmethod
    def get_all_permissions(cls):
        """Retourne la liste de toutes les permissions disponibles dans le système."""
        if cls.ALL_PERMISSIONS is None:
            from apps.core.permissions_catalog import PERMISSION_CATALOG

            cls.ALL_PERMISSIONS = sorted(PERMISSION_CATALOG)
        return cls.ALL_PERMISSIONS

    @classmethod
    def get_effective_permissions(cls, membership):
        """
        Retourne les permissions effectives d'un membership.
        Permissions effectives = permissions du rôle + extra_permissions.
        """
        if not membership:
            return []
        role_perms = set(cls.get_role_permissions(membership.role))
        extra_perms = set(membership.extra_permissions or [])
        return sorted(role_perms | extra_perms)

    @classmethod
    def has_permission(cls, user, organization, permission):
        """
        Vérifie si un utilisateur a une permission spécifique dans une organisation.
        Basé sur le rôle du membre + ses permissions additionnelles.
        """
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True
        ).first()
        if not membership:
            return False
        effective_perms = cls.get_effective_permissions(membership)
        return permission in effective_perms

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
        effective_perms = cls.get_effective_permissions(membership)
        return any(p in effective_perms for p in permissions)

    @classmethod
    def get_user_permissions(cls, user, organization):
        """Retourne toutes les permissions effectives d'un utilisateur dans une organisation."""
        membership = OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True
        ).first()
        if not membership:
            return []
        return cls.get_effective_permissions(membership)

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
