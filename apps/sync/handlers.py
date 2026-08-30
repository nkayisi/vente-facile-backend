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
        # Le terminal a DÉJÀ imprimé le reçu quand cet acte remonte : son numéro
        # est sur le papier que le client détient. Laisser le serveur en allouer
        # un second donnerait deux numéros pour un seul versement, et le ticket
        # ne désignerait plus rien de retrouvable.
        receipt_number=payload.get('receipt_number'),
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

    Le corps est celui du back-office (`contacts.services.record_payment`) : il
    impute sur les factures ouvertes, la plus ancienne d'abord, dans la devise
    d'imputation, et le reliquat devient une avance.

    Ce gestionnaire en tenait auparavant sa PROPRE version, et elle avait déjà
    dérivé sur trois points : elle ignorait `settle_currency` (un client devant
    en USD et payant en francs ne voyait pas sa dette bouger), elle ne résolvait
    pas le taux de change, et elle numérotait toujours en `RGL` - un versement
    sans facture ouverte est une avance, et son reçu porte `AVC`.

    « Avance » et « règlement » sont d'ailleurs le MÊME acte depuis que
    `record_advance` est devenu un alias : inscrire une avance sans toucher aux
    factures d'un client déjà endetté faisait diverger son solde de la somme de
    ses `amount_due`.
    """
    from apps.contacts.models import Customer
    from apps.contacts.views import CustomerViewSet  # noqa: F401 - documente le chemin
    from apps.contacts import services as contacts_services

    _require(payload, 'customer', 'amount')

    customer = Customer.objects.filter(
        id=payload['customer'], organization=ctx.organization
    ).first()
    if customer is None:
        raise OperationRejected("Client introuvable.", code='customer_not_found')

    resultat = contacts_services.record_payment(
        customer, payload['amount'],
        user=ctx.user,
        currency=payload.get('currency'),
        exchange_rate=payload.get('exchange_rate'),
        settle_currency=payload.get('settle_currency'),
        payment_method=payload.get('payment_method', 'cash'),
        reference=payload.get('reference', ''),
        notes=payload.get('notes', ''),
        # Numéro imposé par le terminal : le reçu est sorti hors ligne, sous ce
        # numéro-là, et il ne peut plus changer.
        receipt_number=payload.get('receipt_number'),
    )

    return {
        'server_ids': {
            'customer': str(customer.id),
            'receipt_number': resultat['receipt_number'],
        },
        'authoritative': {
            'id': str(customer.id),
            'current_balance': resultat['new_balance'],
            'balances': resultat['balances'],
            'settled_invoices': resultat['settled_invoices'],
            'advance_amount': resultat['advance_amount'],
        },
    }


@handler('customer.adjust_balance')
def customer_adjust_balance(ctx, payload):
    """
    Ajustement manuel du solde d'un client.

    Positif : dette de plus. Négatif : dette réduite, et l'argent entre au
    tiroir. Même corps que `CustomerViewSet.adjust_balance`.
    """
    from apps.contacts.models import Customer
    from apps.contacts.views import CustomerViewSet  # noqa: F401 - documente le chemin
    from apps.contacts import services as contacts_services

    _require(payload, 'customer', 'amount')

    customer = Customer.objects.filter(
        id=payload['customer'], organization=ctx.organization
    ).first()
    if customer is None:
        raise OperationRejected("Client introuvable.", code='customer_not_found')

    resultat = contacts_services.adjust_customer_balance(
        customer, payload['amount'],
        user=ctx.user,
        currency=payload.get('currency'),
        exchange_rate=payload.get('exchange_rate'),
        notes=payload.get('notes', ''),
        receipt_number=payload.get('receipt_number'),
    )

    return {
        'server_ids': {
            'customer': str(customer.id),
            'receipt_number': resultat['receipt_number'],
        },
        'authoritative': {
            'id': str(customer.id),
            'current_balance': resultat['new_balance'],
            'balances': resultat['balances'],
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


# --------------------------------------------------------- opérations de stock
#
# Les transitions d'un transfert ou d'un ajustement passent par les MÊMES
# fonctions que le back-office (`inventory.services`). Un refus métier y est
# déterministe : il devient verdict `rejected`, jamais `retry`. Réessayer un
# transfert déjà expédié le réexpédierait, et le stock sortirait deux fois.


def _refus_si_impossible(fonction, *args, **kwargs):
    """Traduit un refus de transition en refus d'opération, donc en quarantaine."""
    from apps.inventory.services import TransitionRefusee
    try:
        return fonction(*args, **kwargs)
    except TransitionRefusee as exc:
        raise OperationRejected(str(exc), code='transition_refused')


