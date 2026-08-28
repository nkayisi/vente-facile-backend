"""
Gestionnaires d'opérations.

Chacun délègue au serializer ou au service que le back-office utilise déjà. Un
gestionnaire ne contient donc AUCUNE règle métier : dès qu'il en porterait une,
elle existerait en double et rediverger ne serait qu'une question de temps.

C'est tout l'objet du lot : il n'y a plus qu'un chemin d'écriture.
"""
from apps.core.warehouse_scope import assert_warehouse_allowed_for_request

from .operations import OperationRejected, handler


def _require(payload, *keys):
    manquants = [k for k in keys if not payload.get(k)]
    if manquants:
        raise OperationRejected(
            f"Champs requis absents : {', '.join(manquants)}.",
            code='missing_fields',
            details={k: ['Ce champ est requis.'] for k in manquants},
        )


# ----------------------------------------------------------------------- vente


@handler('sale.create')
def sale_create(ctx, payload):
    """
    Une vente, par le chemin exact du point de vente web.

    `SaleCreateSerializer.create()` enchaîne résolution de fidélité,
    encaissement, entrée en caisse, monnaie rendue, décrément de stock avec
    déconditionnement, inscription de la dette et attribution des points. Rien
    de tout cela n'est réécrit ici, et c'est le but.
    """
    from apps.sales.serializers import SaleCreateSerializer, SaleDetailSerializer

    local_id = payload.pop('id', None)
    reference = payload.pop('reference', None)

    serializer = SaleCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)

    # Le périmètre d'entrepôt se revalide ici comme dans la vue : le serializer
    # expose `warehouse`, donc un terminal pourrait en soumettre un interdit.
    warehouse = serializer.validated_data.get('warehouse')
    assert_warehouse_allowed_for_request(
        ctx.request, getattr(warehouse, 'id', None), allow_none=True
    )

    extra = {}
    if local_id:
        # La clé vient du terminal : c'est ce qui rend l'opération rejouable et
        # ce qui permet à la vente locale de se réconcilier avec elle-même.
        extra['id'] = local_id
    if reference:
        extra['reference'] = reference

    sale = serializer.save(**extra)
    sale.refresh_from_db()

    return {
        'server_ids': {
            'sale': str(sale.id),
            'reference': sale.reference,
        },
        'authoritative': SaleDetailSerializer(sale).data,
    }


@handler('sale.add_payment')
def sale_add_payment(ctx, payload):
    """
    Un règlement sur une facture déjà émise.

    Passe par `apply_payment_to_sale`, point d'entrée UNIQUE d'un règlement :
    numéro de reçu, monnaie, mouvement de caisse, décrément de stock, fidélité
    et dette y sont enchaînés dans le bon ordre.
    """
    from apps.sales.models import Sale
    from apps.sales.serializers import SaleDetailSerializer
    from apps.sales.services import apply_payment_to_sale

    _require(payload, 'sale')

    sale_id = payload.pop('sale')
    if not Sale.objects.filter(id=sale_id, organization=ctx.organization).exists():
        raise OperationRejected(
            "Cette vente n'existe pas dans cet établissement.",
            code='sale_not_found',
        )

    sale, payment = apply_payment_to_sale(
        sale_id,
        ctx.user,
        payment_method_id=payload.get('payment_method'),
        tendered_amount=payload.get('tendered_amount') or payload.get('amount'),
        currency=payload.get('currency'),
        exchange_rate=payload.get('exchange_rate'),
        change_currency=payload.get('change_currency'),
        reference=payload.get('reference', ''),
        notes=payload.get('notes', ''),
    )

    return {
        'server_ids': {
            'sale': str(sale.id),
            'payment': str(payment.id) if payment else None,
            'receipt_number': getattr(payment, 'receipt_number', '') if payment else '',
        },
        'authoritative': SaleDetailSerializer(sale).data,
    }


