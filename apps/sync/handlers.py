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


def _sauver_avec_audit(ctx, serializer, local_id=None, **extra):
    """
    Enregistre comme le ferait `AuditMixin.perform_create`.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ L'AUTEUR D'UNE PIÈCE N'EST PAS UN ORNEMENT.                              │
    │                                                                          │
    │ `created_by` répond à « qui a créé ce retour, cet ajustement, cette      │
    │ session d'inventaire ». Le back-office le pose par son mixin ; les       │
    │ handlers appelaient `serializer.save(organization=...)` et le laissaient │
    │ nul. Une pièce créée depuis un terminal n'avait donc pas d'auteur, et    │
    │ les permissions d'objet (`django-guardian`), sur lesquelles trois vues   │
    │ s'appuient, n'étaient jamais attribuées.                                 │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from guardian.shortcuts import assign_perm

    champs = {'organization': ctx.organization, **extra}
    if hasattr(serializer.Meta.model, 'created_by'):
        champs['created_by'] = ctx.user
    if local_id:
        champs['id'] = local_id

    instance = serializer.save(**champs)

    nom = instance._meta.model_name
    for perm in (f'view_{nom}', f'change_{nom}', f'delete_{nom}'):
        assign_perm(perm, ctx.user, instance)
    return instance


# ----------------------------------------------------------------------- vente


@handler('sale.create', permission='sales.create')
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


@handler('sale.add_payment', permission='sales.create')
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


@handler('sale.cancel', permission='sales.cancel')
def sale_cancel(ctx, payload):
    """
    Annule une vente, par le SERVICE du back-office.

    Ce handler réécrivait le corps de la vue, et avait déjà dérivé sur quatre
    points : la règle « on n'annule que ses propres ventes », le mouvement de
    caisse d'annulation, le type d'écriture client, et l'écrasement des notes.
    Voir `sales.services.cancel_sale`.
    """
    from apps.core.api_permissions import is_manager_or_above
    from apps.sales.models import Sale
    from apps.sales.serializers import SaleDetailSerializer
    from apps.sales.services import AnnulationRefusee, cancel_sale

    _require(payload, 'sale')
    sale = Sale.objects.filter(
        id=payload['sale'], organization=ctx.organization
    ).first()
    if sale is None:
        raise OperationRejected("Cette vente n'existe pas.", code='sale_not_found')

    try:
        cancel_sale(
            sale, ctx.user,
            reason=payload.get('reason', ''),
            autorise_toutes_ventes=is_manager_or_above(ctx.request),
        )
    except AnnulationRefusee as e:
        # Déterministe : la vente ne deviendra pas la sienne en réessayant.
        raise OperationRejected(str(e), code='not_own_sale')

    # Une vente déjà annulée rend un SUCCÈS : le contraire ferait réessayer
    # indéfiniment une annulation déjà faite.
    sale.refresh_from_db()
    return {
        'server_ids': {'sale': str(sale.id)},
        'authoritative': SaleDetailSerializer(sale).data,
    }


# ----------------------------------------------------------------------- caisse


@handler('register_session.open', permission='sales.create')
def register_session_open(ctx, payload):
    """
    Ouvre une session de caisse, par le SERVICE du back-office.

    Le refus le plus probable et le plus important : une contrainte d'unicité
    interdit deux sessions ouvertes sur une même caisse. Un terminal qui a
    ouvert une session hors ligne pendant qu'un autre le faisait aussi verra son
    opération refusée, et toutes les ventes qui s'y rattachaient avec elle. Le
    message doit donc dire QUI l'a ouverte et QUAND - c'est `SessionDejaOuverte`
    qui le porte.

    Ce handler créait la session à la main, sans périmètre entrepôt et sans
    aucune ligne `RegisterSessionCurrencyBalance` : le tiroir en devise
    secondaire partait de zéro, et le Z annonçait un écart tous les soirs.

    LE FONDS COMPTÉ PAR LE CAISSIER EST UN SCALAIRE, et il faut le transmettre.
    L'écran d'ouverture du terminal ne demande qu'un montant, en devise
    principale, et l'envoie sous `opening_balance` (singulier). Ne relayer que
    `opening_balances` le jetait en silence : la session ouvrait à zéro, et le
    Z du soir annonçait un excédent égal au fonds, tous les soirs, sur toutes
    les caisses ouvertes depuis un terminal. Les deux champs sont passés au
    service, qui les compose dans le même ordre qu'à la clôture.
    """
    from apps.sales.register_sessions import (
        CaisseIntrouvable, SessionDejaOuverte, open_register_session,
    )
    from apps.sales.serializers import RegisterSessionDetailSerializer

    _require(payload, 'register')

    try:
        session = open_register_session(
            ctx.organization,
            payload['register'],
            ctx.user,
            opening_balance=payload.get('opening_balance'),
            opening_balances=payload.get('opening_balances'),
            session_id=payload.get('id'),
            request=ctx.request,
        )
    except CaisseIntrouvable as e:
        raise OperationRejected(str(e), code='register_not_found')
    except SessionDejaOuverte as e:
        raise OperationRejected(str(e), code='session_already_open')

    return {
        'server_ids': {'session': str(session.id)},
        'authoritative': RegisterSessionDetailSerializer(session).data,
    }


# ---------------------------------------------------------------------- clients


@handler('customer.create', permission='customers.create')
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


@handler('customer.record_payment', permission='customers.edit')
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


@handler('customer.adjust_balance', permission='customers.edit')
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


@handler('stock_movement.create', permission='stock_movements.create')
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

    from apps.inventory.stock_movements import create_stock_movement

    local_id = payload.pop('id', None)
    # L'ORGANISATION VOYAGE DANS LE CONTEXTE, et ce n'est pas décoratif : sans
    # elle, `_validate_product_prices` ne trouve rien à quoi opposer
    # `products.edit` (il n'y a pas de `view` ici) et laisse passer un report de
    # prix qu'un caissier n'a pas le droit de demander.
    serializer = StockMovementCreateSerializer(
        data=payload,
        context={'request': ctx.request, 'organization': ctx.organization},
    )
    serializer.is_valid(raise_exception=True)
    # Le périmètre entrepôt est vérifié DANS le service, comme pour la vue :
    # l'entrepôt n'est jamais facultatif sur un mouvement, et le tolérer nul
    # ici était une règle que la vue n'avait pas.
    movement = create_stock_movement(
        serializer,
        organization=ctx.organization,
        user=ctx.user,
        request=ctx.request,
        **({'id': local_id} if local_id else {}),
    )
    return {
        'server_ids': {'stock_movement': str(movement.id)},
        'authoritative': StockMovementDetailSerializer(movement).data,
    }


@handler('register_session.close', permission='sales.create')
def register_session_close(ctx, payload):
    """
    Clôture d'une session de caisse.

    ELLE PASSE PAR LE JOURNAL, et ce n'est pas un luxe : le Z se tire au
    comptoir, à la fermeture, souvent avant que le réseau ne revienne. Le
    caissier compte son tiroir, imprime, et rentre chez lui ; l'opération part
    plus tard.

    Le comptage arrive PAR DEVISE (`counted_balances`) : un tiroir contient des
    billets de plusieurs devises, et les additionner donnerait un nombre qui ne
    correspond à aucune liasse.
    """
    from apps.sales.models import RegisterSession
    from apps.sales.serializers import RegisterSessionDetailSerializer
    from apps.sales.register_sessions import close_register_session

    _require(payload, 'session')
    session = RegisterSession.objects.filter(
        id=payload['session'], organization=ctx.organization,
    ).first()
    if session is None:
        raise OperationRejected("Cette session n'existe pas.", code='session_not_found')

    _refus_si_impossible_caisse(
        close_register_session, session, ctx.user,
        {
            'notes': payload.get('notes', ''),
            'counted_balance': payload.get('counted_balance'),
            'counted_balances': payload.get('counted_balances') or [],
        },
    )
    session.refresh_from_db()
    return {
        'server_ids': {'register_session': str(session.id)},
        'authoritative': RegisterSessionDetailSerializer(session).data,
    }


def _refus_si_impossible_caisse(fonction, *args, **kwargs):
    """Traduit un refus de clôture en refus d'opération, donc en quarantaine."""
    from apps.sales.register_sessions import TransitionRefusee
    try:
        return fonction(*args, **kwargs)
    except TransitionRefusee as exc:
        raise OperationRejected(str(exc), code='transition_refused')


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


