"""
Les huit onglets de « Rapports & Statistiques », décrits pour l'export.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE DOCUMENT EST FABRIQUÉ ICI, ET NULLE PART AILLEURS.                       │
│                                                                              │
│ Le back-office et le terminal construisaient chacun leur description, à       │
│ partir des lignes qu'ils avaient déjà à l'écran. Trois conséquences, toutes  │
│ mesurées :                                                                   │
│                                                                              │
│ 1. Le fichier ne portait que la PAGE affichée, vingt lignes, sous un en-tête │
│    qui annonçait « 347 articles ». Il mentait sur son propre contenu.        │
│ 2. Le back-office n'imprimait pas : il ouvrait un onglet et appelait         │
│    `window.print()`. Rien ne se téléchargeait.                               │
│ 3. Deux moteurs, donc deux mises en page pour un seul rapport.               │
│                                                                              │
│ Ici la description est UNE, les trois moteurs de `apps.core.exports` la      │
│ rendent, et les deux surfaces ne font plus que télécharger des octets.       │
└──────────────────────────────────────────────────────────────────────────────┘

Ce module ne contient **aucun agrégat ORM** : il appelle les constructeurs de
lignes de `StatisticsViewSet`, ceux-là mêmes que servent les actions paginées.
C'est ce qui rend la parité structurelle plutôt que surveillée ; y réécrire une
somme serait aussi l'endroit où le `rate=` de `primary_sum` se ferait oublier,
et un montant non converti ne se voit pas.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apps.core.exports import (
    KIND_MEASURE,
    KIND_MONEY,
    KIND_NUMBER,
    KIND_QUANTITY,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
    currency_decimals,
    format_number,
    format_quantity,
)
from apps.core.report_params import format_day
from apps.settings.services import CurrencyService

ZERO = Decimal('0')


# --------------------------------------------------------------------------
# Périodes
# --------------------------------------------------------------------------

#: Libellés des périodes, MOT POUR MOT ceux de l'interface.
#:
#: Ils sont repris de `core/src/report/periodes.ts` (`PERIODES_RAPPORT`), que
#: les deux surfaces affichent dans leur sélecteur. Un document qui annoncerait
#: « last_30_days » là où l'écran dit « 30 derniers jours » obligerait le
#: lecteur à traduire.
PERIOD_LABELS = {
    'last_7_days': '7 derniers jours',
    'last_30_days': '30 derniers jours',
    'last_12_months': '12 derniers mois',
    'today': "Aujourd'hui",
    'week': 'Cette semaine',
    'month': 'Ce mois',
    'quarter': 'Ce trimestre',
    'year': 'Cette année',
    'custom': 'Personnalisé',
}

#: Le défaut du serveur, glissant. Voir `_parse_date_range`.
PERIOD_DEFAULT = 'last_30_days'


def period_label_for(request) -> str:
    """
    Le libellé de la fenêtre demandée, tel que le sélecteur l'affiche.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE LIBELLÉ SUIT LE CHOIX DU MARCHAND, PAS LA FORME DE L'APPEL.          │
    │                                                                          │
    │ Le terminal envoie TOUJOURS des dates explicites - c'est sa doctrine :   │
    │ la fenêtre ne doit pas dépendre d'un défaut serveur qu'il ne voit pas.   │
    │ Le back-office, lui, envoie `period`. En rendant « Personnalisé » dès    │
    │ que deux dates sont là, le même rapport sortait « 30 derniers jours »    │
    │ d'un côté et « Personnalisé » de l'autre, pour la MÊME fenêtre.          │
    │                                                                          │
    │ Mesuré en comparant les deux fichiers : quatre-vingt-une lignes          │
    │ identiques, et cette seule divergence.                                    │
    │                                                                          │
    │ `period` nomme donc la fenêtre quand il est fourni ; les dates, elles,   │
    │ la DÉLIMITENT et gardent la priorité dans `_parse_date_range`.           │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    period = request.query_params.get('period')
    if period:
        return PERIOD_LABELS.get(period, PERIOD_LABELS[PERIOD_DEFAULT])
    if request.query_params.get('date_from') and request.query_params.get('date_to'):
        return PERIOD_LABELS['custom']
    return PERIOD_LABELS[PERIOD_DEFAULT]


# --------------------------------------------------------------------------
# Colonnes
# --------------------------------------------------------------------------
#
# Elles vivent dans des tables plutôt qu'au fil des constructeurs : c'est ce qui
# rend la comparaison avec les deux écrans mécanique. Les intitulés sont ceux du
# back-office et du terminal, AU CARACTÈRE PRÈS, et un test les croise.

# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ UNE COLONNE PAR GRANDEUR, JAMAIS UN « DÉTAIL » FOURRE-TOUT.                 │
# │                                                                              │
# │ « Vue d'ensemble » et « Clients » sortaient sous trois colonnes génériques   │
# │ (Libellé / Détail / Valeur), la colonne du milieu portant une phrase :       │
# │ « Entrées 7 509,5 $ · Sorties 0 $ ». Trois conséquences, toutes mesurables : │
# │ on ne peut pas trier sur une phrase, on ne peut pas la sommer, et le         │
# │ tableur la reçoit comme du TEXTE - donc le marchand ne peut ni additionner   │
# │ ses entrées ni les comparer d'une journée à l'autre.                         │
# │                                                                              │
# │ Chaque grandeur a désormais sa colonne, et son total en bas.                 │
# └──────────────────────────────────────────────────────────────────────────────┘

CASH_FLOW_COLUMNS = [
    ReportColumn('period', 'Période', 34, KIND_TEXT),
    ReportColumn('income', 'Entrées', 32, KIND_MONEY),
    ReportColumn('expenses', 'Sorties', 32, KIND_MONEY),
    ReportColumn('net', 'Net', 32, KIND_MONEY),
]

CUSTOMERS_COLUMNS = [
    ReportColumn('rank', '#', 10, KIND_TEXT),
    ReportColumn('customer_name', 'Client', 52, KIND_TEXT),
    ReportColumn('order_count', 'Commandes', 24, KIND_NUMBER),
    ReportColumn('total_purchases', 'Total acheté', 30, KIND_MONEY),
    ReportColumn('current_balance', 'Dette', 30, KIND_MONEY),
]

DAILY_CASH_COLUMNS = [
    ReportColumn('time', 'Heure', 18, KIND_TEXT),
    ReportColumn('type_display', 'Type', 34, KIND_TEXT),
    ReportColumn('description', 'Description', 56, KIND_TEXT),
    ReportColumn('income', 'Entrée', 26, KIND_MONEY),
    ReportColumn('outcome', 'Sortie', 26, KIND_MONEY),
    ReportColumn('balance_after', 'Solde', 26, KIND_MONEY),
]

SALES_ARTICLE_COLUMNS = [
    ReportColumn('rank', '#', 10, KIND_TEXT),
    ReportColumn('product_name', 'Article', 52, KIND_TEXT),
    ReportColumn('product_sku', 'SKU', 26, KIND_TEXT),
    ReportColumn('quantity_display', 'Quantité', 34, KIND_MEASURE),
    ReportColumn('total_revenue', 'Revenus', 28, KIND_MONEY),
]

PRODUCTS_COLUMNS = [
    ReportColumn('product_name', 'Produit', 46, KIND_TEXT),
    ReportColumn('opening_stock', 'Stock départ', 24, KIND_QUANTITY),
    ReportColumn('supplied', 'Approv.', 30, KIND_MEASURE),
    ReportColumn('quantity_display', 'Qté vendue', 30, KIND_MEASURE),
    ReportColumn('total_revenue', 'Valeur vendue', 26, KIND_MONEY),
    ReportColumn('remaining_display', 'Qté restante', 30, KIND_MEASURE),
    ReportColumn('stock_value', 'Valeur restante', 26, KIND_MONEY),
]

STOCK_COLUMNS = [
    ReportColumn('product_name', 'Produit', 46, KIND_TEXT),
    ReportColumn('category_name', 'Catégorie', 30, KIND_TEXT),
    ReportColumn('stock_display', 'Stock', 30, KIND_MEASURE),
    ReportColumn('available_display', 'Disponible', 30, KIND_MEASURE),
    ReportColumn('stock_value', 'Valeur', 26, KIND_MONEY),
    ReportColumn('status', 'Statut', 20, KIND_TEXT),
]

PROFITS_COLUMNS = [
    ReportColumn('product_name', 'Produit', 46, KIND_TEXT),
    ReportColumn('quantity_display', 'Qté vendue', 30, KIND_MEASURE),
    ReportColumn('total_revenue', 'CA (HT)', 26, KIND_MONEY),
    ReportColumn('total_cost', 'Coût', 26, KIND_MONEY),
    ReportColumn('profit', 'Bénéfice', 26, KIND_MONEY),
    ReportColumn('margin', 'Marge', 20, KIND_MEASURE),
]


