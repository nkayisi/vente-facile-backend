"""
Filtres de la gestion de stock.

Premières ``FilterSet`` du dépôt : jusqu'ici tout passait par ``filterset_fields``,
qui ne sait faire que l'égalité stricte. Or les deux rubriques de rapport
demandent exactement ce que l'égalité ne sait pas exprimer : une catégorie qui
englobe ses sous-catégories, et une plage de dates.

Ces filtres servent à la fois la liste affichée et l'export. Une seule
définition, donc le fichier téléchargé ne peut pas couvrir un périmètre
différent de celui que l'utilisateur voit à l'écran.
"""
from datetime import datetime, time

import django_filters
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    InventorySession, Stock, StockAdjustment, StockMovement, StockTransfer,
)

#: Types de mouvement qui font ENTRER de la marchandise.
#: Repris de ``models.STOCK_IN_MOVEMENT_TYPES`` pour rester une seule vérité.
from .models import STOCK_IN_MOVEMENT_TYPES

STOCK_OUT_MOVEMENT_TYPES = [
    StockMovement.MovementType.SALE,
    StockMovement.MovementType.RETURN_OUT,
    StockMovement.MovementType.TRANSFER_OUT,
    StockMovement.MovementType.ADJUSTMENT_OUT,
    StockMovement.MovementType.DAMAGE,
    StockMovement.MovementType.EXPIRED,
    StockMovement.MovementType.PRODUCTION_OUT,
]


def category_subtree_ids(value):
    """Identifiants de la catégorie demandée et de toute sa descendance."""
    from apps.products.models import Category

    return Category.subtree_ids([value.id if hasattr(value, 'id') else value])


def day_bounds(day):
    """
    Bornes aware d'une journée civile, dans le fuseau de l'organisation.

    ``StockMovement.created_at`` est un ``DateTimeField`` et le projet tourne en
    ``Africa/Kinshasa`` (UTC+1) avec ``USE_TZ``. Comparer une date nue à un
    horodatage UTC décale la frontière d'une heure : une entrée saisie à 23h30 à
    Kinshasa bascule sur le lendemain en UTC et disparaît du rapport du jour.
    Le dépôt s'est déjà fait prendre par ce décalage sur un rapport d'activité.
    """
    zone = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(day, time.min), zone),
        timezone.make_aware(datetime.combine(day, time.max), zone),
    )


class CategorySubtreeFilter(django_filters.UUIDFilter):
    """Filtre catégorie qui inclut la sous-arborescence."""

    def __init__(self, *args, field_path='product__category_id', **kwargs):
        self.category_field_path = field_path
        super().__init__(*args, **kwargs)

    def filter(self, queryset, value):
        if not value:
            return queryset
        return queryset.filter(
            **{f'{self.category_field_path}__in': category_subtree_ids(value)}
        )


class StockFilter(django_filters.FilterSet):
    """Filtres de la rubrique « Niveau de stock »."""

    warehouse = django_filters.UUIDFilter(field_name='warehouse_id')
    product = django_filters.UUIDFilter(field_name='product_id')
    variant = django_filters.UUIDFilter(field_name='variant_id')
    category = CategorySubtreeFilter()
    brand = django_filters.UUIDFilter(field_name='product__brand_id')
    search = django_filters.CharFilter(method='filter_search')
    status = django_filters.ChoiceFilter(
        method='filter_status',
        choices=[
            ('out', 'En rupture'),
            ('low', 'Stock bas'),
            ('available', 'Disponible'),
            ('reserved', 'Avec réservation'),
            # Les deux états EXCLUSIFS du terminal : voir `filter_status`.
            ('low_only', 'Stock bas'),
            ('healthy', 'En stock'),
        ],
    )

    class Meta:
        model = Stock
        fields = ['warehouse', 'product', 'variant', 'category', 'brand', 'status']

    def filter_search(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(product__name__icontains=value)
            | Q(product__sku__icontains=value)
            | Q(product__barcode__icontains=value)
        )

    def filter_status(self, queryset, name, value):
        """
        Traduit un état lisible en condition sur les quantités.

        « Stock bas » se lit par rapport au point de réapprovisionnement du
        produit, pas par rapport à un seuil arbitraire : c'est le même critère
        que celui qui déclenche les alertes de réassort.
        """
        if value == 'out':
            return queryset.filter(quantity__lte=0)
        if value == 'low':
            # Exactement le critère de l'action `low-stock` du ViewSet, à
            # laquelle l'écran « Stock bas » est déjà branché. S'en écarter ferait
            # diverger le fichier exporté de la liste qui l'a déclenché, et
            # personne ne saurait lequel des deux a raison.
            return queryset.filter(
                quantity__lte=F('product__reorder_point'),
                product__track_inventory=True,
            )
        if value == 'available':
            return queryset.filter(quantity__gt=0)
        if value == 'reserved':
            return queryset.filter(reserved_quantity__gt=0)

        # ┌──────────────────────────────────────────────────────────────────┐
        # │ `low` ET `available` SE RECOUVRENT. LES ÉTATS DU TERMINAL, NON.  │
        # │                                                                  │
        # │ `low` contient les ruptures, `available` contient les stocks     │
        # │ bas : ce sont deux ALERTES, et c'est ce que le back-office veut  │
        # │ de son bouton « Stock bas ». L'écran `/rayon` du terminal, lui,  │
        # │ range chaque rayon dans une case et une seule.                   │
        # │                                                                  │
        # │ Traduire son « bas » par `low` produirait donc un DOCUMENT PLUS  │
        # │ LARGE QUE L'ÉCRAN qui l'a déclenché - le défaut que tous les     │
        # │ exports de ce dépôt ont dû corriger. D'où ces deux états.        │
        # │                                                                  │
        # │ `low` et `available` NE BOUGENT PAS : le web les emploie.        │
        # └──────────────────────────────────────────────────────────────────┘
        #
        # Un seuil à zéro ne déclenche RIEN : un produit sans point de
        # réapprovisionnement n'est jamais « bas », il est simplement suivi.
        sous_le_seuil = Q(
            quantity__lte=F('product__reorder_point'),
            product__reorder_point__gt=0,
            product__track_inventory=True,
        )
        if value == 'low_only':
            return queryset.filter(Q(quantity__gt=0) & sous_le_seuil)
        if value == 'healthy':
            return queryset.filter(quantity__gt=0).exclude(sous_le_seuil)
        return queryset


