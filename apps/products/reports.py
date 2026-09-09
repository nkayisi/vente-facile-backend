"""
Le catalogue produits, décrit pour l'export.

┌──────────────────────────────────────────────────────────────────────────────┐
│ IL DESSINAIT SON PROPRE PDF, ET SON PROPRE CLASSEUR.                        │
│                                                                              │
│ `services.py` ouvrait un `SimpleDocTemplate` reportlab et un `Workbook`      │
│ openpyxl à la main, avec ses styles, ses largeurs et son bandeau d'identité, │
│ servis par DEUX endpoints séparés (`/export/excel/`, `/export/pdf/`) qui     │
│ n'acceptaient pas `export_format` et ne rendaient pas de CSV. Le même        │
│ catalogue sortait donc sous une autre marque que les autres rapports du      │
│ produit, et personne ne pouvait s'en apercevoir sans mettre deux fichiers    │
│ côte à côte.                                                                 │
│                                                                              │
│ Et surtout : il IGNORAIT LES FILTRES DE L'ÉCRAN. On filtrait sur une         │
│ catégorie, on exportait, et on recevait tout l'établissement.                │
└──────────────────────────────────────────────────────────────────────────────┘

Les colonnes sont celles du PDF d'origine : le sous-ensemble lisible des seize
du classeur. Les prix voyagent BRUTS en `KIND_MONEY`, ce qui les rend sommables
dans le tableur - l'ancien export les y écrivait en texte.
"""
from apps.core.exports import (
    KIND_MEASURE,
    KIND_MONEY,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
)

PRODUCT_COLUMNS = [
    ReportColumn('name', 'Produit', 44, KIND_TEXT),
    ReportColumn('sku', 'SKU', 24, KIND_TEXT),
    ReportColumn('category', 'Catégorie', 26, KIND_TEXT),
    ReportColumn('brand', 'Marque', 22, KIND_TEXT),
    ReportColumn('cost_price', 'Achat détail', 22, KIND_MONEY),
    ReportColumn('selling_price', 'Vente détail', 22, KIND_MONEY),
    ReportColumn('package_cost_price', 'Achat gros', 22, KIND_MONEY),
    ReportColumn('wholesale_price', 'Vente gros', 22, KIND_MONEY),
    ReportColumn('stock_display', 'Stock', 34, KIND_MEASURE),
    ReportColumn('is_active', 'Actif', 14, KIND_TEXT),
]


def build_product_catalog_report(
    queryset, organization, *, currency='CDF', filters_applied=(),
) -> ReportSpec:
    """
    Décrit le catalogue, sur le périmètre DÉJÀ filtré que la vue lui passe.

    Le queryset arrive filtré et scopé par `ExportableListMixin` : cette
    fonction ne décide de rien, elle met en forme. C'est la règle de tous les
    constructeurs de `apps/*/reports.py`.
    """
    from apps.products.services import ProductExcelService

    rows = [
        ProductExcelService._product_export_row(produit)
        for produit in queryset.select_related(
            'category', 'brand', 'unit', 'packaging_unit'
        ).prefetch_related('stocks')
    ]

    actifs = sum(1 for r in rows if r['is_active'] == 'Oui')
    summary = [
        ('Produits', str(len(rows))),
        ('Actifs', str(actifs)),
        ('Inactifs', str(len(rows) - actifs)),
    ]

    return ReportSpec(
        title='Catalogue des produits',
        organization=organization,
        columns=PRODUCT_COLUMNS,
        rows=rows,
        subtitle='Prix et stock par article',
        filters_applied=filters_applied,
        summary=tuple(summary),
        currency=currency,
        landscape_mode=True,
        # ⚠ AUCUN TOTAL DE COLONNE ICI, et ce n'est pas un oubli : additionner
        # les prix de vente de cent articles rend un nombre qui ne désigne
        # rien. Le stock, lui, mêle des unités différentes (des bouteilles et
        # des casiers), ce que le socle interdit déjà de sommer.
        group_totals=(),
        empty_message='Aucun produit ne correspond aux critères retenus.',
    )
