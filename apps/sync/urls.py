"""
URL configuration for sync app.
"""
from django.urls import path
from .views import SyncView, SyncStatusView

urlpatterns = [
    path('sync/', SyncView.as_view(), name='sync'),
    path('sync/status/', SyncStatusView.as_view(), name='sync-status'),
]