RECEIVABLES_COLUMNS = [
    ReportColumn('customer_name', 'Client', 46, KIND_TEXT),
    ReportColumn('customer_phone', 'Téléphone', 30, KIND_TEXT),
    ReportColumn('invoice_count', 'Factures', 20, KIND_NUMBER),
    ReportColumn('oldest_days', 'Retard (j)', 22, KIND_NUMBER),
    ReportColumn('amount_due', 'Dû', 28, KIND_MONEY),
    ReportColumn('overdue_amount', 'Échu', 28, KIND_MONEY),
]


def user_activity_columns(group_by: str):
    """La première colonne nomme le pas de temps : « Heure » ou « Jour »."""
    return [
        ReportColumn('bucket', 'Heure' if group_by == 'hour' else 'Jour', 34, KIND_TEXT),
        ReportColumn('count', 'Ventes', 24, KIND_NUMBER),
        ReportColumn('total', 'Total', 34, KIND_MONEY),
    ]


#: Le radical du nom de fichier servi, par onglet. L'horodatage et l'extension
#: sont ajoutés par `ExportResponseMixin.export_file_response`.
EXPORT_BASENAMES = {
    'overview': 'rapport_vue_ensemble',
    'daily-cash': 'rapport_journalier_caisse',
    'sales': 'rapport_ventes_par_article',
    'products': 'rapport_produits_vendus',
    'customers': 'rapport_meilleurs_clients',
    'stock': 'rapport_etat_du_stock',
    'profits': 'rapport_benefices_par_produit',
    'user-activity': 'rapport_par_utilisateur',
    'receivables': 'rapport_creances',
}


# --------------------------------------------------------------------------
# Contexte
# --------------------------------------------------------------------------

@dataclass
class TabContext:
    """Ce que les huit constructeurs partagent."""

    organization: Any
    currency: str
    decimals: int
    start_date: Any
    end_date: Any
    prev_start: Any
    prev_end: Any
    group_by: str
    period_label: str
    filters: list = field(default_factory=list)
    summary: list = field(default_factory=list)

    def money(self, value) -> str:
        return format_number(value or ZERO, self.decimals)

    def percent(self, value) -> str:
        """Un pourcentage à la française : « 32,17 % » et non « 32.17% »."""
        return format_number(value or ZERO, 2) + ' %'


def build_tab_context(viewset, request, *, avec_releves: bool = True) -> TabContext:
    """
    Assemble la fenêtre, l'identité monétaire et, s'ils ont lieu d'être, les
    relevés globaux.

    `avec_releves=False` n'est pas qu'une économie : il empêche un onglet qui a
    sa PROPRE fenêtre de porter les chiffres d'une autre. Voir
    `TABS_SANS_RELEVES`. Accessoirement, les quatre agrégats de
    `build_global_summary` ne sont alors plus exécutés pour rien.
    """
    org = viewset.get_organization()
    start_date, end_date, prev_start, prev_end = viewset._parse_date_range(request)
    currency = CurrencyService.primary_code(org)

    ctx = TabContext(
        organization=org,
        currency=currency,
        decimals=currency_decimals(currency),
        start_date=start_date,
        end_date=end_date,
        prev_start=prev_start,
        prev_end=prev_end,
        group_by=request.query_params.get('group_by', 'day'),
        period_label=period_label_for(request),
    )
    # Le PÉRIMÈTRE, et il n'est jamais tu : un papier voyage seul, et son
    # lecteur n'a pas la barre de filtres sous les yeux.
    ctx.filters = [
        ('Période', ctx.period_label),
        ('Du', format_day(start_date.strftime('%Y-%m-%d'))),
        ('Au', format_day(end_date.strftime('%Y-%m-%d'))),
    ]
    ctx.summary = build_global_summary(viewset, request, ctx) if avec_releves else []
    return ctx


