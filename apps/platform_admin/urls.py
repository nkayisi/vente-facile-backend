"""
URLs for the Platform Admin module.
All endpoints are prefixed with /api/v1/platform-admin/
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AdminActionLogViewSet,
    AdminAppReleaseView,
    AdminDashboardView,
    AdminImpersonationEndView,
    AdminImpersonationSessionViewSet,
    AdminIncidentsView,
    AdminMeView,
    AdminMetricsView,
    AdminOrganizationViewSet,
    AdminPlanViewSet,
    AdminPlatformAdminViewSet,
    AdminReconcilePaymentsView,
    AdminSubscriptionViewSet,
    AdminUserViewSet,
)

router = DefaultRouter()
router.register(r'organizations', AdminOrganizationViewSet, basename='admin-organization')
router.register(r'users', AdminUserViewSet, basename='admin-user')
router.register(r'platform-admins', AdminPlatformAdminViewSet, basename='admin-platform-admin')
router.register(r'audit-log', AdminActionLogViewSet, basename='admin-audit-log')
router.register(r'impersonation-sessions', AdminImpersonationSessionViewSet, basename='admin-impersonation-session')
router.register(r'plans', AdminPlanViewSet, basename='admin-plan')
router.register(r'subscriptions', AdminSubscriptionViewSet, basename='admin-subscription')

app_name = 'platform_admin'

urlpatterns = [
    path('me/', AdminMeView.as_view(), name='admin-me'),
    path('dashboard/', AdminDashboardView.as_view(), name='admin-dashboard'),
    path('metrics/', AdminMetricsView.as_view(), name='admin-metrics'),
    path('incidents/', AdminIncidentsView.as_view(), name='admin-incidents'),
    path('incidents/reconcile/', AdminReconcilePaymentsView.as_view(), name='admin-incidents-reconcile'),
    path('app-release/', AdminAppReleaseView.as_view(), name='admin-app-release'),
    path('impersonation/end/', AdminImpersonationEndView.as_view(), name='admin-impersonation-end'),
    path('', include(router.urls)),
]
