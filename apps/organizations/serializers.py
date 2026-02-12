"""
Serializers DRF pour l'app Organizations.
"""
from rest_framework import serializers
from .models import Organization, OrganizationMembership, Branch, OrganizationInvitation


# =============================================================================
# ORGANIZATION SERIALIZERS
# =============================================================================

class OrganizationListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes d'organisations."""
    
    business_type_display = serializers.CharField(
        source='get_business_type_display', read_only=True
    )
    members_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'slug', 'business_type', 'business_type_display',
            'logo', 'is_active', 'members_count', 'created_at'
        ]
        read_only_fields = ['id', 'slug', 'created_at']

    def get_members_count(self, obj):
        return obj.memberships.filter(is_active=True).count()


class OrganizationDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une organisation."""
    
    business_type_display = serializers.CharField(
        source='get_business_type_display', read_only=True
    )
    subscription_status = serializers.SerializerMethodField()
    
    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'slug', 'business_type', 'business_type_display',
            'logo', 'email', 'phone', 'address', 'city', 'country',
            'tax_id', 'rccm', 'id_nat',
            'currency', 'timezone', 'is_active',
            'settings', 'subscription_status',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']

    def get_subscription_status(self, obj):
        subscription = obj.get_active_subscription()
        if subscription:
            return {
                'plan': subscription.plan.name,
                'status': subscription.status,
                'expires_at': subscription.current_period_end
            }
        return None


class OrganizationCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création d'organisation."""
    
    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'business_type', 'logo',
            'email', 'phone', 'address', 'city', 'country',
            'tax_id', 'rccm', 'id_nat',
            'currency', 'timezone'
        ]
        read_only_fields = ['id']


class OrganizationUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour la mise à jour d'organisation."""
    
    class Meta:
        model = Organization
        fields = [
            'name', 'business_type', 'logo',
            'email', 'phone', 'address', 'city', 'country',
            'tax_id', 'rccm', 'id_nat',
            'currency', 'timezone', 'settings'
        ]


# =============================================================================
# MEMBERSHIP SERIALIZERS
# =============================================================================

class OrganizationMembershipSerializer(serializers.ModelSerializer):
    """Serializer pour les membres d'organisation."""
    
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_name = serializers.CharField(source='user.full_name', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    invited_by_name = serializers.CharField(source='invited_by.full_name', read_only=True)
    
    class Meta:
        model = OrganizationMembership
        fields = [
            'id', 'user', 'user_email', 'user_name',
            'role', 'role_display', 'is_active',
            'invited_by', 'invited_by_name', 'joined_at'
        ]
        read_only_fields = ['id', 'joined_at']


class MembershipCreateSerializer(serializers.Serializer):
    """Serializer pour ajouter un membre."""
    
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=OrganizationMembership.Role.choices)


class MembershipUpdateSerializer(serializers.Serializer):
    """Serializer pour modifier un membre."""
    
    role = serializers.ChoiceField(choices=OrganizationMembership.Role.choices)
    is_active = serializers.BooleanField(required=False)


# =============================================================================
# BRANCH SERIALIZERS
# =============================================================================

class BranchListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de branches."""
    
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    
    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'code', 'city', 'phone',
            'is_main', 'is_active', 'manager', 'manager_name'
        ]
        read_only_fields = ['id']


class BranchDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une branche."""
    
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    warehouses_count = serializers.SerializerMethodField()
    registers_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'code', 'address', 'city', 'phone',
            'is_main', 'is_active', 'manager', 'manager_name',
            'warehouses_count', 'registers_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_warehouses_count(self, obj):
        return obj.warehouses.filter(is_deleted=False).count()

    def get_registers_count(self, obj):
        return obj.registers.filter(is_deleted=False).count()


class BranchCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de branche."""
    
    class Meta:
        model = Branch
        fields = ['name', 'code', 'address', 'city', 'phone', 'is_main', 'is_active', 'manager']

    def validate_code(self, value):
        organization = self.context['request'].headers.get('X-Organization-ID')
        if Branch.objects.filter(
            organization_id=organization,
            code=value,
            is_deleted=False
        ).exists():
            raise serializers.ValidationError("Ce code existe déjà.")
        return value


# =============================================================================
# INVITATION SERIALIZERS
# =============================================================================

class OrganizationInvitationSerializer(serializers.ModelSerializer):
    """Serializer pour les invitations."""
    
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    invited_by_name = serializers.CharField(source='invited_by.full_name', read_only=True)
    
    class Meta:
        model = OrganizationInvitation
        fields = [
            'id', 'email', 'role', 'role_display',
            'status', 'status_display',
            'invited_by', 'invited_by_name',
            'expires_at', 'created_at'
        ]
        read_only_fields = ['id', 'token', 'created_at']


class InvitationCreateSerializer(serializers.Serializer):
    """Serializer pour créer une invitation."""
    
    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=OrganizationMembership.Role.choices,
        default=OrganizationMembership.Role.CASHIER
    )
