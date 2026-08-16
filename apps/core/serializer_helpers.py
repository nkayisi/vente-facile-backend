"""
Helpers pour scoper les FK des serializers DRF par organisation.

Sans ce scoping, les ``PrimaryKeyRelatedField`` auto-créés par
``ModelSerializer`` utilisent ``queryset = Model.objects.all()`` : donc un
client peut référencer un objet d'une autre organisation (faille
cross-tenant). Le viewset filtre la queryset principale, mais les FK dans
le serializer restent ouvertes.

Usage type dans un ``__init__`` de serializer ::

    class SaleCreateSerializer(serializers.ModelSerializer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            scope_fk_to_org(self, 'customer', 'register', 'warehouse', 'price_list')

Conditions :
- ``self.context['request']`` doit contenir le header ``X-Organization-ID``
  (présent pour tous les endpoints scopés via ``TenantViewSetMixin``).
- Les modèles cibles doivent avoir un champ ``organization`` (cas standard
  pour tous les ``TenantModel`` / ``TenantSoftDeleteModel``).
- Si l'org n'est pas résolvable (call hors HTTP, contexte de test sans
  request), le queryset reste celui par défaut : c'est le viewset qui
  protège l'API.
"""
from __future__ import annotations

from typing import Iterable


def scope_fk_to_org(serializer, *fk_names: str) -> None:
    """Restreint les FK listées au queryset de l'organisation courante.

    Lève silencieusement (no-op) si :
    - le champ ``fk_name`` n'existe pas sur le serializer (mauvaise saisie),
    - le request n'est pas dans le contexte (test, command),
    - le header ``X-Organization-ID`` est absent,
    - le modèle cible n'a pas de champ ``organization``.

    Ces conditions sont normales hors HTTP - le code applicatif passe par
    des querysets explicites via ``.for_organization(org)`` dans ce cas.
    """
    request = serializer.context.get('request')
    if not request:
        return

    org_id = request.headers.get('X-Organization-ID')
    if not org_id:
        return

    for name in fk_names:
        field = serializer.fields.get(name)
        if field is None or not hasattr(field, 'queryset') or field.queryset is None:
            continue

        model = field.queryset.model
        if not hasattr(model, 'organization'):
            continue

        filtered = field.queryset.filter(organization_id=org_id)
        # Exclure soft-deleted si applicable.
        if hasattr(model, 'is_deleted'):
            filtered = filtered.filter(is_deleted=False)
        field.queryset = filtered


def scope_nested_fk_to_org(
    serializer,
    nested_field: str,
    fk_names: Iterable[str],
) -> None:
    """Applique ``scope_fk_to_org`` aux items d'un serializer imbriqué (``many=True``).

    Utilisé pour scoper par ex. ``items[*].product`` quand ``SaleCreateSerializer``
    contient un ``SaleItemCreateSerializer(many=True)``.
    """
    nested = serializer.fields.get(nested_field)
    if nested is None:
        return

    child = getattr(nested, 'child', None)
    if child is None:
        return

    # Le child hérite du context du parent, donc on peut réutiliser le helper
    # en lui passant l'instance enfant.
    scope_fk_to_org(child, *fk_names)