def _objet_de_lorg(modele, ctx, identifiant, quoi):
    objet = modele.objects.filter(id=identifiant, organization=ctx.organization).first()
    if objet is None:
        raise OperationRejected(f"{quoi} introuvable.", code='not_found')
    return objet


@handler('stock.unpack')
def stock_unpack(ctx, payload):
    """
    Ouvre des conditionnements scellés. Geste de comptoir, donc hors ligne.
    """
    from apps.inventory.models import Stock
    from apps.inventory.serializers import StockDetailSerializer
    from apps.inventory.services import unpack_stock

    _require(payload, 'stock')
    stock = _objet_de_lorg(Stock, ctx, payload['stock'], 'Cette ligne de stock')
    assert_warehouse_allowed_for_request(ctx.request, stock.warehouse_id)

    resultat = _refus_si_impossible(
        unpack_stock, stock, ctx.user, payload.get('packages', 1),
    )
    stock.refresh_from_db()
    return {
        'server_ids': {'stock': str(stock.id)},
        'authoritative': {
            **StockDetailSerializer(stock).data,
            'packages_opened': resultat['packages_opened'],
        },
    }


@handler('stock_transfer.create')
def stock_transfer_create(ctx, payload):
    from apps.inventory.serializers import (
        StockTransferCreateSerializer, StockTransferDetailSerializer,
    )

    local_id = payload.pop('id', None)
    serializer = StockTransferCreateSerializer(
        data=payload, context={'request': ctx.request}
    )
    serializer.is_valid(raise_exception=True)

    # Les DEUX entrepôts sont contrôlés, comme dans `perform_create` : un
    # magasinier ne doit pas pouvoir sortir du stock d'un dépôt qu'il ne voit
    # pas, ni s'en faire livrer.
    for cle in ('source_warehouse', 'destination_warehouse'):
        assert_warehouse_allowed_for_request(
            ctx.request, getattr(serializer.validated_data.get(cle), 'id', None),
            allow_none=True,
        )

    transfert = serializer.save(
        organization=ctx.organization, **({'id': local_id} if local_id else {})
    )
    return {
        'server_ids': {'stock_transfer': str(transfert.id)},
        'authoritative': StockTransferDetailSerializer(transfert).data,
    }


def _transition_transfert(ctx, payload, fonction, **extra):
    from apps.inventory.models import StockTransfer
    from apps.inventory.serializers import StockTransferDetailSerializer

    _require(payload, 'transfer')
    transfert = _objet_de_lorg(
        StockTransfer, ctx, payload['transfer'], 'Ce transfert',
    )
    _refus_si_impossible(fonction, transfert, ctx.user, **extra)
    transfert.refresh_from_db()
    return {
        'server_ids': {'stock_transfer': str(transfert.id)},
        'authoritative': StockTransferDetailSerializer(transfert).data,
    }


@handler('stock_transfer.approve')
def stock_transfer_approve(ctx, payload):
    from apps.inventory.services import approve_transfer
    return _transition_transfert(ctx, payload, approve_transfer)


