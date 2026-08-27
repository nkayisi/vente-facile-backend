"""
Constructeurs de rapports de la gestion de stock.

Chaque fonction traduit un queryset déjà filtré et déjà scopé en une
``ReportSpec`` : une description de document que ``apps.core.exports`` sait
rendre indifféremment en PDF ou en classeur Excel. Aucune de ces fonctions ne
décide du périmètre des données ; c'est la vue qui filtre, elles ne font que
mettre en forme.
"""
from decimal import Decimal


from apps.core.exports import (
    KIND_DATETIME,
    KIND_MEASURE,
    KIND_MONEY,
    KIND_NUMBER,
    KIND_QUANTITY,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
    format_number,
    format_quantity,
    currency_decimals,
)

ZERO = Decimal('0')


def _display_quantity(stock):
    """Quantité lisible d'une ligne de stock (« 12 cartons + 3 bouteilles »)."""
    from apps.inventory.packaging import PackagingService

    return PackagingService.format_stock(stock)


def _display_available(stock):
    """Disponible d'une ligne de stock, dans les mêmes termes que la quantité."""
    from apps.inventory.packaging import PackagingService

    return PackagingService.format_available(stock)


def _stock_status(stock):
    """État de réassort d'une ligne, dans les mots employés à l'écran."""
    if stock.quantity <= 0:
        return 'Rupture'
    reorder = stock.product.reorder_point or 0
    if reorder and stock.quantity <= reorder:
        return 'Stock bas'
    if stock.reserved_quantity and stock.reserved_quantity > 0:
        return 'Réservé'
    return 'En stock'


# --------------------------------------------------------------------------
# Rapport « Niveau de stock »
# --------------------------------------------------------------------------

STOCK_LEVEL_COLUMNS = [
    ReportColumn('product_name', 'Produit', 40, KIND_TEXT),
    ReportColumn('sku', 'SKU', 24, KIND_TEXT),
    ReportColumn('category', 'Catégorie', 24, KIND_TEXT),
    ReportColumn('warehouse', 'Entrepôt', 22, KIND_TEXT),
    ReportColumn('quantity_display', 'Quantité', 30, KIND_MEASURE),
    ReportColumn('available_display', 'Disponible', 30, KIND_MEASURE),
    ReportColumn('quantity', 'Total (unités)', 20, KIND_QUANTITY),
    ReportColumn('reserved', 'Réservé', 16, KIND_QUANTITY),
    ReportColumn('unit_cost', 'Coût unit.', 20, KIND_MONEY),
    ReportColumn('stock_value', 'Valeur achat', 22, KIND_MONEY),
    ReportColumn('selling_price', 'Prix vente', 20, KIND_MONEY),
    ReportColumn('sale_value', 'Valeur vente', 22, KIND_MONEY),
    ReportColumn('status', 'Statut', 18, KIND_TEXT),
]


