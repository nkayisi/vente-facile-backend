"""
ViewSets DRF pour l'app Organizations.
"""
import secrets
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from datetime import timedelta

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin
from apps.core.api_permissions import (
    IsTenantMember, IsTenantAdmin, IsTenantOwner, IsTenantManager,
    HasActiveSubscription, HasPermission, _get_membership
)
from apps.core.services import OrganizationService, PermissionService
from apps.subscriptions.services import SubscriptionService
from rest_framework.exceptions import ValidationError
from .models import Organization, OrganizationMembership, Branch, OrganizationInvitation
from .serializers import (
    OrganizationListSerializer, OrganizationDetailSerializer,
    OrganizationCreateSerializer, OrganizationUpdateSerializer,
    OrganizationMembershipSerializer, MembershipCreateSerializer,
    MemberCreateWithUserSerializer, MembershipUpdateSerializer,
    MembershipPermissionsSerializer,
    BranchListSerializer, BranchDetailSerializer, BranchCreateSerializer,
    OrganizationInvitationSerializer, InvitationCreateSerializer
)


# =============================================================================
# ORGANIZATION VIEWSET
# =============================================================================

def _dashboard_top_product(row):
    """
    Ligne « produit le plus vendu » du tableau de bord, quantité ventilée.

    Le partage vient des contenants réellement facturés, non d'une division du
    total : 5 casiers plus 120 bouteilles ne se relisent pas « 10 casiers », et
    le facteur d'un produit peut avoir changé depuis la vente.
    """
    from decimal import Decimal

    from apps.inventory.packaging import PackagingProfile, PackagingService

    profile = PackagingProfile.from_values(row)
    factor = PackagingService.factor(profile)
    quantity = Decimal(row['quantity_sold'] or 0)
    packages = Decimal(row.get('packages_sold') or 0)
    packaged_units = Decimal(row.get('packaged_units') or 0)

    if factor is not None and packages > 0:
        loose = max(Decimal('0'), quantity - packaged_units)
        display = PackagingService.format_split(profile, packages, loose)
    else:
        display = PackagingService.format_quantity(profile, quantity)

    return {
        'id': str(row['product__id']),
        'name': row['product__name'],
        'sku': row['product__sku'],
        'quantity': quantity,
        'quantity_display': display,
        'packaging_factor': factor,
        'revenue': str(row['total_revenue']),
    }


