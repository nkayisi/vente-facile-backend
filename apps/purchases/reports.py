"""
Documents des achats : commandes, réceptions, règlements.

Rendus par le registre d'exports SERVEUR, comme tous les documents A4 du
projet depuis le 4 septembre. `frontend/lib/pdf-utils.ts` a été supprimé ce
jour-là précisément pour qu'il n'y ait plus deux moteurs à tenir en phase à la
main, et un export d'achat n'a aucune raison de rouvrir cette dette.

┌──────────────────────────────────────────────────────────────────────────────┐
│ AUCUNE SOMME INTER-DEVISES, ET L'AVERTISSEMENT PLUTÔT QUE LE SILENCE.       │
│                                                                              │
│ Une commande d'achat porte sa devise : un fournisseur chinois facture en     │
│ dollars pendant que le voisin facture en francs. `ReportSpec` ne connaît     │
│ qu'une devise de document, donc on ventile la synthèse par devise, on RETIRE │
│ les totaux de colonne dès que deux monnaies coexistent, et on l'écrit dans   │
│ le sous-titre. C'est mot pour mot l'arbitrage de `build_sales_report`.       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from django.db.models import Count

from apps.core.exports import (
    KIND_DATE,
    KIND_DATETIME,
    KIND_MEASURE,
    KIND_MONEY,
    KIND_NUMBER,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
    currency_decimals,
    format_number,
)

ZERO = Decimal('0.00')


def _sous_titre(base, par_devise):
    """Nomme la ventilation quand plusieurs monnaies coexistent."""
    if len(par_devise) <= 1:
        return base
    return (
        f"{base} - ATTENTION : plusieurs devises "
        f"({', '.join(sorted(par_devise))}), les totaux de colonne ne sont pas convertis"
    )


# =============================================================================
# COMMANDES D'ACHAT
# =============================================================================

COLONNES_COMMANDE = (
    ReportColumn(key='reference', header='Référence', kind=KIND_TEXT, width=32),
    ReportColumn(key='date', header='Date', kind=KIND_DATE, width=24),
    ReportColumn(key='expected', header='Attendu le', kind=KIND_DATE, width=24),
    ReportColumn(key='supplier', header='Fournisseur', kind=KIND_TEXT, width=42),
    ReportColumn(key='warehouse', header='Entrepôt', kind=KIND_TEXT, width=28),
    ReportColumn(key='status', header='Statut', kind=KIND_TEXT, width=26),
    ReportColumn(key='items_count', header='Lignes', kind=KIND_NUMBER, width=14),
    ReportColumn(key='currency', header='Devise', kind=KIND_TEXT, width=14),
    ReportColumn(key='total', header='Total', kind=KIND_MONEY, width=26),
    ReportColumn(key='amount_paid', header='Payé', kind=KIND_MONEY, width=26),
    ReportColumn(key='amount_due', header='Reste dû', kind=KIND_MONEY, width=26),
)


def build_purchase_orders_report(queryset, organization, *, currency='CDF',
                                 filters_applied=()):
    """Journal des commandes d'achat, tel que l'écran le montre."""
    queryset = (
        queryset
        .select_related('supplier', 'warehouse')
        # ⚠ LE NOMBRE DE LIGNES SE COMPTE EN SQL, PAS LIGNE PAR LIGNE.
        # `po.items.count()` dans la boucle faisait UNE requête par commande,
        # sur un export délibérément NON paginé : le coût croissait avec la
        # table. `_items_count` était déjà lu en premier, personne ne
        # l'alimentait.
        .annotate(_items_count=Count('items'))
        .order_by('-order_date', '-reference')
    )
    libelles = dict(queryset.model.Status.choices)
    par_devise = {}

    lignes = []
    for po in queryset.iterator(chunk_size=500):
        code = po.currency
        cumul = par_devise.setdefault(code, [0, ZERO, ZERO, ZERO])
        cumul[0] += 1
        cumul[1] += po.total or ZERO
        cumul[2] += po.amount_paid or ZERO
        cumul[3] += po.amount_due or ZERO

        lignes.append({
            'reference': po.reference,
            'date': po.order_date,
            'expected': po.expected_date,
            'supplier': po.supplier.name if po.supplier else '',
            # Une commande sans entrepôt est un cas ordinaire (brouillon, achat
            # d'établissement) : on l'écrit plutôt que de laisser une case vide,
            # qui se lirait comme une donnée manquante.
            'warehouse': po.warehouse.name if po.warehouse else 'Non affecté',
            'status': libelles.get(po.status, po.status),
            'items_count': getattr(po, '_items_count', None) or po.items.count(),
            'currency': code,
            'total': po.total,
            'amount_paid': po.amount_paid,
            'amount_due': po.amount_due,
        })

    total_commandes = sum(c[0] for c in par_devise.values())
    synthese = [('Commandes', str(total_commandes))]
    for code in sorted(par_devise):
        _, total, _paye, du = par_devise[code]
        decimales = currency_decimals(code)
        synthese.append((f'Total {code}', format_number(total, decimales)))
        if du > ZERO:
            synthese.append((f'Reste dû {code}', format_number(du, decimales)))

    seule_devise = next(iter(par_devise)) if len(par_devise) == 1 else None

    return ReportSpec(
        title="Commandes d'achat",
        organization=organization,
        columns=COLONNES_COMMANDE,
        rows=lignes,
        subtitle=_sous_titre('Journal des commandes', par_devise),
        filters_applied=filters_applied,
        summary=tuple(synthese),
        group_totals=(
            ('total', 'amount_paid', 'amount_due') if len(par_devise) <= 1 else ()
        ),
        currency_field='currency' if len(par_devise) > 1 else None,
        currency=seule_devise or currency,
        landscape_mode=True,
        signatures=('Établi par', 'Approuvé par'),
        empty_message='Aucune commande ne correspond aux critères retenus.',
    )


# =============================================================================
# RÉCEPTIONS
# =============================================================================

COLONNES_RECEPTION = (
    ReportColumn(key='reference', header='Référence', kind=KIND_TEXT, width=30),
    ReportColumn(key='date', header='Date', kind=KIND_DATETIME, width=30),
    ReportColumn(key='order', header='Commande', kind=KIND_TEXT, width=30),
    ReportColumn(key='supplier', header='Fournisseur', kind=KIND_TEXT, width=38),
    ReportColumn(key='warehouse', header='Entrepôt', kind=KIND_TEXT, width=28),
    ReportColumn(key='status', header='Statut', kind=KIND_TEXT, width=22),
    ReportColumn(key='invoice', header='Facture', kind=KIND_TEXT, width=26),
    ReportColumn(key='received_by', header='Reçu par', kind=KIND_TEXT, width=30),
    ReportColumn(key='lines', header='Lignes', kind=KIND_NUMBER, width=14),
    # ⚠ Une quantité écrite en mots (« 3 cartons + 7 bouteilles ») est du TEXTE,
    # mais le lecteur y compare des grandeurs : `KIND_MEASURE` la range à droite
    # avec les nombres, pour que le bloc chiffré n'ait qu'un seul bord.
    ReportColumn(key='accepted', header='Accepté', kind=KIND_MEASURE, width=30),
    ReportColumn(key='rejected', header='Refusé', kind=KIND_MEASURE, width=26),
)


def build_goods_receipts_report(queryset, organization, *, currency='CDF',
                                filters_applied=()):
    """Journal des réceptions de marchandises."""
    from apps.inventory.packaging import PackagingService

    queryset = (
        queryset
        .select_related('purchase_order', 'purchase_order__supplier', 'warehouse', 'received_by')
        .prefetch_related('items', 'items__product')
        .order_by('-receipt_date')
    )
    libelles = dict(queryset.model.Status.choices)

    lignes = []
    nb_validees = 0
    for grn in queryset.iterator(chunk_size=200):
        if grn.status == 'completed':
            nb_validees += 1

        items = list(grn.items.all())
        acceptees = sum((i.quantity_accepted or Decimal('0.000') for i in items), Decimal('0.000'))
        refusees = sum((i.quantity_rejected or Decimal('0.000') for i in items), Decimal('0.000'))

        # ⚠ On N'ADDITIONNE PAS des unités de produits différents en prétendant
        # les nommer : le total ci-dessus mêle des bouteilles et des cartons.
        # Quand une seule ligne existe, on peut la nommer ; au-delà, on écrit un
        # nombre nu et c'est le détail de la réception qui ventile.
        if len(items) == 1:
            lecture_acceptee = PackagingService.format_base_total(items[0].product, acceptees)
            lecture_refusee = (
                PackagingService.format_base_total(items[0].product, refusees) if refusees else '-'
            )
        else:
            lecture_acceptee = format_number(acceptees, 3)
            lecture_refusee = format_number(refusees, 3) if refusees else '-'

        recu_par = grn.received_by
        lignes.append({
            'reference': grn.reference,
            'date': grn.receipt_date,
            'order': grn.purchase_order.reference if grn.purchase_order else '',
            'supplier': (
                grn.purchase_order.supplier.name
                if grn.purchase_order and grn.purchase_order.supplier else ''
            ),
            'warehouse': grn.warehouse.name if grn.warehouse else '',
            'status': libelles.get(grn.status, grn.status),
            'invoice': grn.supplier_invoice or '-',
            'received_by': (
                (recu_par.full_name or recu_par.email) if recu_par else ''
            ),
            'lines': len(items),
            'accepted': lecture_acceptee,
            'rejected': lecture_refusee,
        })

    return ReportSpec(
        title='Réceptions de marchandises',
        organization=organization,
        columns=COLONNES_RECEPTION,
        rows=lignes,
        subtitle='Journal des réceptions',
        filters_applied=filters_applied,
        summary=(
            ('Réceptions', str(len(lignes))),
            ('Validées', str(nb_validees)),
        ),
        # ⚠ AUCUN total de colonne : les quantités portent des unités
        # différentes d'une ligne à l'autre, et leur somme ne désignerait rien.
        # Le libellé des colonnes le dit déjà.
        group_totals=(),
        currency=currency,
        landscape_mode=True,
        signatures=('Reçu par', 'Contrôlé par'),
        empty_message='Aucune réception ne correspond aux critères retenus.',
    )


# =============================================================================
# RÈGLEMENTS FOURNISSEURS
# =============================================================================

COLONNES_REGLEMENT = (
    ReportColumn(key='reference', header='Référence', kind=KIND_TEXT, width=30),
    ReportColumn(key='date', header='Date', kind=KIND_DATE, width=24),
    ReportColumn(key='supplier', header='Fournisseur', kind=KIND_TEXT, width=44),
    ReportColumn(key='method', header='Moyen', kind=KIND_TEXT, width=28),
    ReportColumn(key='external', header='Pièce', kind=KIND_TEXT, width=28),
    ReportColumn(key='orders', header='Imputé sur', kind=KIND_TEXT, width=40),
    ReportColumn(key='status', header='Statut', kind=KIND_TEXT, width=22),
    ReportColumn(key='currency', header='Devise', kind=KIND_TEXT, width=14),
    ReportColumn(key='amount', header='Montant', kind=KIND_MONEY, width=28),
)


def build_supplier_payments_report(queryset, organization, *, currency='CDF',
                                   filters_applied=()):
    """Journal des règlements fournisseurs, avec leur imputation."""
    queryset = (
        queryset
        .select_related('supplier', 'payment_method')
        .prefetch_related('allocations', 'allocations__purchase_order')
        .order_by('-payment_date', '-reference')
    )
    libelles = dict(queryset.model.Status.choices)
    par_devise = {}
    # ⚠ TOUTES LES DEVISES PRÉSENTES, ANNULÉES COMPRISES, ET IL FAUT LES DEUX
    # ENSEMBLES. `par_devise` ne porte que ce qui a réellement décaissé, mais
    # c'est la présence de PLUSIEURS monnaies qui décide de `currency_field`,
    # des totaux de colonne et de l'avertissement. S'en remettre au seul cumul
    # faisait formater un règlement annulé en francs avec les décimales du
    # dollar, et taisait l'avertissement sur un document pourtant mixte.
    devises_presentes = set()

    lignes = []
    for paiement in queryset.iterator(chunk_size=300):
        devises_presentes.add(paiement.currency)
        # ⚠ Un règlement ANNULÉ n'a pas décaissé : le compter ferait un total
        # qui ne correspond à aucun tiroir. Il reste LISTÉ, parce que sa trace
        # est ce qui explique un solde.
        if paiement.status != 'cancelled':
            code = paiement.currency
            cumul = par_devise.setdefault(code, [0, ZERO])
            cumul[0] += 1
            cumul[1] += paiement.amount or ZERO

        imputations = [
            a.purchase_order.reference
            for a in paiement.allocations.all()
            if a.purchase_order
        ]
        lignes.append({
            'reference': paiement.reference,
            'date': paiement.payment_date,
            'supplier': paiement.supplier.name if paiement.supplier else '',
            'method': paiement.payment_method.name if paiement.payment_method else '-',
            'external': paiement.payment_reference or '-',
            # « Avance » et non une case vide : un règlement sans imputation est
            # un acompte versé avant toute commande, pas une donnée manquante.
            'orders': ', '.join(imputations) if imputations else 'Avance',
            'status': libelles.get(paiement.status, paiement.status),
            'currency': paiement.currency,
            'amount': paiement.amount,
        })

    synthese = [('Règlements', str(sum(c[0] for c in par_devise.values())))]
    for code in sorted(par_devise):
        _, total = par_devise[code]
        synthese.append((
            f'Décaissé {code}',
            format_number(total, currency_decimals(code)),
        ))

    seule_devise = (
        next(iter(devises_presentes)) if len(devises_presentes) == 1 else None
    )

    return ReportSpec(
        title='Règlements fournisseurs',
        organization=organization,
        columns=COLONNES_REGLEMENT,
        rows=lignes,
        subtitle=_sous_titre('Journal des règlements', devises_presentes),
        filters_applied=filters_applied,
        summary=tuple(synthese),
        group_totals=('amount',) if len(devises_presentes) <= 1 else (),
        currency_field='currency' if len(devises_presentes) > 1 else None,
        currency=seule_devise or currency,
        landscape_mode=True,
        signatures=('Établi par', 'Vérifié par'),
        empty_message='Aucun règlement ne correspond aux critères retenus.',
    )