def build_stock_levels_report(queryset, organization, *, currency='CDF',
                              filters_applied=(), group_by_category=True):
    """
    Rapport de situation des produits, valorisé au coût et au prix de vente.

    La valorisation s'appuie sur ``Stock.effective_cost`` (coût moyen pondéré de
    l'entrepôt, replié sur le prix d'achat catalogue si le stock n'a jamais été
    approvisionné). C'est le même coût que celui affiché dans la rubrique
    « Niveau de stock », donc le fichier téléchargé et l'écran s'accordent.

    ``group_by_category`` trie par catégorie et produit un sous-total par
    catégorie, ce que demande une situation de stock lue par rayon.
    """
    queryset = queryset.select_related(
        'product', 'product__category', 'product__unit',
        'product__packaging_unit', 'warehouse',
    )
    queryset = queryset.order_by(
        'product__category__name', 'product__name', 'warehouse__name',
    ) if group_by_category else queryset.order_by('product__name', 'warehouse__name')

    totals = {
        'lines': 0,
        'quantity': ZERO,
        'stock_value': ZERO,
        'sale_value': ZERO,
        'out': 0,
        'low': 0,
    }

    def rows():
        # `iterator` : un inventaire complet peut compter plusieurs milliers de
        # lignes, les charger toutes ferait grimper la mémoire du worker.
        for stock in queryset.iterator(chunk_size=500):
            product = stock.product
            unit_cost = stock.effective_cost
            selling_price = product.selling_price or ZERO
            stock_value = stock.quantity * unit_cost
            sale_value = stock.quantity * selling_price
            status = _stock_status(stock)

            totals['lines'] += 1
            totals['quantity'] += stock.quantity
            totals['stock_value'] += stock_value
            totals['sale_value'] += sale_value
            if status == 'Rupture':
                totals['out'] += 1
            elif status == 'Stock bas':
                totals['low'] += 1

            yield {
                'product_name': product.name,
                'sku': product.sku or '',
                'category': product.category.name if product.category else 'Sans catégorie',
                'warehouse': stock.warehouse.name,
                'quantity_display': _display_quantity(stock),
                'available_display': _display_available(stock),
                'quantity': stock.quantity,
                'reserved': stock.reserved_quantity,
                'unit_cost': unit_cost,
                'stock_value': stock_value,
                'selling_price': selling_price,
                'sale_value': sale_value,
                'status': status,
            }

    # La synthèse est calculée pendant la traversée : elle n'est donc lisible
    # qu'une fois les lignes consommées. Les deux moteurs de rendu écrivant le
    # cartouche AVANT le tableau, on matérialise ici pour disposer des totaux.
    materialized = list(rows())
    decimals = currency_decimals(currency)

    summary = (
        ('Lignes de stock', str(totals['lines'])),
        ('Quantité totale (unités)', format_quantity(totals['quantity'])),
        ('Valeur au coût', format_number(totals['stock_value'], decimals)),
        ('Valeur de vente', format_number(totals['sale_value'], decimals)),
        ('En rupture', str(totals['out'])),
        ('Stock bas', str(totals['low'])),
    )

    return ReportSpec(
        title='Niveau de stock',
        organization=organization,
        columns=STOCK_LEVEL_COLUMNS,
        rows=materialized,
        subtitle='Situation des produits, valorisée au coût moyen pondéré',
        filters_applied=filters_applied,
        summary=summary,
        group_by='category' if group_by_category else None,
        group_totals=('quantity', 'stock_value', 'sale_value'),
        currency=currency,
        signatures=('Établi par', 'Vérifié par'),
        empty_message='Aucun stock ne correspond aux critères retenus.',
    )


# --------------------------------------------------------------------------
# Rapport d'approvisionnement
# --------------------------------------------------------------------------

SUPPLY_DETAIL_COLUMNS = [
    ReportColumn('date', 'Date', 25, KIND_DATETIME),
    ReportColumn('reference', 'Référence', 24, KIND_TEXT),
    ReportColumn('product_name', 'Produit', 38, KIND_TEXT),
    ReportColumn('sku', 'SKU', 24, KIND_TEXT),
    ReportColumn('category', 'Catégorie', 22, KIND_TEXT),
    ReportColumn('warehouse', 'Entrepôt', 22, KIND_TEXT),
    ReportColumn('supplier', 'Fournisseur', 28, KIND_TEXT),
    ReportColumn('movement_label', 'Type', 20, KIND_TEXT),
    ReportColumn('quantity_display', 'Quantité', 28, KIND_MEASURE),
    ReportColumn('unit_cost', 'Coût unit.', 20, KIND_MONEY),
    ReportColumn('purchase_value', "Valeur d'achat", 24, KIND_MONEY),
]

SUPPLY_PRODUCT_COLUMNS = [
    ReportColumn('product_name', 'Produit', 46, KIND_TEXT),
    ReportColumn('sku', 'SKU', 26, KIND_TEXT),
    ReportColumn('category', 'Catégorie', 28, KIND_TEXT),
    ReportColumn('entries', 'Entrées', 16, KIND_NUMBER),
    ReportColumn('quantity', 'Quantité reçue (unités)', 28, KIND_QUANTITY),
    ReportColumn('avg_unit_cost', 'Coût unit. moyen', 26, KIND_MONEY),
    ReportColumn('purchase_value', "Valeur d'achat", 28, KIND_MONEY),
]


