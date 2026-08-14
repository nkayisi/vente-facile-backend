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

        # Si la vente provient d'une conversion de devis et a réservé du
        # stock, libérer la réservation avant le décrément effectif. Sinon
        # ``reserved_quantity`` resterait gonflée alors que ``quantity``
        # baisse, biaisant ``available_quantity`` négativement.
        if sale.stock_reserved:
            SaleStockService.release_reservation(sale, user)

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
    def reserve_stock(sale, user):
        """
        Réserve du stock pour une vente (conversion de devis).

        Incrémente ``Stock.reserved_quantity`` pour chaque item, sous lock.
        Marque ``sale.stock_reserved = True`` pour rendre l'opération
        idempotente. Lève ``ValidationError`` si la quantité disponible
        (``quantity - reserved_quantity``) est insuffisante et que
        ``warehouse.allow_negative_stock`` est ``False``.

        À appeler à la conversion d'un Quotation → Sale, pour empêcher
        qu'un autre cashier ne vende les mêmes unités pendant la fenêtre
        entre conversion et encaissement.
        """
        from apps.inventory.models import Stock

        if sale.stock_reserved:
            return

        warehouse = sale.warehouse
        if not warehouse:
            return

        org = sale.organization
        allow_negative = getattr(warehouse, 'allow_negative_stock', False)

        for item in sale.items.select_related('product', 'variant').all():
            if not item.product.track_inventory:
                continue

            stock = Stock.objects.select_for_update().filter(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
            ).first()

            if stock is None:
                if allow_negative:
                    continue
                raise ValidationError({
                    'items': (
                        f"Stock indisponible pour {item.product.name} "
                        f"dans l'entrepôt {warehouse.name}."
                    )
                })

            available = stock.quantity - stock.reserved_quantity
            if item.quantity > available and not allow_negative:
                raise ValidationError({
                    'items': (
                        f"Stock disponible insuffisant pour {item.product.name}. "
                        f"Disponible (hors réservations) : {available}, "
                        f"demandé : {item.quantity}."
                    )
                })

            stock.reserved_quantity = stock.reserved_quantity + item.quantity
            stock.save()

        sale.stock_reserved = True
        sale.save(update_fields=['stock_reserved'])

    @staticmethod
    @transaction.atomic
    def release_reservation(sale, user):
        """
        Libère la réservation faite par ``reserve_stock``. Idempotent :
        ne fait rien si ``sale.stock_reserved`` est ``False``.

        À appeler :
        - Avant la décrémentation effective (``apply_decrement``) pour
          libérer la réservation avant que ``Stock.quantity`` soit modifié.
        - À l'annulation d'une vente en pending qui n'avait pas encore été
          encaissée mais avait réservé son stock.
        """
        from apps.inventory.models import Stock
        from decimal import Decimal as _D

        if not sale.stock_reserved:
            return

        warehouse = sale.warehouse
        if not warehouse:
            sale.stock_reserved = False
            sale.save(update_fields=['stock_reserved'])
            return

        org = sale.organization

        for item in sale.items.select_related('product', 'variant').all():
            if not item.product.track_inventory:
                continue

            stock = Stock.objects.select_for_update().filter(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
            ).first()

            if stock is None:
                continue

            stock.reserved_quantity = max(
                _D('0.000'), stock.reserved_quantity - item.quantity
            )
            stock.save()

        sale.stock_reserved = False
        sale.save(update_fields=['stock_reserved'])

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


# =============================================================================
# MULTI-DEVISE : conversion des règlements et de la monnaie rendue
# =============================================================================

RATE_QUANTIZE = Decimal('0.000001')


def resolve_sale_currency(organization_id, currency=None, exchange_rate=None):
    """
    Devise de facture + taux snapshot d'une vente, résolus côté serveur.

    Retourne ``(currency, exchange_rate)`` où ``exchange_rate`` = unités de
    devise PRINCIPALE de l'org pour 1 unité de ``currency``.

    - devise absente ⇒ devise principale de l'organisation, taux 1 ;
    - devise = principale ⇒ taux 1 (forcé) ;
    - devise secondaire avec un taux de 1 ⇒ le taux est (re)lu depuis
      ``OrganizationCurrency`` : 1 est impossible pour une devise secondaire,
      c'est la marque d'un taux jamais renseigné, qui fausserait les rapports.
    """
    from apps.organizations.models import Organization
    from apps.settings.services import CurrencyService

    primary = (
        Organization.objects.filter(id=organization_id)
        .values_list('currency', flat=True)
        .first()
    ) or 'CDF'
    currency = (currency or '').strip() or primary

    if currency == primary:
        return currency, Decimal('1.000000')

    if exchange_rate is None or Decimal(exchange_rate) in (Decimal('0'), Decimal('1')):
        oc = CurrencyService.get_org_currencies(
            Organization.objects.get(id=organization_id)
        ).get(currency)
        if oc is not None:
            return currency, oc.exchange_rate
        return currency, Decimal('1.000000')

    return currency, Decimal(exchange_rate)