@handler('stock_transfer.ship')
def stock_transfer_ship(ctx, payload):
    from apps.inventory.services import ship_transfer
    return _transition_transfert(ctx, payload, ship_transfer)


@handler('stock_transfer.receive')
def stock_transfer_receive(ctx, payload):
    """
    Réception. `items` porte ce que le magasinier a RÉELLEMENT compté, en
    contenants ou en total : une réception partielle est le cas courant.
    """
    from apps.inventory.services import receive_transfer
    return _transition_transfert(
        ctx, payload, receive_transfer, received_items=payload.get('items') or [],
    )


@handler('stock_transfer.cancel')
def stock_transfer_cancel(ctx, payload):
    from apps.inventory.services import cancel_transfer
    return _transition_transfert(ctx, payload, cancel_transfer)


@handler('stock_adjustment.create')
def stock_adjustment_create(ctx, payload):
    from apps.inventory.serializers import (
        StockAdjustmentCreateSerializer, StockAdjustmentDetailSerializer,
    )

    local_id = payload.pop('id', None)
    serializer = StockAdjustmentCreateSerializer(
        data=payload, context={'request': ctx.request}
    )
    serializer.is_valid(raise_exception=True)
    assert_warehouse_allowed_for_request(
        ctx.request,
        getattr(serializer.validated_data.get('warehouse'), 'id', None),
        allow_none=True,
    )

    ajustement = serializer.save(
        organization=ctx.organization, **({'id': local_id} if local_id else {})
    )
    return {
        'server_ids': {'stock_adjustment': str(ajustement.id)},
        'authoritative': StockAdjustmentDetailSerializer(ajustement).data,
    }


def _transition_ajustement(ctx, payload, fonction):
    from apps.inventory.models import StockAdjustment
    from apps.inventory.serializers import StockAdjustmentDetailSerializer

    _require(payload, 'adjustment')
    ajustement = _objet_de_lorg(
        StockAdjustment, ctx, payload['adjustment'], 'Cet ajustement',
    )
    assert_warehouse_allowed_for_request(ctx.request, ajustement.warehouse_id)
    _refus_si_impossible(fonction, ajustement, ctx.user)
    ajustement.refresh_from_db()
    return {
        'server_ids': {'stock_adjustment': str(ajustement.id)},
        'authoritative': StockAdjustmentDetailSerializer(ajustement).data,
    }


@handler('stock_adjustment.approve')
def stock_adjustment_approve(ctx, payload):
    from apps.inventory.services import approve_adjustment
    return _transition_ajustement(ctx, payload, approve_adjustment)


@handler('stock_adjustment.reject')
def stock_adjustment_reject(ctx, payload):
    from apps.inventory.services import reject_adjustment
    return _transition_ajustement(ctx, payload, reject_adjustment)


# ------------------------------------------------------------- inventaire
#
# COMPTER EST LE MEILLEUR USAGE MOBILE DU PRODUIT : on compte debout dans le
# rayon, souvent au fond d'un dépôt sans réseau. Les cinq transitions passent
# donc par le journal, et leurs corps sont ceux du back-office.


def _transition_inventaire(ctx, payload, fonction, serialiser=True, **extra):
    from apps.inventory.models import InventorySession
    from apps.inventory.serializers import InventorySessionDetailSerializer

    _require(payload, 'session')
    session = _objet_de_lorg(
        InventorySession, ctx, payload['session'], "Cette session d'inventaire",
    )
    assert_warehouse_allowed_for_request(ctx.request, session.warehouse_id)

    resultat = _refus_si_impossible(fonction, session, ctx.user, **extra)
    session.refresh_from_db()
    return {
        'server_ids': {'inventory_session': str(session.id)},
        'authoritative': (
            InventorySessionDetailSerializer(session).data if serialiser else resultat
        ),
    }


