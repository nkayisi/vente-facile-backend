"""URLs du module Notifications."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AlertViewSet, NotificationViewSet

router = DefaultRouter()
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'alerts', AlertViewSet, basename='alert')

app_name = 'notifications'

urlpatterns = [path('', include(router.urls))]
