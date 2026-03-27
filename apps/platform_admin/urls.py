"""
URLs for the Platform Admin module.
All endpoints are prefixed with /api/v1/platform-admin/
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    AdminDashboardView,
    AdminOrganizationViewSet,
    AdminUserViewSet,
    AdminPlanViewSet,
    AdminSubscriptionViewSet,
)

router = DefaultRouter()
router.register(r'organizations', AdminOrganizationViewSet, basename='admin-organization')
router.register(r'users', AdminUserViewSet, basename='admin-user')
router.register(r'plans', AdminPlanViewSet, basename='admin-plan')
router.register(r'subscriptions', AdminSubscriptionViewSet, basename='admin-subscription')

app_name = 'platform_admin'

urlpatterns = [
    path('dashboard/', AdminDashboardView.as_view(), name='admin-dashboard'),
    path('', include(router.urls)),
]
