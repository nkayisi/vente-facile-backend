"""
Views for the Platform Admin module.
All endpoints require is_staff=True.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncMonth, TruncDate
from datetime import timedelta
from decimal import Decimal

from .permissions import IsPlatformAdmin
from .serializers import (
    AdminOrganizationListSerializer,
    AdminOrganizationDetailSerializer,
    AdminUserListSerializer,
    AdminPlanSerializer,
    AdminPlanCreateUpdateSerializer,
    AdminSubscriptionListSerializer,
    AdminActivateSubscriptionSerializer,
    AdminCreateSubscriptionSerializer,
)
from apps.organizations.models import Organization
from apps.users.models import User
from apps.subscriptions.models import Plan, Subscription, SubscriptionPayment


# =============================================================================
# DASHBOARD
# =============================================================================

class AdminDashboardView(APIView):
    """
    Platform-wide dashboard statistics.
    GET /api/v1/platform-admin/dashboard/
    """
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Organization stats
        total_orgs = Organization.objects.filter(is_deleted=False).count()
        active_orgs = Organization.objects.filter(
            is_deleted=False, is_active=True
        ).count()

        # User stats
        total_users = User.objects.filter(is_active=True).count()
        new_users_month = User.objects.filter(
            date_joined__gte=month_start
        ).count()

        # Revenue stats
        total_revenue = SubscriptionPayment.objects.filter(
            status='completed'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        revenue_month = SubscriptionPayment.objects.filter(
            status='completed',
            paid_at__gte=month_start
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        # Subscriptions by status
        subs_by_status = dict(
            Subscription.objects.values_list('status').annotate(
                count=Count('id')
            ).values_list('status', 'count')
        )

        # Subscriptions by plan
        subs_by_plan = list(
            Subscription.objects.filter(
                status__in=['trial', 'active', 'past_due']
            ).values(
                'plan__name'
            ).annotate(
                count=Count('id')
            ).order_by('-count')
        )

        # Recent organizations
        recent_orgs = Organization.objects.filter(
            is_deleted=False
        ).order_by('-created_at')[:5]
        recent_orgs_data = [
            {
                'id': str(org.id),
                'name': org.name,
                'business_type': org.business_type,
                'created_at': org.created_at,
            }
            for org in recent_orgs
        ]

        # Growth trend (last 6 months - new orgs per month)
        six_months_ago = now - timedelta(days=180)
        growth = list(
            Organization.objects.filter(
                created_at__gte=six_months_ago,
                is_deleted=False
            ).annotate(
                month=TruncMonth('created_at')
            ).values('month').annotate(
                count=Count('id')
            ).order_by('month')
        )
        growth_data = [
            {'month': g['month'].strftime('%Y-%m'), 'count': g['count']}
            for g in growth
        ]

        # New users trend (last 6 months)
        users_trend = list(
            User.objects.filter(
                date_joined__gte=six_months_ago
            ).annotate(
                month=TruncMonth('date_joined')
            ).values('month').annotate(
                count=Count('id')
            ).order_by('month')
        )
        users_trend_data = [
            {'month': u['month'].strftime('%Y-%m'), 'count': u['count']}
            for u in users_trend
        ]

        # Revenue trend (last 6 months)
        revenue_trend = list(
            SubscriptionPayment.objects.filter(
                status='completed',
                paid_at__gte=six_months_ago
            ).annotate(
                month=TruncMonth('paid_at')
            ).values('month').annotate(
                total=Sum('amount')
            ).order_by('month')
        )
        revenue_trend_data = [
            {'month': r['month'].strftime('%Y-%m'), 'total': str(r['total'])}
            for r in revenue_trend
        ]

        return Response({
            'total_organizations': total_orgs,
            'active_organizations': active_orgs,
            'total_users': total_users,
            'new_users_this_month': new_users_month,
            'total_revenue': str(total_revenue),
            'revenue_this_month': str(revenue_month),
            'subscriptions_by_status': subs_by_status,
            'subscriptions_by_plan': subs_by_plan,
            'recent_organizations': recent_orgs_data,
            'growth_trend': growth_data,
            'users_trend': users_trend_data,
            'revenue_trend': revenue_trend_data,
        })


# =============================================================================
# ORGANIZATIONS
# =============================================================================

class AdminOrganizationViewSet(viewsets.ModelViewSet):
    """
    Admin management of all organizations on the platform.
    """
    permission_classes = [IsAuthenticated, IsPlatformAdmin]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['name', 'email', 'phone']
    ordering_fields = ['name', 'created_at']
    ordering = ['-created_at']
    filterset_fields = ['is_active', 'business_type', 'country']

    def get_queryset(self):
        return Organization.objects.filter(is_deleted=False)

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AdminOrganizationDetailSerializer
        return AdminOrganizationListSerializer

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate or deactivate an organization."""
        org = self.get_object()
        org.is_active = not org.is_active
        org.save(update_fields=['is_active', 'updated_at'])
        return Response({
            'message': f"Organisation {'activée' if org.is_active else 'désactivée'}.",
            'is_active': org.is_active,
        })

    @action(detail=True, methods=['post'])
    def activate_subscription(self, request, pk=None):
        """Admin activates/extends subscription for an organization."""
        org = self.get_object()
        serializer = AdminActivateSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan_id = serializer.validated_data['plan_id']
        billing_cycle = serializer.validated_data['billing_cycle']
        notes = serializer.validated_data.get('notes', '')

        try:
            plan = Plan.objects.get(id=plan_id, is_active=True)
        except Plan.DoesNotExist:
            return Response(
                {'error': 'Plan introuvable ou inactif.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from apps.subscriptions.services import SubscriptionService
        subscription = SubscriptionService.activate_subscription(
            organization=org,
            plan=plan,
            billing_cycle=billing_cycle,
            activated_by=request.user,
            notes=notes,
        )

        return Response({
            'message': f"Abonnement {plan.name} activé pour {org.name}.",
            'subscription_id': str(subscription.id),
        })


# =============================================================================
# USERS
# =============================================================================

class AdminUserViewSet(viewsets.ModelViewSet):
    """
    Admin management of all platform users.
    """
    permission_classes = [IsAuthenticated, IsPlatformAdmin]
    serializer_class = AdminUserListSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['email', 'first_name', 'last_name', 'phone']
    ordering_fields = ['email', 'date_joined', 'last_login']
    ordering = ['-date_joined']
    filterset_fields = ['is_active', 'is_staff']

    def get_queryset(self):
        return User.objects.all()

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate or deactivate a user."""
        user = self.get_object()
        if user == request.user:
            return Response(
                {'error': 'Vous ne pouvez pas vous désactiver vous-même.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        user.is_active = not user.is_active
        user.save(update_fields=['is_active'])
        return Response({
            'message': f"Utilisateur {'activé' if user.is_active else 'désactivé'}.",
            'is_active': user.is_active,
        })

    @action(detail=True, methods=['post'])
    def toggle_staff(self, request, pk=None):
        """Toggle staff status for a user."""
        user = self.get_object()
        if user == request.user:
            return Response(
                {'error': 'Vous ne pouvez pas modifier votre propre statut admin.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        user.is_staff = not user.is_staff
        user.save(update_fields=['is_staff'])
        return Response({
            'message': f"Statut admin {'accordé' if user.is_staff else 'retiré'}.",
            'is_staff': user.is_staff,
        })


# =============================================================================
# PLANS
# =============================================================================

class AdminPlanViewSet(viewsets.ModelViewSet):
    """
    Admin CRUD for subscription plans.
    """
    permission_classes = [IsAuthenticated, IsPlatformAdmin]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'code']
    ordering = ['sort_order', 'price_monthly']

    def get_queryset(self):
        return Plan.objects.all().select_related("currency")

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return AdminPlanCreateUpdateSerializer
        return AdminPlanSerializer


# =============================================================================
# SUBSCRIPTIONS
# =============================================================================

class AdminSubscriptionViewSet(viewsets.ModelViewSet):
    """
    Admin management of all subscriptions on the platform.
    Supports listing, creating manually, extending and cancelling.
    """
    permission_classes = [IsAuthenticated, IsPlatformAdmin]
    serializer_class = AdminSubscriptionListSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['organization__name']
    ordering_fields = ['created_at', 'current_period_end']
    ordering = ['-created_at']
    filterset_fields = ['status', 'billing_cycle', 'plan']
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        return Subscription.objects.select_related('organization', 'plan').all()

    def create(self, request, *args, **kwargs):
        """Manually create a subscription for an organization."""
        serializer = AdminCreateSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        organization = serializer.validated_data['organization']
        plan = serializer.validated_data['plan']
        billing_cycle = serializer.validated_data['billing_cycle']
        notes = serializer.validated_data.get('notes', '')

        from apps.subscriptions.services import SubscriptionService
        subscription = SubscriptionService.activate_subscription(
            organization=organization,
            plan=plan,
            billing_cycle=billing_cycle,
            activated_by=request.user,
            notes=notes,
        )

        out = AdminSubscriptionListSerializer(subscription)
        return Response(out.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def extend(self, request, pk=None):
        """Extend a subscription by N days."""
        subscription = self.get_object()
        days = int(request.data.get('days', 30))

        now = timezone.now()
        start = max(now, subscription.current_period_end) if subscription.current_period_end else now
        subscription.current_period_end = start + timedelta(days=days)

        if subscription.status in ['expired', 'past_due']:
            subscription.status = 'active'

        subscription.save()

        return Response({
            'message': f"Abonnement prolongé de {days} jours.",
            'new_end_date': subscription.current_period_end,
        })

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a subscription."""
        subscription = self.get_object()
        subscription.status = 'cancelled'
        subscription.cancelled_at = timezone.now()
        subscription.save()

        return Response({
            'message': 'Abonnement annulé.',
        })