@handler('stock.unpack', permission='stock_movements.create')
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


@handler('stock_transfer.create', permission='stock_transfers.create')
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

    transfert = _sauver_avec_audit(ctx, serializer, local_id)
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


@handler('stock_transfer.approve', permission='stock_transfers.ship')
def stock_transfer_approve(ctx, payload):
    from apps.inventory.services import approve_transfer
    return _transition_transfert(ctx, payload, approve_transfer)


@handler('stock_transfer.ship', permission='stock_transfers.ship')
def stock_transfer_ship(ctx, payload):
    from apps.inventory.services import ship_transfer
    return _transition_transfert(ctx, payload, ship_transfer)


@handler('stock_transfer.receive', permission='stock_transfers.receive')
def stock_transfer_receive(ctx, payload):
    """
    Réception. `items` porte ce que le magasinier a RÉELLEMENT compté, en
    contenants ou en total : une réception partielle est le cas courant.
    """
    from apps.inventory.services import receive_transfer
    return _transition_transfert(
        ctx, payload, receive_transfer, received_items=payload.get('items') or [],
    )


@handler('stock_transfer.cancel', permission='stock_transfers.cancel')
def stock_transfer_cancel(ctx, payload):
    from apps.inventory.services import cancel_transfer
    return _transition_transfert(ctx, payload, cancel_transfer)


