"""
Serializers for the Platform Admin module.
"""
from rest_framework import serializers
from apps.organizations.models import Organization, OrganizationMembership
from apps.users.models import User
from apps.subscriptions.models import Plan, PlanFeature, Subscription, SubscriptionPayment


# =============================================================================
# DASHBOARD
# =============================================================================

class AdminDashboardSerializer(serializers.Serializer):
    """Platform-wide dashboard statistics."""
    total_organizations = serializers.IntegerField()
    active_organizations = serializers.IntegerField()
    total_users = serializers.IntegerField()
    new_users_this_month = serializers.IntegerField()
    total_revenue = serializers.DecimalField(max_digits=15, decimal_places=2)
    revenue_this_month = serializers.DecimalField(max_digits=15, decimal_places=2)
    subscriptions_by_status = serializers.DictField()
    subscriptions_by_plan = serializers.ListField()
    recent_organizations = serializers.ListField()
    growth_trend = serializers.ListField()


# =============================================================================
# ORGANIZATIONS
# =============================================================================

class AdminOrganizationListSerializer(serializers.ModelSerializer):
    owner_name = serializers.SerializerMethodField()
    owner_email = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()
    subscription_status = serializers.SerializerMethodField()
    subscription_plan = serializers.SerializerMethodField()
    subscription_end = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'business_type', 'country', 'city',
            'phone', 'email', 'is_active',
            'owner_name', 'owner_email', 'members_count',
            'subscription_status', 'subscription_plan', 'subscription_end',
            'created_at',
        ]

    def _get_owner(self, obj):
        if not hasattr(obj, '_cached_owner'):
            membership = obj.memberships.filter(role='owner', is_active=True).select_related('user').first()
            obj._cached_owner = membership.user if membership else None
        return obj._cached_owner

    def get_owner_name(self, obj):
        owner = self._get_owner(obj)
        return owner.full_name if owner else '-'

    def get_owner_email(self, obj):
        owner = self._get_owner(obj)
        return owner.email if owner else '-'

    def get_members_count(self, obj):
        return obj.memberships.filter(is_active=True).count()

    def get_subscription_status(self, obj):
        sub = obj.get_active_subscription()
        return sub.get_status_display() if sub else 'Aucun'

    def get_subscription_plan(self, obj):
        sub = obj.get_active_subscription()
        return sub.plan.name if sub else '-'

    def get_subscription_end(self, obj):
        sub = obj.get_active_subscription()
        return sub.current_period_end if sub else None


class AdminOrganizationDetailSerializer(AdminOrganizationListSerializer):
    recent_activity = serializers.SerializerMethodField()

    class Meta(AdminOrganizationListSerializer.Meta):
        fields = AdminOrganizationListSerializer.Meta.fields + [
            'address', 'tax_id', 'currency',
            'recent_activity', 'updated_at',
        ]

    def get_recent_activity(self, obj):
        from apps.users.models import UserActivity
        activities = UserActivity.objects.filter(
            organization=obj
        ).select_related('user').order_by('-created_at')[:10]
        return [
            {
                'user': a.user.full_name,
                'action': a.get_action_display(),
                'resource_type': a.resource_type,
                'created_at': a.created_at,
            }
            for a in activities
        ]


# =============================================================================
# USERS
# =============================================================================

class AdminUserListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    organizations_count = serializers.SerializerMethodField()
    organizations = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone', 'is_active', 'is_staff', 'is_email_verified',
            'organizations_count', 'organizations',
            'date_joined', 'last_login',
        ]

    def get_organizations_count(self, obj):
        return obj.memberships.filter(is_active=True).count()

    def get_organizations(self, obj):
        memberships = obj.memberships.filter(is_active=True).select_related('organization')[:3]
        return [
            {
                'id': str(m.organization.id),
                'name': m.organization.name,
                'role': m.get_role_display(),
            }
            for m in memberships
        ]


# =============================================================================
# PLANS
# =============================================================================

class AdminPlanFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanFeature
        fields = ['id', 'name', 'code', 'description', 'is_enabled', 'limit_value']


class AdminPlanSerializer(serializers.ModelSerializer):
    plan_features = AdminPlanFeatureSerializer(many=True, read_only=True)
    subscribers_count = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'code', 'description',
            'price_monthly', 'price_yearly', 'currency',
            'max_users', 'max_branches', 'max_products',
            'max_monthly_transactions', 'storage_limit_mb',
            'features', 'is_active', 'is_featured',
            'trial_days', 'sort_order',
            'plan_features', 'subscribers_count',
            'created_at', 'updated_at',
        ]

    def get_subscribers_count(self, obj):
        return obj.subscriptions.filter(
            status__in=['trial', 'active', 'past_due']
        ).count()


class AdminPlanCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            'name', 'code', 'description',
            'price_monthly', 'price_yearly', 'currency',
            'max_users', 'max_branches', 'max_products',
            'max_monthly_transactions', 'storage_limit_mb',
            'features', 'is_active', 'is_featured',
            'trial_days', 'sort_order',
        ]


# =============================================================================
# SUBSCRIPTIONS
# =============================================================================

class AdminSubscriptionListSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    days_remaining = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    billing_cycle_display = serializers.CharField(source='get_billing_cycle_display', read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'organization', 'organization_name',
            'plan', 'plan_name',
            'status', 'status_display',
            'billing_cycle', 'billing_cycle_display',
            'price', 'currency',
            'current_period_start', 'current_period_end',
            'days_remaining',
            'created_at',
        ]


class AdminActivateSubscriptionSerializer(serializers.Serializer):
    """Serializer for admin to activate/extend a subscription."""
    plan_id = serializers.UUIDField()
    billing_cycle = serializers.ChoiceField(choices=Plan.BillingCycle.choices)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class AdminCreateSubscriptionSerializer(serializers.Serializer):
    """Serializer for admin to manually create a subscription for an organization."""
    organization = serializers.UUIDField()
    plan = serializers.UUIDField()
    billing_cycle = serializers.ChoiceField(choices=Plan.BillingCycle.choices)
    start_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_organization(self, value):
        try:
            return Organization.objects.get(id=value, is_deleted=False)
        except Organization.DoesNotExist:
            raise serializers.ValidationError("Établissement introuvable.")

    def validate_plan(self, value):
        try:
            return Plan.objects.get(id=value, is_active=True)
        except Plan.DoesNotExist:
            raise serializers.ValidationError("Plan introuvable ou inactif.")