@handler('inventory_session.create')
def inventory_session_create(ctx, payload):
    from apps.inventory.serializers import (
        InventorySessionCreateSerializer, InventorySessionDetailSerializer,
    )

    local_id = payload.pop('id', None)
    serializer = InventorySessionCreateSerializer(
        data=payload, context={'request': ctx.request}
    )
    serializer.is_valid(raise_exception=True)
    assert_warehouse_allowed_for_request(
        ctx.request,
        getattr(serializer.validated_data.get('warehouse'), 'id', None),
        allow_none=True,
    )

    session = serializer.save(
        organization=ctx.organization, **({'id': local_id} if local_id else {})
    )
    return {
        'server_ids': {'inventory_session': str(session.id)},
        'authoritative': InventorySessionDetailSerializer(session).data,
    }


@handler('inventory_session.start')
def inventory_session_start(ctx, payload):
    from apps.inventory.services import start_inventory_session
    return _transition_inventaire(ctx, payload, start_inventory_session)


@handler('inventory_session.count')
def inventory_session_count(ctx, payload):
    """
    Comptages d'une ou plusieurs lignes.

    Une ligne inconnue est IGNORÉE côté service, pas refusée : un lot de
    comptages remonté d'un terminal ne doit pas être condamné en entier par une
    ligne supprimée entre-temps.
    """
    from apps.inventory.services import record_inventory_counts
    return _transition_inventaire(
        ctx, payload, record_inventory_counts, serialiser=False,
        lignes=payload.get('counts') or [],
    )


@handler('inventory_session.submit')
def inventory_session_submit(ctx, payload):
    from apps.inventory.services import submit_inventory_session
    return _transition_inventaire(ctx, payload, submit_inventory_session)


@handler('inventory_session.validate')
def inventory_session_validate(ctx, payload):
    from apps.inventory.services import validate_inventory_session
    return _transition_inventaire(ctx, payload, validate_inventory_session)


@handler('inventory_session.cancel')
def inventory_session_cancel(ctx, payload):
    from apps.inventory.services import cancel_inventory_session
    return _transition_inventaire(
        ctx, payload, cancel_inventory_session, serialiser=False,
    )


# --------------------------------------------------------------- catalogue


@handler('product.create')
def product_create(ctx, payload):
    """
    Création d'un article, par le serializer du back-office.

    Le SKU et le code-barres restent facultatifs et uniques par organisation :
    deux terminaux hors ligne en fabriqueraient fatalement le même, et le
    serveur refuserait le second - refus déterministe, donc quarantaine, avec
    le message qui va bien.
    """
    from apps.products.serializers import ProductCreateSerializer, ProductDetailSerializer

    local_id = payload.pop('id', None)
    serializer = ProductCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    produit = serializer.save(
        organization=ctx.organization, **({'id': local_id} if local_id else {})
    )
    return {
        'server_ids': {'product': str(produit.id)},
        'authoritative': ProductDetailSerializer(produit).data,
    }


def _referentiel_create(ctx, payload, modele, serializer_classe, cle):
    local_id = payload.pop('id', None)
    serializer = serializer_classe(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    objet = serializer.save(
        organization=ctx.organization, **({'id': local_id} if local_id else {})
    )
    return {
        'server_ids': {cle: str(objet.id)},
        'authoritative': serializer_classe(objet).data,
    }


@handler('category.create')
def category_create(ctx, payload):
    from apps.products.models import Category
    from apps.products.serializers import CategoryCreateSerializer
    return _referentiel_create(
        ctx, payload, Category, CategoryCreateSerializer, 'category',
    )


@handler('brand.create')
def brand_create(ctx, payload):
    from apps.products.models import Brand
    from apps.products.serializers import BrandSerializer
    return _referentiel_create(ctx, payload, Brand, BrandSerializer, 'brand')


@handler('unit.create')
def unit_create(ctx, payload):
    from apps.products.models import Unit
    from apps.products.serializers import UnitSerializer
    return _referentiel_create(ctx, payload, Unit, UnitSerializer, 'unit')


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
