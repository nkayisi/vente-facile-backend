"""
Filtres des ventes, retours, devis et sessions de caisse.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE PÉRIMÈTRE DU RÔLE ET LE FILTRE VOLONTAIRE SONT DEUX CHOSES.              │
│                                                                              │
│ `SaleViewSet.get_queryset` borne déjà par entrepôt et, sans                  │
│ `sales.view_all`, par vendeur : c'est le périmètre du RÔLE, il n'est pas     │
│ négociable. Ce fichier ajoute le filtre VOLONTAIRE, celui qu'un              │
│ propriétaire pose pour dire « montre-moi le dépôt B » ou « la journée de ce  │
│ caissier ». Les deux se cumulent, et le volontaire ne peut donc jamais       │
│ élargir ce que le rôle a fermé.                                              │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN SEUL NOM DE PARAMÈTRE : `warehouse` ET `user`, PARTOUT.                  │
│                                                                              │
│ `sold_by`, `created_by` et `opened_by` restent où ils existaient déjà, pour  │
│ ne rien casser. Mais le nom canonique est `user` : deux noms voudraient dire │
│ une table de correspondance par écran, donc un écran qui en manquera.        │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import django_filters

from .models import Quotation, RegisterSession, Sale, SaleReturn


class SaleFilter(django_filters.FilterSet):
    """Filtres de la liste des ventes."""

    warehouse = django_filters.UUIDFilter(field_name='warehouse_id')
    user = django_filters.UUIDFilter(field_name='sold_by_id')

    class Meta:
        model = Sale
        fields = ['status', 'sale_type', 'customer', 'register', 'is_pos']


class SaleReturnFilter(django_filters.FilterSet):
    """
    Filtres de la liste des retours.

    Un retour n'a pas d'entrepôt à lui : il hérite de celui de sa vente, et
    c'est déjà le chemin que `warehouse_scope_field` emprunte pour le périmètre
    du rôle. En prendre un autre ici ferait diverger le filtre de la borne.
    """

    warehouse = django_filters.UUIDFilter(field_name='original_sale__warehouse_id')
    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = SaleReturn
        fields = ['status', 'return_type', 'original_sale']


class QuotationFilter(django_filters.FilterSet):
    """
    Filtres de la liste des devis.

    ⚠ PAS de `warehouse`, et c'est délibéré : `Quotation` ne porte aucun
    entrepôt, ni au serveur ni en base locale. Le dériver de
    `converted_sale__warehouse` ferait disparaître TOUS les devis non convertis
    dès qu'un entrepôt est choisi, c'est-à-dire la grande majorité d'entre eux.
    """

    user = django_filters.UUIDFilter(field_name='created_by_id')

    class Meta:
        model = Quotation
        fields = ['status', 'customer']


class RegisterSessionFilter(django_filters.FilterSet):
    """Filtres de la liste des sessions de caisse."""

    warehouse = django_filters.UUIDFilter(field_name='register__warehouse_id')
    user = django_filters.UUIDFilter(field_name='opened_by_id')

    class Meta:
        model = RegisterSession
        # `opened_by` est conservé : le back-office l'emploie déjà.
        fields = ['status', 'register', 'opened_by']