class OrganizationViewSet(viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des organisations.
    
    Endpoints:
    - GET /organizations/ : Liste des organisations de l'utilisateur
    - POST /organizations/ : Créer une organisation
    - GET /organizations/{id}/ : Détail d'une organisation
    - PUT/PATCH /organizations/{id}/ : Modifier une organisation
    - DELETE /organizations/{id}/ : Supprimer une organisation
    - POST /organizations/{id}/switch/ : Changer d'organisation active
    """
    
    queryset = Organization.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']

    def get_queryset(self):
        """Retourne uniquement les organisations de l'utilisateur."""
        return Organization.objects.filter(
            memberships__user=self.request.user,
            memberships__is_active=True,
            is_deleted=False
        ).distinct()

    def get_serializer_class(self):
        if self.action == 'list':
            return OrganizationListSerializer
        elif self.action == 'create':
            return OrganizationCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return OrganizationUpdateSerializer
        return OrganizationDetailSerializer

    def get_permissions(self):
        if self.action in ['update', 'partial_update']:
            return [IsAuthenticated(), IsTenantAdmin()]
        elif self.action == 'destroy':
            return [IsAuthenticated(), IsTenantOwner()]
        return super().get_permissions()

    def perform_create(self, serializer):
        """Crée une organisation avec l'utilisateur comme owner."""
        data = serializer.validated_data
        organization = OrganizationService.create_organization(
            user=self.request.user,
            name=data['name'],
            business_type=data.get('business_type', 'boutique'),
            **{k: v for k, v in data.items() if k not in ['name', 'business_type']}
        )
        serializer.instance = organization

    def perform_destroy(self, instance):
        """Soft delete de l'organisation."""
        instance.soft_delete()

    @action(detail=True, methods=['post'])
    def switch(self, request, pk=None):
        """Change l'organisation active de l'utilisateur."""
        organization = self.get_object()
        
        # Vérifier que l'utilisateur est membre
        if not request.user.memberships.filter(
            organization=organization,
            is_active=True
        ).exists():
            return Response(
                {'error': 'Vous n\'êtes pas membre de cette organisation'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        request.user.active_organization = organization
        request.user.save(update_fields=['active_organization'])
        
        return Response({
            'message': 'Organisation active changée',
            'organization': OrganizationDetailSerializer(organization).data
        })

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Statistiques de l'organisation."""
        organization = self.get_object()
        
        from apps.products.models import Product
        from apps.contacts.models import Customer, Supplier
        from apps.sales.models import Sale
        from django.db.models import Sum, Count
        
        today = timezone.now().date()
        month_start = today.replace(day=1)
        
        stats = {
            'products': Product.objects.filter(
                organization=organization, is_deleted=False
            ).count(),
            'customers': Customer.objects.filter(
                organization=organization, is_deleted=False
            ).count(),
            'suppliers': Supplier.objects.filter(
                organization=organization, is_deleted=False
            ).count(),
            'members': organization.memberships.filter(is_active=True).count(),
            'branches': organization.branches.filter(is_deleted=False).count(),
            'sales_today': Sale.objects.filter(
                organization=organization,
                status='completed',
                sale_date__date=today
            ).aggregate(
                count=Count('id'),
                total=Sum('total')
            ),
            'sales_month': Sale.objects.filter(
                organization=organization,
                status='completed',
                sale_date__date__gte=month_start
            ).aggregate(
                count=Count('id'),
                total=Sum('total')
            )
        }
        
        return Response(stats)

    @action(detail=True, methods=['get'])
    def dashboard(self, request, pk=None):
        """Statistiques complètes pour le dashboard avec données d'évolution."""
        from django.core.cache import cache
        from django.db.models import Sum, Count, Avg, F, Case, When, Value, DecimalField, ExpressionWrapper
        from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, Coalesce
        from decimal import Decimal
        from apps.products.models import Product
        from apps.contacts.models import Customer, Supplier
        from apps.sales.models import Sale, SaleItem, Payment
        from apps.inventory.models import Stock

        organization = self.get_object()
        period = request.query_params.get('period', 'month')  # day, week, month, year

        # Cache court (60s) du payload dashboard par (org, période) : le dashboard
        # est rafraîchi souvent et ces agrégations sont lourdes. Une péremption de
        # 60s est acceptable pour des statistiques.
        cache_key = f"vf:dash:{organization.id}:{period}"
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            return Response(cached_payload)

        today = timezone.now().date()
        
        # Définir les périodes
        if period == 'day':
            current_start = today
            previous_start = today - timedelta(days=1)
            previous_end = today - timedelta(days=1)
            chart_days = 24  # heures
        elif period == 'week':
            current_start = today - timedelta(days=6)
            previous_start = today - timedelta(days=13)
            previous_end = today - timedelta(days=7)
            chart_days = 7
        elif period == 'year':
            current_start = today.replace(month=1, day=1)
            previous_start = (today.replace(month=1, day=1) - timedelta(days=365)).replace(month=1, day=1)
            previous_end = today.replace(month=1, day=1) - timedelta(days=1)
            chart_days = 12  # mois
        else:  # month
            current_start = today.replace(day=1)
            if today.month == 1:
                previous_start = today.replace(year=today.year-1, month=12, day=1)
            else:
                previous_start = today.replace(month=today.month-1, day=1)
            previous_end = current_start - timedelta(days=1)
            chart_days = 30
        
        # Ventes période actuelle
        current_sales = Sale.objects.filter(
            organization=organization,
            status='completed',
            is_deleted=False,
            sale_date__date__gte=current_start,
            sale_date__date__lte=today
        )
        
        current_stats = current_sales.aggregate(
            total_sales=Sum('total'),
            total_cost=Sum(F('items__cost_price') * F('items__quantity')),
            count=Count('id'),
            units_sold=Sum('items__quantity')
        )
        
        # Ventes période précédente
        previous_sales = Sale.objects.filter(
            organization=organization,
            status='completed',
            is_deleted=False,
            sale_date__date__gte=previous_start,
            sale_date__date__lte=previous_end
        )
        
        previous_stats = previous_sales.aggregate(
            total_sales=Sum('total'),
            count=Count('id'),
            units_sold=Sum('items__quantity')
        )
        
        # Calcul des variations
        def calc_variation(current, previous):
            if not previous or previous == 0:
                return 100 if current else 0
            return round(((current - previous) / previous) * 100, 1)
        
        current_total = current_stats['total_sales'] or Decimal('0')
        previous_total = previous_stats['total_sales'] or Decimal('0')
        current_count = current_stats['count'] or 0
        previous_count = previous_stats['count'] or 0
        current_units = current_stats['units_sold'] or 0
        previous_units = previous_stats['units_sold'] or 0
        current_cost = current_stats['total_cost'] or Decimal('0')
        gross_profit = current_total - current_cost
        
        # Clients
        total_customers = Customer.objects.filter(
            organization=organization, is_deleted=False
        ).count()
        
        new_customers_current = Customer.objects.filter(
            organization=organization,
            is_deleted=False,
            created_at__date__gte=current_start
        ).count()
        
        new_customers_previous = Customer.objects.filter(
            organization=organization,
            is_deleted=False,
            created_at__date__gte=previous_start,
            created_at__date__lte=previous_end
        ).count()
        
        # Données pour le graphique d'évolution des ventes
        if period == 'year':
            # Grouper par mois
            sales_evolution = current_sales.annotate(
                period=TruncMonth('sale_date')
            ).values('period').annotate(
                total=Sum('total'),
                count=Count('id')
            ).order_by('period')
        elif period == 'week' or period == 'month':
            # Grouper par jour
            sales_evolution = current_sales.annotate(
                period=TruncDate('sale_date')
            ).values('period').annotate(
                total=Sum('total'),
                count=Count('id')
            ).order_by('period')
        else:
            # Jour: grouper par heure (simplifié en jour)
            sales_evolution = current_sales.annotate(
                period=TruncDate('sale_date')
            ).values('period').annotate(
                total=Sum('total'),
                count=Count('id')
            ).order_by('period')
        
        # Formater les données d'évolution
        evolution_data = []
        for item in sales_evolution:
            evolution_data.append({
                'date': item['period'].isoformat() if item['period'] else None,
                'total': str(item['total'] or 0),
                'count': item['count'] or 0
            })
        
        # Top produits vendus. `packages_sold` cumule les contenants réellement
        # facturés : « 10 casiers + 5 bouteilles » se lit tout autrement que
        # « 245 unités » quand il s'agit de décider d'un réassort.
        top_products = SaleItem.objects.filter(
            sale__in=current_sales
        ).values(
            'product__id', 'product__name', 'product__sku',
            'product__selling_mode', 'product__units_per_package',
            'product__unit__name', 'product__packaging_unit__name',
        ).annotate(
            quantity_sold=Sum('quantity'),
            packages_sold=Coalesce(
                Sum('package_quantity'), Decimal('0'), output_field=DecimalField()
            ),
            packaged_units=Coalesce(
                Sum(F('package_quantity') * F('packaging_factor')),
                Decimal('0'), output_field=DecimalField(),
            ),
            total_revenue=Sum('total')
        ).order_by('-quantity_sold')[:10]
        
        # Ventes par méthode de paiement
        by_payment_method = Payment.objects.filter(
            sale__in=current_sales,
            status='completed'
        ).values('payment_method__name').annotate(
            total=Sum('amount'),
            count=Count('id')
        ).order_by('-total')
        
        # Stock bas
        low_stock_count = Stock.objects.filter(
            organization=organization,
            quantity__lte=F('product__reorder_point'),
            product__track_inventory=True,
            product__is_deleted=False
        ).count()
        
        # Valeur totale du stock - agrégée en base (une seule requête) au lieu de
        # charger tous les stocks en mémoire et sommer en Python. Coût unitaire :
        # avg_cost s'il est > 0, sinon le cost_price du produit (0 par défaut).
        unit_cost = Case(
            When(avg_cost__gt=0, then=F('avg_cost')),
            default=Coalesce(F('product__cost_price'), Value(Decimal('0'))),
            output_field=DecimalField(max_digits=20, decimal_places=4),
        )
        stock_value = Stock.objects.filter(
            organization=organization,
            product__is_deleted=False
        ).aggregate(
            v=Sum(
                ExpressionWrapper(
                    F('quantity') * unit_cost,
                    output_field=DecimalField(max_digits=24, decimal_places=4),
                )
            )
        )['v'] or Decimal('0')

        payload = {
            'cards': {
                'total_sales': {
                    'value': str(current_total),
                    'variation': calc_variation(float(current_total), float(previous_total)),
                    'previous': str(previous_total)
                },
                'total_customers': {
                    'value': total_customers,
                    'new_count': new_customers_current,
                    'variation': calc_variation(new_customers_current, new_customers_previous)
                },
                'units_sold': {
                    'value': current_units,
                    'variation': calc_variation(current_units, previous_units),
                    'previous': previous_units
                },
                'gross_profit': {
                    'value': str(gross_profit),
                    'margin': round((float(gross_profit) / float(current_total) * 100), 1) if current_total > 0 else 0
                }
            },
            'charts': {
                'sales_evolution': evolution_data,
                'by_payment_method': [
                    {'name': p['payment_method__name'] or 'Non défini', 'value': str(p['total']), 'count': p['count']}
                    for p in by_payment_method
                ],
                'top_products': [
                    _dashboard_top_product(p) for p in top_products
                ]
            },
            'inventory': {
                'low_stock_count': low_stock_count,
                'stock_value': str(stock_value)
            },
            'period': period,
            'date_range': {
                'start': current_start.isoformat(),
                'end': today.isoformat()
            }
        }

        cache.set(cache_key, payload, timeout=60)
        return Response(payload)


# =============================================================================
# MEMBERSHIP VIEWSET
# =============================================================================

class OrganizationMembershipViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des membres d'organisation.
    
    Hiérarchie des rôles :
    - owner (Admin) peut gérer : manager, stock_keeper, cashier
    - manager (Gérant) peut gérer : stock_keeper, cashier
    - stock_keeper et cashier ne peuvent gérer personne
    
    Endpoints:
    - GET /memberships/ : Liste des membres
    - POST /memberships/ : Ajouter un membre existant (par email)
    - POST /memberships/create_user/ : Créer un nouvel utilisateur + l'ajouter
    - GET /memberships/{id}/ : Détail d'un membre
    - PUT/PATCH /memberships/{id}/ : Modifier le rôle / activer/désactiver
    - DELETE /memberships/{id}/ : Retirer un membre
    """
    
    queryset = OrganizationMembership.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['role', 'is_active']
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    ordering = ['user__email']
    
    select_related_fields = ['user', 'organization', 'invited_by']
    
    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'create': 'users.create',
        'create_user': 'users.create',
        'update': 'users.edit',
        'partial_update': 'users.edit',
        'destroy': 'users.deactivate',
        'manage_permissions': 'users.edit',
        'reset_password': 'users.edit',
    }

    def get_serializer_class(self):
        if self.action == 'create':
            return MembershipCreateSerializer
        elif self.action == 'create_user':
            return MemberCreateWithUserSerializer
        elif self.action in ['update', 'partial_update']:
            return MembershipUpdateSerializer
        return OrganizationMembershipSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.get_organization()
        if organization:
            queryset = queryset.filter(organization=organization).prefetch_related(
                'assigned_warehouses'
            )
        return queryset

    def _check_role_hierarchy(self, request, target_role):
        """
        Vérifie que l'utilisateur courant peut gérer le rôle cible.
        Retourne (ok, error_response).
        """
        membership = _get_membership(request)
        if not membership:
            return False, Response(
                {'detail': "Vous n'êtes pas membre de cette organisation."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        if not membership.can_manage_role(target_role):
            return False, Response(
                {'detail': f"Vous n'avez pas la permission de gérer le rôle '{dict(OrganizationMembership.Role.choices).get(target_role, target_role)}'."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        return True, None

    def create(self, request, *args, **kwargs):
        """Ajoute un membre existant par email."""
        organization = self.get_organization()
        serializer = MembershipCreateSerializer(
            data=request.data,
            context={'organization': organization},
        )
        serializer.is_valid(raise_exception=True)
        
        email = serializer.validated_data['email']
        role = serializer.validated_data['role']
        warehouse_ids = serializer.validated_data['warehouse_ids']
        
        # Vérifier la hiérarchie des rôles
        ok, error = self._check_role_hierarchy(request, role)
        if not ok:
            return error
        
        # Empêcher de créer un owner
        if role == 'owner':
            return Response(
                {'detail': "Impossible de créer un autre administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            SubscriptionService.assert_can_add_user(organization)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        # Chercher l'utilisateur
        from apps.users.models import User
        user = User.objects.filter(email=email).first()
        
        if not user:
            return Response(
                {'detail': "Aucun utilisateur trouvé avec cet email. Utilisez 'Créer un utilisateur' pour créer un nouveau compte."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Vérifier s'il est déjà membre
        existing = OrganizationMembership.objects.filter(
            user=user, organization=organization
        ).first()
        if existing:
            if existing.is_active:
                return Response(
                    {'detail': "Cet utilisateur est déjà membre de l'organisation."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Réactiver le membre
            existing.role = role
            existing.is_active = True
            existing.save()
            PermissionService.assign_role_permissions(user, organization, role)
            OrganizationService.sync_membership_assigned_warehouses(
                existing, warehouse_ids, organization
            )
            return Response(
                OrganizationMembershipSerializer(existing).data,
                status=status.HTTP_200_OK
            )
        
        membership = OrganizationService.add_member(
            organization=organization,
            user=user,
            role=role,
            invited_by=request.user,
            warehouse_ids=warehouse_ids,
        )
        return Response(
            OrganizationMembershipSerializer(membership).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=['post'], url_path='create-user')
    def create_user(self, request):
        """
        Crée un nouvel utilisateur et l'ajoute à l'organisation.
        L'admin peut créer manager, stock_keeper, cashier.
        Le gérant peut créer stock_keeper, cashier uniquement.
        """
        organization = self.get_organization()
        serializer = MemberCreateWithUserSerializer(
            data=request.data,
            context={'organization': organization},
        )
        serializer.is_valid(raise_exception=True)
        
        role = serializer.validated_data['role']
        warehouse_ids = serializer.validated_data['warehouse_ids']
        
        # Vérifier la hiérarchie des rôles
        ok, error = self._check_role_hierarchy(request, role)
        if not ok:
            return error
        
        # Empêcher de créer un owner
        if role == 'owner':
            return Response(
                {'detail': "Impossible de créer un autre administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            SubscriptionService.assert_can_add_user(organization)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        # Créer l'utilisateur
        from apps.users.models import User
        from django.db import transaction
        
        with transaction.atomic():
            user = User.objects.create(
                email=serializer.validated_data['email'],
                first_name=serializer.validated_data['first_name'],
                last_name=serializer.validated_data['last_name'],
                phone=serializer.validated_data.get('phone', ''),
                is_active=True,
            )
            user.set_password(serializer.validated_data['password'])
            user.active_organization = organization
            user.save()
            
            membership = OrganizationService.add_member(
                organization=organization,
                user=user,
                role=role,
                invited_by=request.user,
                warehouse_ids=warehouse_ids,
            )
        
        return Response(
            OrganizationMembershipSerializer(membership).data,
            status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        """Met à jour le rôle d'un membre."""
        membership = self.get_object()
        
        # Empêcher de modifier le owner
        if membership.role == 'owner':
            return Response(
                {'detail': "Impossible de modifier l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Empêcher de se modifier soi-même
        if membership.user == request.user:
            return Response(
                {'detail': "Vous ne pouvez pas modifier votre propre rôle."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        organization = self.get_organization()
        serializer = MembershipUpdateSerializer(
            data=request.data,
            context={
                'organization': organization,
                'membership': membership,
            },
        )
        serializer.is_valid(raise_exception=True)
        
        new_role = serializer.validated_data.get('role', membership.role)
        is_active = serializer.validated_data.get('is_active', membership.is_active)
        warehouse_ids = serializer.validated_data.get('warehouse_ids')
        extra_permissions = serializer.validated_data.get('extra_permissions')
        
        # Vérifier la hiérarchie pour le rôle actuel ET le nouveau rôle
        current_membership = _get_membership(request)
        if not current_membership:
            return Response(
                {'detail': "Vous n'êtes pas membre de cette organisation."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier qu'on peut gérer le rôle actuel du membre
        if not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de modifier cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier qu'on peut attribuer le nouveau rôle
        if new_role != membership.role:
            if not current_membership.can_manage_role(new_role):
                return Response(
                    {'detail': f"Vous n'avez pas la permission d'attribuer le rôle '{dict(OrganizationMembership.Role.choices).get(new_role, new_role)}'."},
                    status=status.HTTP_403_FORBIDDEN
                )
        
        PermissionService.remove_all_permissions(membership.user, membership.organization)
        
        membership.role = new_role
        membership.is_active = is_active
        if extra_permissions is not None:
            membership.extra_permissions = extra_permissions
        membership.save()
        
        if is_active:
            PermissionService.assign_role_permissions(
                membership.user, membership.organization, new_role
            )

        if warehouse_ids is not None:
            OrganizationService.sync_membership_assigned_warehouses(
                membership, warehouse_ids, organization
            )
        
        return Response(OrganizationMembershipSerializer(membership).data)

    @action(detail=True, methods=['post'], url_path='reset-password')
    def reset_password(self, request, pk=None):
        """Réinitialise le mot de passe d'un membre (admin / gérant selon hiérarchie).

        Réservé aux rôles qui peuvent gérer le membre cible (owner → tous ;
        gérant → magasinier/caissier). On ne réinitialise pas l'administrateur
        principal ni son propre compte (utiliser « changer mon mot de passe »).
        """
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        membership = self.get_object()

        if membership.role == OrganizationMembership.Role.OWNER:
            return Response(
                {'detail': "Impossible de réinitialiser le mot de passe de l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if membership.user_id == request.user.id:
            return Response(
                {'detail': "Utilisez « changer mon mot de passe » pour votre propre compte."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        current_membership = _get_membership(request)
        if not current_membership or not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de gérer cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN,
            )

        new_password = (request.data or {}).get('new_password') or ''
        try:
            validate_password(new_password, user=membership.user)
        except DjangoValidationError as exc:
            return Response({'new_password': list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user = membership.user
        user.set_password(new_password)
        user.save(update_fields=['password'])

        # Invalide les refresh tokens en cours pour forcer une reconnexion.
        try:
            from rest_framework_simplejwt.token_blacklist.models import (
                OutstandingToken, BlacklistedToken,
            )
            for token in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=token)
        except Exception:
            pass  # token_blacklist non installé/migré : non bloquant

        from apps.users.models import UserActivity
        UserActivity.objects.create(
            user=request.user,
            organization=membership.organization,
            action=UserActivity.ActionType.UPDATE,
            resource_type='user',
            resource_id=str(user.id),
            details={'event': 'password_reset_by_admin', 'target_user_id': str(user.id)},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        )
        return Response({'detail': 'Mot de passe réinitialisé avec succès.'})

    @action(detail=True, methods=['get', 'patch'], url_path='permissions')
    def manage_permissions(self, request, pk=None):
        """
        GET: Retourne les permissions du membre (rôle, extra, effectives).
        PATCH: Met à jour les permissions additionnelles du membre.
        """
        membership = self.get_object()
        
        if request.method == 'GET':
            return Response({
                'role': membership.role,
                'role_display': membership.get_role_display(),
                'role_permissions': PermissionService.get_role_permissions(membership.role),
                'extra_permissions': membership.extra_permissions or [],
                'effective_permissions': PermissionService.get_effective_permissions(membership),
                'all_permissions': PermissionService.get_all_permissions(),
            })
        
        # PATCH: modifier les permissions additionnelles
        # Empêcher de modifier le owner
        if membership.role == 'owner':
            return Response(
                {'detail': "Impossible de modifier les permissions de l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        current_membership = _get_membership(request)
        if not current_membership:
            return Response(
                {'detail': "Vous n'êtes pas membre de cette organisation."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Seuls owner et manager peuvent modifier les permissions
        if current_membership.role not in ['owner', 'manager']:
            return Response(
                {'detail': "Vous n'avez pas la permission de modifier les permissions."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier qu'on peut gérer le rôle du membre cible
        if not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de modifier cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = MembershipPermissionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        membership.extra_permissions = serializer.validated_data['extra_permissions']
        membership.save(update_fields=['extra_permissions', 'updated_at'])
        
        return Response({
            'role': membership.role,
            'role_display': membership.get_role_display(),
            'role_permissions': PermissionService.get_role_permissions(membership.role),
            'extra_permissions': membership.extra_permissions,
            'effective_permissions': PermissionService.get_effective_permissions(membership),
        })

    def destroy(self, request, *args, **kwargs):
        """Retire un membre de l'organisation."""
        membership = self.get_object()
        
        # Empêcher de retirer le owner
        if membership.role == 'owner':
            return Response(
                {'detail': "Impossible de retirer l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Empêcher de se retirer soi-même
        if membership.user == request.user:
            return Response(
                {'detail': "Vous ne pouvez pas vous retirer vous-même."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Vérifier la hiérarchie
        current_membership = _get_membership(request)
        if current_membership and not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de retirer cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        OrganizationService.remove_member(membership.organization, membership.user)
        
        return Response(status=status.HTTP_204_NO_CONTENT)


# =============================================================================
# BRANCH VIEWSET
# =============================================================================

class BranchViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des branches/succursales.
    
    Endpoints:
    - GET /branches/ : Liste des branches
    - POST /branches/ : Créer une branche
    - GET /branches/{id}/ : Détail d'une branche
    - PUT/PATCH /branches/{id}/ : Modifier une branche
    - DELETE /branches/{id}/ : Supprimer une branche
    """
    
    queryset = Branch.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'is_main']
    search_fields = ['name', 'code', 'city']
    ordering = ['name']
    
    select_related_fields = ['manager']
    
    action_permissions = {
        'list': 'organization.view',
        'retrieve': 'organization.view',
        'create': 'organization.edit',
        'update': 'organization.edit',
        'partial_update': 'organization.edit',
        'destroy': 'organization.edit',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return BranchListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return BranchCreateSerializer
        return BranchDetailSerializer

    def perform_create(self, serializer):
        """Vérifie les limites d'abonnement avant création."""
        organization = self.get_organization()
        SubscriptionService.assert_can_add_branch(organization)
        serializer.save(organization=organization)


# =============================================================================
# INVITATION VIEWSET
# =============================================================================

class OrganizationInvitationViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des invitations.
    
    Endpoints:
    - GET /invitations/ : Liste des invitations
    - POST /invitations/ : Créer une invitation
    - DELETE /invitations/{id}/ : Annuler une invitation
    - POST /invitations/{id}/resend/ : Renvoyer une invitation
    """
    
    queryset = OrganizationInvitation.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status', 'role']
    search_fields = ['email']
    ordering = ['-created_at']
    
    select_related_fields = ['organization', 'invited_by']
    
    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'create': 'users.create',
        'destroy': 'users.deactivate',
        'resend': 'users.create',
    }

    def get_serializer_class(self):
        if self.action == 'create':
            return InvitationCreateSerializer
        return OrganizationInvitationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.get_organization()
        if organization:
            queryset = queryset.filter(organization=organization)
        return queryset

    def create(self, request, *args, **kwargs):
        """Crée une nouvelle invitation."""
        serializer = InvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        organization = self.get_organization()
        email = serializer.validated_data['email']
        role = serializer.validated_data['role']
        
        # Vérifier si déjà membre
        from apps.users.models import User
        user = User.objects.filter(email=email).first()
        if user and organization.memberships.filter(user=user, is_active=True).exists():
            return Response(
                {'error': 'Cet utilisateur est déjà membre'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Vérifier si invitation en cours
        existing = OrganizationInvitation.objects.filter(
            organization=organization,
            email=email,
            status='pending'
        ).first()
        
        if existing:
            return Response(
                {'error': 'Une invitation est déjà en cours pour cet email'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            SubscriptionService.assert_can_add_user(organization)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        
        invitation = OrganizationInvitation.objects.create(
            organization=organization,
            email=email,
            role=role,
            token=secrets.token_urlsafe(32),
            invited_by=request.user,
            expires_at=timezone.now() + timedelta(days=7)
        )

        from apps.core.email_service import send_invitation_email
        send_invitation_email(invitation)

        return Response(
            OrganizationInvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def resend(self, request, pk=None):
        """Renvoie une invitation."""
        invitation = self.get_object()
        
        if invitation.status != 'pending':
            return Response(
                {'error': 'Cette invitation n\'est plus en attente'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Prolonger l'expiration
        invitation.expires_at = timezone.now() + timedelta(days=7)
        invitation.save()

        from apps.core.email_service import send_invitation_email
        send_invitation_email(invitation)

        return Response({'message': 'Invitation renvoyée'})