def build_global_summary(viewset, request, ctx: TabContext) -> list:
    """
    Les dix relevés globaux, ceux qui restent affichés quel que soit l'onglet.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ ILS PARTENT AVEC CHAQUE ONGLET.                                          │
    │                                                                          │
    │ Ils sont à l'écran en permanence, au-dessus des huit onglets : un fichier │
    │ qui ne les porterait pas dirait moins que ce qu'on avait sous les yeux en │
    │ le demandant.                                                            │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    sales = viewset._get_sales_stats(
        ctx.organization, ctx.start_date, ctx.end_date,
        ctx.prev_start, ctx.prev_end, request=request,
    )
    stock = viewset._get_stock_stats(ctx.organization, request=request)
    cashbook = viewset._get_cashbook_stats(
        ctx.organization, ctx.start_date, ctx.end_date, request=request
    )
    customers = viewset._get_customer_stats(
        ctx.organization, ctx.start_date, ctx.end_date
    )

    rows = [
        ("Chiffre d'affaires", ctx.money(sales['total_sales'])),
        ('Ventes', str(sales['total_orders'])),
        ('Panier moyen', ctx.money(sales['average_order_value'])),
        ('Articles vendus', format_quantity(sales['total_items_sold'])),
        ('Solde caisse', ctx.money(cashbook['current_balance'])),
        ('Créances clients', ctx.money(customers['total_receivables'])),
        ('Produits actifs', str(stock['total_products'])),
        ('Valeur stock', ctx.money(stock['total_stock_value'])),
        ('Stock bas', str(stock['low_stock_count'])),
        ('Ruptures', str(stock['out_of_stock_count'])),
    ]

    # Le tiroir RÉEL, ventilé, et seulement s'il y a plusieurs devises : comme à
    # l'écran. Chaque ligne porte les décimales de SA devise, sans quoi un solde
    # en dollars s'écrirait sans centimes dans une organisation tenue en francs.
    par_devise = cashbook.get('balance_by_currency') or []
    if len(par_devise) > 1:
        for solde in par_devise:
            code = solde['currency']
            rows.append((
                f"Caisse {code}",
                format_number(solde['balance'], currency_decimals(code)),
            ))
    return rows


def _spec(ctx: TabContext, *, onglet: str, section: str, columns, rows,
          extra_summary=None, empty: str = '', totals=()) -> ReportSpec:
    """
    Assemble le document, dans les termes que les deux écrans emploient.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LA PAGE SUIT LE TABLEAU, PAS L'INVERSE.                                  │
    │                                                                          │
    │ Quatre colonnes étalées sur une A4 paysage laissent dix centimètres      │
    │ entre la date et son montant : l'œil doit traverser la page pour relier  │
    │ deux cases de la même ligne, et sur douze lignes il se trompe de rangée. │
    │ Un tableau étroit se lit en PORTRAIT ; le paysage est fait pour les sept │
    │ colonnes de « Détails des produits vendus ».                             │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    return ReportSpec(
        landscape_mode=len(columns) > 4,
        title=f"{onglet.upper()} - {section.upper()}",
        organization=ctx.organization,
        columns=columns,
        rows=rows,
        subtitle=f"{ctx.period_label} ({ctx.filters[1][1]} au {ctx.filters[2][1]})",
        filters_applied=ctx.filters,
        summary=ctx.summary + list(extra_summary or []),
        currency=ctx.currency,
        group_totals=totals,
        empty_message=empty or 'Aucune ligne sur ce périmètre.',
    )


# --------------------------------------------------------------------------
# Les huit onglets
# --------------------------------------------------------------------------

def build_overview_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """
    Vue d'ensemble : le flux de trésorerie, période par période.

    Quatre colonnes et non un « détail » en phrase : ce sont trois grandeurs
    distinctes, elles se comparent d'une ligne à l'autre et se somment en bas.
    """
    rows = [
        {
            'period': format_day(row['period']),
            'income': row['income'],
            'expenses': row['expenses'],
            'net': row['net'],
        }
        for row in viewset._cash_flow_rows(
            request, ctx.organization, ctx.start_date, ctx.end_date, ctx.group_by
        )
    ]
    return _spec(
        ctx, onglet="Vue d'ensemble", section='Flux de trésorerie',
        columns=CASH_FLOW_COLUMNS, rows=rows,
        empty='Aucun mouvement de caisse sur cette période.',
        totals=('income', 'expenses', 'net'),
    )


