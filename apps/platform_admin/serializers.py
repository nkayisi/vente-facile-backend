"""
Serializers for the Platform Admin module.

⚠ LES LISTES NE RELISENT RIEN LIGNE À LIGNE. Le sérialiseur d'organisation
appelait `get_active_subscription()` TROIS fois par ligne (statut, plan,
échéance), plus le propriétaire et le décompte des membres : une page de vingt
organisations coûtait une centaine de requêtes. Les vues préchargent désormais
ce que les sérialiseurs lisent (`_proprietaires`, `_abonnements_courants`,
`nb_membres`), et chaque lecture garde un repli pour l'objet chargé seul.
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.organizations.models import Organization
from apps.settings.models import Currency
from apps.subscriptions.models import GlobalConfig, Plan, PlanFeature, Subscription
from apps.users.models import User

from .models import AdminActionLog, ImpersonationSession, PlatformAdminProfile, PlatformRole
from .permissions_catalog import PLATFORM_PERMISSION_CATALOG


# =============================================================================
# ORGANIZATIONS
# =============================================================================

def _abonnement_courant(org):
    """L'abonnement en cours (essai compris), préchargé si possible."""
    precharges = getattr(org, '_abonnements_courants', None)
    if precharges is not None:
        return precharges[0] if precharges else None
    if not hasattr(org, '_abonnement_courant_memo'):
        org._abonnement_courant_memo = org.get_active_subscription()
    return org._abonnement_courant_memo


def _proprietaire(org):
    precharges = getattr(org, '_proprietaires', None)
    if precharges is not None:
        return precharges[0].user if precharges else None
    if not hasattr(org, '_proprietaire_memo'):
        membership = (
            org.memberships.filter(role='owner', is_active=True)
            .select_related('user').order_by('joined_at').first()
        )
        org._proprietaire_memo = membership.user if membership else None
    return org._proprietaire_memo


class AdminOrganizationListSerializer(serializers.ModelSerializer):
    owner_name = serializers.SerializerMethodField()
    owner_email = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()
    subscription_status = serializers.SerializerMethodField()
    subscription_status_code = serializers.SerializerMethodField()
    subscription_plan = serializers.SerializerMethodField()
    subscription_end = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'business_type', 'country', 'city',
            'phone', 'email', 'is_active', 'currency',
            'owner_name', 'owner_email', 'members_count',
            'subscription_status', 'subscription_status_code',
            'subscription_plan', 'subscription_end',
            'created_at',
        ]

    def get_owner_name(self, obj):
        owner = _proprietaire(obj)
        return owner.full_name if owner else '-'

    def get_owner_email(self, obj):
        owner = _proprietaire(obj)
        return owner.email if owner else '-'

    def get_members_count(self, obj):
        annote = getattr(obj, 'nb_membres', None)
        if annote is not None:
            return annote
        return obj.memberships.filter(is_active=True).count()

    def get_subscription_status(self, obj):
        sub = _abonnement_courant(obj)
        return sub.get_status_display() if sub else 'Aucun'

    def get_subscription_status_code(self, obj):
        sub = _abonnement_courant(obj)
        return sub.status if sub else None

    def get_subscription_plan(self, obj):
        sub = _abonnement_courant(obj)
        return sub.plan.name if sub else '-'

    def get_subscription_end(self, obj):
        sub = _abonnement_courant(obj)
        return sub.current_period_end if sub else None