def resolve_tender(sale_currency, organization, tendered_amount, currency=None,
                   exchange_rate=None):
    """
    Convertit un montant remis par le client (exprimé dans `currency`) vers la
    devise de la vente `sale_currency`.

    Convention de retour : ``exchange_rate`` = unités de devise de la VENTE pour
    1 unité de ``currency`` ⇒ ``amount = tendered_amount × exchange_rate``.

    - Si ``currency`` est vide ou identique à la devise de la vente : taux 1.
    - Sinon, si un taux est fourni (front) et > 0 : on l'utilise tel quel.
    - Sinon : conversion via ``CurrencyService`` (passe par la devise principale).

    Retourne ``(amount_in_sale_currency, currency, exchange_rate)``.
    """
    tendered_amount = Decimal(tendered_amount)
    currency = (currency or '').strip()

    if not currency or currency == sale_currency:
        return tendered_amount.quantize(TWO_PLACES), sale_currency, Decimal('1.000000')

    if exchange_rate is not None and Decimal(exchange_rate) > 0:
        rate = Decimal(exchange_rate)
        return (tendered_amount * rate).quantize(TWO_PLACES), currency, rate

    from apps.settings.services import CurrencyService
    result = CurrencyService.convert(
        tendered_amount, currency, sale_currency, organization
    )
    return result['converted_amount'], currency, result['exchange_rate']


def create_payment(sale, user, *, payment_method=None, payment_method_id=None,
                   tendered_amount=None, amount=None, currency=None,
                   exchange_rate=None, reference='', notes='', status='completed'):
    """
    Crée un ``Payment`` pour ``sale`` en convertissant le montant remis dans la
    devise de la vente. ``tendered_amount`` est prioritaire ; à défaut ``amount``
    est traité comme le montant remis (rétro-compatibilité mono-devise).

    Ne met PAS à jour ``sale.amount_paid`` — l'appelant agrège les ``payment.amount``.
    Retourne le ``Payment`` créé.
    """
    from apps.sales.models import Payment

    tendered = tendered_amount if tendered_amount is not None else amount
    if tendered is None:
        raise ValidationError("Montant du règlement manquant.")

    applied, ccy, rate = resolve_tender(
        sale.currency, sale.organization, tendered, currency, exchange_rate
    )

    if payment_method is not None and payment_method_id is None:
        payment_method_id = getattr(payment_method, 'id', payment_method)

    return Payment.objects.create(
        sale=sale,
        organization=sale.organization,
        payment_method_id=payment_method_id,
        amount=applied,
        tendered_amount=Decimal(tendered).quantize(TWO_PLACES),
        currency=ccy,
        exchange_rate=rate,
        reference=reference or '',
        notes=notes or '',
        received_by=user,
        status=status,
    )


def resolve_change(sale, change_currency=None):
    """
    Calcule la monnaie à rendre à partir du surplus payé
    (``amount_paid - total``, en devise de la vente), exprimée dans
    ``change_currency`` (choix du caissier ; défaut = devise de la vente).

    Retourne ``(change_amount, change_currency, exchange_rate_to_primary)`` où le
    montant est exprimé dans ``change_currency``. ``(0, devise, 1)`` si pas de surplus.
    """
    change_currency = (change_currency or '').strip() or sale.currency
    overpay = (sale.amount_paid - sale.total)
    if overpay <= 0:
        return Decimal('0.00'), change_currency, Decimal('1.000000')

    if change_currency == sale.currency:
        return overpay.quantize(TWO_PLACES), change_currency, Decimal('1.000000')

    from apps.settings.services import CurrencyService
    result = CurrencyService.convert(
        overpay, sale.currency, change_currency, sale.organization
    )
    return result['converted_amount'], change_currency, result['exchange_rate']
