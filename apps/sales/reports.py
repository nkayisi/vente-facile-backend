"""
Description du rapport des ventes, pour les deux moteurs de rendu.

Un rapport se DÉCRIT une fois (`ReportSpec`) et deux moteurs le rendent, si
bien que le PDF et le classeur d'un même export ne peuvent pas diverger.
C'est le socle posé pour les niveaux de stock, employé ici sans le réécrire.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LES VENTES SONT MULTI-DEVISES, ET `ReportSpec` N'EN PORTE QU'UNE.            │
│                                                                              │
│ Additionner des dollars et des francs donne un nombre qui n'existe pas, et    │
│ qui a l'air juste. Le rapport ventile donc la synthèse PAR DEVISE et écrit    │
│ l'avertissement quand plusieurs coexistent, plutôt que de sommer en silence.  │
│ Le motif vient de `inventory/reports.py`, où la même question s'est posée     │
│ sur les prix d'achat.                                                         │
│                                                                              │
│ Les montants des LIGNES restent dans la devise de leur facture : les          │
│ convertir au taux du jour ferait bouger l'historique à chaque changement de   │
│ cours, et le marchand ne saurait pas lequel croire.                           │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from apps.core.exports import (
    KIND_DATETIME,
    KIND_MONEY,
    KIND_NUMBER,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
    currency_decimals,
    format_number,
)

ZERO = Decimal('0')

#: Les colonnes du journal des ventes. « Devise » est une colonne à part
#: entière : sans elle, deux montants voisins dans deux monnaies se liraient
#: comme deux montants comparables.
#: Largeurs en MILLIMÈTRES, pour les 267 mm utiles d'une A4 paysage. Les
#: références et les noms de clients sont ce qui déborde en premier ; les
#: montants, eux, ne se tronquent jamais (un montant tronqué est un faux
#: montant, c'est la règle du ticket imprimé).
SALE_COLUMNS = (
    ReportColumn(key='reference', header='Référence', kind=KIND_TEXT, width=34),
    ReportColumn(key='date', header='Date', kind=KIND_DATETIME, width=30),
    ReportColumn(key='customer', header='Client', kind=KIND_TEXT, width=40),
    ReportColumn(key='seller', header='Vendeur', kind=KIND_TEXT, width=30),
    ReportColumn(key='status', header='Statut', kind=KIND_TEXT, width=22),
    ReportColumn(key='items_count', header='Articles', kind=KIND_NUMBER, width=16),
    ReportColumn(key='currency', header='Devise', kind=KIND_TEXT, width=14),
    ReportColumn(key='total', header='Total', kind=KIND_MONEY, width=27),
    ReportColumn(key='amount_paid', header='Payé', kind=KIND_MONEY, width=27),
    ReportColumn(key='amount_due', header='Reste dû', kind=KIND_MONEY, width=27),
)


def build_sales_report(queryset, organization, *, currency='CDF',
                       filters_applied=()):
    """
    Journal des ventes, tel qu'affiché dans l'historique.

    La période arrive par `filters_applied` : elle est annoncée UNE fois, en
    tête du document, comme sur tous les autres rapports.
    """
    queryset = queryset.select_related('customer', 'sold_by').order_by('-sale_date')

    labels = dict(queryset.model.Status.choices)

    #: Par devise : (nombre, total, payé, dû). On ne somme jamais entre elles.
    par_devise = {}

    def rows():
        for sale in queryset.iterator(chunk_size=500):
            code = sale.currency
            cumul = par_devise.setdefault(code, [0, ZERO, ZERO, ZERO])
            cumul[0] += 1
            cumul[1] += sale.total or ZERO
            cumul[2] += sale.amount_paid or ZERO
            cumul[3] += sale.amount_due or ZERO

            vendeur = sale.sold_by
            yield {
                'reference': sale.reference,
                'date': sale.sale_date,
                # « Client anonyme » et non une case vide : une vente au
                # comptoir sans client nommé est un cas ordinaire, pas une
                # donnée manquante.
                'customer': sale.customer.name if sale.customer else 'Client anonyme',
                'seller': (vendeur.full_name or vendeur.email) if vendeur else '',
                'status': labels.get(sale.status, sale.status),
                'items_count': getattr(sale, '_items_count', None) or sale.items.count(),
                'currency': code,
                'total': sale.total,
                'amount_paid': sale.amount_paid,
                'amount_due': sale.amount_due,
            }

    materialized = list(rows())

    total_ventes = sum(c[0] for c in par_devise.values())
    summary = [('Ventes', str(total_ventes))]
    for code in sorted(par_devise):
        nombre, total, _paye, du = par_devise[code]
        decimales = currency_decimals(code)
        summary.append((f'Total {code}', format_number(total, decimales)))
        if du > ZERO:
            summary.append((f'Reste dû {code}', format_number(du, decimales)))
    # ⚠ La période est DÉJÀ en tête, dans `filters_applied` (voir
    # `SaleViewSet.build_export_spec`). La répéter en synthèse faisait lire deux
    # fois la même date au même lecteur, qui se demande alors laquelle prime.
    # Relevé en comparant le fichier du terminal à l'écran qui l'a déclenché.

    # L'avertissement plutôt qu'une somme muette : les sous-totaux de colonne
    # du tableau mêlent par construction les devises, et le lecteur doit le
    # savoir avant de lire le bas de la page.
    sous_titre = 'Journal des ventes'
    if len(par_devise) > 1:
        sous_titre += (
            f" - ATTENTION : ventes en plusieurs devises "
            f"({', '.join(sorted(par_devise))}), les totaux de colonne ne sont pas convertis"
        )

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ CHAQUE LIGNE S'ÉCRIT DANS SA MONNAIE, SINON LE MONTANT EST FAUX.        │
    # │                                                                          │
    # │ Le spec ne posait pas `currency_field` alors que ses lignes portent leur │
    # │ code et qu'une colonne « Devise » est affichée : tous les montants       │
    # │ prenaient les décimales du DOCUMENT. Dans un établissement tenu en CDF   │
    # │ (zéro décimale), une vente de 120,75 USD sortait « 121 », et l'en-tête   │
    # │ annonçait « Montants en FC » au-dessus de lignes en dollars.             │
    # │                                                                          │
    # │ Avec une seule devise, on nomme CETTE devise plutôt que d'annoncer une   │
    # │ ventilation qui n'a rien à ventiler : l'en-tête peut alors écrire        │
    # │ « Montants en $ », y compris quand elle n'est pas la principale.          │
    # └──────────────────────────────────────────────────────────────────────────┘
    seule_devise = next(iter(par_devise)) if len(par_devise) == 1 else None

    return ReportSpec(
        title='Historique des ventes',
        organization=organization,
        columns=SALE_COLUMNS,
        rows=materialized,
        subtitle=sous_titre,
        filters_applied=filters_applied,
        summary=tuple(summary),
        group_by=None,
        # Pas de total de colonne quand les devises se mêlent : il n'existerait
        # pas. Avec une seule devise, il est juste et vaut d'être écrit.
        group_totals=('total', 'amount_paid', 'amount_due') if len(par_devise) <= 1 else (),
        currency_field='currency' if len(par_devise) > 1 else None,
        currency=seule_devise or currency,
        landscape_mode=True,
        signatures=('Établi par', 'Vérifié par'),
        empty_message='Aucune vente ne correspond aux critères retenus.',
    )
