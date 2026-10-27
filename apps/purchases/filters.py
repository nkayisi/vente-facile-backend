"""
Filtres des achats.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CES `FilterSet` EXISTENT PARCE QUE LE FILTRE « UTILISATEUR » ÉTAIT INERTE.   │
│                                                                              │
│ Les quatre ViewSets déclaraient des `filterset_fields`, qui ne savent faire  │
│ que l'égalité stricte sur un champ du modèle. Le composant de périmètre du   │
│ back-office envoie `user`, et il ne correspond à AUCUN champ : le marchand   │
│ choisissait un collègue, rien ne changeait, et rien ne l'expliquait.         │
│                                                                              │
│ ⚠ Pire : `TenantViewSetMixin.filter_queryset` ne VALIDE un `?user=` que si   │
│ la vue le DÉCLARE (`_filtre_declare`). Non déclaré, il n'était ni appliqué   │
│ ni refusé - le silence exact du filtre de devise de `getCashMovements`.      │
└──────────────────────────────────────────────────────────────────────────────┘

Une seule définition sert la liste ET l'export : le fichier téléchargé ne peut
pas couvrir un autre périmètre que l'écran qui l'a déclenché.
"""
import django_filters

from .models import GoodsReceipt, PurchaseOrder, PurchaseReturn, SupplierPayment


class PurchaseOrderFilter(django_filters.FilterSet):
    """Filtres de la liste des commandes d'achat."""

    #: Qui a PASSÉ la commande. `created_by` et non `approved_by` : c'est
    #: l'auteur qu'on cherche quand on filtre par utilisateur, et un
    #: approbateur peut ne jamais avoir vu la commande qu'il confirme.
    user = django_filters.UUIDFilter(field_name='created_by_id')

    date_from = django_filters.DateFilter(field_name='order_date', lookup_expr='gte')
    date_to = django_filters.DateFilter(field_name='order_date', lookup_expr='lte')

    class Meta:
        model = PurchaseOrder
        fields = ['status', 'supplier', 'warehouse']


class GoodsReceiptFilter(django_filters.FilterSet):
    """Filtres de la liste des réceptions."""

    #: Qui a RÉCEPTIONNÉ. C'est la seule trace de qui était au dépôt quand le
    #: camion est arrivé, et la question qu'on pose devant un écart.
    user = django_filters.UUIDFilter(field_name='received_by_id')

    class Meta:
        model = GoodsReceipt
        fields = ['status', 'purchase_order', 'warehouse']


class SupplierPaymentFilter(django_filters.FilterSet):
    """Filtres de la liste des règlements fournisseurs."""

    user = django_filters.UUIDFilter(field_name='created_by_id')

    #: ⚠ `warehouse` traverse l'imputation : un règlement n'a pas d'entrepôt à
    #: lui, il en hérite de la commande qu'il solde. C'est le même chemin que
    #: `warehouse_scope_field` de la vue, et les deux doivent le dire pareil -
    #: sinon la liste et son périmètre de rôle ne montrent pas le même ensemble.
    warehouse = django_filters.UUIDFilter(
        field_name='allocations__purchase_order__warehouse_id',
        distinct=True,
    )

    class Meta:
        model = SupplierPayment
        fields = ['status', 'supplier', 'payment_method']


class PurchaseReturnFilter(django_filters.FilterSet):
    """Filtres de la liste des retours fournisseurs."""

    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = PurchaseReturn
        fields = ['status', 'supplier', 'warehouse', 'purchase_order']