class AdminOrganizationDetailSerializer(AdminOrganizationListSerializer):
    recent_activity = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    subscription = serializers.SerializerMethodField()

    class Meta(AdminOrganizationListSerializer.Meta):
        fields = AdminOrganizationListSerializer.Meta.fields + [
            'address', 'tax_id', 'rccm', 'id_nat', 'timezone',
            'subscription_floor_tier',
            'members', 'subscription',
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

    def get_members(self, obj):
        """
        L'équipe, avec ce que le support doit savoir avant de prendre la main.

        `is_platform_admin` est ce qui ferme le bouton de prise en main : on ne
        se met jamais à la place d'un autre administrateur.
        """
        memberships = (
            obj.memberships.select_related('user', 'user__platform_admin_profile')
            .order_by('-is_active', 'role', 'user__first_name')
        )
        sortie = []
        for m in memberships:
            try:
                profil = m.user.platform_admin_profile
            except PlatformAdminProfile.DoesNotExist:
                profil = None
            sortie.append({
                'user_id': str(m.user_id),
                'name': m.user.full_name,
                'email': m.user.email,
                'role': m.role,
                'role_display': m.get_role_display(),
                'is_active': m.is_active and m.user.is_active,
                'is_platform_admin': bool(
                    m.user.is_superuser or (profil is not None and profil.is_active)
                ),
                'joined_at': m.joined_at,
            })
        return sortie

    def get_subscription(self, obj):
        sub = _abonnement_courant(obj)
        if sub is None:
            return None
        return {
            'id': str(sub.id),
            'plan_id': str(sub.plan_id),
            'plan_name': sub.plan.name,
            'status': sub.status,
            'status_display': sub.get_status_display(),
            'billing_cycle': sub.billing_cycle,
            'price': str(sub.price),
            'currency': sub.currency,
            'current_period_start': sub.current_period_start,
            'current_period_end': sub.current_period_end,
            'days_remaining': sub.days_remaining,
        }


class AdminOrganizationUpdateSerializer(serializers.ModelSerializer):
    """
    Les champs qu'un administrateur corrige pour le compte d'un marchand.

    ⚠ NI `is_active` NI `currency` NI `slug`. Le premier passe par
    `toggle_active`, qui le journalise ; la devise principale ne se change pas
    sans réécrire tout ce qui a été encaissé dans l'ancienne, et le slug
    identifie l'organisation ailleurs.
    """

    class Meta:
        model = Organization
        fields = [
            'name', 'business_type', 'email', 'phone', 'address',
            'city', 'country', 'tax_id', 'rccm', 'id_nat',
        ]
        extra_kwargs = {'name': {'required': False}}

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError("Le nom ne peut pas être vide.")
        return value


# =============================================================================
# USERS
# =============================================================================

def _profil_actif(user):
    try:
        profil = user.platform_admin_profile
    except PlatformAdminProfile.DoesNotExist:
        return None
    return profil if profil.is_active else None


def _role_plateforme(user):
    profil = _profil_actif(user)
    if profil is not None:
        return profil.role
    return PlatformRole.SUPER_ADMIN if user.is_superuser else None


def _adhesions(user):
    precharges = getattr(user, '_adhesions_actives', None)
    if precharges is not None:
        return precharges
    return list(user.memberships.filter(is_active=True).select_related('organization'))


class AdminUserListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    organizations_count = serializers.SerializerMethodField()
    organizations = serializers.SerializerMethodField()
    platform_role = serializers.SerializerMethodField()
    platform_role_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone', 'is_active', 'is_staff', 'is_superuser', 'is_email_verified',
            'platform_role', 'platform_role_display',
            'organizations_count', 'organizations',
            'date_joined', 'last_login',
        ]

    def get_organizations_count(self, obj):
        annote = getattr(obj, 'nb_organisations', None)
        if annote is not None:
            return annote
        return len(_adhesions(obj))

    def get_organizations(self, obj):
        return [
            {
                'id': str(m.organization.id),
                'name': m.organization.name,
                'role': m.get_role_display(),
                'role_code': m.role,
            }
            for m in _adhesions(obj)[:3]
        ]

    def get_platform_role(self, obj):
        return _role_plateforme(obj)

    def get_platform_role_display(self, obj):
        role = _role_plateforme(obj)
        return PlatformRole(role).label if role else None


class AdminUserDetailSerializer(AdminUserListSerializer):
    platform_profile = serializers.SerializerMethodField()

    class Meta(AdminUserListSerializer.Meta):
        fields = AdminUserListSerializer.Meta.fields + ['platform_profile']

    def get_organizations(self, obj):
        # Le détail les montre TOUTES : la liste n'en garde que trois pour tenir
        # dans une cellule.
        return [
            {
                'id': str(m.organization.id),
                'name': m.organization.name,
                'role': m.get_role_display(),
                'role_code': m.role,
            }
            for m in _adhesions(obj)
        ]

    def get_platform_profile(self, obj):
        try:
            profil = obj.platform_admin_profile
        except PlatformAdminProfile.DoesNotExist:
            return None
        return PlatformAdminProfileSerializer(profil).data


class AdminUserCreateSerializer(serializers.ModelSerializer):
    """
    La création d'un compte depuis l'administration.

    ⚠ LE MOT DE PASSE PASSE PAR `create_user`, JAMAIS PAR `save()`. Le
    sérialiseur d'origine n'avait aucun champ `password` et laissait DRF créer
    l'utilisateur directement : le compte naissait sans mot de passe haché, donc
    inutilisable, et rien ne le signalait avant la première connexion.
    """

    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name', 'phone', 'password', 'password_confirm']

    def validate_email(self, value):
        value = (value or '').strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Un compte existe déjà avec cette adresse.")
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirm'):
            raise serializers.ValidationError(
                {'password_confirm': "Les deux mots de passe ne correspondent pas."}
            )
        try:
            validate_password(attrs['password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password')
        return User.objects.create_user(password=password, **validated_data)


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """
    ⚠ NI `email` NI `is_active` NI `is_staff`. Changer l'adresse d'un compte
    change son identifiant de connexion ; les deux autres ont leurs actions,
    journalisées.
    """

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'phone']


class AdminGrantRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=PlatformRole.choices)
    extra_permissions = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_extra_permissions(self, value):
        inconnues = sorted(set(value) - PLATFORM_PERMISSION_CATALOG)
        if inconnues:
            raise serializers.ValidationError(
                f"Permissions inconnues : {', '.join(inconnues)}."
            )
        return sorted(set(value))


class AdminReasonSerializer(serializers.Serializer):
    """
    Le motif d'un geste d'administration.

    ⚠ `requis=True` pour les gestes qui ARRÊTENT quelqu'un : désactiver un
    établissement ou un compte, annuler un abonnement, retirer un rôle. L'écran
    l'exigeait seul : un appel direct à l'API passait sans motif, et le journal
    d'audit gardait un geste lourd sans dire pourquoi.
    """

    LONGUEUR_MINIMALE = 5

    reason = serializers.CharField(required=False, allow_blank=True, default='', max_length=1000)

    def __init__(self, *args, requis=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.requis = requis

    def validate(self, attrs):
        attrs['reason'] = attrs.get('reason', '').strip()
        if self.requis and len(attrs['reason']) < self.LONGUEUR_MINIMALE:
            raise serializers.ValidationError({
                'reason': "Le motif est obligatoire pour ce geste : il est gardé au journal d'audit.",
            })
        return attrs


# =============================================================================
# PLATFORM ADMINS
# =============================================================================

class PlatformAdminProfileSerializer(serializers.ModelSerializer):
    user_id = serializers.UUIDField(source='user.id', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    full_name = serializers.CharField(source='user.full_name', read_only=True)
    user_is_active = serializers.BooleanField(source='user.is_active', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    granted_by_email = serializers.SerializerMethodField()
    revoked_by_email = serializers.SerializerMethodField()

    class Meta:
        model = PlatformAdminProfile
        fields = [
            'id', 'user_id', 'email', 'full_name', 'user_is_active',
            'role', 'role_display', 'is_active', 'extra_permissions',
            'granted_by_email', 'granted_at', 'revoked_by_email', 'revoked_at',
            'notes',
        ]

    def get_granted_by_email(self, obj):
        return obj.granted_by.email if obj.granted_by_id else None

    def get_revoked_by_email(self, obj):
        return obj.revoked_by.email if obj.revoked_by_id else None


# =============================================================================
# AUDIT LOG
# =============================================================================

class AdminActionLogSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    target_type_display = serializers.CharField(source='get_target_type_display', read_only=True)
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AdminActionLog
        fields = [
            'id', 'created_at', 'actor', 'actor_email', 'actor_name',
            'action', 'action_display', 'target_type', 'target_type_display',
            'target_id', 'target_repr', 'before', 'after', 'reason',
            'ip_address',
        ]

    def get_actor_name(self, obj):
        return obj.actor.full_name if obj.actor_id and obj.actor else None


class ImpersonationSessionSerializer(serializers.ModelSerializer):
    impersonator_email = serializers.CharField(source='impersonator.email', read_only=True)
    target_email = serializers.CharField(source='target_user.email', read_only=True)
    target_name = serializers.CharField(source='target_user.full_name', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    ended_reason_display = serializers.CharField(source='get_ended_reason_display', read_only=True)
    is_live = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImpersonationSession
        fields = [
            'id', 'impersonator', 'impersonator_email',
            'target_user', 'target_email', 'target_name',
            'organization', 'organization_name', 'reason',
            'started_at', 'expires_at', 'ended_at', 'ended_reason', 'ended_reason_display',
            'is_live', 'ip_address',
        ]


# =============================================================================
# PLANS
# =============================================================================

class AdminPlanFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanFeature
        fields = ['id', 'name', 'code', 'description', 'is_enabled', 'limit_value']


class AdminPlanCurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ['id', 'code', 'symbol']


class AdminPlanSerializer(serializers.ModelSerializer):
    plan_features = AdminPlanFeatureSerializer(many=True, read_only=True)
    currency = AdminPlanCurrencySerializer(read_only=True)
    subscribers_count = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'code', 'description',
            'price_monthly', 'price_yearly', 'currency',
            'max_users', 'max_branches', 'max_warehouses', 'max_products',
            'max_monthly_transactions', 'storage_limit_mb',
            'features', 'is_active', 'is_featured',
            'trial_days', 'sort_order', 'tier',
            'plan_features', 'subscribers_count',
            'created_at', 'updated_at',
        ]

    def get_subscribers_count(self, obj):
        annote = getattr(obj, 'nb_abonnes', None)
        if annote is not None:
            return annote
        return obj.subscriptions.filter(
            status__in=['trial', 'active', 'past_due']
        ).count()


class AdminPlanCreateUpdateSerializer(serializers.ModelSerializer):
    currency = serializers.PrimaryKeyRelatedField(
        queryset=Currency.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Plan
        fields = [
            'name', 'code', 'description',
            'price_monthly', 'price_yearly', 'currency',
            'max_users', 'max_branches', 'max_warehouses', 'max_products',
            'max_monthly_transactions', 'storage_limit_mb',
            'features', 'is_active', 'is_featured',
            'trial_days', 'sort_order', 'tier',
        ]

    def validate(self, attrs):
        if self.instance is None:
            cur = attrs.get('currency')
            if cur is None:
                usd = Currency.objects.filter(code='USD', is_active=True).first()
                if not usd:
                    raise serializers.ValidationError({
                        'currency': "Aucune devise USD active : configurez les devises (settings) d'abord.",
                    })
                attrs['currency'] = usd
        elif 'currency' in attrs and attrs['currency'] is None:
            raise serializers.ValidationError({
                'currency': 'La devise ne peut pas être vide.',
            })
        return attrs


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
            'days_remaining', 'cancelled_at',
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

    def validate_start_date(self, value):
        """
        ⚠ UNE DATE FUTURE EST REFUSÉE, elle n'est pas ignorée. Le statut d'un
        abonnement ne lit que son échéance : un abonnement « qui commence le 20 »
        serait actif dès aujourd'hui, et le marchand gagnerait les jours d'avance
        sans que rien ne le dise. Le cas qu'on sert est l'inverse : rattacher un
        paiement reçu en espèces il y a quelques jours au jour où il a été reçu.
        """
        from django.utils import timezone
        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError(
                "La date de début ne peut pas être dans le futur."
            )
        return value


class AdminExtendSubscriptionSerializer(serializers.Serializer):
    """
    ⚠ `days` EST UN ENTIER BORNÉ. L'ancienne action faisait `int(request.data[...])`
    sans garde : « 30 jours » ou une valeur vide rendaient 500, et un nombre
    négatif RACCOURCISSAIT l'abonnement sous le libellé « Prolonger ».
    """
    days = serializers.IntegerField(min_value=1, max_value=3650)
    reason = serializers.CharField(required=False, allow_blank=True, default='', max_length=1000)


# =============================================================================
# PORTE DE MISE A JOUR MOBILE
# =============================================================================

class AdminAppReleaseSerializer(serializers.ModelSerializer):
    """
    Les huit réglages de la porte de mise à jour du terminal.

    ⚠ **LA VALIDATION EST CELLE DU MODÈLE, jamais une seconde écriture.**
    `GlobalConfig.clean()` porte les trois règles (forme du semver, seuil qui ne
    dépasse pas la version du magasin, seuil qui mène quelque part) et l'admin
    Django les oppose déjà. Les redire ici les ferait diverger, et c'est ce
    formulaire-là qui peut bloquer un parc entier.
    """

    class Meta:
        model = GlobalConfig
        fields = [
            'android_minimum_version', 'android_current_version',
            'android_store_url', 'android_update_message',
            'ios_minimum_version', 'ios_current_version',
            'ios_store_url', 'ios_update_message',
        ]

    def validate(self, attrs):
        """
        On valide l'instance TELLE QU'ELLE SERA, pas les seuls champs reçus : un
        PATCH qui ne touche qu'au seuil doit être confronté à la version du
        magasin déjà enregistrée, sinon la règle qui protège le parc saute dès
        qu'on modifie un champ à la fois.

        ⚠ **CETTE MÉTHODE ÉCRIT SUR L'INSTANCE, et c'est un piège de SONDE.** Sans
        conséquence en production - `attrs` est exactement le `validated_data` que
        `update()` reposera, et l'instance vient d'un `get()` par requête - mais un
        script qui réutiliserait le même objet pour deux mesures verrait la seconde
        fausse. Payé une fois en mesurant l'effacement de la plateforme iOS :
        instancier à neuf pour chaque essai. Et c'est aussi pourquoi la vue relève
        l'état « avant » pour le journal AVANT d'appeler `is_valid()`.
        """
        instance = self.instance or GlobalConfig.get()
        for champ, valeur in attrs.items():
            setattr(instance, champ, valeur)
        instance.clean()
        return attrs
