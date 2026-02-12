"""
URLs pour l'app Users.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    UserViewSet, UserActivityViewSet,
    RegisterView, RegisterWithOrganizationView, LoginView, LogoutView,
    ResetPasswordRequestView, ResetPasswordConfirmView
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'user-activities', UserActivityViewSet, basename='user-activity')

app_name = 'users'

urlpatterns = [
    # Auth endpoints
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/register-with-organization/', RegisterWithOrganizationView.as_view(), name='register-with-organization'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/password-reset/', ResetPasswordRequestView.as_view(), name='password-reset'),
    path('auth/password-reset/confirm/', ResetPasswordConfirmView.as_view(), name='password-reset-confirm'),
    
    # Router URLs
    path('', include(router.urls)),
]
