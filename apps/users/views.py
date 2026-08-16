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

from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string

from apps.core.api_mixins import TenantViewSetMixin
from apps.core.api_permissions import IsTenantMember, IsTenantAdmin, HasPermission
from .models import User, UserActivity
from .serializers import (
    UserListSerializer, UserDetailSerializer,
    UserCreateSerializer, UserUpdateSerializer,
    ChangePasswordSerializer, ResetPasswordRequestSerializer, ResetPasswordConfirmSerializer,
    UserActivitySerializer, LoginSerializer, TokenResponseSerializer,
    RegisterWithOrganizationSerializer, CustomTokenObtainPairSerializer
)


class CustomTokenObtainPairView(APIView):
    """
    Custom JWT token endpoint that returns user info (including is_staff).
    Replaces the default TokenObtainPairView.
    POST /api/v1/auth/token/
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CustomTokenObtainPairSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)


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
    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'permissions': '*',
        'me': '*',
        'change_password': '*',
        'activities': '*',
    }

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
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated(), IsTenantMember(), HasPermission()]
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
        
        Retourne:
        - role: rôle du membre
        - role_display: nom affiché du rôle
        - role_permissions: permissions héritées du rôle
        - extra_permissions: permissions additionnelles accordées individuellement
        - permissions: permissions effectives (role + extra)
        - manageable_roles: rôles que cet utilisateur peut gérer
        - all_permissions: liste de toutes les permissions du système (pour UI)
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
        
        role_permissions = PermissionService.get_role_permissions(membership.role)
        extra_permissions = membership.extra_permissions or []
        effective_permissions = PermissionService.get_effective_permissions(membership)
        manageable_roles = OrganizationMembership.MANAGEABLE_ROLES.get(membership.role, [])
        
        return Response({
            'role': membership.role,
            'role_display': membership.get_role_display(),
            'role_permissions': role_permissions,
            'extra_permissions': extra_permissions,
            'permissions': effective_permissions,
            'manageable_roles': [
                {'value': r, 'label': dict(OrganizationMembership.Role.choices).get(r, r)}
                for r in manageable_roles
            ],
            'all_permissions': PermissionService.get_all_permissions(),
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
        """Retourne les activités de l'utilisateur connecté avec pagination."""
        activities = UserActivity.objects.filter(
            user=request.user
        ).order_by('-created_at')
        
        page = self.paginate_queryset(activities)
        if page is not None:
            serializer = UserActivitySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
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
    Génère un token et envoie un email avec le lien de réinitialisation.
    
    POST /auth/password-reset/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        email = serializer.validated_data['email']
        user = User.objects.filter(email=email, is_active=True).first()
        
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            
            frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3005')
            reset_link = f"{frontend_url}/auth/reset-password?uid={uid}&token={token}"
            
            subject = "Vente Facile - Réinitialisation de votre mot de passe"
            message = (
                f"Bonjour {user.first_name or user.email},\n\n"
                f"Vous avez demandé la réinitialisation de votre mot de passe.\n\n"
                f"Cliquez sur le lien ci-dessous pour choisir un nouveau mot de passe :\n"
                f"{reset_link}\n\n"
                f"Ce lien est valide pendant 24 heures.\n\n"
                f"Si vous n'avez pas fait cette demande, ignorez simplement cet email.\n\n"
                f"- L'équipe Vente Facile"
            )
            
            try:
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    fail_silently=False,
                )
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Erreur envoi email reset password à {email}: {e}")
        
        # Toujours retourner succès pour éviter l'énumération d'emails
        return Response({
            'message': 'Si cet email existe, un lien de réinitialisation a été envoyé.'
        })


class ResetPasswordConfirmView(APIView):
    """
    Vue pour confirmer la réinitialisation de mot de passe.
    Valide le token et applique le nouveau mot de passe.
    
    POST /auth/password-reset/confirm/
    """
    
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        token = serializer.validated_data['token']
        uid_b64 = serializer.validated_data['uid']
        new_password = serializer.validated_data['new_password']
        
        try:
            uid = force_str(urlsafe_base64_decode(uid_b64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response(
                {'error': 'Le lien de réinitialisation est invalide.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not default_token_generator.check_token(user, token):
            return Response(
                {'error': 'Le lien de réinitialisation a expiré ou est invalide.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user.set_password(new_password)
        user.save()
        
        UserActivity.objects.create(
            user=user,
            action='update',
            resource_type='password',
            details={'method': 'password_reset'},
        )
        
        return Response({'message': 'Mot de passe réinitialisé avec succès.'})


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
