"""
URL configuration for sync app.
"""
from django.urls import path
from .operations import SyncOperationsView
from .pull import SyncManifestView, SyncPullView
from .views import SyncView, SyncStatusView

urlpatterns = [
    path('sync/', SyncView.as_view(), name='sync'),
    path('sync/status/', SyncStatusView.as_view(), name='sync-status'),

    # Tirage a curseurs. Remplace le GET /sync/, qui tronquait au-dela de
    # 1 000 lignes sans ordre defini et perdait le reste definitivement.
    path('sync/pull/', SyncPullView.as_view(), name='sync-pull'),
    path('sync/pull/manifest/', SyncManifestView.as_view(), name='sync-pull-manifest'),

    # Journal d'operations. Chaque acte est rejoue par le serializer que le
    # back-office utilise deja : la parite n'est pas surveillee, elle est
    # structurelle.
    path('sync/operations/', SyncOperationsView.as_view(), name='sync-operations'),
]
