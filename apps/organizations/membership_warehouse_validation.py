"""Validation des entrepôts assignés à un membership (évite cycles imports serializers/services)."""
from typing import List

from rest_framework import serializers as drf_serializers

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership


def validate_membership_warehouse_ids(
    role: str,
    warehouse_ids: List | None,
    organization,
    *,
    demandeur=None,
    actuels=None,
) -> List:
    """
    Retourne la liste d'UUID valides pour le rôle.
    Owner : liste vide en base (accès tous entrepôts).

    ┌──────────────────────────────────────────────────────────────────────┐
    │ ON N'AFFECTE QUE DANS SES ENTREPÔTS, ET JAMAIS UN ENTREPÔT FERMÉ.    │
    │                                                                      │
    │ - Un entrepôt DÉSACTIVÉ ne s'ajoute pas : le membre n'y pourrait     │
    │   rien faire. Une affectation qu'il portait déjà (`actuels`) reste   │
    │   permise, sans quoi on ne pourrait plus enregistrer le reste de sa  │
    │   fiche.                                                             │
    │ - Un GÉRANT (`demandeur` non propriétaire) n'ajoute ni ne retire que │
    │   dans ses propres entrepôts : il ne voit pas les autres, et les     │
    │   affectations qui les concernent appartiennent au propriétaire.     │
    └──────────────────────────────────────────────────────────────────────┘
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
    trouves = dict(
        Warehouse.objects.filter(
            organization=organization,
            id__in=unique_ids,
            is_deleted=False,
        ).values_list('id', 'is_active')
    )
    if len(trouves) != len(unique_ids):
        raise drf_serializers.ValidationError(
            {"warehouse_ids": "Entrepôt invalide ou inexistant pour cette organisation."}
        )

    deja = {str(i) for i in (actuels or [])}
    fermes = [
        i for i, actif in trouves.items() if not actif and str(i) not in deja
    ]
    if fermes:
        raise drf_serializers.ValidationError({"warehouse_ids": drf_serializers.ErrorDetail(
            "Un entrepôt désactivé ne s'affecte pas : réactivez-le d'abord.",
            code='entrepot_inactif',
        )})

    if demandeur is not None and demandeur.role != OrganizationMembership.Role.OWNER:
        from apps.core.warehouse_scope import entrepots_actionnables

        siens = {str(i) for i in (entrepots_actionnables(demandeur) or [])}
        demandes = {str(i) for i in unique_ids}
        if (demandes - siens) != (deja - siens):
            raise drf_serializers.ValidationError({"warehouse_ids": (
                "Vous n'affectez que dans vos propres entrepôts : les autres "
                "affectations relèvent du propriétaire."
            )})

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