def _movement_quantity_display(movement):
    """
    Quantité lisible d'un mouvement, conditionnement compris.

    Passe par ``format_movement_quantity``, donc par le facteur FIGÉ sur le
    mouvement et par ses champs de saisie. L'export lisait auparavant le
    conditionnement d'aujourd'hui : un produit repassé de 24 à 12 unités par
    casier faisait diverger le fichier téléchargé de l'écran qui l'avait
    déclenché, et l'historique se réécrivait à chaque changement de facteur.
    """
    from apps.inventory.packaging import PackagingService

    return PackagingService.format_movement_quantity(movement)


def _movement_levels(movement):
    """
    Niveaux de stock avant et après, en un seul jeton compact : « 42 → 39 ».

    L'unité n'est PAS répétée ici : la colonne « Quantité » de la même ligne la
    nomme déjà. La porter deux fois faisait déborder la cellule (« 118 → 117
    PLAQUETTES » repassait à la ligne quand « 42 → 39 PLAQUETTES » tenait), et
    des hauteurs de ligne inégales suffisent à rendre un journal illisible.

    Seul le TOTAL est enregistré à ces deux bornes : inventer un partage
    scellé/vrac y serait faux (voir la note du serializer des mouvements).
    """
    return (
        f"{format_quantity(movement.quantity_before)} → "
        f"{format_quantity(movement.quantity_after)}"
    )


def _resolve_suppliers(movement_ids_by_receipt):
    """
    Résout les fournisseurs en UNE requête, jamais un ``get()`` par ligne.

    ``StockMovement.reference_id`` est un UUID nu et non une clé étrangère : sans
    ce regroupement, un rapport de 500 réceptions déclencherait 500 requêtes.
    """
    if not movement_ids_by_receipt:
        return {}

    from apps.purchases.models import GoodsReceipt

    rows = (
        GoodsReceipt.objects
        .filter(id__in=movement_ids_by_receipt)
        .select_related('purchase_order', 'purchase_order__supplier')
        .values_list(
            'id', 'reference',
            'purchase_order__supplier__name',
            'purchase_order__currency',
        )
    )
    return {
        receipt_id: {
            'reference': reference,
            'supplier': supplier_name or '',
            'currency': currency,
        }
        for receipt_id, reference, supplier_name, currency in rows
    }