@handler('stock_adjustment.create', permission='stock_adjustments.create')
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

    ajustement = _sauver_avec_audit(ctx, serializer, local_id)
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


@handler('stock_adjustment.approve', permission='stock_adjustments.approve')
def stock_adjustment_approve(ctx, payload):
    from apps.inventory.services import approve_adjustment
    return _transition_ajustement(ctx, payload, approve_adjustment)


@handler('stock_adjustment.reject', permission='stock_adjustments.approve')
def stock_adjustment_reject(ctx, payload):
    from apps.inventory.services import reject_adjustment
    return _transition_ajustement(ctx, payload, reject_adjustment)


# ------------------------------------------------------------ retours et devis
#
# Ni le retour ni le devis n'avait d'écran, nulle part - ni sur le web, ni sur
# le mobile. Le terminal crée donc la référence, et ces actes sont le seul
# chemin d'écriture depuis un comptoir.


def _refus_vente(fonction, *args, **kwargs):
    from apps.sales.returns_quotations import TransitionRefusee
    try:
        return fonction(*args, **kwargs)
    except TransitionRefusee as exc:
        raise OperationRejected(str(exc), code='transition_refused')


@handler('sale_return.create', permission='sale_returns.create')
def sale_return_create(ctx, payload):
    from apps.sales.serializers import (
        SaleReturnCreateSerializer, SaleReturnDetailSerializer,
    )

    # ┌──────────────────────────────────────────────────────────────────────┐
    # │ AUCUN CONTRÔLE DE PÉRIMÈTRE ICI, ET C'EST VOULU.                     │
    # │                                                                      │
    # │ Un retour n'a pas d'entrepôt à lui : il hérite de celui de sa vente. │
    # │ `SaleReturnCreateSerializer` ne porte donc pas `warehouse`, et le    │
    # │ périmètre est vérifié au bon endroit, dans son `validate`, sur la    │
    # │ VENTE D'ORIGINE. `SaleReturnViewSet` n'appelle jamais l'assertion.   │
    # │                                                                      │
    # │ Ce handler l'appelait quand même, sur un champ toujours nul : tout   │
    # │ membre non propriétaire recevait « Un entrepôt est requis pour votre │
    # │ compte ». Aucun caissier ni gérant ne pouvait créer un retour depuis │
    # │ son terminal, et personne ne l'a vu parce que toutes les             │
    # │ vérifications avaient été faites en propriétaire.                    │
    # └──────────────────────────────────────────────────────────────────────┘
    local_id = payload.pop('id', None)
    serializer = SaleReturnCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    retour = _sauver_avec_audit(ctx, serializer, local_id)
    return {
        'server_ids': {'sale_return': str(retour.id)},
        'authoritative': SaleReturnDetailSerializer(retour).data,
    }


def _transition_retour(ctx, payload, fonction):
    from apps.sales.models import SaleReturn
    from apps.sales.serializers import SaleReturnDetailSerializer

    _require(payload, 'sale_return')
    retour = _objet_de_lorg(SaleReturn, ctx, payload['sale_return'], 'Ce retour')
    _refus_vente(fonction, retour, ctx.user)
    retour.refresh_from_db()
    return {
        'server_ids': {'sale_return': str(retour.id)},
        'authoritative': SaleReturnDetailSerializer(retour).data,
    }