def build_daily_cash_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """Rapport journalier : les mouvements du jour, et la synthèse en tête."""
    report_date = viewset._parse_report_date(request)
    report, movements = viewset._daily_cash_data(request, ctx.organization, report_date)

    rows = []
    for m in viewset._daily_cash_movement_rows(movements):
        # ENTRÉE et SORTIE dans DEUX colonnes : une cellule vide dit « ce n'en
        # est pas une », là où un montant signé demande de lire le signe.
        rows.append({
            'time': m['time'].strftime('%H:%M:%S') if m['time'] else '',
            'type_display': m['type_display'],
            'description': m['description'] or '-',
            'income': m['amount'] if m['direction'] == 'in' else None,
            'outcome': m['amount'] if m['direction'] == 'out' else None,
            'balance_after': m['balance_after'],
        })

    extra = [
        ("Solde d'ouverture", ctx.money(report['opening_balance'])),
        ('Solde de clôture', ctx.money(report['closing_balance'])),
        ('Ventes du jour',
         f"{ctx.money(report['total_sales'])} ({report['total_sales_count']} ventes)"),
        ('Dépenses',
         f"{ctx.money(report['expenses'])} ({report['expenses_count']} dépenses)"),
        ('Espèces', ctx.money(report['cash_sales'])),
        ('Mobile Money', ctx.money(report['mobile_money_sales'])),
        ('Carte', ctx.money(report['card_sales'])),
        ('Crédit', ctx.money(report['credit_sales'])),
    ]
    spec = _spec(
        ctx, onglet='Rapport journalier', section='Mouvements du jour',
        columns=DAILY_CASH_COLUMNS, rows=rows, extra_summary=extra,
        empty='Aucun mouvement pour cette date',
        totals=('income', 'outcome'),
    )
    # Cet onglet a sa PROPRE date : le périmètre annoncé doit être la sienne, et
    # non la fenêtre des sept autres onglets. Il est pour la même raison dans
    # `TABS_SANS_RELEVES` - remplacer le périmètre sans retirer les relevés
    # posait « Chiffre d'affaires » sur trente jours juste au-dessus de « Ventes
    # du jour », deux totaux contradictoires dans un même cartouche.
    jour = format_day(report_date.strftime('%Y-%m-%d'))
    spec.filters_applied = [('Date du rapport', jour)]
    spec.subtitle = f"Journée du {jour}"
    return spec


def build_sales_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """Ventes : le détail par article."""
    rows = []
    values = viewset._top_products_values(
        request, ctx.organization, ctx.start_date, ctx.end_date
    )
    for index, item in enumerate(values, start=1):
        row = viewset._top_product_row(item)
        rows.append({
            'rank': str(index),
            'product_name': row['product_name'],
            'product_sku': row['product_sku'] or '',
            'quantity_display': _quantity_cell(row),
            'total_revenue': row['total_revenue'],
        })
    return _spec(
        ctx, onglet='Ventes', section='Ventes par article',
        columns=SALES_ARTICLE_COLUMNS, rows=rows,
        empty='Aucune donnée pour cette période',
        totals=('total_revenue',),
    )


def build_products_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """Produits : le croisement de trois rubriques."""
    return _spec(
        ctx, onglet='Produits', section='Détails des produits vendus',
        columns=PRODUCTS_COLUMNS,
        rows=build_product_rows(viewset, request, ctx),
        empty='Aucune donnée pour cette période',
        totals=('total_revenue', 'stock_value'),
    )


def build_customers_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """
    Clients : le palmarès, avec la dette de chacun.

    La dette a SA colonne : c'est le chiffre sur lequel on décide de relancer,
    et le noyer dans une parenthèse à la suite du total achat le rendait
    incomparable d'une ligne à l'autre. Une case vide s'y lit « ne doit rien »,
    ce qui est exactement l'information cherchée.
    """
    rows = []
    for index, client in enumerate(
        viewset._top_customer_rows(request, ctx.organization, ctx.start_date, ctx.end_date),
        start=1,
    ):
        du = client['current_balance'] or ZERO
        rows.append({
            'rank': str(index),
            'customer_name': client['customer_name'],
            'order_count': client['order_count'],
            'total_purchases': client['total_purchases'],
            'current_balance': du if du > 0 else None,
        })
    return _spec(
        ctx, onglet='Clients', section='Meilleurs clients',
        columns=CUSTOMERS_COLUMNS, rows=rows,
        empty='Aucune donnée pour cette période',
        totals=('order_count', 'total_purchases', 'current_balance'),
    )


#: Les mots de l'écran pour l'état d'un rayon. Les traduire ici et non dans le
#: gabarit : « out_of_stock » sur un papier ne dit rien à un magasinier.
STOCK_STATUS_LABELS = {'out_of_stock': 'Rupture', 'low_stock': 'Bas'}


def build_stock_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """Stock : l'état du rayon, plus les totaux de mouvements en synthèse."""
    stocks = viewset._stock_detail_rows(
        request, ctx.organization, request.query_params.get('status')
    )
    rows = [
        {
            'product_name': s['product_name'],
            'category_name': s['category_name'] or '-',
            'stock_display': s['stock_display'],
            'available_display': s['available_display'],
            'stock_value': s['stock_value'],
            'status': STOCK_STATUS_LABELS.get(s['status'], 'OK'),
        }
        for s in stocks
    ]

    mouvements = viewset._stock_movements_summary_data(
        request, ctx.organization, ctx.start_date, ctx.end_date
    )
    extra = [
        ('Entrées totales', format_quantity(mouvements['total_in'])),
        ('Sorties totales', format_quantity(mouvements['total_out'])),
        ('Ventes', format_quantity(mouvements['sales_out'])),
        ('Retours', format_quantity(mouvements['returns_in'])),
    ]
    return _spec(
        ctx, onglet='Stock', section='État du stock',
        columns=STOCK_COLUMNS, rows=rows, extra_summary=extra,
        empty='Aucun produit en stock sur ce périmètre.',
        totals=('stock_value',),
    )


