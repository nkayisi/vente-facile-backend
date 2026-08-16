"""
Règles de prix d'un produit vendu en gros et au détail.

Point d'entrée **unique** des règles de prix. Trois chemins les appliquent et
doivent donner exactement le même verdict : la fiche produit (serializers DRF),
l'approvisionnement (un mouvement de stock peut mettre à jour les prix) et
l'import Excel, qui écrit en base sans passer par DRF.

Vocabulaire
-----------
Un produit porte quatre prix, deux par canal de vente :

    détail : ``cost_price``          (achat d'une unité)
             ``selling_price``       (vente d'une unité)
    gros   : ``package_cost_price``  (achat d'un conditionnement entier)
             ``wholesale_price``     (vente d'un conditionnement entier)

``cost_price`` et ``selling_price`` sont **toujours** exprimés à l'unité de
détail, qui est aussi l'unité de base du stock. C'est la seule grandeur avec
laquelle le coût moyen pondéré et les lots FIFO savent travailler.
"""
from decimal import Decimal

from rest_framework import serializers

from apps.inventory.packaging import PackagingService


RETAIL_ONLY = 'retail_only'


def _to_decimal(value):
    """Décimal, ou ``None`` pour une valeur absente. Ne lève jamais."""
    if value is None or value == '':
        return None
    try:
        return Decimal(value)
    except (TypeError, ArithmeticError, ValueError):
        return None


class ProductPricingService:
    """Résolution et validation des quatre prix d'un produit."""

    @staticmethod
    def resolve(
        *,
        selling_mode,
        units_per_package,
        cost_price=None,
        package_cost_price=None,
    ) -> dict:
        """
        Complète le prix d'achat manquant, sans jamais lever.

        Le marchand qui achète au carton ne doit pas avoir à diviser de tête :
        s'il n'a saisi que le prix du conditionnement, on en déduit le prix
        unitaire. La dérivation ne joue **que** dans ce sens et **que** si le
        prix unitaire est absent : un prix saisi à la main fait toujours foi.
        """
        resolved = {}
        cost_price = _to_decimal(cost_price)
        package_cost_price = _to_decimal(package_cost_price)

        if cost_price is not None:
            resolved['cost_price'] = cost_price
        if package_cost_price is not None:
            resolved['package_cost_price'] = package_cost_price

        if selling_mode == RETAIL_ONLY:
            return resolved

        factor = int(units_per_package or 0)
        if factor >= 2 and package_cost_price and not cost_price:
            resolved['cost_price'] = PackagingService.unit_cost_from_package(
                package_cost_price, factor
            )
        return resolved

    @staticmethod
    def collect_errors(
        *,
        selling_mode,
        cost_price=None,
        selling_price=None,
        wholesale_price=None,
        require_wholesale=True,
    ) -> dict:
        """
        Erreurs de prix, indexées par nom de champ, sans lever.

        Rendue publique pour que la fiche produit puisse fusionner ces erreurs
        avec ses propres contrôles de structure et tout remonter en une fois :
        un formulaire qui révèle ses reproches un par un fait revenir le
        marchand trois fois.

        Note : la cohérence entre prix de vente et prix d'achat n'est vérifiée
        qu'au détail. Au gros, une marge négative s'affiche en rouge dans
        l'interface mais ne bloque pas l'enregistrement : des produits existants
        sont déjà dans ce cas et deviendraient impossibles à modifier.
        """
        errors: dict = {}
        cost_price = _to_decimal(cost_price)
        selling_price = _to_decimal(selling_price)
        wholesale_price = _to_decimal(wholesale_price)

        if selling_price and cost_price and selling_price < cost_price:
            errors['selling_price'] = (
                "Le prix de vente ne peut pas être inférieur au prix d'achat."
            )

        if (
            selling_mode != RETAIL_ONLY
            and require_wholesale
            and (wholesale_price is None or wholesale_price <= 0)
        ):
            errors['wholesale_price'] = (
                "Indiquez le prix de vente d'un conditionnement entier."
            )

        return errors

    @classmethod
    def resolve_and_validate(
        cls,
        *,
        selling_mode,
        units_per_package,
        cost_price=None,
        package_cost_price=None,
        selling_price=None,
        wholesale_price=None,
        require_wholesale=True,
    ) -> dict:
        """
        Prix complétés et validés, prêts à être écrits sur le produit.

        Lève ``ValidationError`` avec des clés de champ, ce qui la rend
        utilisable telle quelle depuis un serializer comme depuis l'import.
        """
        resolved = cls.resolve(
            selling_mode=selling_mode,
            units_per_package=units_per_package,
            cost_price=cost_price,
            package_cost_price=package_cost_price,
        )

        selling_price = _to_decimal(selling_price)
        wholesale_price = _to_decimal(wholesale_price)

        errors = cls.collect_errors(
            selling_mode=selling_mode,
            cost_price=resolved.get('cost_price'),
            selling_price=selling_price,
            wholesale_price=wholesale_price,
            require_wholesale=require_wholesale,
        )
        if errors:
            raise serializers.ValidationError(errors)

        if selling_price is not None:
            resolved['selling_price'] = selling_price
        if wholesale_price is not None:
            resolved['wholesale_price'] = wholesale_price
        return resolved

    @staticmethod
    def apply(product, values: dict) -> list:
        """
        Écrit les prix fournis sur le produit et retourne les champs modifiés.

        Un prix inchangé n'est pas réécrit : sans cela, chaque approvisionnement
        ferait remonter le produit au mobile sans qu'aucune valeur n'ait bougé.

        ``updated_at`` et ``sync_updated_at`` sont obligatoires dans
        ``update_fields`` : ``SyncableModel.save()`` rafraîchit le second, mais
        une affectation absente de ``update_fields`` n'est jamais écrite, et le
        mobile garderait alors un prix périmé pour toujours.
        """
        allowed = (
            'cost_price', 'package_cost_price', 'selling_price', 'wholesale_price',
        )
        changed = []
        for field in allowed:
            if field not in values or values[field] is None:
                continue
            new_value = Decimal(values[field])
            if getattr(product, field) == new_value:
                continue
            setattr(product, field, new_value)
            changed.append(field)

        if changed:
            product.save(update_fields=[*changed, 'updated_at', 'sync_updated_at'])
        return changed
