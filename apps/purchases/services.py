"""
Services métier pour l'app Purchases.

Centralise la logique d'incrémentation / restitution du stock à la réception
de marchandises (GoodsReceipt) pour éviter la duplication entre la view
``GoodsReceiptViewSet.complete`` et tout autre point d'appel futur.

Pattern miroir de ``apps.sales.services.SaleStockService`` :
- idempotence basée sur l'existence d'un ``StockMovement`` lié au GRN ;
- atomicité via ``@transaction.atomic`` ;
- verrouillage ``select_for_update`` sur ``Stock``.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError


TWO_PLACES = Decimal('0.01')


class GoodsReceiptStockService:
    """Incrémente et restitue le stock pour une réception de marchandises."""

    @staticmethod
    def is_committed(grn) -> bool:
        """True si le stock a déjà été incrémenté pour ce GRN."""
        from apps.inventory.models import StockMovement
        return StockMovement.objects.filter(
            reference_type='goods_receipt',
            reference_id=grn.id,
            movement_type='purchase',
        ).exists()

    @staticmethod
    @transaction.atomic
    def apply_increment(grn, user):
        """
        Incrémente le stock pour chaque item du GRN. Crée les lots si
        ``batch_number`` ou ``expiry_date`` est renseigné. Met à jour le coût
        moyen pondéré sur ``Stock``.

        Idempotent : ne fait rien si ``is_committed(grn)`` est vrai.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService
        from apps.inventory.services import FIFOService

        if GoodsReceiptStockService.is_committed(grn):
            return

        warehouse = grn.warehouse
        org = grn.organization

        for item in grn.items.select_related('product', 'variant').all():
            if not item.quantity_accepted or item.quantity_accepted <= 0:
                continue

            stock, _ = Stock.objects.select_for_update().get_or_create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                defaults={
                    'quantity': Decimal('0.000'),
                    'avg_cost': item.unit_cost or Decimal('0.00'),
                },
            )

            quantity_before = stock.quantity
            new_quantity = quantity_before + item.quantity_accepted

            unit_cost = item.unit_cost or Decimal('0.00')
            if new_quantity > 0 and unit_cost > 0:
                total_value = (quantity_before * stock.avg_cost) + (
                    item.quantity_accepted * unit_cost
                )
                stock.avg_cost = (total_value / new_quantity).quantize(TWO_PLACES)
            elif stock.avg_cost == 0 and unit_cost > 0:
                stock.avg_cost = unit_cost

            # Ce qui arrive scellé reste scellé : seule la part reçue hors
            # contenant vient grossir le vrac. Une réception partiellement
            # refusée voit sa part scellée replafonnée par `loose_share`.
            loose_accepted = PackagingService.loose_share(
                item.product, item.quantity_accepted, item.loose_quantity
            )
            PackagingService.apply_delta(
                stock, item.product,
                delta_base=item.quantity_accepted,
                delta_loose=loose_accepted,
            )
            PackagingService.touch(stock)
            stock.save()

            batch = None
            if item.batch_number or item.expiry_date:
                batch = FIFOService.add_to_batch(
                    organization=org,
                    product=item.product,
                    warehouse=warehouse,
                    quantity=item.quantity_accepted,
                    cost_price=unit_cost,
                    batch_number=item.batch_number or None,
                    variant=item.variant,
                    expiry_date=item.expiry_date,
                    user=user,
                )

            StockMovement.objects.create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                batch=batch,
                movement_type='purchase',
                quantity=item.quantity_accepted,
                unit_cost=unit_cost,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                input_package_quantity=(
                    (item.quantity_accepted - loose_accepted) / item.packaging_factor
                    if item.packaging_factor else Decimal('0.000')
                ),
                input_loose_quantity=(
                    loose_accepted if item.packaging_factor else Decimal('0.000')
                ),
                packaging_factor=item.packaging_factor,
                reference_type='goods_receipt',
                reference_id=grn.id,
                notes=f"Réception {grn.reference}",
                created_by=user,
            )

    @staticmethod
    @transaction.atomic
    def revert(grn, user):
        """
        Annule l'incrément de stock effectué par ``apply_increment`` : crée
        des mouvements ``return_out`` et décrémente ``Stock.quantity``.

        Lève ``ValidationError`` si le stock résultant deviendrait négatif et
        que ``warehouse.allow_negative_stock`` est ``False``.

        Ne fait rien si le stock n'avait pas été incrémenté.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService

        if not GoodsReceiptStockService.is_committed(grn):
            return

        warehouse = grn.warehouse
        org = grn.organization
        allow_negative = getattr(warehouse, 'allow_negative_stock', False)

        for item in grn.items.select_related('product', 'variant').all():
            if not item.quantity_accepted or item.quantity_accepted <= 0:
                continue

            stock = Stock.objects.select_for_update().filter(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
            ).first()

            if not stock:
                continue

            quantity_before = stock.quantity
            new_quantity = quantity_before - item.quantity_accepted

            if new_quantity < 0 and not allow_negative:
                raise ValidationError({
                    'items': (
                        f"Impossible d'annuler la réception : stock insuffisant pour "
                        f"{item.product.name}. Disponible: {quantity_before}, "
                        f"à retirer: {item.quantity_accepted}."
                    )
                })

            # Symétrique exact de l'entrée : on retire du vrac ce qui y était
            # entré, le reste des contenants repart scellé.
            loose_accepted = PackagingService.loose_share(
                item.product, item.quantity_accepted, item.loose_quantity
            )
            PackagingService.apply_delta(
                stock, item.product,
                delta_base=-item.quantity_accepted,
                delta_loose=-loose_accepted,
            )
            PackagingService.touch(stock)
            stock.save()

            StockMovement.objects.create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                movement_type='return_out',
                quantity=-item.quantity_accepted,
                unit_cost=item.unit_cost or Decimal('0.00'),
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                input_package_quantity=(
                    (item.quantity_accepted - loose_accepted) / item.packaging_factor
                    if item.packaging_factor else Decimal('0.000')
                ),
                input_loose_quantity=(
                    loose_accepted if item.packaging_factor else Decimal('0.000')
                ),
                packaging_factor=item.packaging_factor,
                reference_type='goods_receipt_cancel',
                reference_id=grn.id,
                notes=f"Annulation réception {grn.reference}",
                created_by=user,
            )
