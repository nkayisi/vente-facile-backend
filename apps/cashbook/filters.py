"""
Filtres du livre de caisse.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN MOUVEMENT DE CAISSE N'A PAS D'ENTREPÔT : IL SE DÉRIVE.                   │
│                                                                              │
│ `CashMovement` ne porte aucune colonne `warehouse`. Le serveur le déduit     │
│ déjà, pour le périmètre du rôle, par trois chemins (`_scope_cash_movements`  │
│ de `apps/reports/views.py`) : la vente, la dépense, ou la session de caisse  │
│ et son comptoir. Le filtre volontaire emprunte EXACTEMENT les mêmes.         │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ AUCUN `isnull` DANS LE FILTRE VOLONTAIRE.                                   │
│                                                                              │
│ Le périmètre du RÔLE tolère les mouvements non rattachés - apports, retraits │
│ - pour ne pas les masquer aux non-propriétaires : c'est délibéré. Un filtre  │
│ VOLONTAIRE, non. Sinon le même apport sans pièce apparaît sous « Entrepôt A »│
│ ET sous « Entrepôt B », la somme des dépôts dépasse le total de la caisse,   │
│ et rien à l'écran n'explique l'écart.                                        │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import django_filters
from django.db.models import Q

from .models import CashMovement, Expense


class ExpenseFilter(django_filters.FilterSet):
    """Filtres de la liste des dépenses."""

    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = Expense
        fields = ['status', 'category', 'is_recurring', 'warehouse', 'currency']


class CashMovementFilter(django_filters.FilterSet):
    """Filtres du journal de caisse."""

    user = django_filters.UUIDFilter(field_name='created_by_id')
    warehouse = django_filters.UUIDFilter(method='filter_warehouse')

    class Meta:
        model = CashMovement
        fields = ['direction', 'movement_type', 'is_cancelled', 'currency']

    def filter_warehouse(self, queryset, name, value):
        """Les trois chemins par lesquels un mouvement se rattache à un dépôt."""
        return queryset.filter(
            Q(sale__warehouse_id=value)
            | Q(expense__warehouse_id=value)
            | Q(session__register__warehouse_id=value)
        )