@handler('sale_return.approve', permission='sale_returns.approve')
def sale_return_approve(ctx, payload):
    from apps.sales.returns_quotations import approve_return
    return _transition_retour(ctx, payload, approve_return)


@handler('sale_return.reject', permission='sale_returns.approve')
def sale_return_reject(ctx, payload):
    from apps.sales.returns_quotations import reject_return
    return _transition_retour(ctx, payload, reject_return)


@handler('quotation.create', permission='sales.create')
def quotation_create(ctx, payload):
    from apps.sales.serializers import (
        QuotationCreateSerializer, QuotationDetailSerializer,
    )

    local_id = payload.pop('id', None)
    serializer = QuotationCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    devis = _sauver_avec_audit(ctx, serializer, local_id)
    return {
        'server_ids': {'quotation': str(devis.id)},
        'authoritative': QuotationDetailSerializer(devis).data,
    }


@handler('quotation.convert', permission='sales.create')
def quotation_convert(ctx, payload):
    """
    Conversion d'un devis en vente.

    **La DETTE est inscrite par le service**, comme sur le chemin web : un devis
    converti est une facture émise et non payée. L'oublier rendait le client
    artificiellement créditeur au règlement suivant - c'est le défaut que la
    session 2026-08-24 a corrigé, et il ne doit pas revenir par cette porte.
    """
    from apps.core.warehouse_scope import accessible_warehouse_ids
    from apps.sales.models import Quotation
    from apps.sales.serializers import QuotationDetailSerializer
    from apps.sales.returns_quotations import (
        convert_quotation, resolve_conversion_warehouse,
    )

    _require(payload, 'quotation')
    devis = _objet_de_lorg(Quotation, ctx, payload['quotation'], 'Ce devis')

    explicite = payload.get('warehouse')
    if explicite:
        assert_warehouse_allowed_for_request(ctx.request, explicite)

    membership = _membership_du_contexte(ctx)
    autorises = accessible_warehouse_ids(membership) if membership else None
    warehouse = resolve_conversion_warehouse(devis, explicite, autorises)

    vente = _refus_vente(
        convert_quotation, devis, ctx.user, warehouse,
        perimetre_borne=autorises is not None,
    )
    devis.refresh_from_db()
    return {
        'server_ids': {'quotation': str(devis.id), 'sale': str(vente.id)},
        'authoritative': QuotationDetailSerializer(devis).data,
    }


def _membership_du_contexte(ctx):
    from apps.core.warehouse_scope import get_membership_for_request
    return get_membership_for_request(ctx.request)


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


@handler('inventory_session.create', permission='inventory.create')
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

    session = _sauver_avec_audit(ctx, serializer, local_id)
    return {
        'server_ids': {'inventory_session': str(session.id)},
        'authoritative': InventorySessionDetailSerializer(session).data,
    }


@handler('inventory_session.start', permission='inventory.start')
def inventory_session_start(ctx, payload):
    from apps.inventory.services import start_inventory_session
    return _transition_inventaire(ctx, payload, start_inventory_session)


@handler('inventory_session.count', permission='inventory.count')
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


@handler('inventory_session.submit', permission='inventory.submit')
def inventory_session_submit(ctx, payload):
    from apps.inventory.services import submit_inventory_session
    return _transition_inventaire(ctx, payload, submit_inventory_session)


@handler('inventory_session.validate', permission='inventory.validate')
def inventory_session_validate(ctx, payload):
    from apps.inventory.services import validate_inventory_session
    return _transition_inventaire(ctx, payload, validate_inventory_session)


@handler('inventory_session.cancel', permission='inventory.cancel')
def inventory_session_cancel(ctx, payload):
    """
    Annule la session et DÉVERROUILLE son stock.

    ⚠ Elle passait `serialiser=False`, si bien qu'`authoritative` recevait le
    retour brut du service - et `cancel_inventory_session` rend l'objet
    `InventorySession`, pas un dictionnaire. Le rendu de la réponse levait
    alors « Object of type InventorySession is not JSON serializable », que
    `_classify` range en `unexpected`, donc en `retry` : l'opération repartait
    à chaque synchronisation, POUR TOUJOURS, et une session lancée depuis un
    terminal ne pouvait plus y être annulée - alors qu'elle VERROUILLE le stock
    de ses produits, donc bloque la vente.

    `serialiser=False` n'est juste que pour `count`, dont le service rend bien
    un dictionnaire.
    """
    from apps.inventory.services import cancel_inventory_session
    return _transition_inventaire(ctx, payload, cancel_inventory_session)


