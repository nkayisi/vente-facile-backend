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
        from apps.inventory.packaging import PackagingService
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

        for item in sale.items.select_related('product', 'variant').order_by('product_id'):
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

            # Conditionnement - sous le verrou pris ci-dessus, ce qui empêche
            # deux ventes simultanées d'ouvrir chacune un emballage pour le
            # même besoin.
            package_quantity = item.package_quantity or Decimal('0.000')
            loose_needed = item.loose_quantity

            # Vendre en gros exige des emballages encore scellés : on ne remet
            # pas des unités déjà sorties dans un emballage neuf.
            PackagingService.assert_sealed_available(
                stock, item.product, package_quantity
            )

            # Servir au détail : ouvrir juste ce qu'il faut, sans interrompre le
            # caissier. Les mouvements créés sont rattachés à la vente et
            # remontés dans la réponse pour l'informer.
            if loose_needed > 0:
                PackagingService.ensure_loose_available(
                    stock, item.product, loose_needed, user=user,
                    reference_type='sale', reference_id=sale.id,
                )

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

            # Les deux canaux se décrémentent séparément, chacun dans son
            # compteur : la vente en gros retire des conditionnements scellés,
            # la vente au détail retire des unités déjà ouvertes.
            PackagingService.apply_delta(
                stock, item.product,
                delta_packages=-package_quantity,
                delta_loose=-loose_needed,
            )
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
                input_package_quantity=package_quantity,
                input_loose_quantity=loose_needed,
                packaging_factor=item.packaging_factor,
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

        for item in sale.items.select_related('product', 'variant').order_by('product_id'):
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

        for item in sale.items.select_related('product', 'variant').order_by('product_id'):
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

        Les mouvements de déconditionnement de la vente ne sont **pas** annulés :
        un emballage ouvert le reste. Ils portent `quantity = 0`, ils
        n'influencent donc aucun total.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService

        if not SaleStockService.is_committed(sale):
            return

        warehouse = sale.warehouse
        if not warehouse:
            return

        org = sale.organization

        for item in sale.items.select_related('product', 'variant').order_by('product_id'):
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

            # Les unités reviennent en vrac : un client qui rend 2 bouteilles ne
            # reconstitue pas un emballage scellé, même si la ligne d'origine
            # portait sur des conditionnements entiers.
            PackagingService.apply_delta(
                stock, item.product,
                delta_packages=0,
                delta_loose=item.quantity,
            )
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
                # Le retour se relit dans ses propres termes : tout revient en
                # vrac. Sans ces champs, l'historique d'une annulation se lisait
                # en unités brutes, contrairement à celui d'un retour client.
                input_package_quantity=Decimal('0.000'),
                input_loose_quantity=item.quantity,
                packaging_factor=item.packaging_factor,
                reference_type='sale_cancel',
                reference_id=sale.id,
                notes=f"Annulation vente {sale.reference}",
                created_by=user,
            )

    @staticmethod
    @transaction.atomic
    def apply_return(sale_return, user):
        """
        Remet en stock les articles d'un retour client approuvé.

        Extrait de ``SaleReturnViewSet.approve``, où il dupliquait le recalcul de
        coût moyen de ``revert``. Les garder séparés aurait obligé à maintenir
        la mise à jour du vrac à deux endroits.

        Les unités reviennent **en vrac** : un client qui rend 2 bouteilles ne
        reconstitue pas un emballage scellé.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService

        original_sale = sale_return.original_sale
        warehouse = original_sale.warehouse
        if not warehouse:
            return

        org = sale_return.organization

        items = (
            sale_return.items.filter(restock=True)
            .select_related('original_item__product', 'original_item__variant')
            .order_by('original_item__product_id')
        )

        for item in items:
            original_item = item.original_item
            product = original_item.product
            if not product.track_inventory:
                continue

            product_cost = product.cost_price or Decimal('0.00')
            cost = (
                original_item.cost_price
                if original_item.cost_price and original_item.cost_price > 0
                else product_cost
            )
            stock, created = Stock.objects.select_for_update().get_or_create(
                organization=org,
                product=product,
                variant=original_item.variant,
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
                    total_incoming = item.quantity * original_item.cost_price
                    stock.avg_cost = (
                        (total_existing + total_incoming) /
                        (stock.quantity + item.quantity)
                    ).quantize(TWO_PLACES)
                else:
                    stock.avg_cost = original_item.cost_price

            PackagingService.apply_delta(
                stock, product,
                delta_packages=0,
                delta_loose=item.quantity,
            )
            stock.last_movement_at = timezone.now()
            stock.save()

            StockMovement.objects.create(
                organization=org,
                product=product,
                variant=original_item.variant,
                warehouse=warehouse,
                movement_type='return_in',
                quantity=item.quantity,
                unit_cost=original_item.cost_price,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                input_loose_quantity=item.quantity,
                packaging_factor=original_item.packaging_factor,
                reference_type='sale_return',
                reference_id=sale_return.id,
                notes=f"Retour {sale_return.reference}",
                created_by=user,
            )


# =============================================================================
# MULTI-DEVISE : conversion des règlements et de la monnaie rendue
# =============================================================================

RATE_QUANTIZE = Decimal('0.000001')


def resolve_sale_currency(organization_id, currency=None, exchange_rate=None):
    """
    Devise de facture + taux snapshot d'une vente, résolus côté serveur.

    Délègue à ``CurrencyService.resolve``, point d'entrée unique du projet.
    Non strict : une vente ne doit jamais échouer parce qu'une devise
    historique a été désactivée depuis.
    """
    from apps.settings.services import CurrencyService

    return CurrencyService.resolve(organization_id, currency, exchange_rate)


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

    Ne met PAS à jour ``sale.amount_paid`` - l'appelant agrège les ``payment.amount``.
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


@transaction.atomic
def get_loyalty_payment_method(organization):
    """
    Moyen de paiement « points de fidélité » de l'organisation, créé au besoin.

    Ce n'est pas de l'argent physique : il est exclu des mouvements de caisse et
    du comptage de clôture de session.
    """
    from apps.sales.models import PaymentMethod

    method, _ = PaymentMethod.objects.get_or_create(
        organization=organization,
        method_type=PaymentMethod.MethodType.LOYALTY,
        defaults={
            'name': 'Points de fidélité',
            'code': 'LOYALTY',
            'is_active': True,
        },
    )
    return method


def apply_payment_to_sale(sale_id, user, *, payment_method_id=None,
                          tendered_amount=None, currency=None, exchange_rate=None,
                          change_currency=None, reference='', notes='',
                          award_loyalty=True, points_used=0):
    """
    Applique un règlement à une facture existante, de bout en bout.

    Point d'entrée UNIQUE : utilisé par ``SaleViewSet.add_payment`` (règlement
    depuis la facture) ET par ``CustomerViewSet.record_payment`` (règlement
    depuis la fiche client). Avant ce lot, le second ne touchait aucune facture :
    le solde du client bougeait pendant que la vente restait ``partially_paid``,
    et comme la vente n'atteignait jamais ``completed``, les points de fidélité
    n'étaient jamais attribués.

    Enchaîne, sous verrou sur la vente : création du règlement, recalcul des
    montants, monnaie rendue, décrément de stock, attribution des points,
    mouvement de caisse et mise à jour de la dette client.

    Retourne ``(sale, payment)``.
    """
    from apps.sales.models import Sale

    sale = (
        Sale.objects.select_related('customer', 'organization', 'warehouse')
        .select_for_update(of=('self',))
        .get(pk=sale_id)
    )
    if sale.status in ['completed', 'cancelled', 'refunded']:
        raise ValidationError("Impossible d'ajouter un paiement à cette vente")

    previous_status = sale.status
    due_before = sale.amount_due

    payments = []

    # 1) Règlement en points d'abord : il réduit le reste à payer en argent.
    loyalty_resolution = None
    if points_used and points_used > 0 and sale.customer:
        from apps.settings.services import LoyaltyService

        loyalty_resolution = LoyaltyService.resolve_redemption(
            sale.customer, sale.organization, points_used, sale.amount_due,
            target_currency=sale.currency,
        )
        if loyalty_resolution is None:
            raise ValidationError(
                "Points inutilisables : solde insuffisant, minimum non atteint, "
                "ou aucun programme de fidélité actif."
            )
        payments.append(create_payment(
            sale, user,
            payment_method_id=get_loyalty_payment_method(sale.organization).id,
            tendered_amount=loyalty_resolution['amount'],
            currency=sale.currency,
            notes=f"{loyalty_resolution['points']} points utilisés",
        ))

    # 2) Puis le règlement en argent, s'il y en a un.
    if tendered_amount is not None and Decimal(tendered_amount) > 0:
        payments.append(create_payment(
            sale, user,
            payment_method_id=payment_method_id,
            tendered_amount=tendered_amount,
            currency=currency,
            exchange_rate=exchange_rate,
            reference=reference,
            notes=notes,
        ))

    if not payments:
        raise ValidationError("Aucun règlement à appliquer.")

    payment = payments[-1]
    for p in payments:
        sale.amount_paid += p.amount
    sale.amount_due = (sale.total - sale.amount_paid).quantize(TWO_PLACES)

    change_amount = Decimal('0.00')
    resolved_change_currency = change_currency or sale.change_currency or sale.currency
    if sale.amount_paid >= sale.total:
        change_amount, resolved_change_currency, _ = resolve_change(
            sale, resolved_change_currency,
        )
        sale.change_amount = change_amount
        sale.change_currency = resolved_change_currency
        sale.amount_due = Decimal('0.00')
        sale.status = 'completed'
    else:
        sale.status = 'partially_paid'

    sale.save()

    # Consommation effective des points, une fois la vente enregistrée.
    if loyalty_resolution:
        from apps.settings.services import LoyaltyService
        LoyaltyService.persist_redemption(
            sale, loyalty_resolution, user=user, payment=payments[0],
        )

    if sale.status == 'completed' and previous_status != 'completed':
        if not sale.warehouse:
            from apps.inventory.models import Warehouse as WarehouseModel
            default_wh = WarehouseModel.objects.filter(
                organization=sale.organization,
                is_default=True, is_active=True, is_deleted=False,
            ).first() or WarehouseModel.objects.filter(
                organization=sale.organization,
                is_active=True, is_deleted=False,
            ).first()
            if default_wh:
                sale.warehouse = default_wh
                sale.save(update_fields=['warehouse'])

        SaleStockService.apply_decrement(sale, user)

        if award_loyalty and sale.customer:
            from apps.settings.services import LoyaltyService
            LoyaltyService.award_points_for_sale(sale, user=user)

    # Caisse : dans la devise réellement remise. Un règlement en points n'est
    # pas de l'argent physique et ne doit pas entrer au tiroir.
    from apps.cashbook.services import (
        record_change, record_debt_collection_payment, record_sale_payment_income,
    )
    for p in payments:
        if p.payment_method and p.payment_method.method_type == 'loyalty':
            continue
        if sale.sale_type == 'credit' and sale.customer:
            record_debt_collection_payment(
                sale.organization, sale, p, sale.customer, user,
            )
        else:
            record_sale_payment_income(sale.organization, sale, p, user)
    if change_amount > 0:
        record_change(
            sale.organization, sale, change_amount, resolved_change_currency, user,
        )

    # Dette client : on ne solde QUE ce qui était réellement dû. Le surplus est
    # rendu physiquement au client (monnaie), le lui créditer en plus reviendrait
    # à le compter deux fois.
    if sale.customer and due_before > 0:
        from apps.contacts import services as contacts_services
        applied = sum((p.amount for p in payments), Decimal('0.00'))
        settled = min(applied, due_before)
        if settled > 0:
            contacts_services.settle_debt(
                sale.customer, settled,
                currency=sale.currency,
                sale=sale,
                reference=sale.reference,
                notes=f"Paiement sur facture {sale.reference}",
                user=user,
                payment_method=(
                    payment.payment_method.method_type
                    if payment.payment_method else 'cash'
                ),
            )

    return sale, payment


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
