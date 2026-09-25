"""
Terminaux enrôlés et sessions longues.

La plateforme délivre des JWT courts : accès 30 minutes, rafraîchissement
7 jours, avec rotation et liste noire. C'est un bon réglage pour un navigateur.
C'est intenable pour un point de vente en RDC, où une boutique peut rester une
semaine sans réseau : au retour, le caissier tombait sur un écran de connexion
qu'il ne pouvait pas franchir faute de connexion, caisse ouverte et clients
devant lui.

Le jeton d'appareil comble cet écart sans affaiblir le reste. Il
**n'authentifie aucun endpoint métier** : il sert uniquement à réobtenir une
paire JWT. Son rayon d'action est donc minimal, il se révoque depuis le
back-office, et son échéance glisse à chaque usage plutôt que d'être fixe.

`POST /auth/devices/session/` fait tout en un aller-retour : jetons, identité,
permissions, paramètres, devises, fidélité. C'est le chemin de réveil d'un
terminal resté trois semaines dans le noir, et il doit tenir en une requête,
parce que la connexion qui vient de revenir peut repartir.
"""
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.api_permissions import HasPermission, IsTenantMember, _get_membership, DENY
from apps.core.services import PermissionService
from apps.organizations.models import Organization

from .models import Device

# Nombre de tentatives d'attribution du code d'appareil. Une collision sur
# 32^4 possibilités est déjà improbable ; cinq essais la rendent négligeable.
CODE_ATTEMPTS = 5


# ---------------------------------------------------------------- sérialiseurs