def build_profits_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """Bénéfices : par produit, sous les marges globales."""
    rows = [
        {
            'product_name': p['product_name'],
            'quantity_display': _quantity_cell(p),
            'total_revenue': p['total_revenue'],
            'total_cost': p['total_cost'],
            'profit': p['profit'],
            'margin': ctx.percent(p['margin_percentage']),
        }
        for p in viewset._product_profit_rows(
            request, ctx.organization, ctx.start_date, ctx.end_date
        )
    ]

    marges = viewset._profit_margins_data(
        request, ctx.organization, ctx.start_date, ctx.end_date
    )
    extra = [
        ('CA (HT net)', ctx.money(marges['total_revenue'])),
        ('Coût des marchandises', ctx.money(marges['total_cost'])),
        ('Bénéfice brut',
         f"{ctx.money(marges['gross_profit'])} (Marge: {ctx.percent(marges['gross_margin_percentage'])})"),
        ('Bénéfice net',
         f"{ctx.money(marges['net_profit'])} (Marge: {ctx.percent(marges['net_margin_percentage'])})"),
    ]
    return _spec(
        ctx, onglet='Bénéfices', section='Bénéfices par produit',
        columns=PROFITS_COLUMNS, rows=rows, extra_summary=extra,
        empty='Aucune donnée pour cette période',
        totals=('total_revenue', 'total_cost', 'profit'),
    )


def build_user_activity_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """Par utilisateur : la journée d'un employé, pas à pas."""
    from .views import USER_ACTIVITY_INCONNU

    membership = viewset._resolve_activity_member(
        ctx.organization, request.query_params.get('user')
    )
    if not membership:
        from rest_framework.exceptions import NotFound
        raise NotFound({'user': USER_ACTIVITY_INCONNU})

    group_by = request.query_params.get('group_by', 'day')
    data = viewset._user_activity_data(
        request, ctx.organization, membership,
        ctx.start_date, ctx.end_date, group_by,
    )

    rows = [
        {'bucket': b['bucket'], 'count': b['count'], 'total': b['total']}
        for b in data['breakdown']
    ]
    cash = data['cash']
    extra = [
        ('Utilisateur', data['user']['name']),
        ('Ventes',
         f"{ctx.money(data['sales']['total'])} ({data['sales']['count']} ventes)"),
        ('Dépenses créées',
         f"{ctx.money(data['expenses']['total'])} ({data['expenses']['count']} dépenses)"),
        ('Entrées / Sorties caisse',
         f"+{ctx.money(cash['cash_in'])} / -{ctx.money(cash['cash_out'])}"),
        ('Caisse nette', ctx.money(cash['net'])),
    ]
    return _spec(
        ctx, onglet='Par utilisateur', section='Détail des ventes',
        columns=user_activity_columns(group_by), rows=rows, extra_summary=extra,
        empty="Aucune vente pour cet employé sur cette période.",
        totals=('count', 'total'),
    )


#: Les libellés des tranches d'ancienneté, ceux des deux écrans.
LIBELLES_TRANCHES = {
    'current': 'Pas encore échu',
    'd1_30': '1 à 30 j',
    'd31_60': '31 à 60 j',
    'd61_90': '61 à 90 j',
    'd90_plus': 'Plus de 90 j',
}


