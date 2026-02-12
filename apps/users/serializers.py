"""
Serializers DRF pour l'app Users.
"""
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import User, UserActivity


# =============================================================================
# USER SERIALIZERS
# =============================================================================

class UserListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes d'utilisateurs."""
    
    full_name = serializers.CharField(read_only=True)
    organizations_count = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone', 'avatar', 'is_active', 'is_email_verified',
            'organizations_count', 'date_joined'
        ]
        read_only_fields = ['id', 'date_joined']

    def get_organizations_count(self, obj):
        return obj.memberships.filter(is_active=True).count()


class UserDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un utilisateur."""
    
    full_name = serializers.CharField(read_only=True)
    active_organization_name = serializers.CharField(
        source='active_organization.name', read_only=True
    )
    organizations = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone', 'avatar', 'is_active', 'is_email_verified',
            'active_organization', 'active_organization_name',
            'organizations', 'preferences',
            'date_joined', 'last_login'
        ]
        read_only_fields = ['id', 'email', 'date_joined', 'last_login']

    def get_organizations(self, obj):
        """Retourne les organisations de l'utilisateur avec son rôle."""
        memberships = obj.memberships.filter(is_active=True).select_related('organization')
        return [
            {
                'id': str(m.organization.id),
                'name': m.organization.name,
                'role': m.role,
                'role_display': m.get_role_display()
            }
            for m in memberships
        ]


class UserCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création d'utilisateur (inscription)."""
    
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)
    
    class Meta:
        model = User
        fields = [
            'email', 'password', 'password_confirm',
            'first_name', 'last_name', 'phone'
        ]

    def validate(self, data):
        if data['password'] != data['password_confirm']:
            raise serializers.ValidationError({
                'password_confirm': 'Les mots de passe ne correspondent pas.'
            })
        return data

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        password = validated_data.pop('password')
        
        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()
        
        return user


class RegisterWithOrganizationSerializer(serializers.Serializer):
    """
    Serializer pour l'inscription complète avec création de boutique.
    Crée User + Organization + Subscription en une seule étape.
    """
    
    # Informations utilisateur
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    
    # Informations boutique
    organization_name = serializers.CharField(max_length=255)
    business_type = serializers.ChoiceField(
        choices=[
            ('boutique', 'Boutique'),
            ('supermarket', 'Supermarché'),
            ('pharmacy', 'Pharmacie'),
            ('depot', 'Dépôt'),
            ('restaurant', 'Restaurant'),
            ('other', 'Autre'),
        ],
        default='boutique'
    )
    currency = serializers.ChoiceField(
        choices=[('CDF', 'Franc Congolais'), ('USD', 'Dollar US')],
        default='CDF'
    )
    country = serializers.CharField(max_length=100, default='RDC')

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Un compte avec cet email existe déjà.')
        return value

    def validate(self, data):
        if data['password'] != data['password_confirm']:
            raise serializers.ValidationError({
                'password_confirm': 'Les mots de passe ne correspondent pas.'
            })
        return data

    def create(self, validated_data):
        from apps.core.services import OrganizationService
        
        # Extraire les données
        password = validated_data.pop('password')
        validated_data.pop('password_confirm')
        
        org_name = validated_data.pop('organization_name')
        business_type = validated_data.pop('business_type')
        currency = validated_data.pop('currency')
        country = validated_data.pop('country')
        
        # Créer l'utilisateur
        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()
        
        # Créer l'organisation avec subscription trial
        organization = OrganizationService.create_organization(
            user=user,
            name=org_name,
            business_type=business_type,
            currency=currency,
            country=country
        )
        
        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour la mise à jour du profil utilisateur."""
    
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'phone', 'avatar', 'preferences']


class ChangePasswordSerializer(serializers.Serializer):
    """Serializer pour le changement de mot de passe."""
    
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    new_password_confirm = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Mot de passe actuel incorrect.')
        return value

    def validate(self, data):
        if data['new_password'] != data['new_password_confirm']:
            raise serializers.ValidationError({
                'new_password_confirm': 'Les mots de passe ne correspondent pas.'
            })
        return data


class ResetPasswordRequestSerializer(serializers.Serializer):
    """Serializer pour la demande de réinitialisation de mot de passe."""
    
    email = serializers.EmailField()


class ResetPasswordConfirmSerializer(serializers.Serializer):
    """Serializer pour la confirmation de réinitialisation."""
    
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    new_password_confirm = serializers.CharField(write_only=True)

    def validate(self, data):
        if data['new_password'] != data['new_password_confirm']:
            raise serializers.ValidationError({
                'new_password_confirm': 'Les mots de passe ne correspondent pas.'
            })
        return data


# =============================================================================
# USER ACTIVITY SERIALIZERS
# =============================================================================

class UserActivitySerializer(serializers.ModelSerializer):
    """Serializer pour les activités utilisateur."""
    
    user_email = serializers.CharField(source='user.email', read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    
    class Meta:
        model = UserActivity
        fields = [
            'id', 'user', 'user_email', 'organization',
            'action', 'action_display',
            'resource_type', 'resource_id',
            'details', 'ip_address', 'user_agent',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']


# =============================================================================
# AUTH SERIALIZERS
# =============================================================================

class LoginSerializer(serializers.Serializer):
    """Serializer pour la connexion."""
    
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class TokenResponseSerializer(serializers.Serializer):
    """Serializer pour la réponse de token."""
    
    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserDetailSerializer()