class DeviceSerializer(serializers.ModelSerializer):
    """Ce que le gérant lit dans la liste des appareils."""

    user_name = serializers.CharField(source='user.full_name', read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    revoked_by_name = serializers.CharField(
        source='revoked_by.full_name', read_only=True, default=None
    )
    is_usable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Device
        fields = [
            'id', 'name', 'device_code', 'platform', 'model', 'os_version',
            'app_version', 'user', 'user_name', 'user_email',
            'created_at', 'last_seen_at', 'last_ip', 'expires_at',
            'revoked_at', 'revoked_by', 'revoked_by_name', 'is_usable',
        ]
        read_only_fields = [
            'id', 'device_code', 'user', 'created_at', 'last_seen_at',
            'last_ip', 'expires_at', 'revoked_at', 'revoked_by',
        ]


class DeviceEnrollSerializer(serializers.Serializer):
    """Saisie de l'enrôlement, sous une session déjà authentifiée."""

    name = serializers.CharField(max_length=120)
    platform = serializers.ChoiceField(choices=Device.Platform.choices)
    model = serializers.CharField(max_length=120, required=False, allow_blank=True)
    os_version = serializers.CharField(max_length=40, required=False, allow_blank=True)
    app_version = serializers.CharField(max_length=40, required=False, allow_blank=True)


class DeviceSessionSerializer(serializers.Serializer):
    device_token = serializers.CharField()


# -------------------------------------------------------------------- services


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def enroll_device(user, organization, *, name, platform, model='', os_version='',
                  app_version='', ip=None):
    """
    Crée le terminal et retourne `(device, raw_token)`.

    Le jeton brut n'existe qu'ici : seule son empreinte est stockée, et il n'est
    montré qu'une fois. Un terminal qui le perd se réenrôle, il ne le récupère
    pas.
    """
    raw_token = Device.generate_token()

    for attempt in range(CODE_ATTEMPTS):
        code = Device.generate_device_code()
        try:
            with transaction.atomic():
                device = Device.objects.create(
                    user=user,
                    organization=organization,
                    name=name.strip() or 'Terminal',
                    platform=platform,
                    model=model or '',
                    os_version=os_version or '',
                    app_version=app_version or '',
                    device_code=code,
                    token_hash=Device.hash_token(raw_token),
                    expires_at=Device.default_expiry(),
                    last_seen_at=timezone.now(),
                    last_ip=ip,
                )
            return device, raw_token
        except IntegrityError:
            # Code déjà pris dans cette organisation : on retire.
            if attempt == CODE_ATTEMPTS - 1:
                raise

    raise IntegrityError("Impossible d'attribuer un code d'appareil")


def resolve_device(raw_token):
    """
    Retrouve le terminal derrière un jeton brut, ou `None`.

    On recherche par empreinte : le jeton en clair ne touche jamais la base.
    """
    if not raw_token:
        return None
    return (
        Device.objects
        .select_related('user', 'organization')
        .filter(token_hash=Device.hash_token(raw_token))
        .first()
    )


def subscription_payload(organization, membership):
    """
    L'abonnement, tel qu'un TERMINAL doit le comprendre.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ IL DESCEND PAR ICI PARCE QU'IL NE PEUT PAS DESCENDRE AILLEURS.           │
    │                                                                          │
    │ `/subscriptions/status/` exige la permission `subscription.view`, qui    │
    │ n'est accordée qu'au PROPRIÉTAIRE. Or un terminal est tenu par un        │
    │ caissier : une porte d'abonnement adossée à cet endpoint ne se           │
    │ fermerait jamais pour la population même qu'il faut retenir. Ce chemin   │
    │ de réveil, lui, sert tous les rôles, et il répond même quand             │
    │ l'organisation est bloquée (`AllowAny`, jeton d'appareil) - sans quoi    │
    │ un marchand qui vient de payer ne l'apprendrait jamais.                  │
    └──────────────────────────────────────────────────────────────────────────┘

    **`access_until` est la pièce maîtresse.** C'est la date jusqu'à laquelle le
    serveur laisserait écrire, grâce comprise. Le terminal n'a rien d'autre à
    comprendre : hors ligne, il la compare à son horloge et tranche seul. Sans
    elle, un mode avion offrirait des mois gratuits, puisque le dernier verdict
    connu resterait « non bloqué » indéfiniment.

    ⚠ **On passe par `get_subscription_status` et non par le cache allégé** :
    celui-ci ne porte ni `current_period_end` ni `grace_end`, donc pas de quoi
    calculer `access_until`. C'est un chemin FROID - une fois par réveil de
    session - et non le chemin chaud des requêtes d'écriture.
    """
    from datetime import timedelta

    from apps.subscriptions.models import GlobalConfig
    from apps.subscriptions.services import SubscriptionService

    etat = SubscriptionService.get_subscription_status(organization)
    subscription = etat.get('subscription')

    access_until = None
    if subscription is not None and subscription.current_period_end:
        access_until = subscription.current_period_end + timedelta(
            days=GlobalConfig.get().grace_period_days
        )

    return {
        'is_blocked': etat['is_blocked'],
        'status': etat['status'],
        'message': etat.get('message'),
        'access_until': access_until,
        'days_remaining': etat.get('days_remaining'),
        # Le plan EN COURS, pour que le terminal propose de le prolonger plutôt
        # qu'un autre : on renouvelle bien plus souvent qu'on ne change d'offre.
        'plan_id': str(subscription.plan_id) if subscription else None,
        'plan_name': subscription.plan.name if subscription else None,
        # Régler l'abonnement est réservé au propriétaire (`moko_initiate` est
        # `IsTenantOwner`). Le dire ici évite d'offrir à un caissier un bouton
        # que le serveur refusera une fois son numéro saisi.
        'can_manage': bool(membership and membership.role == 'owner'),
    }


#: Plafond du roster descendu dans la session.
#:
#: Une liste non bornée dans une réponse servie à CHAQUE réveil, sur une 2G
#: congolaise, est la façon dont un démarrage à froid devient un délai d'attente.
#: Au-delà, `truncated` le dit : une organisation de cinq cents comptes n'utilise
#: pas un sélecteur déroulant, elle a besoin d'une recherche serveur.
MAX_MEMBRES_EQUIPE = 200


def build_team_payload(organization, membership):
    """
    L'équipe, telle qu'un filtre « Utilisateur » a besoin de la connaître.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN SEUL CONSTRUCTEUR, DEUX PORTES.                                      │
    │                                                                          │
    │ La session (terminal) et `GET /memberships/team/` (back-office) rendent  │
    │ EXACTEMENT ce payload. Deux constructions finiraient par proposer deux   │
    │ listes d'utilisateurs pour le même entrepôt, et le marchand n'aurait     │
    │ aucun moyen de savoir laquelle croire.                                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `visible` EST EXPLICITE, JAMAIS DÉDUIT D'UNE LISTE VIDE.                │
    │                                                                          │
    │ C'est ce qui permet au client de distinguer « roster inconnu » (un       │
    │ instantané mis en cache avant ce lot n'a pas la clé) de « roster vide ». │
    │ Lire l'un pour l'autre afficherait « aucun collègue » à un propriétaire  │
    │ qui en a cinquante : `null` ne se lit jamais comme zéro.                 │
    └──────────────────────────────────────────────────────────────────────────┘

    La borne est le RÔLE, pas `users.view` : cette permission n'est accordée
    qu'au propriétaire et au gérant, or la règle donne le filtre utilisateur au
    MAGASINIER aussi. Un caissier, lui, n'en a aucun usage - ses deux filtres
    sont verrouillés sur lui-même - et son terminal est le plus exposé du parc.

    ⚠ Ni email, ni téléphone : un sélecteur a besoin d'un NOM, et ce payload
    part à chaque réveil. Les identifiants d'entrepôt voyagent nus, leurs noms
    descendant déjà par la table `warehouses` du tirage.
    """
    from apps.core.warehouse_scope import membership_targets_q
    from apps.inventory.models import Warehouse
    from apps.organizations.models import OrganizationMembership

    if membership is None or membership.role == OrganizationMembership.Role.CASHIER:
        return {'visible': False, 'truncated': False, 'members': []}

    # DEUX requêtes, constantes quel que soit l'effectif. Un
    # `assigned_warehouses` par membre en coûterait 1+N, soit cinquante et une
    # pour cinquante employés, à chaque réveil de chaque terminal.
    lignes = (
        OrganizationMembership.objects
        .filter(organization=organization, is_active=True)
        .select_related('user')
        .prefetch_related(Prefetch(
            'assigned_warehouses',
            queryset=Warehouse.objects.filter(is_deleted=False).only('id'),
        ))
    )

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ ON NE PROPOSE QUE CE QUE LA VALIDATION ACCEPTERA.                       │
    # │                                                                          │
    # │ Le roster rendait TOUS les membres actifs, sans aucune intersection      │
    # │ avec le périmètre du demandeur : un gérant en choisissait un hors de     │
    # │ ses dépôts, et `assert_user_allowed_for_membership` rendait 400 sur un   │
    # │ nom que l'application venait de lui proposer. Le prédicat est donc       │
    # │ le MÊME des deux côtés, et il vit dans `warehouse_scope`.                │
    # │                                                                          │
    # │ ⚠ LA BORNE EST EN SQL, ET AVANT LE PLAFOND. Filtrer en Python après la   │
    # │ tranche ferait disparaître les collègues d'un gérant dès que            │
    # │ l'organisation dépasse deux cents comptes - un défaut qui ne se voit     │
    # │ qu'à cette taille-là.                                                    │
    # │                                                                          │
    # │ ⚠ `.distinct()` : le prédicat traverse un M2M, et un membre affecté à    │
    # │ deux de mes dépôts sortirait en double, donc compterait deux fois        │
    # │ contre le plafond.                                                       │
    # └──────────────────────────────────────────────────────────────────────────┘
    cibles = membership_targets_q(membership)
    if cibles is not None:
        lignes = lignes.filter(cibles).distinct()

    lignes = list(
        lignes
        .order_by('user__first_name', 'user__last_name', 'user__email')
        [: MAX_MEMBRES_EQUIPE + 1]
    )
    truncated = len(lignes) > MAX_MEMBRES_EQUIPE
    lignes = lignes[:MAX_MEMBRES_EQUIPE]

    return {
        'visible': True,
        'truncated': truncated,
        'members': [
            {
                'user_id': str(m.user_id),
                # Un nom vide rendrait une ligne muette dans le sélecteur ;
                # l'email est le seul repli qui identifie encore quelqu'un.
                'name': (m.user.full_name or '').strip() or m.user.email,
                'role': m.role,
                'warehouses': [str(w.id) for w in m.assigned_warehouses.all()],
            }
            for m in lignes
        ],
    }


def build_session_payload(user, organization, device=None):
    """
    Tout ce qu'il faut pour travailler hors ligne, en une seule réponse.

    Ce n'est pas une commodité : la connexion qui vient de revenir peut repartir
    avant le deuxième appel. Ce qui manquerait ici manquerait pour la journée.
    """
    from apps.organizations.serializers import OrganizationDetailSerializer
    from apps.settings.models import LoyaltyProgram, OrganizationCurrency, OrganizationSettings
    from apps.settings.serializers import (
        LoyaltyProgramSerializer, OrganizationCurrencySerializer,
        OrganizationSettingsSerializer,
    )

    refresh = RefreshToken.for_user(user)

    membership = user.memberships.filter(
        organization=organization, is_active=True
    ).first()

    permissions, role, warehouses = [], None, []
    if membership:
        role = membership.role
        permissions = PermissionService.get_effective_permissions(membership)
        warehouses = [
            {'id': str(w.id), 'name': w.name}
            for w in membership.assigned_warehouses.filter(is_deleted=False)
        ]

    team = build_team_payload(organization, membership)

    currencies = OrganizationCurrency.objects.filter(
        organization=organization, is_active=True
    ).select_related('currency')

    settings_row = OrganizationSettings.objects.filter(
        organization=organization
    ).first()

    loyalty = LoyaltyProgram.objects.filter(
        organization=organization, is_active=True
    ).first()

    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'user': {
            'id': str(user.id),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'full_name': user.full_name,
            'phone': user.phone,
            'is_staff': user.is_staff,
        },
        'organization': OrganizationDetailSerializer(organization).data,
        'membership': {
            'role': role,
            'permissions': permissions,
            'assigned_warehouses': warehouses,
        },
        # Sœur de `membership`, et non dedans : `membership` décrit le PORTEUR,
        # `team` décrit l'organisation.
        'team': team,
        'settings': (
            OrganizationSettingsSerializer(settings_row).data if settings_row else None
        ),
        'currencies': OrganizationCurrencySerializer(currencies, many=True).data,
        'loyalty_program': (
            LoyaltyProgramSerializer(loyalty).data if loyalty else None
        ),
        'subscription': subscription_payload(organization, membership),
        'device': (
            {
                'id': str(device.id),
                'device_code': device.device_code,
                'name': device.name,
                'expires_at': device.expires_at,
            }
            if device else None
        ),
        'server_time': timezone.now(),
    }


