"""
Périmètre entrepôt par membership : helpers réutilisables pour filtres API.

Convention :
- role ``owner`` : accès à tous les entrepôts de l'organisation (pas de filtre warehouse).
- autres rôles : entrepôts listés via M2M ``assigned_warehouses`` ; liste vide = aucun accès aux données scoped.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from django.db.models import Q, QuerySet
from rest_framework.exceptions import ValidationError

from apps.organizations.models import OrganizationMembership


def get_membership_for_request(request):
    """Membership actif pour ``request.user`` et ``X-Organization-ID``."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    org_id = request.headers.get("X-Organization-ID")
    if not org_id:
        return None
    return (
        OrganizationMembership.objects.filter(
            user=request.user,
            organization_id=org_id,
            is_active=True,
        )
        .prefetch_related("assigned_warehouses")
        .first()
    )


def accessible_warehouse_ids(membership: OrganizationMembership) -> Optional[list[UUID]]:
    """
    Retourne ``None`` si pas de restriction (owner), sinon liste d'UUID d'entrepôts.
    """
    if membership.role == OrganizationMembership.Role.OWNER:
        return None
    ids = list(
        membership.assigned_warehouses.filter(is_deleted=False).values_list(
            "id", flat=True
        )
    )
    return ids


def restrict_visibility_for_membership(
    queryset: QuerySet,
    membership: Optional[OrganizationMembership],
    *,
    warehouse_field: str,
    creator_field: str,
    include_null_warehouse: bool = False,
) -> QuerySet:
    """Visibilité par rôle pour les données financières/opérationnelles.

    - ``owner`` : voit tout (aucun filtre).
    - ``cashier`` : voit uniquement ses propres enregistrements
      (``creator_field == membership.user``), partout dans l'application.
    - ``manager`` / ``stock_keeper`` : périmètre entrepôt (``assigned_warehouses``)
      via ``warehouse_field``. Par défaut, les enregistrements sans entrepôt
      (``NULL``) ne leur sont pas visibles (réservés au owner) - passer
      ``include_null_warehouse=True`` pour les inclure (ex. ventes legacy).
    """
    if membership is None:
        return queryset
    role = membership.role
    if role == OrganizationMembership.Role.OWNER:
        return queryset
    if role == OrganizationMembership.Role.CASHIER:
        return queryset.filter(**{creator_field: membership.user})
    return filter_queryset_by_related_warehouse(
        queryset, membership, warehouse_field, include_null=include_null_warehouse
    )


def restrict_visibility_for_request(
    queryset: QuerySet,
    request,
    *,
    warehouse_field: str,
    creator_field: str,
    include_null_warehouse: bool = False,
) -> QuerySet:
    """Variante basée sur la requête (résout le membership via X-Organization-ID)."""
    return restrict_visibility_for_membership(
        queryset,
        get_membership_for_request(request),
        warehouse_field=warehouse_field,
        creator_field=creator_field,
        include_null_warehouse=include_null_warehouse,
    )


def filter_queryset_by_warehouse_ids(
    queryset: QuerySet,
    membership: OrganizationMembership,
    warehouse_field: str = "warehouse_id",
) -> QuerySet:
    """Filtre un queryset sur un champ FK warehouse si le membership est restreint."""
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(**{f"{warehouse_field}__in": ids})


def filter_sales_for_membership(queryset: QuerySet, membership: OrganizationMembership) -> QuerySet:
    """
    Ventes : restreint voit uniquement ``warehouse_id`` dans son périmètre.
    Les ventes sans warehouse (legacy) sont réservées au owner.
    """
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(Q(warehouse_id__in=ids))


def filter_queryset_by_related_warehouse(
    queryset: QuerySet,
    membership: OrganizationMembership,
    warehouse_field: str,
    *,
    include_null: bool = False,
) -> QuerySet:
    """Filtre via une relation indirecte (ex. ``original_sale__warehouse_id``,
    ``register__warehouse_id``, ``sale__warehouse_id``).

    Si ``include_null`` est ``True``, les lignes dont la relation est ``NULL``
    restent visibles (utile pour mouvements de caisse non liés à une vente).
    """
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    if include_null:
        return queryset.filter(
            Q(**{f"{warehouse_field}__in": ids})
            | Q(**{f"{warehouse_field}__isnull": True})
        )
    return queryset.filter(**{f"{warehouse_field}__in": ids})


def filter_stock_transfer_queryset(
    queryset: QuerySet, membership: OrganizationMembership
) -> QuerySet:
    """Transferts où source ou destination est dans le périmètre."""
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(
        Q(source_warehouse_id__in=ids) | Q(destination_warehouse_id__in=ids)
    )


def assert_warehouse_allowed_for_request(
    request,
    warehouse_id,
    *,
    allow_none: bool = False,
):
    """
    Vérifie que ``warehouse_id`` est dans le périmètre du membership courant.
    ``warehouse_id`` peut être None si ``allow_none`` (ex. legacy réservé owner - éviter si possible).
    """
    if warehouse_id is None:
        if allow_none:
            membership = get_membership_for_request(request)
            if membership and membership.role != OrganizationMembership.Role.OWNER:
                raise ValidationError(
                    {"warehouse": "Un entrepôt est requis pour votre compte."}
                )
            return
        raise ValidationError({"warehouse": "Entrepôt requis."})

    membership = get_membership_for_request(request)
    if not membership:
        raise ValidationError({"detail": "Organisation requise."})

    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return
    wid = warehouse_id if isinstance(warehouse_id, UUID) else UUID(str(warehouse_id))
    if wid not in ids:
        raise ValidationError(
            {"warehouse": "Entrepôt non autorisé pour votre compte."}
        )
