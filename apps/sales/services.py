"""
Services métier pour l'app Sales.

Centralise la logique de décrémentation/réincrémentation du stock pour
éviter la duplication entre SaleCreateSerializer.create, SaleViewSet.add_payment
et SaleViewSet.cancel.
"""
from __future__ import annotations

from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError


TWO_PLACES = Decimal('0.01')


class SaleStockService:
    """Centralise la décrémentation et la restitution du stock pour une vente."""

    @staticmethod
    def is_committed(sale) -> bool:
        """True si le stock a déjà été décrémenté pour cette vente (StockMovement de type sale)."""
        from apps.inventory.models import StockMovement
        return StockMovement.objects.filter(
            reference_type='sale',
            reference_id=sale.id,
            movement_type='sale',
        ).exists()

    @staticmethod
    @transaction.atomic
    def apply_decrement(sale, user):
        """
        Décrémente le stock pour chaque item de la vente (avec FIFO sur les lots
        si le produit a un suivi de stock). Respecte `Warehouse.allow_negative_stock`
        au moment de la décrémentation (re-vérification après lock).

        Idempotent : ne fait rien si `is_committed(sale)` est True.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.services import FIFOService

        if SaleStockService.is_committed(sale):
            return

        warehouse = sale.warehouse
        if not warehouse:
            return

        org = sale.organization

        for item in sale.items.select_related('product', 'variant').all():
            if not item.product.track_inventory:
                continue

            # FIFO : consomme les lots les plus anciens (ou FEFO si périssable)
            FIFOService.consume_from_batches(
                organization=org,
                product=item.product,
                warehouse=warehouse,
                quantity=item.quantity,
                variant=item.variant,
                reference_type='sale',
                reference_id=str(sale.id),
                user=user,
                notes=f"Vente {sale.reference}",
                exclude_expired=True,
                use_fefo=getattr(item.product, 'has_expiry_date', False),
            )

            # Stock agrégé : lock + re-check allow_negative + décrément
            product_cost = item.product.cost_price or Decimal('0.00')
            cost = item.cost_price if item.cost_price and item.cost_price > 0 else product_cost
            stock, created = Stock.objects.select_for_update().get_or_create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                defaults={'quantity': Decimal('0.000'), 'avg_cost': cost},
            )
            if not created and stock.avg_cost == 0 and cost > 0:
                stock.avg_cost = cost

            quantity_before = stock.quantity
            new_quantity = quantity_before - item.quantity
            allow_negative = getattr(warehouse, 'allow_negative_stock', False)
            if new_quantity < 0 and not allow_negative:
                # Rollback de toute la transaction via raise
                raise ValidationError({
                    'items': (
                        f"Stock insuffisant pour {item.product.name} dans l'entrepôt "
                        f"{warehouse.name}. Disponible: {quantity_before}, demandé: {item.quantity}."
                    )
                })

            stock.quantity = new_quantity
            stock.last_movement_at = timezone.now()
            stock.save()

            StockMovement.objects.create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                batch=item.batch,
                movement_type='sale',
                quantity=-item.quantity,
                unit_cost=item.cost_price,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                reference_type='sale',
                reference_id=sale.id,
                notes=f"Vente {sale.reference}",
                created_by=user,
            )

    @staticmethod
    @transaction.atomic
    def revert(sale, user):
        """
        Ré-incrémente le stock pour chaque item (mouvements `return_in`).
        Ne fait rien si le stock n'avait pas été décrémenté.
        """
        from apps.inventory.models import Stock, StockMovement

        if not SaleStockService.is_committed(sale):
            return

        warehouse = sale.warehouse
        if not warehouse:
            return

        org = sale.organization

        for item in sale.items.select_related('product', 'variant').all():
            if not item.product.track_inventory:
                continue

            product_cost = item.product.cost_price or Decimal('0.00')
            cost = item.cost_price if item.cost_price and item.cost_price > 0 else product_cost
            stock, created = Stock.objects.select_for_update().get_or_create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                defaults={'quantity': Decimal('0.000'), 'avg_cost': cost},
            )
            if not created and stock.avg_cost == 0 and cost > 0:
                stock.avg_cost = cost

            quantity_before = stock.quantity

            # Recalcul du coût moyen pondéré au retour
            if cost > 0 and item.quantity > 0:
                if stock.quantity > 0:
                    total_existing = stock.quantity * stock.avg_cost
                    total_incoming = item.quantity * item.cost_price
                    stock.avg_cost = (
                        (total_existing + total_incoming) /
                        (stock.quantity + item.quantity)
                    ).quantize(TWO_PLACES)
                else:
                    stock.avg_cost = item.cost_price

            stock.quantity += item.quantity
            stock.last_movement_at = timezone.now()
            stock.save()

            StockMovement.objects.create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                movement_type='return_in',
                quantity=item.quantity,
                unit_cost=item.cost_price,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                reference_type='sale_cancel',
                reference_id=sale.id,
                notes=f"Annulation vente {sale.reference}",
                created_by=user,
            )