@handler('sale.cancel')
def sale_cancel(ctx, payload):
    from apps.sales.models import Sale
    from apps.sales.serializers import SaleDetailSerializer
    from apps.sales.views import SaleViewSet  # noqa: F401 - documente le chemin web

    _require(payload, 'sale')
    sale = Sale.objects.filter(
        id=payload['sale'], organization=ctx.organization
    ).first()
    if sale is None:
        raise OperationRejected("Cette vente n'existe pas.", code='sale_not_found')

    from apps.sales.services import SaleStockService
    from apps.settings.services import LoyaltyService
    from apps.contacts import services as contacts_services

    if sale.status in ('cancelled', 'refunded'):
        # Déjà annulée : le verdict est le même qu'un succès, sans quoi le
        # terminal réessaierait indéfiniment une annulation déjà faite.
        return {
            'server_ids': {'sale': str(sale.id)},
            'authoritative': SaleDetailSerializer(sale).data,
        }

    if sale.stock_reserved:
        SaleStockService.release_reservation(sale, ctx.user)
    if SaleStockService.is_committed(sale):
        SaleStockService.revert(sale, ctx.user)

    if sale.customer and sale.amount_due > 0:
        contacts_services.adjust_balance(
            sale.customer, -sale.amount_due,
            currency=sale.currency, exchange_rate=sale.exchange_rate,
            notes=f"Annulation de la vente {sale.reference}", user=ctx.user,
        )

    LoyaltyService.reverse_sale_transactions(sale, ctx.user)

    sale.status = 'cancelled'
    sale.amount_due = 0
    sale.notes = (payload.get('reason') or sale.notes)
    sale.save()

    return {
        'server_ids': {'sale': str(sale.id)},
        'authoritative': SaleDetailSerializer(sale).data,
    }


# ----------------------------------------------------------------------- caisse


@handler('register_session.open')
def register_session_open(ctx, payload):
    """
    Ouvre une session de caisse.

    Le refus le plus probable et le plus important : une contrainte d'unicité
    interdit deux sessions ouvertes sur une même caisse. Un terminal qui a ouvert
    une session hors ligne pendant qu'un autre le faisait aussi verra son
    opération refusée, et toutes les ventes qui s'y rattachaient avec elle. Le
    message doit donc dire QUI l'a ouverte et QUAND.
    """
    from apps.sales.models import Register, RegisterSession
    from apps.sales.serializers import RegisterSessionDetailSerializer

    _require(payload, 'register')

    register = Register.objects.filter(
        id=payload['register'], organization=ctx.organization, is_active=True
    ).first()
    if register is None:
        raise OperationRejected("Caisse introuvable ou inactive.", code='register_not_found')

    ouverte = RegisterSession.objects.select_for_update().filter(
        register=register, status='open'
    ).first()
    if ouverte is not None:
        if str(ouverte.id) == str(payload.get('id')):
            return {
                'server_ids': {'session': str(ouverte.id)},
                'authoritative': RegisterSessionDetailSerializer(ouverte).data,
            }
        raise OperationRejected(
            f"Une session est déjà ouverte sur {register.name}, "
            f"par {ouverte.opened_by.full_name if ouverte.opened_by else 'un autre utilisateur'} "
            f"le {ouverte.opened_at:%d/%m à %H:%M}.",
            code='session_already_open',
        )

    session = RegisterSession.objects.create(
        id=payload.get('id') or None,
        organization=ctx.organization,
        register=register,
        opened_by=ctx.user,
        opening_balance=payload.get('opening_balance') or 0,
        status='open',
        notes=payload.get('notes', ''),
    )
    return {
        'server_ids': {'session': str(session.id)},
        'authoritative': RegisterSessionDetailSerializer(session).data,
    }


# ---------------------------------------------------------------------- clients


@handler('customer.create')
def customer_create(ctx, payload):
    from apps.contacts.serializers import CustomerCreateSerializer, CustomerDetailSerializer

    local_id = payload.pop('id', None)
    serializer = CustomerCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)

    customer = serializer.save(
        organization=ctx.organization, **({'id': local_id} if local_id else {})
    )
    return {
        'server_ids': {'customer': str(customer.id)},
        'authoritative': CustomerDetailSerializer(customer).data,
    }


