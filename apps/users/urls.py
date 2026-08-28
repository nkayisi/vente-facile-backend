"""
URLs pour l'app Users.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .devices import DeviceEnrollView, DeviceSessionView, DeviceViewSet
from .views import (
    UserViewSet, UserActivityViewSet,
    RegisterView, RegisterWithOrganizationView, LoginView, LogoutView,
    ResetPasswordRequestView, ResetPasswordConfirmView
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'user-activities', UserActivityViewSet, basename='user-activity')
router.register(r'auth/devices', DeviceViewSet, basename='device')

app_name = 'users'

urlpatterns = [
    # Auth endpoints
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/register-with-organization/', RegisterWithOrganizationView.as_view(), name='register-with-organization'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/password-reset/', ResetPasswordRequestView.as_view(), name='password-reset'),
    path('auth/password-reset/confirm/', ResetPasswordConfirmView.as_view(), name='password-reset-confirm'),

    # Terminaux enrôlés. `enroll` exige une session ouverte ; `session` est le
    # chemin de réveil d'un appareil resté hors ligne, et se suffit du jeton
    # d'appareil. Déclarés AVANT le routeur pour que `auth/devices/enroll/` ne
    # soit pas capté comme un détail de `auth/devices/{pk}/`.
    path('auth/devices/enroll/', DeviceEnrollView.as_view(), name='device-enroll'),
    path('auth/devices/session/', DeviceSessionView.as_view(), name='device-session'),
    
    # Router URLs
    path('', include(router.urls)),
]