def build_supplies_report(queryset, organization, *, currency='CDF',
                          filters_applied=(), group_by='product',
                          period_label=''):
    """
    Rapport d'approvisionnement valorisé, porté par les entrées de stock.

    ``group_by='product'`` (défaut) donne une ligne par produit avec la valeur
    d'achat cumulée, ce que demande la lecture courante. ``group_by='movement'``
    déroule le détail chronologique, réception par réception.

    Une entrée sans coût unitaire (un ajustement positif, typiquement) est
    reportée avec un tiret et EXCLUE des totaux : la compter à zéro
    sous-estimerait la valeur d'achat sans que rien ne le signale.
    """
    queryset = queryset.select_related(
        'product', 'product__category', 'product__unit',
        'product__packaging_unit', 'warehouse',
    )

    movements = list(queryset.order_by('created_at').iterator(chunk_size=500))

    receipt_ids = {
        movement.reference_id for movement in movements
        if movement.reference_type == 'goods_receipt' and movement.reference_id
    }
    receipts = _resolve_suppliers(receipt_ids)

    labels = dict(queryset.model.MovementType.choices)
    decimals = currency_decimals(currency)

    total_value = ZERO
    # Ventilation par devise. `GoodsReceiptItem.unit_cost` est enregistré SANS
    # conversion, alors que `PurchaseOrder` porte sa propre devise : un achat en
    # dollars laisse donc un coût en dollars à côté de coûts en francs. Les
    # additionner produirait un total qui ne veut rien dire, ce que le projet
    # proscrit depuis la refonte des créances.
    value_by_currency: dict = {}
    valued_lines = 0
    unvalued_lines = 0
    total_quantity = ZERO
    products_seen = set()

    detail_rows = []
    per_product: dict = {}

    for movement in movements:
        receipt = receipts.get(movement.reference_id) or {}
        unit_cost = movement.unit_cost if movement.unit_cost and movement.unit_cost > 0 else None
        purchase_value = (movement.quantity * unit_cost) if unit_cost is not None else None

        line_currency = (receipt.get('currency') or currency).upper()
        if purchase_value is not None:
            total_value += purchase_value
            value_by_currency[line_currency] = (
                value_by_currency.get(line_currency, ZERO) + purchase_value
            )
            valued_lines += 1
        else:
            unvalued_lines += 1
        total_quantity += movement.quantity
        products_seen.add(movement.product_id)

        product = movement.product
        category = product.category.name if product.category else 'Sans catégorie'

        detail_rows.append({
            'date': movement.created_at,
            'reference': receipt.get('reference') or (movement.reference_type or ''),
            'product_name': product.name,
            'sku': product.sku or '',
            'category': category,
            'warehouse': movement.warehouse.name,
            'supplier': receipt.get('supplier') or '',
            'movement_label': labels.get(movement.movement_type, movement.movement_type),
            'quantity_display': _movement_quantity_display(movement),
            'quantity': movement.quantity,
            'unit_cost': unit_cost,
            'purchase_value': purchase_value,
        })

        bucket = per_product.setdefault(movement.product_id, {
            'product_name': product.name,
            'sku': product.sku or '',
            'category': category,
            'entries': 0,
            'quantity': ZERO,
            'valued_quantity': ZERO,
            'purchase_value': ZERO,
            'has_value': False,
        })
        bucket['entries'] += 1
        bucket['quantity'] += movement.quantity
        if purchase_value is not None:
            bucket['purchase_value'] += purchase_value
            bucket['valued_quantity'] += movement.quantity
            bucket['has_value'] = True

    # Une seule devise : un total unique se lit mieux. Plusieurs : une ligne par
    # devise, jamais de somme, comme le fait `MultiCurrencyTotal` côté interface.
    if len(value_by_currency) > 1:
        summary = [
            (f"Valeur d'achat ({code})", format_number(amount, currency_decimals(code)))
            for code, amount in sorted(value_by_currency.items())
        ]
    else:
        summary = [("Valeur d'achat totale", format_number(total_value, decimals))]

    summary += [
        ('Produits approvisionnés', str(len(products_seen))),
        ('Entrées', str(len(movements))),
        ('Quantité totale (unités)', format_quantity(total_quantity)),
    ]
    if unvalued_lines:
        # Signalé explicitement : un total muet sur des lignes sans coût
        # laisserait croire que l'approvisionnement a coûté moins qu'en réalité.
        summary.append(('Entrées sans coût saisi', str(unvalued_lines)))
    if period_label:
        summary.insert(0, ('Période', period_label))

    # Les sous-totaux du tableau additionnent une colonne unique : en présence de
    # plusieurs devises, ils mélangent des unités différentes. On le DIT sur le
    # document plutôt que de laisser un total muet induire en erreur.
    mixed_currencies = len(value_by_currency) > 1
    warning = (
        " - ATTENTION : entrées en plusieurs devises "
        f"({', '.join(sorted(value_by_currency))}), les totaux du tableau ne sont "
        "pas convertis"
        if mixed_currencies else ''
    )

    if group_by == 'movement':
        return ReportSpec(
            title="Rapport d'approvisionnement",
            organization=organization,
            columns=SUPPLY_DETAIL_COLUMNS,
            rows=detail_rows,
            subtitle='Détail chronologique des entrées de stock' + warning,
            filters_applied=filters_applied,
            summary=tuple(summary),
            group_by=None,
            group_totals=('quantity', 'purchase_value'),
            currency=currency,
            signatures=('Établi par', 'Vérifié par'),
            empty_message="Aucune entrée de stock sur la période retenue.",
        )

    product_rows = []
    for bucket in sorted(
        per_product.values(), key=lambda item: (item['category'], item['product_name'])
    ):
        # Le coût moyen se rapporte aux seules quantités valorisées, sinon une
        # entrée gratuite ferait chuter artificiellement le coût unitaire.
        average = (
            bucket['purchase_value'] / bucket['valued_quantity']
            if bucket['valued_quantity'] > 0 else None
        )
        product_rows.append({
            'product_name': bucket['product_name'],
            'sku': bucket['sku'],
            'category': bucket['category'],
            'entries': bucket['entries'],
            'quantity': bucket['quantity'],
            'avg_unit_cost': average,
            'purchase_value': bucket['purchase_value'] if bucket['has_value'] else None,
        })

    return ReportSpec(
        title="Rapport d'approvisionnement",
        organization=organization,
        columns=SUPPLY_PRODUCT_COLUMNS,
        rows=product_rows,
        subtitle="Valeur d'achat par produit" + warning,
        filters_applied=filters_applied,
        summary=tuple(summary),
        group_by='category',
        group_totals=('quantity', 'purchase_value'),
        currency=currency,
        signatures=('Établi par', 'Vérifié par'),
        empty_message="Aucune entrée de stock sur la période retenue.",
    )


