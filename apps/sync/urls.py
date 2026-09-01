"""
Routes de synchronisation.

L'ancien endpoint `POST /api/v1/sync/` (protocole de l'application heritee,
last-write-wins) a ete retire avec elle : il contournait le service de dette et
acceptait `current_balance` en poussee, deux chemins qu'aucun client ne doit
plus emprunter.
"""
from django.urls import path

from .operations import SyncOperationsView
from .pull import SyncChangedTablesView, SyncManifestView, SyncPullView

urlpatterns = [
    # Tirage a curseurs, sur le couple `(updated_at, id)`. Le point de reprise
    # n'avance que si la table est tiree en entier.
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
