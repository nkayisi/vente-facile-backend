"""
URL configuration for sync app.
"""
from django.urls import path
from .operations import SyncOperationsView
from .pull import SyncChangedTablesView, SyncManifestView, SyncPullView
from .views import SyncView, SyncStatusView

urlpatterns = [
    path('sync/', SyncView.as_view(), name='sync'),
    path('sync/status/', SyncStatusView.as_view(), name='sync-status'),

    # Tirage a curseurs. Remplace le GET /sync/, qui tronquait au-dela de
    # 1 000 lignes sans ordre defini et perdait le reste definitivement.
    path('sync/pull/', SyncPullView.as_view(), name='sync-pull'),
    path('sync/pull/manifest/', SyncManifestView.as_view(), name='sync-pull-manifest'),
    # Sonde prealable : quelles tables ont du neuf. Sans elle, une sync sans
    # aucun changement coutait 31 GET sequentiels rendant tous "rien de neuf".
    path('sync/pull/changed/', SyncChangedTablesView.as_view(), name='sync-pull-changed'),

    # Journal d'operations. Chaque acte est rejoue par le serializer que le
    # back-office utilise deja : la parite n'est pas surveillee, elle est
    # structurelle.
    path('sync/operations/', SyncOperationsView.as_view(), name='sync-operations'),
]
