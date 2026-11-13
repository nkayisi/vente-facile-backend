"""
Les vues de l'administration de la plateforme.

┌──────────────────────────────────────────────────────────────────────────────┐
│ TROIS RÈGLES, TENUES SUR CHAQUE VUE.                                        │
│                                                                              │
│ 1. Chaque action est DÉCLARÉE dans `action_permissions`. Une action absente  │
│    est refusée, et `apps/platform_admin/tests/test_contrat_permissions.py`   │
│    exige que toute action routée soit déclarée ou fermée par `DENY`.         │
│ 2. Rien ne se SUPPRIME. Une organisation ou un compte se désactive, un plan  │
│    se retire de la vente. `http_method_names` ferme `DELETE` et `PUT` : un   │
│    `DELETE` exécutait un vrai DELETE SQL, en cascade sur toutes les ventes   │
│    du marchand, malgré la suppression douce prévue sur `Organization`.       │
│ 3. Chaque acte écrit sa ligne d'audit DANS sa transaction.                   │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import datetime, time, timedelta

import django_filters
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_permissions import DENY
from apps.organizations.models import Organization, OrganizationMembership
from apps.subscriptions.models import GlobalConfig, Plan, Subscription, SubscriptionPayment
from apps.users.models import User

from .models import AdminActionLog, ImpersonationSession, PlatformAdminProfile, PlatformRole
from .permissions import HasPlatformPermission, IsPlatformAdmin
from .serializers import (
    AdminActionLogSerializer,
    AdminActivateSubscriptionSerializer,
    AdminAppReleaseSerializer,
    AdminCreateSubscriptionSerializer,
    AdminExtendSubscriptionSerializer,
    AdminGrantRoleSerializer,
    AdminOrganizationDetailSerializer,
    AdminOrganizationListSerializer,
    AdminOrganizationUpdateSerializer,
    AdminPlanCreateUpdateSerializer,
    AdminPlanSerializer,
    AdminReasonSerializer,
    AdminSubscriptionListSerializer,
    AdminUserCreateSerializer,
    AdminUserDetailSerializer,
    AdminUserListSerializer,
    AdminUserUpdateSerializer,
    ImpersonationSessionSerializer,
    PlatformAdminProfileSerializer,
)
from .services import (
    AuditLogService,
    PlatformAdminService,
    PlatformPermissionService,
    RefusAdministration,
    _comptes_capables_de_tout,
    profil_admin,
    role_de,
)

#: Les gardes de toute vue de ce module.
GARDES = [IsAuthenticated, IsPlatformAdmin, HasPlatformPermission]

#: Ce qui ferme une réécriture complète ou une suppression : DELETE et PUT.
SANS_SUPPRESSION = ['get', 'post', 'patch', 'head', 'options']

Action = AdminActionLog.Action
Cible = AdminActionLog.Target


def _ecart(instance, donnees):
    """
    `(avant, après)` pour les seuls champs qui changent, en texte.

    Le journal garde ce qui a BOUGÉ, pas une photo de la fiche : une ligne qui
    recopierait dix champs dont un seul a changé ne dirait pas lequel.
    """
    avant, apres = {}, {}
    for champ, valeur in donnees.items():
        ancienne = getattr(instance, champ, None)
        if ancienne != valeur:
            avant[champ] = None if ancienne is None else str(ancienne)
            apres[champ] = None if valeur is None else str(valeur)
    return avant, apres


# =============================================================================
# QUI SUIS-JE
# =============================================================================

class AdminMeView(APIView):
    """
    Le rôle et les permissions de l'administrateur connecté.
    GET /api/v1/platform-admin/me/

    ⚠ LE BACK-OFFICE LIT ICI, PAS DANS LE JETON. Un rôle posé dans le JWT serait
    figé jusqu'à la reconnexion - sept jours - et un support rétrogradé garderait
    à l'écran des boutons que le serveur lui refuse. Ici, la réponse est celle de
    l'instant.
    """

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        user = request.user
        role = role_de(user)
        return Response({
            'id': str(user.id),
            'email': user.email,
            'full_name': user.full_name,
            'role': role,
            'role_display': PlatformRole(role).label if role else None,
            'is_super_admin': role == PlatformRole.SUPER_ADMIN,
            'is_support': role == PlatformRole.SUPPORT,
            'permissions': sorted(PlatformPermissionService.get_effective_permissions(user)),
        })


# =============================================================================
# DASHBOARD
# =============================================================================

class AdminDashboardView(APIView):
    """
    Platform-wide dashboard statistics.
    GET /api/v1/platform-admin/dashboard/
    """
    permission_classes = GARDES
    action_permissions = {'get': ('metrics.view', 'organizations.view')}

    def get(self, request):
        from .metrics import PlatformMetricsService

        local = timezone.localtime()
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        total_orgs = Organization.objects.filter(is_deleted=False).count()
        active_orgs = Organization.objects.filter(is_deleted=False, is_active=True).count()

        total_users = User.objects.filter(is_active=True).count()
        new_users_month = User.objects.filter(date_joined__gte=month_start).count()

        # ⚠ L'ENCAISSÉ SE VENTILE PAR DEVISE. Il était sommé toutes devises
        # confondues, puis affiché sans symbole : des francs et des dollars
        # additionnés, sous une étiquette qui ne disait ni l'un ni l'autre.
        payes = SubscriptionPayment.objects.filter(status='completed')
        par_devise = {
            r['currency'] or '?': {'currency': r['currency'] or '?', 'total': str(r['total']), 'this_month': '0.00'}
            for r in payes.values('currency').annotate(total=Sum('amount')).order_by('currency')
        }
        for r in (
            payes.filter(paid_at__gte=month_start).values('currency')
            .annotate(total=Sum('amount')).order_by('currency')
        ):
            code = r['currency'] or '?'
            par_devise.setdefault(code, {'currency': code, 'total': '0.00', 'this_month': '0.00'})
            par_devise[code]['this_month'] = str(r['total'])

        subs_by_status = dict(
            Subscription.objects.values_list('status').annotate(
                count=Count('id')
            ).values_list('status', 'count')
        )

        subs_by_plan = list(
            Subscription.objects.filter(
                status__in=['trial', 'active', 'past_due']
            ).values('plan__name').annotate(count=Count('id')).order_by('-count')
        )

        recent_orgs = Organization.objects.filter(is_deleted=False).order_by('-created_at')[:5]
        recent_orgs_data = [
            {
                'id': str(org.id),
                'name': org.name,
                'business_type': org.business_type,
                'created_at': org.created_at,
            }
            for org in recent_orgs
        ]

        six_months_ago = local - timedelta(days=180)
        growth = (
            Organization.objects.filter(created_at__gte=six_months_ago, is_deleted=False)
            .annotate(month=TruncMonth('created_at')).values('month')
            .annotate(count=Count('id')).order_by('month')
        )
        users_trend = (
            User.objects.filter(date_joined__gte=six_months_ago)
            .annotate(month=TruncMonth('date_joined')).values('month')
            .annotate(count=Count('id')).order_by('month')
        )
        revenue_trend = {}
        for r in (
            payes.filter(paid_at__gte=six_months_ago)
            .annotate(month=TruncMonth('paid_at')).values('month', 'currency')
            .annotate(total=Sum('amount')).order_by('month')
        ):
            mois = r['month'].strftime('%Y-%m')
            revenue_trend.setdefault(mois, {})[r['currency'] or '?'] = str(r['total'])

        recurrent = PlatformMetricsService.instantane()

        return Response({
            'total_organizations': total_orgs,
            'active_organizations': active_orgs,
            'total_users': total_users,
            'new_users_this_month': new_users_month,
            'revenue_by_currency': list(par_devise.values()),
            'subscriptions_by_status': subs_by_status,
            'subscriptions_by_plan': subs_by_plan,
            'recent_organizations': recent_orgs_data,
            'growth_trend': [
                {'month': g['month'].strftime('%Y-%m'), 'count': g['count']} for g in growth
            ],
            'users_trend': [
                {'month': u['month'].strftime('%Y-%m'), 'count': u['count']} for u in users_trend
            ],
            'revenue_trend': [
                {'month': mois, 'by_currency': totaux} for mois, totaux in revenue_trend.items()
            ],
            # Les chiffres RÉCURRENTS : ce que la plateforme encaisse chaque mois,
            # pas ce qu'elle a encaissé ce mois-ci (un abonnement annuel payé en
            # janvier ferait de janvier un mois record et des onze autres des creux).
            'mrr': recurrent['mrr'],
            'mrr_currency': recurrent['currency'],
            'mrr_by_currency': recurrent['mrr_by_currency'],
            'arpu': recurrent['arpu'],
            'paying_organizations': recurrent['paying_organizations'],
            'trialing_organizations': recurrent['trialing_organizations'],
            'churn_rate_30d': recurrent['churn_rate_30d'],
        })


class AdminMetricsView(APIView):
    """
    Les chiffres récurrents et leur évolution.
    GET /api/v1/platform-admin/metrics/?months=12
    """
    permission_classes = GARDES
    action_permissions = {'get': 'metrics.view'}

    def get(self, request):
        from .metrics import PlatformMetricsService

        try:
            mois = int(request.query_params.get('months', 12))
        except (TypeError, ValueError):
            raise RefusAdministration("`months` doit être un nombre de mois.")
        donnees = PlatformMetricsService.instantane()
        donnees['trend'] = PlatformMetricsService.serie_mensuelle(mois)
        return Response(donnees)


class AdminIncidentsView(APIView):
    """
    Ce que le support doit traiter aujourd'hui.
    GET /api/v1/platform-admin/incidents/
    """
    permission_classes = GARDES
    action_permissions = {'get': 'incidents.view'}

    def get(self, request):
        from .incidents import tableau_des_incidents
        return Response(tableau_des_incidents())


class AdminReconcilePaymentsView(APIView):
    """
    Rattrape un paiement Mobile Money encaissé mais jamais activé.
    POST /api/v1/platform-admin/incidents/reconcile/  {reference?, dry_run?}

    C'est le corps que la tâche Celery exécute toutes les trente minutes, appelé
    à la demande : avec une référence, il passe outre la cadence, et le rapport
    dit ce que l'opérateur a répondu - y compris la raison d'une activation
    différée, qui n'est écrite nulle part ailleurs.
    """
    permission_classes = GARDES
    action_permissions = {'post': 'incidents.reconcile_payments'}

    def post(self, request):
        from apps.subscriptions.services import SubscriptionService

        reference = str(request.data.get('reference') or '').strip()
        dry_run = bool(request.data.get('dry_run'))
        rapport = SubscriptionService.reconcile_moko_payments(
            reference=reference, dry_run=dry_run,
        )
        resume = {
            'checked': rapport['checked'],
            'reconciled': len(rapport['reconciled']),
            'still_pending': len(rapport['still_pending']),
            'really_failed': len(rapport['really_failed']),
            'unresolved': len(rapport['unresolved']),
        }
        if not dry_run:
            AuditLogService.record(
                actor=request.user, action=Action.RECONCILE_PAYMENTS,
                target_type=Cible.PAYMENT, target_id=reference,
                target_repr=reference or 'Lot complet',
                after=resume, request=request,
            )
        # Les listes du rapport sont des couples (référence, motif).
        return Response({
            **resume,
            'dry_run': dry_run,
            'details': {
                cle: [{'reference': r, 'detail': d} for r, d in rapport[cle]]
                for cle in ('reconciled', 'still_pending', 'really_failed', 'unresolved')
            },
            'skipped': rapport.get('skipped', 0),
        })


# =============================================================================
# ORGANIZATIONS
# =============================================================================

class AdminOrganizationViewSet(viewsets.ModelViewSet):
    """
    Admin management of all organizations on the platform.

    ⚠ `create` EST FERMÉ, et c'est délibéré. Une organisation naît d'une
    inscription : propriétaire, entrepôt principal, devise et ligne de devise.
    Créée d'ici, elle serait orpheline - aucun membre pour s'y connecter - et son
    `slug` vide entrerait en collision avec la suivante.
    """
    permission_classes = GARDES
    http_method_names = SANS_SUPPRESSION
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['name', 'email', 'phone', 'city']
    ordering_fields = ['name', 'created_at']
    ordering = ['-created_at']
    filterset_fields = ['is_active', 'business_type', 'country']

    action_permissions = {
        'list': 'organizations.view',
        'retrieve': 'organizations.view',
        'partial_update': 'organizations.edit',
        'toggle_active': 'organizations.toggle_active',
        'activate_subscription': 'organizations.activate_subscription',
        # Fermé : voir la docstring de la classe.
        'create': DENY,
    }

    def get_queryset(self):
        return (
            Organization.objects.filter(is_deleted=False)
            .annotate(
                nb_membres=Count(
                    'memberships', filter=Q(memberships__is_active=True), distinct=True,
                )
            )
            .prefetch_related(
                Prefetch(
                    'memberships',
                    queryset=OrganizationMembership.objects.filter(
                        is_active=True, role=OrganizationMembership.Role.OWNER,
                    ).select_related('user').order_by('joined_at'),
                    to_attr='_proprietaires',
                ),
                Prefetch(
                    'subscriptions',
                    queryset=Subscription.objects.filter(
                        status__in=[Subscription.Status.TRIAL, Subscription.Status.ACTIVE],
                    ).select_related('plan').order_by('-created_at'),
                    to_attr='_abonnements_courants',
                ),
            )
        )

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AdminOrganizationDetailSerializer
        if self.action == 'partial_update':
            return AdminOrganizationUpdateSerializer
        return AdminOrganizationListSerializer

    def partial_update(self, request, *args, **kwargs):
        org = self.get_object()
        serializer = AdminOrganizationUpdateSerializer(org, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        avant, apres = _ecart(org, serializer.validated_data)
        with transaction.atomic():
            serializer.save()
            if avant:
                AuditLogService.record(
                    actor=request.user, action=Action.UPDATE,
                    target_type=Cible.ORGANIZATION, target_id=org.pk, target_repr=org.name,
                    before=avant, after=apres, request=request,
                )
        frais = self.get_queryset().get(pk=org.pk)
        return Response(AdminOrganizationDetailSerializer(frais).data)

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate or deactivate an organization."""
        cible = self.get_object()
        motif = AdminReasonSerializer(data=request.data, requis=cible.is_active)
        motif.is_valid(raise_exception=True)
        with transaction.atomic():
            org = Organization.objects.select_for_update().get(pk=cible.pk)
            org.is_active = not org.is_active
            org.save(update_fields=['is_active', 'updated_at'])
            if not org.is_active:
                # Un établissement suspendu ne se consulte plus, même par le support.
                from .impersonation import fermer_sessions

                fermer_sessions(organization=org, acteur=request.user,
                                raison=motif.validated_data['reason'], request=request)
            AuditLogService.record(
                actor=request.user, action=Action.TOGGLE_ACTIVE,
                target_type=Cible.ORGANIZATION, target_id=org.pk, target_repr=org.name,
                before={'is_active': not org.is_active}, after={'is_active': org.is_active},
                reason=motif.validated_data['reason'], request=request,
            )
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

        try:
            plan = Plan.objects.get(id=serializer.validated_data['plan_id'], is_active=True)
        except Plan.DoesNotExist:
            return Response(
                {'detail': 'Plan introuvable ou inactif.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.subscriptions.services import SubscriptionService
        notes = serializer.validated_data.get('notes', '')
        with transaction.atomic():
            subscription = SubscriptionService.activate_subscription(
                organization=org,
                plan=plan,
                billing_cycle=serializer.validated_data['billing_cycle'],
                activated_by=request.user,
                notes=notes,
            )
            AuditLogService.record(
                actor=request.user, action=Action.ACTIVATE_SUBSCRIPTION,
                target_type=Cible.SUBSCRIPTION, target_id=subscription.pk,
                target_repr=f'{org.name} · {plan.name}',
                after={
                    'plan': plan.code, 'billing_cycle': subscription.billing_cycle,
                    'current_period_end': subscription.current_period_end.isoformat(),
                },
                reason=notes, request=request,
            )

        return Response({
            'message': f"Abonnement {plan.name} activé pour {org.name}.",
            'subscription_id': str(subscription.id),
        })


# =============================================================================
# USERS
# =============================================================================

class AdminUserViewSet(viewsets.ModelViewSet):
    """Admin management of all platform users."""
    permission_classes = GARDES
    http_method_names = SANS_SUPPRESSION
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['email', 'first_name', 'last_name', 'phone']
    ordering_fields = ['email', 'date_joined', 'last_login']
    ordering = ['-date_joined']
    filterset_fields = ['is_active', 'is_staff']

    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'create': 'users.create',
        'partial_update': 'users.edit',
        'toggle_active': 'users.toggle_active',
        'grant_role': 'platform_admins.manage',
        'revoke_role': 'platform_admins.manage',
        'impersonate': 'impersonation.start',
    }

    def get_queryset(self):
        return (
            User.objects.all()
            .select_related('platform_admin_profile')
            .annotate(
                nb_organisations=Count(
                    'memberships', filter=Q(memberships__is_active=True), distinct=True,
                )
            )
            .prefetch_related(
                Prefetch(
                    'memberships',
                    queryset=OrganizationMembership.objects.filter(is_active=True)
                    .select_related('organization').order_by('joined_at'),
                    to_attr='_adhesions_actives',
                ),
            )
        )

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AdminUserDetailSerializer
        if self.action == 'create':
            return AdminUserCreateSerializer
        if self.action == 'partial_update':
            return AdminUserUpdateSerializer
        return AdminUserListSerializer

    def _detail(self, user):
        return Response(AdminUserDetailSerializer(self.get_queryset().get(pk=user.pk)).data)

    def create(self, request, *args, **kwargs):
        serializer = AdminUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = serializer.save()
            AuditLogService.record(
                actor=request.user, action=Action.CREATE,
                target_type=Cible.USER, target_id=user.pk, target_repr=user.email,
                after={'email': user.email}, request=request,
            )
        return Response(
            AdminUserDetailSerializer(self.get_queryset().get(pk=user.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        user = self.get_object()
        serializer = AdminUserUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        avant, apres = _ecart(user, serializer.validated_data)
        with transaction.atomic():
            # `save()` sans `update_fields` : `updated_at` (auto_now) suit, et la
            # table `users` descend sur les terminaux avec le nom et le téléphone.
            serializer.save()
            if avant:
                AuditLogService.record(
                    actor=request.user, action=Action.UPDATE,
                    target_type=Cible.USER, target_id=user.pk, target_repr=user.email,
                    before=avant, after=apres, request=request,
                )
        return self._detail(user)

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate or deactivate a user."""
        cible = self.get_object()
        motif = AdminReasonSerializer(data=request.data, requis=cible.is_active)
        motif.is_valid(raise_exception=True)
        if cible.pk == request.user.pk:
            raise RefusAdministration("Vous ne pouvez pas vous désactiver vous-même.")

        # ⚠ Un administrateur se désactive seulement par qui peut le nommer : sans
        # cette garde, le support fermerait le compte d'un super-admin.
        est_admin = cible.is_superuser or profil_admin(cible) is not None
        if est_admin and not PlatformPermissionService.has(request.user, 'platform_admins.manage'):
            raise RefusAdministration(
                "Ce compte administre la plateforme : seul un super-administrateur peut le désactiver."
            )

        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=cible.pk)
            if user.is_active and est_admin and _comptes_capables_de_tout(exclure_user_id=user.pk) == 0:
                raise RefusAdministration(
                    "C'est le dernier super-administrateur actif : il ne peut pas être désactivé."
                )
            user.is_active = not user.is_active
            if not user.is_active:
                # Un compte fermé ne regarde plus personne, et personne ne le regarde.
                from .impersonation import fermer_sessions

                raison = motif.validated_data['reason']
                fermer_sessions(impersonator=user, acteur=request.user, raison=raison, request=request)
                fermer_sessions(target_user=user, acteur=request.user, raison=raison, request=request)
            # ⚠ `updated_at` EXPLICITE. `User` n'hérite pas de `TimeStampedModel` :
            # un `update_fields=['is_active']` laissait l'horodatage où il était, et
            # la table `users` descend au tirage avec `is_active`. La désactivation
            # n'atteignait donc JAMAIS les terminaux.
            user.save(update_fields=['is_active', 'updated_at'])
            AuditLogService.record(
                actor=request.user, action=Action.TOGGLE_ACTIVE,
                target_type=Cible.USER, target_id=user.pk, target_repr=user.email,
                before={'is_active': not user.is_active}, after={'is_active': user.is_active},
                reason=motif.validated_data['reason'], request=request,
            )
        return Response({
            'message': f"Utilisateur {'activé' if user.is_active else 'désactivé'}.",
            'is_active': user.is_active,
        })

    @action(detail=True, methods=['post'])
    def grant_role(self, request, pk=None):
        """Accorde ou change le rôle plateforme d'un compte."""
        user = self.get_object()
        serializer = AdminGrantRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        PlatformAdminService.grant(
            user, serializer.validated_data['role'],
            granted_by=request.user,
            extra_permissions=serializer.validated_data['extra_permissions'],
            notes=serializer.validated_data['notes'],
            request=request,
        )
        return self._detail(user)

    @action(detail=True, methods=['post'])
    def revoke_role(self, request, pk=None):
        """Retire le rôle plateforme d'un compte."""
        user = self.get_object()
        motif = AdminReasonSerializer(data=request.data, requis=True)
        motif.is_valid(raise_exception=True)
        PlatformAdminService.revoke(
            user, revoked_by=request.user,
            reason=motif.validated_data['reason'], request=request,
        )
        return self._detail(user)


    @action(detail=True, methods=['post'])
    def impersonate(self, request, pk=None):
        """
        Prend la main sur ce compte, dans une organisation, en lecture seule.
        Rend un jeton d'accès de vingt minutes, et AUCUN jeton de rafraîchissement.
        """
        from .impersonation import demarrer

        cible = self.get_object()
        org_id = request.data.get('organization')
        try:
            organisation = Organization.objects.get(pk=org_id)
        except (Organization.DoesNotExist, ValueError, TypeError, DjangoValidationError):
            raise RefusAdministration("Organisation introuvable.")
        session, jeton = demarrer(
            impersonator=request.user, target_user=cible, organization=organisation,
            reason=request.data.get('reason', ''), request=request,
        )
        return Response({
            'access': jeton,
            'session_id': str(session.pk),
            'expires_at': session.expires_at,
            'target_user': {
                'id': str(cible.pk), 'email': cible.email, 'full_name': cible.full_name,
            },
            'organization': {'id': str(organisation.pk), 'name': organisation.name},
        }, status=status.HTTP_201_CREATED)


# =============================================================================
# PLATFORM ADMINS
# =============================================================================

class AdminPlatformAdminViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Qui administre la plateforme, et qui l'a administrée.

    Les profils RÉVOQUÉS restent listés (`?is_active=false`) : la question « qui
    avait ce pouvoir au moment de l'incident » ne se pose qu'après coup.
    """
    permission_classes = GARDES
    serializer_class = PlatformAdminProfileSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    filterset_fields = ['is_active', 'role']
    action_permissions = {
        'list': 'platform_admins.view',
        'retrieve': 'platform_admins.view',
    }

    def get_queryset(self):
        return (
            PlatformAdminProfile.objects
            .select_related('user', 'granted_by', 'revoked_by')
            .order_by('-is_active', 'role', 'user__email')
        )


# =============================================================================
# AUDIT LOG
# =============================================================================

class AdminActionLogFilter(django_filters.FilterSet):
    """
    ⚠ LES BORNES SONT LOCALES et `date_to` est INCLUSIVE, comme partout : un acte
    de 23 h 30 à Kinshasa est déjà le lendemain en UTC.
    """
    date_from = django_filters.DateFilter(method='filter_date_from')
    date_to = django_filters.DateFilter(method='filter_date_to')

    class Meta:
        model = AdminActionLog
        fields = ['actor', 'action', 'target_type', 'target_id']

    def _borne(self, jour):
        return timezone.make_aware(datetime.combine(jour, time.min))

    def filter_date_from(self, queryset, name, value):
        return queryset.filter(created_at__gte=self._borne(value))

    def filter_date_to(self, queryset, name, value):
        return queryset.filter(created_at__lt=self._borne(value + timedelta(days=1)))


class AdminActionLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Le journal des actes d'administration. Lecture seule, et c'est sa nature :
    un journal qu'on peut corriger ne prouve plus rien.
    """
    permission_classes = GARDES
    serializer_class = AdminActionLogSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    filterset_class = AdminActionLogFilter
    search_fields = ['target_repr', 'actor_email', 'reason']
    action_permissions = {
        'list': 'audit_log.view',
        'retrieve': 'audit_log.view',
    }

    def get_queryset(self):
        return AdminActionLog.objects.select_related('actor').order_by('-created_at')


# =============================================================================
# PRISE EN MAIN
# =============================================================================

class AdminImpersonationEndView(APIView):
    """
    Termine la prise en main en cours.
    POST /api/v1/platform-admin/impersonation/end/

    ⚠ AUTHENTIFIÉE PAR LE JETON DE PRISE EN MAIN LUI-MÊME : c'est lui que le
    navigateur tient à cet instant, le jeton de l'administrateur étant mis de
    côté. Elle ne passe donc pas par `IsPlatformAdmin` - le compte présenté est
    celui du marchand - et c'est la seule écriture que ce jeton puisse faire.
    """
    permission_classes = [IsAuthenticated]
    http_method_names = ['post', 'options']

    def post(self, request):
        from .impersonation import CLAIM_SESSION, terminer

        jeton = request.auth
        session_id = jeton.get(CLAIM_SESSION) if jeton is not None and jeton.get('imp') else None
        if not session_id:
            raise RefusAdministration("Aucune prise en main en cours.")
        session = ImpersonationSession.objects.filter(pk=session_id).first()
        if session is None:
            raise RefusAdministration("Aucune prise en main en cours.")
        terminer(session, request=request)
        return Response({'message': 'Prise en main terminée.'})


class AdminImpersonationSessionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Qui a pris la main sur qui, quand, combien de temps, et pourquoi.
    """
    permission_classes = GARDES
    serializer_class = ImpersonationSessionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['impersonator', 'target_user', 'organization']
    action_permissions = {
        'list': 'audit_log.view',
        'retrieve': 'audit_log.view',
        'end': 'impersonation.end',
    }

    def get_queryset(self):
        qs = ImpersonationSession.objects.select_related(
            'impersonator', 'target_user', 'organization',
        ).order_by('-started_at')
        # « En cours » se lit à l'instant : une session échue sans fin écrite
        # n'est plus en cours, même avant le balayage nocturne.
        if self.request.query_params.get('en_cours') in ('1', 'true'):
            qs = qs.filter(ended_at__isnull=True, expires_at__gt=timezone.now())
        return qs

    @action(detail=True, methods=['post'])
    def end(self, request, pk=None):
        """
        Arrête la prise en main d'un autre administrateur.

        Réservé au super-administrateur, motivé, et attribué à lui dans le
        journal : c'est lui qui a décidé de couper l'accès.
        """
        from .impersonation import terminer

        motif = AdminReasonSerializer(data=request.data, requis=True)
        motif.is_valid(raise_exception=True)
        session = self.get_object()
        # ⚠ Une session ÉCHUE dont la fin n'est pas encore écrite ne se « révoque »
        # pas : l'accès a cessé de lui-même, et le journal attribuerait sinon à ce
        # super-administrateur une coupure qu'il n'a pas faite. Le balayage
        # nocturne l'écrira `EXPIRED`.
        if session.ended_at is not None or session.expires_at <= timezone.now():
            raise RefusAdministration("Cette prise en main est déjà terminée.")
        session = terminer(
            session, motif=ImpersonationSession.EndReason.REVOKED, request=request,
            acteur=request.user, raison=motif.validated_data['reason'],
        )
        return Response(ImpersonationSessionSerializer(session).data)


# =============================================================================
# PLANS
# =============================================================================

class AdminPlanViewSet(viewsets.ModelViewSet):
    """
    Admin CRUD for subscription plans.

    ⚠ UN PLAN NE SE SUPPRIME PAS, il se retire de la vente (`toggle_active`).
    `Subscription.plan` est en `PROTECT` : supprimer un plan souscrit levait une
    `ProtectedError` non rattrapée, donc un 500 ; supprimer un plan jamais souscrit
    effaçait la ligne de prix qu'une facture passée désigne peut-être encore.
    """
    permission_classes = GARDES
    http_method_names = SANS_SUPPRESSION
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'code']
    ordering = ['sort_order', 'price_monthly']

    action_permissions = {
        'list': 'plans.view',
        'retrieve': 'plans.view',
        'create': 'plans.create',
        'partial_update': 'plans.edit',
        'toggle_active': 'plans.toggle_active',
    }

    def get_queryset(self):
        return (
            Plan.objects.all().select_related('currency').prefetch_related('plan_features')
            .annotate(nb_abonnes=Count(
                'subscriptions',
                filter=Q(subscriptions__status__in=['trial', 'active', 'past_due']),
                distinct=True,
            ))
        )

    def get_serializer_class(self):
        if self.action in ['create', 'partial_update']:
            return AdminPlanCreateUpdateSerializer
        return AdminPlanSerializer

    def _lu(self, plan):
        return AdminPlanSerializer(self.get_queryset().get(pk=plan.pk)).data

    def create(self, request, *args, **kwargs):
        serializer = AdminPlanCreateUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            plan = serializer.save()
            AuditLogService.record(
                actor=request.user, action=Action.CREATE,
                target_type=Cible.PLAN, target_id=plan.pk, target_repr=plan.name,
                after={'code': plan.code, 'price_monthly': str(plan.price_monthly)},
                request=request,
            )
        return Response(self._lu(plan), status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        plan = self.get_object()
        serializer = AdminPlanCreateUpdateSerializer(plan, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        avant, apres = _ecart(plan, serializer.validated_data)
        with transaction.atomic():
            serializer.save()
            if avant:
                AuditLogService.record(
                    actor=request.user, action=Action.UPDATE,
                    target_type=Cible.PLAN, target_id=plan.pk, target_repr=plan.name,
                    before=avant, after=apres, request=request,
                )
        return Response(self._lu(plan))

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Retire un plan de la vente, ou l'y remet. Les abonnés en cours le gardent."""
        motif = AdminReasonSerializer(data=request.data)
        motif.is_valid(raise_exception=True)
        with transaction.atomic():
            plan = Plan.objects.select_for_update().get(pk=self.get_object().pk)
            plan.is_active = not plan.is_active
            plan.save(update_fields=['is_active', 'updated_at'])
            AuditLogService.record(
                actor=request.user, action=Action.TOGGLE_ACTIVE,
                target_type=Cible.PLAN, target_id=plan.pk, target_repr=plan.name,
                before={'is_active': not plan.is_active}, after={'is_active': plan.is_active},
                reason=motif.validated_data['reason'], request=request,
            )
        return Response(self._lu(plan))


# =============================================================================
# SUBSCRIPTIONS
# =============================================================================

def _debut_local(jour):
    return timezone.make_aware(datetime.combine(jour, time.min))


class AdminSubscriptionViewSet(viewsets.ModelViewSet):
    """
    Admin management of all subscriptions on the platform.
    Supports listing, creating manually, extending and cancelling.
    """
    permission_classes = GARDES
    serializer_class = AdminSubscriptionListSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['organization__name']
    ordering_fields = ['created_at', 'current_period_end']
    ordering = ['-created_at']
    filterset_fields = ['status', 'billing_cycle', 'plan', 'organization']
    http_method_names = ['get', 'post', 'head', 'options']

    action_permissions = {
        'list': 'subscriptions.view',
        'retrieve': 'subscriptions.view',
        'create': 'subscriptions.create',
        'extend': 'subscriptions.extend',
        'cancel': 'subscriptions.cancel',
    }

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
        start_date = serializer.validated_data.get('start_date')

        from apps.subscriptions.services import SubscriptionService
        with transaction.atomic():
            subscription = SubscriptionService.activate_subscription(
                organization=organization,
                plan=plan,
                billing_cycle=billing_cycle,
                activated_by=request.user,
                notes=notes,
            )
            # ⚠ La date de début était VALIDÉE puis ignorée : l'abonnement partait
            # toujours d'aujourd'hui. On recale la période ICI, sans toucher à
            # `activate_subscription`, que le paiement Moko emprunte aussi.
            if start_date is not None and start_date < timezone.localdate():
                duree = subscription.current_period_end - subscription.current_period_start
                debut = _debut_local(start_date)
                fin = debut + duree
                if fin <= timezone.now():
                    raise RefusAdministration(
                        "Avec cette date de début, la période serait déjà échue."
                    )
                subscription.current_period_start = debut
                subscription.current_period_end = fin
                subscription.save(update_fields=[
                    'current_period_start', 'current_period_end', 'updated_at',
                ])
            AuditLogService.record(
                actor=request.user, action=Action.ACTIVATE_SUBSCRIPTION,
                target_type=Cible.SUBSCRIPTION, target_id=subscription.pk,
                target_repr=f'{organization.name} · {plan.name}',
                after={
                    'plan': plan.code, 'billing_cycle': billing_cycle,
                    'current_period_start': subscription.current_period_start.isoformat(),
                    'current_period_end': subscription.current_period_end.isoformat(),
                },
                reason=notes, request=request,
            )

        return Response(
            AdminSubscriptionListSerializer(subscription).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def extend(self, request, pk=None):
        """Extend a subscription by N days."""
        serializer = AdminExtendSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        days = serializer.validated_data['days']

        with transaction.atomic():
            subscription = (
                Subscription.objects.select_for_update()
                .select_related('organization', 'plan').get(pk=self.get_object().pk)
            )
            if subscription.status == Subscription.Status.CANCELLED:
                raise RefusAdministration(
                    "Un abonnement annulé ne se prolonge pas : créez-en un nouveau."
                )
            avant = {
                'status': subscription.status,
                'current_period_end': subscription.current_period_end.isoformat(),
            }
            now = timezone.now()
            depart = max(now, subscription.current_period_end) if subscription.current_period_end else now
            subscription.current_period_end = depart + timedelta(days=days)
            if subscription.status in [Subscription.Status.EXPIRED, Subscription.Status.PAST_DUE]:
                subscription.status = Subscription.Status.ACTIVE
            subscription.save(update_fields=['current_period_end', 'status', 'updated_at'])
            AuditLogService.record(
                actor=request.user, action=Action.EXTEND_SUBSCRIPTION,
                target_type=Cible.SUBSCRIPTION, target_id=subscription.pk,
                target_repr=f'{subscription.organization.name} · {subscription.plan.name}',
                before=avant,
                after={
                    'status': subscription.status, 'days': days,
                    'current_period_end': subscription.current_period_end.isoformat(),
                },
                reason=serializer.validated_data['reason'], request=request,
            )

        return Response({
            'message': f"Abonnement prolongé de {days} jours.",
            'new_end_date': subscription.current_period_end,
            'status': subscription.status,
        })

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a subscription."""
        motif = AdminReasonSerializer(data=request.data, requis=True)
        motif.is_valid(raise_exception=True)
        with transaction.atomic():
            subscription = (
                Subscription.objects.select_for_update()
                .select_related('organization', 'plan').get(pk=self.get_object().pk)
            )
            if subscription.status in [Subscription.Status.CANCELLED, Subscription.Status.EXPIRED]:
                raise RefusAdministration("Cet abonnement n'est plus en cours.")
            avant = subscription.status
            subscription.status = Subscription.Status.CANCELLED
            subscription.cancelled_at = timezone.now()
            subscription.save(update_fields=['status', 'cancelled_at', 'updated_at'])
            AuditLogService.record(
                actor=request.user, action=Action.CANCEL_SUBSCRIPTION,
                target_type=Cible.SUBSCRIPTION, target_id=subscription.pk,
                target_repr=f'{subscription.organization.name} · {subscription.plan.name}',
                before={'status': avant}, after={'status': subscription.status},
                reason=motif.validated_data['reason'], request=request,
            )

        return Response({'message': 'Abonnement annulé.'})


# =============================================================================
# PORTE DE MISE A JOUR MOBILE
# =============================================================================

class AdminAppReleaseView(APIView):
    """
    La porte de mise à jour du terminal mobile.
    GET / PATCH /api/v1/platform-admin/app-release/

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ CE FORMULAIRE PEUT BLOQUER TOUT LE PARC, ET LE DÉBLOQUER.                │
    │                                                                          │
    │ Poser un seuil au-dessus de ce que le magasin sert rend chaque terminal  │
    │ inutilisable sans qu'aucun ne puisse s'y conformer. Vider le champ est   │
    │ la seule réparation, et elle agit au réveil suivant parce que            │
    │ `GlobalConfig.save()` purge le cache des réglages.                       │
    └──────────────────────────────────────────────────────────────────────────┘

    Un singleton, donc ni liste ni création : `GET` lit, `PATCH` corrige. Un
    `PUT` exigerait les huit champs et ferait effacer une plateforme en voulant
    régler l'autre.

    ⚠ L'écriture est réservée au super-admin : c'est l'acte de la plateforme qui
    a le plus de portée, et le seul qui ne se répare depuis aucun terminal.
    """

    permission_classes = GARDES
    http_method_names = ['get', 'patch', 'options', 'head']
    action_permissions = {
        'get': 'app_release.view',
        'patch': 'app_release.edit',
    }

    def get(self, request):
        return Response(AdminAppReleaseSerializer(GlobalConfig.get()).data)

    def patch(self, request):
        config = GlobalConfig.get()
        # ⚠ L'ÉTAT « AVANT » SE RELÈVE AVANT `is_valid()` : `validate` écrit sur
        # l'instance pour confronter le PATCH à ce qui est enregistré.
        avant_complet = dict(AdminAppReleaseSerializer(config).data)
        serializer = AdminAppReleaseSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            serializer.save()
            apres_complet = dict(serializer.data)
            avant = {k: v for k, v in avant_complet.items() if apres_complet.get(k) != v}
            apres = {k: apres_complet[k] for k in avant}
            if avant:
                AuditLogService.record(
                    actor=request.user, action=Action.UPDATE_APP_RELEASE,
                    target_type=Cible.APP_RELEASE, target_id='global',
                    target_repr='Porte de mise à jour', before=avant, after=apres,
                    request=request,
                )
        return Response(serializer.data)
