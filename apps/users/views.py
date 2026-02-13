"""
ViewSets DRF pour l'app Users.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from apps.core.api_mixins import TenantViewSetMixin
from apps.core.api_permissions import IsTenantMember, IsTenantAdmin
from .models import User, UserActivity
from .serializers import (
    UserListSerializer, UserDetailSerializer,
    UserCreateSerializer, UserUpdateSerializer,
    ChangePasswordSerializer, ResetPasswordRequestSerializer, ResetPasswordConfirmSerializer,
    UserActivitySerializer, LoginSerializer, TokenResponseSerializer,
    RegisterWithOrganizationSerializer
)


# =============================================================================
# USER VIEWSET
# =============================================================================

class UserViewSet(viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des utilisateurs.
    
    Endpoints:
    - GET /users/me/ : Profil de l'utilisateur connecté
    - PUT/PATCH /users/me/ : Modifier son profil
    - POST /users/me/change-password/ : Changer son mot de passe
    - GET /users/ : Liste des utilisateurs (admin org)
    """
    
    queryset = User.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['email', 'first_name', 'last_name']

    def get_queryset(self):
        """
        Les utilisateurs ne peuvent voir que les membres de leurs organisations.
        """
        user = self.request.user
        org_id = self.request.headers.get('X-Organization-ID')
        
        if org_id:
            # Utilisateurs de l'organisation spécifiée
            return User.objects.filter(
                memberships__organization_id=org_id,
                memberships__is_active=True
            ).distinct()
        else:
            # Tous les utilisateurs des organisations de l'utilisateur
            org_ids = user.memberships.filter(is_active=True).values_list('organization_id', flat=True)
            return User.objects.filter(
                memberships__organization_id__in=org_ids,
                memberships__is_active=True
            ).distinct()

    def get_serializer_class(self):
        if self.action == 'list':
            return UserListSerializer
        elif self.action in ['update', 'partial_update']:
            return UserUpdateSerializer
        elif self.action == 'change_password':
            return ChangePasswordSerializer
        return UserDetailSerializer

    def get_permissions(self):
        if self.action == 'create':
            return [AllowAny()]
        return super().get_permissions()

    @action(detail=False, methods=['get', 'put', 'patch'])
    def me(self, request):
        """Retourne ou modifie le profil de l'utilisateur connecté."""
        user = request.user
        
        if request.method == 'GET':
            serializer = UserDetailSerializer(user)
            return Response(serializer.data)
        
        serializer = UserUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response(UserDetailSerializer(user).data)

    @action(detail=False, methods=['get'], url_path='me/permissions')
    def permissions(self, request):
        """
        Retourne le rôle et les permissions de l'utilisateur dans l'organisation courante.
        Nécessite le header X-Organization-ID.
        """
        from apps.core.services import PermissionService
        from apps.organizations.models import OrganizationMembership
        
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return Response(
                {'detail': 'Header X-Organization-ID requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        membership = request.user.memberships.filter(
            organization_id=org_id,
            is_active=True
        ).first()
        
        if not membership:
            return Response(
                {'detail': "Vous n'êtes pas membre de cette organisation"},
                status=status.HTTP_403_FORBIDDEN
            )
        
        permissions_list = PermissionService.get_role_permissions(membership.role)
        manageable_roles = OrganizationMembership.MANAGEABLE_ROLES.get(membership.role, [])
        
        return Response({
            'role': membership.role,
            'role_display': membership.get_role_display(),
            'permissions': permissions_list,
            'manageable_roles': [
                {'value': r, 'label': dict(OrganizationMembership.Role.choices).get(r, r)}
                for r in manageable_roles
            ],
        })

    @action(detail=False, methods=['post'], url_path='me/change-password')
    def change_password(self, request):
        """Change le mot de passe de l'utilisateur connecté."""
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        
        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        
        # Logger l'activité
        UserActivity.objects.create(
            user=user,
            action='update',
            resource_type='password',
            ip_address=self._get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({'message': 'Mot de passe modifié avec succès'})

    @action(detail=False, methods=['get'])
    def activities(self, request):
        """Retourne les activités de l'utilisateur connecté."""
        activities = UserActivity.objects.filter(
            user=request.user
        ).order_by('-created_at')[:50]
        
        serializer = UserActivitySerializer(activities, many=True)
        return Response(serializer.data)

    def _get_client_ip(self, request):
        """Récupère l'IP du client."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0]
        return request.META.get('REMOTE_ADDR')


# =============================================================================
# AUTH VIEWS
# =============================================================================

class RegisterView(APIView):
    """
    Vue pour l'inscription d'un nouvel utilisateur.
    
    POST /auth/register/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Générer les tokens
        refresh = RefreshToken.for_user(user)
        
        # Logger l'activité
        UserActivity.objects.create(
            user=user,
            action='create',
            resource_type='user',
            details={'method': 'registration'},
            ip_address=self._get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserDetailSerializer(user).data
        }, status=status.HTTP_201_CREATED)

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0]
        return request.META.get('REMOTE_ADDR')