# ------------------------------------------------------------------------ vues


class DeviceEnrollView(APIView):
    """
    `POST /api/v1/auth/devices/enroll/`

    Sous une session déjà authentifiée, avec `X-Organization-ID`. Retourne le
    jeton brut, **une seule fois**, ainsi que le nécessaire pour démarrer hors
    ligne : c'est le seul appel de l'enrôlement.
    """

    permission_classes = [IsAuthenticated, IsTenantMember]

    @extend_schema(
        summary="Enrôler un terminal",
        request=DeviceEnrollSerializer,
        responses={201: None},
    )
    def post(self, request):
        serializer = DeviceEnrollSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        membership = _get_membership(request)
        organization = membership.organization

        device, raw_token = enroll_device(
            request.user,
            organization,
            ip=client_ip(request),
            **serializer.validated_data,
        )

        payload = build_session_payload(request.user, organization, device)
        # Le jeton brut ne réapparaîtra jamais : le client DOIT le ranger
        # maintenant, avant même de traiter le reste de la réponse.
        payload['device_token'] = raw_token

        return Response(payload, status=status.HTTP_201_CREATED)


class DeviceSessionView(APIView):
    """
    `POST /api/v1/auth/devices/session/`

    Chemin de réveil. Ouvert sans authentification puisque c'est précisément ce
    qu'il délivre, mais fortement limité en débit : un jeton d'appareil est un
    secret long, il ne se devine pas, et cinq essais par minute suffisent
    largement à un usage légitime.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'device_session'

    @extend_schema(
        summary="Ouvrir une session depuis un terminal enrôlé",
        request=DeviceSessionSerializer,
        responses={200: None},
    )
    def post(self, request):
        serializer = DeviceSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device = resolve_device(serializer.validated_data['device_token'])

        # Un jeton inconnu et un jeton révoqué donnent la même réponse : rien ne
        # doit permettre de distinguer les deux depuis l'extérieur.
        if device is None or not device.is_usable:
            return Response(
                {
                    'detail': "Cet appareil n'est plus autorisé. "
                              "Reconnectez-vous avec votre mot de passe.",
                    'code': 'device_not_authorized',
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not device.user.is_active:
            return Response(
                {
                    'detail': "Ce compte a été désactivé.",
                    'code': 'user_inactive',
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        membership = device.user.memberships.filter(
            organization=device.organization, is_active=True
        ).first()
        if membership is None:
            return Response(
                {
                    'detail': "Vous n'avez plus accès à cet établissement.",
                    'code': 'membership_revoked',
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        device.touch(ip=client_ip(request))
        return Response(build_session_payload(device.user, device.organization, device))


class DeviceViewSet(viewsets.ModelViewSet):
    """
    Appareils de l'organisation courante.

    Consultation ouverte à qui peut voir les utilisateurs ; révocation et
    renommage à qui peut les modifier. Un caissier ne révoque pas la caisse
    d'à côté.
    """

    serializer_class = DeviceSerializer
    # `HasPermission` est ce qui LIT `action_permissions` : sans lui la table
    # ci-dessous ne serait qu'un commentaire, et un caissier révoquerait la
    # caisse d'à côté.
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    http_method_names = ['get', 'patch', 'post', 'head', 'options']

    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'partial_update': 'users.edit',
        'revoke': 'users.edit',
        # Un appareil s'enrôle par `POST /auth/devices/enroll/`, sous une
        # session déjà authentifiée : c'est là que le jeton est émis, une
        # seule fois. Créer un Device par cette route ne produirait rien
        # d'utilisable.
        'create': DENY,
    }

    def get_queryset(self):
        org_id = self.request.headers.get('X-Organization-ID')
        return (
            Device.objects
            .filter(organization_id=org_id)
            .select_related('user', 'revoked_by')
        )

    def get_serializer_class(self):
        return DeviceSerializer

    @action(detail=True, methods=['post'])
    def revoke(self, request, pk=None):
        """
        Coupe l'accès serveur du terminal, immédiatement.

        N'efface RIEN sur l'appareil : les ventes non synchronisées y restent, et
        un réenrôlement les enverra. Révoquer n'est pas effacer.
        """
        device = self.get_object()
        device.revoke(by=request.user)
        return Response(DeviceSerializer(device).data)
