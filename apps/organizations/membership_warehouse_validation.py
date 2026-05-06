"""Validation des entrepôts assignés à un membership (évite cycles imports serializers/services)."""
from typing import List

from rest_framework import serializers as drf_serializers

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership


def validate_membership_warehouse_ids(
    role: str,
    warehouse_ids: List | None,
    organization,
) -> List:
    """
    Retourne la liste d'UUID valides pour le rôle.
    Owner : liste vide en base (accès tous entrepôts).
    """
    if role == OrganizationMembership.Role.OWNER:
        if warehouse_ids:
            raise drf_serializers.ValidationError(
                {
                    "warehouse_ids": "Non applicable pour le rôle administrateur.",
                }
            )
        return []

    if not warehouse_ids:
        raise drf_serializers.ValidationError(
            {"warehouse_ids": "Au moins un entrepôt est requis."}
        )

    unique_ids = list(dict.fromkeys(warehouse_ids))
    count = Warehouse.objects.filter(
        organization=organization,
        id__in=unique_ids,
        is_deleted=False,
    ).count()
    if count != len(unique_ids):
        raise drf_serializers.ValidationError(
            {"warehouse_ids": "Entrepôt invalide ou inexistant pour cette organisation."}
        )

    if role in (
        OrganizationMembership.Role.STOCK_KEEPER,
        OrganizationMembership.Role.CASHIER,
    ):
        if len(unique_ids) != 1:
            raise drf_serializers.ValidationError(
                {
                    "warehouse_ids": "Ce rôle doit avoir exactement un entrepôt assigné.",
                }
            )

    return unique_ids
