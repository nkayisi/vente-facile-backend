"""
URL configuration for settings module.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CurrencyViewSet, OrganizationCurrencyViewSet,
    LoyaltyProgramViewSet, LoyaltyRewardViewSet,
    CustomerLoyaltyViewSet, OrganizationSettingsViewSet
)

router = DefaultRouter()
router.register(r'currencies', CurrencyViewSet, basename='currencies')
router.register(r'organization-currencies', OrganizationCurrencyViewSet, basename='organization-currencies')
router.register(r'loyalty-program', LoyaltyProgramViewSet, basename='loyalty-program')
router.register(r'loyalty-rewards', LoyaltyRewardViewSet, basename='loyalty-rewards')
router.register(r'customer-loyalty', CustomerLoyaltyViewSet, basename='customer-loyalty')
router.register(r'organization-settings', OrganizationSettingsViewSet, basename='organization-settings')

urlpatterns = [
    path('', include(router.urls)),
]