@handler('customer.record_payment')
def customer_record_payment(ctx, payload):
    """
    Règlement porté au compte d'un client.

    `record_payment` impute sur les factures ouvertes, la plus ancienne d'abord,
    dans la devise du règlement, et le reliquat devient une avance. Toute cette
    logique reste côté serveur.
    """
    from apps.contacts.models import Customer
    from apps.contacts.views import CustomerViewSet  # noqa: F401 - documente le chemin

    _require(payload, 'customer', 'amount')

    customer = Customer.objects.filter(
        id=payload['customer'], organization=ctx.organization
    ).first()
    if customer is None:
        raise OperationRejected("Client introuvable.", code='customer_not_found')

    from apps.sales.services import apply_payment_to_sale
    from apps.contacts import services as contacts_services
    from apps.core.numbering import PREFIX_DEBT_PAYMENT, allocate_document_number

    receipt = payload.get('receipt_number') or allocate_document_number(
        ctx.organization, PREFIX_DEBT_PAYMENT
    )
    reste = contacts_services._quantize(payload['amount'])
    devise = payload.get('currency')

    for vente in contacts_services.open_credit_sales(customer, devise):
        if reste <= 0:
            break
        part = min(reste, vente.amount_due)
        apply_payment_to_sale(
            vente.id, ctx.user,
            payment_method_id=payload.get('payment_method'),
            tendered_amount=part, currency=devise,
            notes=payload.get('notes', ''), receipt_number=receipt,
        )
        reste -= part

    if reste > 0:
        contacts_services.settle_debt(
            customer, reste, currency=devise, user=ctx.user,
            notes=payload.get('notes', ''), receipt_number=receipt,
        )

    customer.refresh_from_db()
    return {
        'server_ids': {'customer': str(customer.id), 'receipt_number': receipt},
        'authoritative': {
            'id': str(customer.id),
            'current_balance': str(customer.current_balance),
        },
    }


# ------------------------------------------------------------------------ stock


@handler('stock_movement.create')
def stock_movement_create(ctx, payload):
    """
    Un mouvement de stock, par le serializer du back-office.

    Celui-ci passe par `PackagingService` : la saisie « X contenants + Y unités »
    y est convertie, et le partage scellé/vrac reste juste. L'ancienne
    synchronisation faisait `stock.quantity += ...` à la main, ce qui réparait le
    partage en silence et transformait trois casiers en « 2 casiers +
    19 bouteilles ».
    """
    from apps.inventory.serializers import StockMovementCreateSerializer, StockMovementDetailSerializer

    local_id = payload.pop('id', None)
    serializer = StockMovementCreateSerializer(
        data=payload, context={'request': ctx.request}
    )
    serializer.is_valid(raise_exception=True)

    assert_warehouse_allowed_for_request(
        ctx.request,
        getattr(serializer.validated_data.get('warehouse'), 'id', None),
        allow_none=True,
    )

    movement = serializer.save(**({'id': local_id} if local_id else {}))
    return {
        'server_ids': {'stock_movement': str(movement.id)},
        'authoritative': StockMovementDetailSerializer(movement).data,
    }


# ------------------------------------------------------------------- livre de caisse


@handler('expense.create')
def expense_create(ctx, payload):
    from apps.cashbook.serializers import ExpenseCreateSerializer, ExpenseDetailSerializer

    local_id = payload.pop('id', None)
    serializer = ExpenseCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    expense = serializer.save(**({'id': local_id} if local_id else {}))
    return {
        'server_ids': {'expense': str(expense.id)},
        'authoritative': ExpenseDetailSerializer(expense).data,
    }


@handler('cash_movement.create')
def cash_movement_create(ctx, payload):
    from apps.cashbook.serializers import CashMovementCreateSerializer, CashMovementDetailSerializer

    local_id = payload.pop('id', None)
    serializer = CashMovementCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    movement = serializer.save(**({'id': local_id} if local_id else {}))
    return {
        'server_ids': {'cash_movement': str(movement.id)},
        'authoritative': CashMovementDetailSerializer(movement).data,
    }