class StockMovementFilter(django_filters.FilterSet):
    """Filtres de la rubrique « Mouvements de stock »."""

    warehouse = django_filters.UUIDFilter(field_name='warehouse_id')
    product = django_filters.UUIDFilter(field_name='product_id')
    variant = django_filters.UUIDFilter(field_name='variant_id')
    category = CategorySubtreeFilter()
    movement_type = django_filters.CharFilter(method='filter_movement_type')
    reference_type = django_filters.CharFilter(field_name='reference_type')
    direction = django_filters.ChoiceFilter(
        method='filter_direction',
        choices=[('in', 'Entrées'), ('out', 'Sorties')],
    )
    date_from = django_filters.DateFilter(method='filter_date_from')
    date_to = django_filters.DateFilter(method='filter_date_to')
    month = django_filters.CharFilter(method='filter_month')
    search = django_filters.CharFilter(method='filter_search')
    created_by = django_filters.UUIDFilter(field_name='created_by_id')
    #: Alias canonique de `created_by`. Un seul nom de paramètre sur toute
    #: l'application - `user` - évite une table de correspondance par écran,
    #: donc un écran qui en manquera. `created_by` reste : le back-office
    #: l'emploie déjà.
    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = StockMovement
        fields = [
            'warehouse', 'product', 'variant', 'category', 'movement_type',
            'reference_type', 'direction', 'date_from', 'date_to', 'month',
            'created_by', 'user',
        ]

    def filter_movement_type(self, queryset, name, value):
        """Accepte plusieurs types séparés par des virgules."""
        wanted = [item.strip() for item in (value or '').split(',') if item.strip()]
        if not wanted:
            return queryset
        return queryset.filter(movement_type__in=wanted)

    def filter_direction(self, queryset, name, value):
        if value == 'in':
            return queryset.filter(movement_type__in=STOCK_IN_MOVEMENT_TYPES)
        if value == 'out':
            return queryset.filter(movement_type__in=STOCK_OUT_MOVEMENT_TYPES)
        return queryset

    def filter_date_from(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(created_at__gte=day_bounds(value)[0])

    def filter_date_to(self, queryset, name, value):
        """La borne haute est INCLUSIVE : « au 31/08 » comprend le 31 août."""
        if not value:
            return queryset
        return queryset.filter(created_at__lte=day_bounds(value)[1])

    def filter_month(self, queryset, name, value):
        """
        Raccourci ``YYYY-MM`` pour tirer le rapport d'un mois entier.

        C'est la maille que demandent les commerçants pour l'approvisionnement,
        et elle évite de leur faire saisir deux dates dont la seconde est
        presque toujours mal bornée (30 ou 31).
        """
        try:
            year, month = (int(part) for part in str(value).split('-')[:2])
            first = datetime(year, month, 1).date()
        except (TypeError, ValueError):
            return queryset.none()

        last_year, last_month = (year + 1, 1) if month == 12 else (year, month + 1)
        last = datetime(last_year, last_month, 1).date()

        start, _ = day_bounds(first)
        end, _ = day_bounds(last)
        return queryset.filter(created_at__gte=start, created_at__lt=end)

    def filter_search(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(product__name__icontains=value)
            | Q(product__sku__icontains=value)
            | Q(notes__icontains=value)
        )


class StockTransferFilter(django_filters.FilterSet):
    """
    Filtres de la liste des transferts.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ SÉMANTIQUE **OU**, JAMAIS ET.                                           │
    │                                                                          │
    │ Un transfert relie DEUX entrepôts. Le borner sur la seule source         │
    │ cacherait au magasinier de destination exactement ce qu'il doit          │
    │ réceptionner - c'est déjà le raisonnement du manifeste de tirage, où     │
    │ `stock_transfers` n'est volontairement pas borné par `warehouse_path`.   │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    warehouse = django_filters.UUIDFilter(method='filter_warehouse')
    user = django_filters.UUIDFilter(field_name='requested_by_id')

    class Meta:
        model = StockTransfer
        fields = ['status', 'source_warehouse', 'destination_warehouse']

    def filter_warehouse(self, queryset, name, value):
        return queryset.filter(
            Q(source_warehouse_id=value) | Q(destination_warehouse_id=value)
        )


class StockAdjustmentFilter(django_filters.FilterSet):
    """Filtres de la liste des ajustements."""

    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = StockAdjustment
        fields = ['status', 'adjustment_type', 'warehouse']


class InventorySessionFilter(django_filters.FilterSet):
    """
    Filtres de la liste des sessions d'inventaire.

    `user` vise le CRÉATEUR de la session. Une feuille de comptage porte bien un
    `counted_by` par ligne, mais filtrer une session sur qui a compté l'une de
    ses lignes rendrait une liste qu'aucun total n'explique.
    """

    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = InventorySession
        fields = ['status', 'scope_type', 'warehouse']