# --------------------------------------------------------------- catalogue


@handler('product.create', permission='products.create')
def product_create(ctx, payload):
    """
    Création d'un article, par le serializer du back-office.

    Le SKU et le code-barres restent facultatifs et uniques par organisation :
    deux terminaux hors ligne en fabriqueraient fatalement le même, et le
    serveur refuserait le second - refus déterministe, donc quarantaine, avec
    le message qui va bien.
    """
    from apps.products.serializers import ProductCreateSerializer, ProductDetailSerializer
    from apps.products.services import create_product

    local_id = payload.pop('id', None)
    serializer = ProductCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    produit = create_product(
        serializer,
        organization=ctx.organization,
        user=ctx.user,
        local_id=local_id,
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


def _referentiel_update(ctx, payload, modele, serializer_classe, cle, quoi, lecture=None):
    """
    Modification d'un référentiel, par le serializer du back-office.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `id` DÉSIGNE LA CIBLE, NON UNE CLÉ À ATTRIBUER.                         │
    │                                                                          │
    │ C'est toute la différence avec `_referentiel_create`, qui passe `id` à   │
    │ `save()` pour que la clé posée au comptoir devienne celle du serveur.    │
    │ Ici la fiche existe déjà : `id` sert à la RETROUVER.                     │
    └──────────────────────────────────────────────────────────────────────────┘

    `partial=True` n'est pas un confort : sans lui, `CategoryDetailSerializer`
    exigerait `name` ET `slug` à chaque envoi, et basculer `is_active` seul
    serait refusé pour deux champs qu'on ne modifiait pas.

    `_objet_de_lorg` rend un refus DÉTERMINISTE (`not_found`) pour une fiche
    absente, d'une autre organisation, ou supprimée en douceur - les managers de
    `Category` et `Brand` étant des `TenantSoftDeleteManager`, qui filtrent
    `is_deleted=False`. Ce chemin est emprunté pour de vrai : une unité
    supprimée dans le back-office SURVIT sur chaque terminal (`Unit` n'a pas de
    suppression douce, donc aucune pierre tombale ne descend), et le terminal
    enverra des `unit.update` sur des unités fantômes.

    `lecture` sépare le serializer d'ÉCRITURE de celui de LECTURE, quand le
    back-office en emploie deux (les catégories de caisse écrivent par
    `…CreateSerializer` et rendent par `…DetailSerializer`). Absent, on rend
    avec celui qui a écrit - c'est le cas des référentiels de produits.
    """
    identifiant = payload.pop('id', None)
    if not identifiant:
        raise OperationRejected(
            "L'identifiant de la fiche à modifier est absent.",
            code='missing_fields',
            details={'id': ['Ce champ est requis.']},
        )
    objet = _objet_de_lorg(modele, ctx, identifiant, quoi)
    serializer = serializer_classe(
        objet, data=payload, partial=True, context={'request': ctx.request},
    )
    serializer.is_valid(raise_exception=True)
    objet = serializer.save()
    return {
        'server_ids': {cle: str(objet.id)},
        'authoritative': (lecture or serializer_classe)(objet).data,
    }


@handler('category.create', permission='categories.create')
def category_create(ctx, payload):
    from apps.products.models import Category
    from apps.products.serializers import CategoryCreateSerializer
    return _referentiel_create(
        ctx, payload, Category, CategoryCreateSerializer, 'category',
    )


@handler('brand.create', permission='products.create')
def brand_create(ctx, payload):
    from apps.products.models import Brand
    from apps.products.serializers import BrandSerializer
    return _referentiel_create(ctx, payload, Brand, BrandSerializer, 'brand')


@handler('unit.create', permission='products.create')
def unit_create(ctx, payload):
    from apps.products.models import Unit
    from apps.products.serializers import UnitSerializer
    return _referentiel_create(ctx, payload, Unit, UnitSerializer, 'unit')


@handler('category.update', permission='categories.edit')
def category_update(ctx, payload):
    """
    ⚠ `CategoryDetailSerializer`, JAMAIS `CategoryCreateSerializer`.

    Le second vérifie l'unicité du nom SANS exclure la fiche modifiée
    (`serializers.py`, aucun `exclude(pk=self.instance.pk)`) : une catégorie s'y
    refuserait ELLE-MÊME à chaque enregistrement qui porte son propre nom,
    c'est-à-dire toujours. Refus déterministe et inconditionnel, découvert hors
    ligne. C'est aussi le serializer que `CategoryViewSet.get_serializer_class`
    retient pour `partial_update`.
    """
    from apps.products.models import Category
    from apps.products.serializers import CategoryDetailSerializer
    return _referentiel_update(
        ctx, payload, Category, CategoryDetailSerializer, 'category',
        'Cette catégorie',
    )


@handler('brand.update', permission='products.edit')
def brand_update(ctx, payload):
    from apps.products.models import Brand
    from apps.products.serializers import BrandSerializer
    return _referentiel_update(
        ctx, payload, Brand, BrandSerializer, 'brand', 'Cette marque',
    )


@handler('unit.update', permission='products.edit')
def unit_update(ctx, payload):
    from apps.products.models import Unit
    from apps.products.serializers import UnitSerializer
    return _referentiel_update(
        ctx, payload, Unit, UnitSerializer, 'unit', 'Cette unité',
    )


# ------------------------------------------------------------------- livre de caisse


@handler('expense.create', permission='cashbook.create_expense')
def expense_create(ctx, payload):
    """
    Une dépense, par le SERVICE du back-office.

    Ce handler appelait `serializer.save()` en direct et ne rejouait donc rien
    de `ExpenseViewSet.perform_create` : ni l'organisation (une FK non nulle,
    d'où un `IntegrityError` et la quarantaine), ni la référence, ni la devise
    résolue avec son taux, ni l'auteur. Mesuré : aucune dépense saisie sur un
    terminal n'est jamais arrivée.
    """
    from apps.cashbook.serializers import ExpenseCreateSerializer, ExpenseDetailSerializer
    from apps.cashbook.services import create_expense

    local_id = payload.pop('id', None)
    # Le numéro imprimé au comptoir, retiré du corps comme l'identifiant :
    # `reference` est `read_only` sur le serializer, elle ne passe que par le
    # service. Absente, le serveur alloue la sienne - c'est le chemin d'une
    # dépense saisie au back-office.
    reference = payload.pop('reference', None)
    serializer = ExpenseCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    expense = create_expense(
        serializer,
        organization=ctx.organization,
        user=ctx.user,
        request=ctx.request,
        reference=reference,
        **({'id': local_id} if local_id else {}),
    )
    return {
        'server_ids': {'expense': str(expense.id)},
        'authoritative': ExpenseDetailSerializer(expense).data,
    }


@handler('cash_movement.create', permission='cashbook.create_movement')
def cash_movement_create(ctx, payload):
    """
    Un mouvement de tiroir, par le SERVICE du back-office.

    Même défaut que la dépense, plus deux propres au tiroir : le solde
    `balance_after` est suivi PAR DEVISE, et la session ouverte rattache le
    mouvement à une caisse donc à un entrepôt. Sans elle, un apport de fonds
    reste invisible aux magasiniers.
    """
    from apps.cashbook.serializers import CashMovementCreateSerializer, CashMovementDetailSerializer
    from apps.cashbook.services import create_manual_cash_movement

    local_id = payload.pop('id', None)
    serializer = CashMovementCreateSerializer(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    movement = create_manual_cash_movement(
        serializer,
        organization=ctx.organization,
        user=ctx.user,
        **({'id': local_id} if local_id else {}),
    )
    return {
        'server_ids': {'cash_movement': str(movement.id)},
        'authoritative': CashMovementDetailSerializer(movement).data,
    }


# ----------------------------------------------- transitions du livre de caisse
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ LES SIX GESTES PASSENT PAR LES SERVICES DU BACK-OFFICE.                  │
# │                                                                          │
# │ Ils vivaient dans les vues, donc le journal n'y avait pas accès : un      │
# │ terminal savait CRÉER une dépense et un mouvement, jamais les faire      │
# │ avancer ni les annuler. Un refus y est déterministe - une dépense déjà   │
# │ payée le restera - donc verdict `rejected`, jamais `retry` : réessayer   │
# │ une approbation créerait un SECOND mouvement de caisse et le tiroir      │
# │ sortirait deux fois la même dépense.                                     │
# └──────────────────────────────────────────────────────────────────────────┘


def _refus_si_impossible_cashbook(fonction, *args, **kwargs):
    """Traduit un refus de transition de caisse en refus d'opération."""
    from apps.cashbook.services import TransitionRefusee
    try:
        return fonction(*args, **kwargs)
    except TransitionRefusee as exc:
        raise OperationRejected(str(exc), code='transition_refused')


def _transition_depense(ctx, payload, fonction, *args, **kwargs):
    from apps.cashbook.models import Expense
    from apps.cashbook.serializers import ExpenseDetailSerializer

    _require(payload, 'expense')
    depense = _objet_de_lorg(Expense, ctx, payload['expense'], 'Cette dépense')
    _refus_si_impossible_cashbook(fonction, depense, *args, **kwargs)
    depense.refresh_from_db()
    return {
        'server_ids': {'expense': str(depense.id)},
        'authoritative': ExpenseDetailSerializer(depense).data,
    }


@handler('expense.submit', permission='cashbook.create_expense')
def expense_submit(ctx, payload):
    """Soumettre sa propre dépense : c'est l'auteur qui le fait, d'où `.create_expense`."""
    from apps.cashbook.services import submit_expense
    return _transition_depense(ctx, payload, submit_expense, ctx.user)


@handler('expense.approve', permission='cashbook.approve_expense')
def expense_approve(ctx, payload):
    from apps.cashbook.services import approve_expense
    return _transition_depense(ctx, payload, approve_expense, ctx.user)


@handler('expense.reject', permission='cashbook.approve_expense')
def expense_reject(ctx, payload):
    from apps.cashbook.services import reject_expense
    return _transition_depense(
        ctx, payload, reject_expense, payload.get('reason', ''), ctx.user
    )


@handler('expense.pay', permission='cashbook.approve_expense')
def expense_pay(ctx, payload):
    from apps.cashbook.services import pay_expense
    return _transition_depense(
        ctx, payload, pay_expense, ctx.user,
        payment_method_id=payload.get('payment_method'),
        payment_reference=payload.get('payment_reference', ''),
    )


@handler('expense.cancel', permission='cashbook.approve_expense')
def expense_cancel(ctx, payload):
    from apps.cashbook.services import cancel_expense
    return _transition_depense(
        ctx, payload, cancel_expense, ctx.user, payload.get('reason', '')
    )


@handler('cash_movement.cancel', permission='cashbook.cancel_movement')
def cash_movement_cancel(ctx, payload):
    """
    Annule un mouvement de tiroir. On MARQUE, on ne supprime pas : une écriture
    comptable se contrepasse et ne se rature pas.
    """
    from apps.cashbook.models import CashMovement
    from apps.cashbook.serializers import CashMovementDetailSerializer
    from apps.cashbook.services import cancel_cash_movement

    _require(payload, 'movement')
    mouvement = _objet_de_lorg(
        CashMovement, ctx, payload['movement'], 'Ce mouvement de caisse',
    )
    _refus_si_impossible_cashbook(
        cancel_cash_movement, mouvement, ctx.user, payload.get('reason', ''),
    )
    mouvement.refresh_from_db()
    return {
        'server_ids': {'cash_movement': str(mouvement.id)},
        'authoritative': CashMovementDetailSerializer(mouvement).data,
    }


def _categorie_de_caisse(ctx, payload, ecriture, lecture):
    """
    Une catégorie de recette ou de dépense, par les serializers du back-office.

    ⚠ DEUX serializers, et il en faut deux : le back-office écrit par
    `…CreateSerializer` et rend par `…DetailSerializer`. Un seul nom pour les
    deux rôles est exactement l'erreur qui a fait boucler cet acte (voir
    `test_handler_imports.py`).

    ⚠ Aucun `slug` n'est fabriqué ici, contrairement aux catégories de
    produits : `IncomeCategory` et `ExpenseCategory` n'en portent pas. Le
    `code` est facultatif côté serveur, et le laisser vide est ce que fait le
    back-office quand le marchand ne le renseigne pas.
    """
    local_id = payload.pop('id', None)
    serializer = ecriture(data=payload, context={'request': ctx.request})
    serializer.is_valid(raise_exception=True)
    categorie = _sauver_avec_audit(ctx, serializer, local_id)
    return categorie, lecture(categorie).data


@handler('income_category.create', permission='cashbook.manage_categories')
def income_category_create(ctx, payload):
    from apps.cashbook.serializers import (
        IncomeCategoryCreateSerializer, IncomeCategoryDetailSerializer,
    )
    categorie, rendu = _categorie_de_caisse(
        ctx, payload, IncomeCategoryCreateSerializer, IncomeCategoryDetailSerializer,
    )
    return {
        'server_ids': {'income_category': str(categorie.id)},
        'authoritative': rendu,
    }


@handler('expense_category.create', permission='cashbook.manage_categories')
def expense_category_create(ctx, payload):
    from apps.cashbook.serializers import (
        ExpenseCategoryCreateSerializer, ExpenseCategoryDetailSerializer,
    )
    categorie, rendu = _categorie_de_caisse(
        ctx, payload, ExpenseCategoryCreateSerializer, ExpenseCategoryDetailSerializer,
    )
    return {
        'server_ids': {'expense_category': str(categorie.id)},
        'authoritative': rendu,
    }


def _categorie_de_caisse_update(ctx, payload, modele, ecriture, lecture, cle, quoi):
    """
    Renommer une catégorie de caisse, changer sa couleur, ou la désactiver.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE SERIALIZER EST LE `Create`, ET C'EST L'INVERSE DES PRODUITS.         │
    │                                                                          │
    │ `category.update` impose `CategoryDetailSerializer` parce que le         │
    │ `CategoryCreateSerializer` des PRODUITS vérifie l'unicité du nom SANS    │
    │ exclure la fiche modifiée : une catégorie s'y refuserait elle-même à     │
    │ chaque enregistrement portant son propre nom.                            │
    │                                                                          │
    │ Ici c'est le contraire, et recopier ce motif « par symétrie » serait le  │
    │ défaut. `IncomeCategoryCreateSerializer.validate` porte déjà son         │
    │ `exclude(pk=self.instance.pk)`, et c'est LUI que                          │
    │ `IncomeCategoryViewSet.get_serializer_class` retient pour                │
    │ `partial_update`. Passer au `…DetailSerializer`, qui n'a AUCUN           │
    │ `validate()`, sauterait le contrôle d'unicité : un nom déjà pris lèverait│
    │ un `IntegrityError` non rattrapé, donc un verdict `rejected` dont le     │
    │ détail est le texte brut de PostgreSQL. Deux tests l'épinglent, dans les │
    │ deux sens.                                                               │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ PAS DE SUPPRESSION, et ce n'est pas un renoncement. `ExpenseCategory` est
    `PROTECT`-référencée par `Expense` (supprimer une catégorie utilisée lève
    `ProtectedError`), `IncomeCategory` est `SET_NULL` (la suppression réussit
    et orpheline l'historique en silence), et surtout AUCUNE des deux tables
    n'émet de pierre tombale au tirage : une suppression côté serveur
    n'atteindrait jamais un terminal, où la catégorie resterait listée et
    proposée à la saisie, pour toujours. `is_active` est le levier juste - il
    retire la rubrique des formulaires sans toucher à l'historique.
    """
    return _referentiel_update(
        ctx, payload, modele, ecriture, cle, quoi, lecture=lecture,
    )


@handler('income_category.update', permission='cashbook.manage_categories')
def income_category_update(ctx, payload):
    from apps.cashbook.models import IncomeCategory
    from apps.cashbook.serializers import (
        IncomeCategoryCreateSerializer, IncomeCategoryDetailSerializer,
    )
    return _categorie_de_caisse_update(
        ctx, payload, IncomeCategory,
        IncomeCategoryCreateSerializer, IncomeCategoryDetailSerializer,
        'income_category', "Ce type d'entrée",
    )


@handler('expense_category.update', permission='cashbook.manage_categories')
def expense_category_update(ctx, payload):
    from apps.cashbook.models import ExpenseCategory
    from apps.cashbook.serializers import (
        ExpenseCategoryCreateSerializer, ExpenseCategoryDetailSerializer,
    )
    return _categorie_de_caisse_update(
        ctx, payload, ExpenseCategory,
        ExpenseCategoryCreateSerializer, ExpenseCategoryDetailSerializer,
        'expense_category', "Cette catégorie de dépense",
    )
