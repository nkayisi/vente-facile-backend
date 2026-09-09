"""
Les quatre rapports de caisse, décrits pour l'export.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ILS ÉTAIENT DESSINÉS DANS LE NAVIGATEUR.                                    │
│                                                                              │
│ `cashbook/reports/page.tsx` montait quatre PDF en jsPDF (`printDailyReport`, │
│ `printMonthlyReport`, `printAnnualReport`, `printCustomReport`), avec ses    │
│ propres styles et son propre bandeau. PDF seul, aucun classeur, aucun CSV,   │
│ et une marque tenue en phase à la main avec celle du serveur.                │
│                                                                              │
│ ⚠ Et le journalier ne mettait dans son tableau que la PAGE affichée          │
│ (`movements.results` est paginé), sous une synthèse qui annonçait toute la   │
│ journée : le document mentait sur son propre contenu, comme les huit onglets │
│ le faisaient avant leur reprise.                                             │
└──────────────────────────────────────────────────────────────────────────────┘

Un seul endpoint sert les quatre : ils ne diffèrent que par leur pas de temps.
Quatre actions donneraient quatre fois la même plomberie, et c'est là que les
écarts naissent.
"""
from decimal import Decimal

from apps.core.exports import (
    KIND_MONEY,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
    currency_decimals,
    format_number,
)
from apps.core.report_params import format_day, month_label

ZERO = Decimal('0.00')

DAILY_COLUMNS = [
    ReportColumn('time', 'Heure', 18, KIND_TEXT),
    ReportColumn('type', 'Type', 30, KIND_TEXT),
    ReportColumn('description', 'Description', 52, KIND_TEXT),
    ReportColumn('currency', 'Devise', 16, KIND_TEXT),
    ReportColumn('income', 'Entrée', 26, KIND_MONEY),
    ReportColumn('outcome', 'Sortie', 26, KIND_MONEY),
]

PERIOD_COLUMNS = [
    ReportColumn('period', 'Période', 34, KIND_TEXT),
    ReportColumn('currency', 'Devise', 18, KIND_TEXT),
    ReportColumn('income', 'Entrées', 30, KIND_MONEY),
    ReportColumn('outcome', 'Sorties', 30, KIND_MONEY),
    ReportColumn('net', 'Net', 30, KIND_MONEY),
]

#: Les intitulés des quatre portées, tels que les onglets de l'écran les nomment.
SCOPE_TITLES = {
    'daily': 'Rapport journalier de caisse',
    'monthly': 'Rapport mensuel de caisse',
    'annual': 'Rapport annuel de caisse',
    'custom': 'Rapport de caisse personnalisé',
}

SCOPE_BASENAMES = {
    'daily': 'rapport_journalier_caisse',
    'monthly': 'rapport_mensuel_caisse',
    'annual': 'rapport_annuel_caisse',
    'custom': 'rapport_caisse_personnalise',
}


def _synthese_par_devise(by_currency) -> list:
    """
    Ouverture, entrées, sorties et clôture, DEVISE PAR DEVISE.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN TIROIR CONTIENT DES LIASSES DISTINCTES.                              │
    │                                                                          │
    │ C'est la règle déjà posée sur la clôture Z et sur la balance âgée :      │
    │ additionner des francs et des dollars rend un nombre qui n'existe pas.   │
    │ Chaque ligne porte donc son code, et ses PROPRES décimales - le CDF n'en │
    │ a aucune, le dollar en a deux.                                           │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    lignes = []
    for row in by_currency:
        code = row['currency']
        d = currency_decimals(code)
        for cle, libelle in (
            ('opening_balance', 'Ouverture'),
            ('total_in', 'Entrées'),
            ('total_out', 'Sorties'),
            ('closing_balance', 'Clôture'),
        ):
            lignes.append((f"{code} · {libelle}", format_number(row.get(cle) or ZERO, d)))
    return lignes


def build_daily_cash_report(
    organization, *, libelle, movements, by_currency, currency,
) -> ReportSpec:
    """Le journal du jour, mouvement par mouvement, sur la JOURNÉE ENTIÈRE."""
    rows = [
        {
            'time': m.movement_date.strftime('%H:%M') if m.movement_date else '',
            'type': m.get_movement_type_display(),
            'description': m.description or m.reference or '-',
            'currency': m.currency,
            # Deux colonnes plutôt qu'un montant signé : une cellule vide dit
            # « ce n'en est pas une », là où un signe demande d'être lu.
            'income': m.amount if m.direction == 'in' else None,
            'outcome': m.amount if m.direction == 'out' else None,
        }
        for m in movements
    ]
    # ⚠ TRIÉES PAR DEVISE D'ABORD. `_grouped` avance en constatant les
    # changements de clé : des lignes en ordre chronologique rouvriraient un
    # groupe « USD » chaque fois qu'un mouvement en francs s'intercale, et le
    # document porterait trois sous-totaux pour deux devises. La chronologie
    # est conservée À L'INTÉRIEUR de chaque devise.
    rows.sort(key=lambda r: (r['currency'], r['time']))
    return _spec(
        organization, 'daily', libelle, DAILY_COLUMNS, rows, by_currency, currency,
        vide='Aucun mouvement de caisse pour cette date.',
    )


def build_period_cash_report(
    organization, *, scope, libelle, buckets, by_currency, currency, par_mois=False,
) -> ReportSpec:
    """Le cumul par jour (mensuel, personnalisé) ou par mois (annuel)."""
    rows = []
    for b in buckets:
        borne = b.get('month') if par_mois else b.get('day')
        if borne is None:
            etiquette = '-'
        elif par_mois:
            etiquette = month_label(borne.strftime('%Y-%m'))
        else:
            etiquette = format_day(borne.strftime('%Y-%m-%d'))
        entrees = b.get('total_in') or ZERO
        sorties = b.get('total_out') or ZERO
        rows.append({
            'period': etiquette,
            'currency': b['currency'],
            'income': entrees,
            'outcome': sorties,
            'net': entrees - sorties,
            '_ordre': borne,
        })
    # Même raison que pour le journalier : le groupe est la devise, donc le tri
    # l'est aussi. Le pas de temps ordonne à l'intérieur.
    rows.sort(key=lambda r: (r['currency'], r['_ordre'] or ''))
    for r in rows:
        r.pop('_ordre', None)
    return _spec(
        organization, scope, libelle, PERIOD_COLUMNS, rows, by_currency, currency,
        vide='Aucun mouvement de caisse sur cette période.',
    )


def _spec(organization, scope, libelle, columns, rows, by_currency, currency, *, vide):
    """Assemble le document, dans les termes des quatre onglets."""
    return ReportSpec(
        title=SCOPE_TITLES[scope],
        organization=organization,
        columns=columns,
        rows=rows,
        subtitle=libelle,
        filters_applied=[('Période', libelle)],
        summary=tuple(_synthese_par_devise(by_currency)),
        currency=currency,
        # Chaque ligne s'écrit avec les décimales de SA devise : un tiroir qui
        # tient des dollars dans un établissement en francs ne doit pas voir
        # ses centimes disparaître.
        currency_field='currency',
        group_by='currency',
        group_label='Devise',
        group_totals=('income', 'outcome', 'net'),
        # Sous-totaux par devise, mais AUCUN total général : il additionnerait
        # des monnaies.
        grand_total=False,
        landscape_mode=True,
        signatures=('Caissier', 'Responsable'),
        empty_message=vide,
    )