def build_receivables_spec(viewset, request, ctx: TabContext) -> ReportSpec:
    """
    La balance âgée : qui doit, combien, et depuis quand.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ AUCUN TOTAL INTER-DEVISES, ET C'EST STRUCTUREL.                         │
    │                                                                          │
    │ Un client peut devoir en francs ET en dollars ; le tableau est donc      │
    │ GROUPÉ par devise, avec un sous-total par groupe et `grand_total=False`. │
    │ Additionner les deux rendrait un nombre qui n'existe pas, sous le        │
    │ symbole de la principale - le défaut que tout ce dépôt combat.           │
    │                                                                          │
    │ Les deux SEULS chiffres convertis (total dû, total échu) sont en         │
    │ synthèse et le disent.                                                    │
    └──────────────────────────────────────────────────────────────────────────┘

    Cette rubrique n'a pas de période : une créance est due AUJOURD'HUI, pas
    « sur les trente derniers jours ». Le document annonce donc son arrêté.
    """
    data = viewset._receivables_data(request, ctx.organization)

    rows = [
        {
            'currency': debiteur['currency'],
            'customer_name': debiteur['customer_name'],
            'customer_phone': debiteur['customer_phone'] or '-',
            'invoice_count': debiteur['invoice_count'],
            'oldest_days': debiteur['oldest_days'] or None,
            'amount_due': debiteur['amount_due'],
            'overdue_amount': debiteur['overdue_amount'] or None,
        }
        # Groupés par devise, et triés par montant DANS chaque devise : un
        # ordre inter-devises classerait 50 000 FC (dix-huit dollars) au-dessus
        # de 3 000 $, et le marchand relance dans l'ordre de la liste.
        for debiteur in sorted(
            data['by_customer'],
            key=lambda d: (d['currency'], -d['amount_due']),
        )
    ]

    # L'arrêté est déjà en tête, dans les filtres : le répéter en synthèse
    # ferait croire à deux dates.
    extra = [
        ('Factures dues', str(data['invoice_count'])),
        ('Débiteurs', str(data['debtor_count'])),
        (f"Total dû ({data['primary_currency']})", ctx.money(data['total_primary'])),
        (f"Total échu ({data['primary_currency']})", ctx.money(data['overdue_primary'])),
    ]
    for devise in data['by_currency']:
        code = devise['currency']
        decimales = currency_decimals(code)
        for cle, libelle in LIBELLES_TRANCHES.items():
            extra.append((
                f"{code} · {libelle}",
                format_number(devise[cle], decimales),
            ))

    spec = _spec(
        ctx, onglet='Créances', section='Balance âgée',
        columns=RECEIVABLES_COLUMNS, rows=rows, extra_summary=extra,
        empty='Aucune facture due à ce jour.',
        totals=('invoice_count', 'amount_due', 'overdue_amount'),
    )
    spec.group_by = 'currency'
    spec.group_label = 'Devise'
    spec.grand_total = False
    # Chaque ligne s'écrit avec les décimales de SA devise : 120,75 USD ne doit
    # pas sortir « 121 » parce que l'établissement est tenu en francs.
    spec.currency_field = 'currency'

    # Cette rubrique a sa PROPRE fenêtre : un arrêté, pas une période. Elle est
    # donc dans `TABS_SANS_RELEVES`, et `spec.summary` ne porte que `extra`.
    spec.filters_applied = [('Arrêté au', format_day(data['as_of']))]
    spec.subtitle = f"Factures encore dues au {format_day(data['as_of'])}"
    return spec


# --------------------------------------------------------------------------
# Le croisement de l'onglet « Produits »
# --------------------------------------------------------------------------

def _quantity_cell(row) -> str:
    """
    Une quantité en mots, sa lecture au total jointe entre parenthèses.

    La sous-ligne que les deux écrans affichent sous la quantité rejoint sa
    cellule : un tableau n'a pas de deuxième ligne, et une colonne de plus pour
    « 658 au total » séparerait deux nombres qui se lisent ensemble.
    """
    display = (row.get('quantity_display') or '').strip()
    quantity = row.get('quantity_sold')
    if not display:
        return format_quantity(quantity)
    if row.get('packaging_factor') is not None:
        return f"{display} ({format_quantity(quantity)} au total)"
    return display