# --------------------------------------------------------------------------
# Rapport « Journal des mouvements »
# --------------------------------------------------------------------------

MOVEMENT_COLUMNS = [
    ReportColumn('date', 'Date', 25, KIND_DATETIME),
    ReportColumn('product_name', 'Produit', 38, KIND_TEXT),
    ReportColumn('sku', 'SKU', 24, KIND_TEXT),
    ReportColumn('category', 'Catégorie', 20, KIND_TEXT),
    ReportColumn('movement_label', 'Type', 18, KIND_TEXT),
    ReportColumn('warehouse', 'Entrepôt', 22, KIND_TEXT),
    ReportColumn('user', 'Par', 24, KIND_TEXT),
    ReportColumn('quantity_display', 'Quantité', 30, KIND_MEASURE),
    ReportColumn('quantity_signed', 'Total (unités)', 18, KIND_QUANTITY),
    ReportColumn('before_after', 'Stock avant → après', 24, KIND_MEASURE),
    ReportColumn('unit_cost', 'Coût unit.', 20, KIND_MONEY),
    ReportColumn('value', 'Valeur', 22, KIND_MONEY),
]


def build_movements_report(queryset, organization, *, currency='CDF',
                           filters_applied=(), period_label=''):
    """
    Journal des mouvements, tel qu'affiché dans la rubrique correspondante.

    La quantité est SIGNÉE : une sortie s'écrit en négatif, faute de quoi un
    journal mélangeant entrées et sorties s'additionne en dépit du bon sens.
    """
    from .filters import STOCK_IN_MOVEMENT_TYPES

    queryset = queryset.select_related(
        'product', 'product__category', 'product__unit',
        'product__packaging_unit', 'warehouse', 'created_by',
    ).order_by('-created_at')

    labels = dict(queryset.model.MovementType.choices)
    decimals = currency_decimals(currency)

    total_in = ZERO
    total_out = ZERO
    total_value = ZERO

    def rows():
        nonlocal total_in, total_out, total_value
        for movement in queryset.iterator(chunk_size=500):
            product = movement.product
            incoming = movement.movement_type in STOCK_IN_MOVEMENT_TYPES
            signed = movement.quantity if incoming else -movement.quantity
            unit_cost = movement.unit_cost if movement.unit_cost and movement.unit_cost > 0 else None
            value = (movement.quantity * unit_cost) if unit_cost is not None else None

            if incoming:
                total_in += movement.quantity
            else:
                total_out += movement.quantity
            if value is not None:
                total_value += value

            user = movement.created_by
            yield {
                'date': movement.created_at,
                'product_name': product.name,
                'sku': product.sku or '',
                'category': product.category.name if product.category else 'Sans catégorie',
                'movement_label': labels.get(movement.movement_type, movement.movement_type),
                'warehouse': movement.warehouse.name,
                'user': (user.full_name or user.email) if user else '',
                'quantity_display': _movement_quantity_display(movement),
                'quantity_signed': signed,
                'before_after': _movement_levels(movement),
                'unit_cost': unit_cost,
                'value': value,
            }

    materialized = list(rows())

    summary = [
        ('Mouvements', str(len(materialized))),
        ('Entrées (unités)', format_quantity(total_in)),
        ('Sorties (unités)', format_quantity(total_out)),
        ('Valeur mouvementée', format_number(total_value, decimals)),
    ]
    if period_label:
        summary.insert(0, ('Période', period_label))

    return ReportSpec(
        title='Journal des mouvements de stock',
        organization=organization,
        columns=MOVEMENT_COLUMNS,
        rows=materialized,
        subtitle='Historique des entrées et sorties',
        filters_applied=filters_applied,
        summary=tuple(summary),
        group_by=None,
        group_totals=('value',),
        currency=currency,
        signatures=('Établi par', 'Vérifié par'),
        empty_message='Aucun mouvement ne correspond aux critères retenus.',
    )