class RegisterWithOrganizationView(APIView):
    """
    Vue pour l'inscription complète avec création de boutique.
    Crée User + Organization + Subscription en une seule étape.
    
    POST /auth/register-with-organization/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterWithOrganizationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Générer les tokens
        refresh = RefreshToken.for_user(user)
        
        # Logger l'activité
        UserActivity.objects.create(
            user=user,
            organization=user.active_organization,
            action='create',
            resource_type='user',
            details={'method': 'registration_with_organization'},
            ip_address=self._get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserDetailSerializer(user).data,
            'organization': {
                'id': str(user.active_organization.id),
                'name': user.active_organization.name,
                'slug': user.active_organization.slug,
            } if user.active_organization else None
        }, status=status.HTTP_201_CREATED)

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0]
        return request.META.get('REMOTE_ADDR')


class LoginView(APIView):
    """
    Vue pour la connexion (alternative au token JWT standard).
    Retourne les tokens et les informations utilisateur.
    
    POST /auth/login/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']
        
        user = User.objects.filter(email=email).first()
        
        if not user or not user.check_password(password):
            return Response(
                {'error': 'Email ou mot de passe incorrect'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        if not user.is_active:
            return Response(
                {'error': 'Ce compte est désactivé'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        # Mettre à jour last_login
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        
        # Générer les tokens
        refresh = RefreshToken.for_user(user)
        
        # Logger l'activité
        UserActivity.objects.create(
            user=user,
            organization=user.active_organization,
            action='login',
            resource_type='session',
            ip_address=self._get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserDetailSerializer(user).data
        })

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0]
        return request.META.get('REMOTE_ADDR')


class LogoutView(APIView):
    """
    Vue pour la déconnexion (blacklist le refresh token).
    
    POST /auth/logout/
    """
    
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get('refresh')
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            
            # Logger l'activité
            UserActivity.objects.create(
                user=request.user,
                organization=request.user.active_organization,
                action='logout',
                resource_type='session',
                ip_address=self._get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )
            
            return Response({'message': 'Déconnexion réussie'})
        except Exception:
            return Response({'message': 'Déconnexion réussie'})

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0]
        return request.META.get('REMOTE_ADDR')


class ResetPasswordRequestView(APIView):
    """
    Vue pour demander une réinitialisation de mot de passe.
    
    POST /auth/password-reset/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        email = serializer.validated_data['email']
        user = User.objects.filter(email=email).first()
        
        if user:
            # TODO: Générer un token et envoyer l'email
            pass
        
        # Toujours retourner succès pour éviter l'énumération d'emails
        return Response({
            'message': 'Si cet email existe, un lien de réinitialisation a été envoyé.'
        })


class ResetPasswordConfirmView(APIView):
    """
    Vue pour confirmer la réinitialisation de mot de passe.
    
    POST /auth/password-reset/confirm/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # TODO: Valider le token et réinitialiser le mot de passe
        
        return Response({'message': 'Mot de passe réinitialisé avec succès'})


# =============================================================================
# USER ACTIVITY VIEWSET
# =============================================================================

class UserActivityViewSet(TenantViewSetMixin, viewsets.ReadOnlyModelViewSet):
    """
    ViewSet pour consulter les activités utilisateur (audit).
    
    Endpoints:
    - GET /user-activities/ : Liste des activités
    - GET /user-activities/{id}/ : Détail d'une activité
    """
    
    queryset = UserActivity.objects.all()
    serializer_class = UserActivitySerializer
    permission_classes = [IsAuthenticated, IsTenantMember, IsTenantAdmin]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['user', 'action', 'resource_type']
    search_fields = ['user__email', 'resource_type']
    ordering = ['-created_at']
    
    select_related_fields = ['user', 'organization']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.get_organization()
        if organization:
            queryset = queryset.filter(organization=organization)
        return queryset