def build_product_rows(viewset, request, ctx: TabContext) -> list:
    """
    « Détails des produits vendus » : le croisement de trois rubriques.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE RESTANT EST SOMMÉ PAR PRODUIT, ET C'EST UN CORRECTIF.                 │
    │                                                                          │
    │ Le back-office faisait `stockDetails.find(d => d.product_id === ...)` et  │
    │ retenait donc la PREMIÈRE ligne de stock ; or `_stock_detail_rows` en     │
    │ rend une par couple (produit, entrepôt). Sur un établissement à plusieurs │
    │ dépôts, « Qté restante » et « Valeur restante » ignoraient tous les       │
    │ autres, sans erreur et sans que rien ne le signale.                       │
    │                                                                          │
    │ La lecture en contenants, elle, ne se somme PAS : c'est un partage        │
    │ scellé/vrac lu sur UNE ligne de stock. Quand un produit s'étale sur       │
    │ plusieurs dépôts, on retombe donc sur le total en unités - un chiffre     │
    │ vrai plutôt qu'un partage inventé.                                        │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    supplies = viewset._product_supplies_map(
        request, ctx.organization, ctx.start_date, ctx.end_date
    )

    par_produit: dict = {}
    for ligne in viewset._stock_detail_rows(request, ctx.organization, None):
        cle = str(ligne['product_id'])
        agrege = par_produit.setdefault(
            cle, {'quantity': ZERO, 'value': ZERO, 'display': None, 'lignes': 0}
        )
        agrege['quantity'] += Decimal(str(ligne['current_stock']))
        agrege['value'] += Decimal(str(ligne['stock_value']))
        agrege['display'] = ligne['stock_display']
        agrege['lignes'] += 1

    rows = []
    for item in viewset._top_products_values(
        request, ctx.organization, ctx.start_date, ctx.end_date
    ):
        vendu = viewset._top_product_row(item)
        cle = str(vendu['product_id'])
        stock = par_produit.get(cle)
        restant = stock['quantity'] if stock else ZERO
        # La reconstitution que les deux écrans affichent déjà : le partage
        # scellé/vrac d'alors n'est enregistré nulle part.
        depart = restant + Decimal(str(vendu['quantity_sold']))
        appro = supplies.get(cle)

        if stock and stock['lignes'] == 1 and stock['display']:
            restant_lisible = stock['display']
        else:
            restant_lisible = format_quantity(restant)

        rows.append({
            'product_name': vendu['product_name'],
            'opening_stock': depart,
            'supplied': (appro['display'] or format_quantity(appro['quantity']))
                        if appro and appro['quantity'] > 0 else '-',
            'quantity_display': _quantity_cell(vendu),
            'total_revenue': vendu['total_revenue'],
            'remaining_display': restant_lisible,
            'stock_value': stock['value'] if stock else ZERO,
        })
    return rows


# --------------------------------------------------------------------------
# Le registre
# --------------------------------------------------------------------------

#: Un REGISTRE, pas une chaîne de `if` : c'est ce qui rend le croisement avec
#: les deux écrans mécanique, et c'est la leçon des chemins d'API, écrits au fil
#: des appels et qui répondaient 404 sept fois sur huit.
TAB_BUILDERS = {
    'overview': build_overview_spec,
    'daily-cash': build_daily_cash_spec,
    'sales': build_sales_spec,
    'products': build_products_spec,
    'customers': build_customers_spec,
    'stock': build_stock_spec,
    'profits': build_profits_spec,
    'user-activity': build_user_activity_spec,
    # Les créances ne sont pas un onglet de la page mais une rubrique à elles
    # seules : elles passent par le MÊME registre pour porter la même marque.
    'receivables': build_receivables_spec,
}


# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ CES RUBRIQUES ONT LEUR PROPRE FENÊTRE, ET NE PORTENT DONC PAS CELLE DE LA    │
# │ PAGE.                                                                        │
# │                                                                              │
# │ Une créance est due AUJOURD'HUI ; le rapport journalier porte UNE journée.    │
# │ Y laisser les dix relevés globaux mettrait « Chiffre d'affaires 560 740,70 »  │
# │ - trente jours - sous un en-tête qui annonce un arrêté ou une date, et le     │
# │ lecteur n'aurait aucun moyen de savoir sur quoi ce chiffre porte. Un chiffre  │
# │ sans sa fenêtre ne se lit pas.                                                │
# │                                                                              │
# │ La déclaration est ICI, à côté du registre, et non dans chaque constructeur : │
# │ le journalier avait remplacé son périmètre affiché sans retirer les relevés,  │
# │ et rien ne rapprochait les deux moitiés du correctif.                         │
# └──────────────────────────────────────────────────────────────────────────────┘
TABS_SANS_RELEVES = frozenset({'receivables', 'daily-cash'})


def build_tab_spec(viewset, request, tab: str) -> ReportSpec:
    """
    Décrit l'onglet demandé.

    Un onglet inconnu est un refus DÉTERMINISTE qui NOMME ce qu'il attend :
    sans ce garde-fou, `TAB_BUILDERS[tab]` lèverait `KeyError` et le serveur
    répondrait 500 pour une faute de frappe de l'appelant.
    """
    from rest_framework.exceptions import ValidationError

    builder = TAB_BUILDERS.get(tab)
    if builder is None:
        raise ValidationError({
            'tab': "Onglet inconnu. Attendu : " + ', '.join(sorted(TAB_BUILDERS)) + '.',
        })
    ctx = build_tab_context(
        viewset, request, avec_releves=tab not in TABS_SANS_RELEVES
    )
    return builder(viewset, request, ctx)
