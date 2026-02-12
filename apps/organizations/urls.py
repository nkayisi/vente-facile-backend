"""
URLs pour l'app Organizations.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    OrganizationViewSet, OrganizationMembershipViewSet,
    BranchViewSet, OrganizationInvitationViewSet
)

router = DefaultRouter()
router.register(r'organizations', OrganizationViewSet, basename='organization')
router.register(r'memberships', OrganizationMembershipViewSet, basename='membership')
router.register(r'branches', BranchViewSet, basename='branch')
router.register(r'invitations', OrganizationInvitationViewSet, basename='invitation')

app_name = 'organizations'

urlpatterns = [
    path('', include(router.urls)),
]
