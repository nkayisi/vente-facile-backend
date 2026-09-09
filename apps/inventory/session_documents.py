"""
Les deux documents d'une session d'inventaire, décrits pour l'export.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ILS ÉTAIENT DESSINÉS DANS LE NAVIGATEUR.                                    │
│                                                                              │
│ `inventory/[id]/page.tsx` montait la FICHE et le RAPPORT en jsPDF, à partir  │
│ d'un `print-data/` qui rendait du JSON. PDF seul, styles et bandeau tenus en │
│ phase à la main avec ceux du serveur.                                        │
└──────────────────────────────────────────────────────────────────────────────┘

Deux documents, deux usages qu'il ne faut pas confondre :

- **La FICHE** s'imprime pour être remplie AU STYLO, au fond d'un dépôt. Elle
  est groupée par catégorie (on compte rayon par rayon), et une ligne non
  comptée y porte des tirets, pas un zéro : « 0 compté » affirmerait qu'on a
  regardé et trouvé vide.
- **Le RAPPORT** ne garde que les ÉCARTS : c'est ce qu'on relit après coup, et
  noyer trente lignes justes autour de deux manquants les rend invisibles.
"""
from decimal import Decimal

from apps.core.exports import (
    KIND_MEASURE,
    KIND_MONEY,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
)

ZERO = Decimal('0')

#: Ce qu'une case à remplir au stylo porte sur le papier.
#:
#: Des tirets et non « 0 » : un zéro affirmerait qu'on a compté et trouvé vide,
#: quand la ligne n'a simplement pas encore été visitée.
A_REMPLIR = '___________'

SHEET_COLUMNS = [
    ReportColumn('product_name', 'Produit', 46, KIND_TEXT),
    ReportColumn('sku', 'SKU', 24, KIND_TEXT),
    ReportColumn('expected', 'Stock système', 28, KIND_MEASURE),
    ReportColumn('counted', 'Compté', 28, KIND_MEASURE),
    ReportColumn('difference', 'Écart', 26, KIND_MEASURE),
    ReportColumn('notes', 'Notes', 40, KIND_TEXT),
]

REPORT_COLUMNS = [
    ReportColumn('product_name', 'Produit', 44, KIND_TEXT),
    ReportColumn('sku', 'SKU', 22, KIND_TEXT),
    ReportColumn('category', 'Catégorie', 26, KIND_TEXT),
    ReportColumn('expected', 'Stock système', 26, KIND_MEASURE),
    ReportColumn('counted', 'Compté', 26, KIND_MEASURE),
    ReportColumn('difference', 'Écart', 24, KIND_MEASURE),
    ReportColumn('difference_value', 'Valeur écart', 26, KIND_MONEY),
]

DOCUMENT_BASENAMES = {
    'sheet': 'fiche_inventaire',
    'report': 'rapport_inventaire',
}


def _lecture(count):
    """
    Les trois quantités d'une ligne, dans les mots du magasinier.

    Les trois passent par les helpers des serializers : comptages d'inventaire
    et lignes d'ajustement enregistrent exactement les mêmes quatre nombres, et
    le même produit compté deux fois ne doit pas donner deux formulations. Les
    réécrire ici en serait une troisième.

    Le facteur est celui FIGÉ sur la ligne, comme pour l'écart : voir
    `_expected_split`, qui porte le motif.
    """
    from apps.inventory.packaging import PackagingService
    from apps.inventory.serializers import _difference_display, _expected_split

    produit = count.product
    partage = _expected_split(
        produit,
        count.quantity_expected,
        count.expected_loose_quantity,
        count.packaging_factor,
    )

    attendu = (
        PackagingService.format_split(produit, *partage)
        if partage
        else PackagingService.format_quantity(produit, count.quantity_expected)
    )

    if not count.is_counted:
        return attendu, A_REMPLIR, A_REMPLIR

    compte = (
        PackagingService.format_split(
            produit, count.counted_package_quantity, count.counted_loose_quantity
        )
        if partage
        else PackagingService.format_quantity(produit, count.quantity_counted)
    )
    ecart = _difference_display(
        produit,
        factor=count.packaging_factor,
        expected_base=count.quantity_expected,
        expected_loose=count.expected_loose_quantity,
        counted_packages=count.counted_package_quantity,
        counted_loose=count.counted_loose_quantity,
        base_delta=count.quantity_difference,
    )
    return attendu, compte, ecart


def _synthese(session) -> list:
    return [
        ('Référence', session.reference),
        ('Entrepôt', session.warehouse.name if session.warehouse_id else '-'),
        ('Statut', session.get_status_display()),
        ('Produits', str(session.items_total)),
        ('Comptés', str(session.items_counted)),
        ('Avec écart', str(session.items_with_difference)),
    ]


def build_inventory_sheet(session, counts, organization, *, currency) -> ReportSpec:
    """La feuille de comptage, à remplir au stylo, groupée par rayon."""
    rows = []
    for count in counts:
        attendu, compte, ecart = _lecture(count)
        rows.append({
            'category': (
                count.product.category.name
                if count.product.category_id else 'Sans catégorie'
            ),
            'product_name': count.product.name,
            'sku': count.product.sku or '',
            'expected': attendu,
            'counted': compte,
            'difference': ecart,
            'notes': count.notes or '',
        })
    # `_grouped` avance en constatant les changements de clé : les lignes
    # doivent être triées par catégorie, sinon un rayon rouvrirait à chaque
    # produit intercalé.
    rows.sort(key=lambda r: (r['category'], r['product_name']))

    return ReportSpec(
        title="Fiche d'inventaire",
        organization=organization,
        columns=SHEET_COLUMNS,
        rows=rows,
        subtitle=f"{session.reference} - à remplir puis à saisir",
        filters_applied=[
            ('Entrepôt', session.warehouse.name if session.warehouse_id else '-'),
            ('Statut', session.get_status_display()),
        ],
        summary=tuple(_synthese(session)),
        group_by='category',
        group_label='Rayon',
        # Aucun total : les lignes mêlent des unités différentes (des casiers
        # et des bouteilles), et le socle interdit déjà de les sommer.
        group_totals=(),
        currency=currency,
        landscape_mode=True,
        signatures=('Signature compteur', 'Signature responsable'),
        empty_message="Aucune ligne de comptage : la feuille est engendrée au démarrage.",
    )


def build_inventory_report(session, counts, organization, *, currency) -> ReportSpec:
    """Le rapport : les ÉCARTS constatés, et eux seuls."""
    rows = []
    for count in counts:
        if not count.is_counted or count.quantity_difference == ZERO:
            continue
        attendu, compte, ecart = _lecture(count)
        rows.append({
            'product_name': count.product.name,
            'sku': count.product.sku or '',
            'category': (
                count.product.category.name
                if count.product.category_id else 'Sans catégorie'
            ),
            'expected': attendu,
            'counted': compte,
            'difference': ecart,
            'difference_value': count.difference_value,
        })

    synthese = _synthese(session)
    synthese.append(('Valeur des écarts', str(session.total_difference_value)))

    return ReportSpec(
        title="Rapport d'inventaire",
        organization=organization,
        columns=REPORT_COLUMNS,
        rows=rows,
        # Le détail complet est la FICHE : le répéter ici ferait deux documents
        # en un, et noierait les écarts au milieu des lignes justes.
        subtitle=(
            f"{session.reference} - écarts constatés ; "
            "le détail complet est sur la fiche"
        ),
        filters_applied=[
            ('Entrepôt', session.warehouse.name if session.warehouse_id else '-'),
            ('Statut', session.get_status_display()),
        ],
        summary=tuple(synthese),
        group_totals=('difference_value',),
        currency=currency,
        landscape_mode=True,
        signatures=('Établi par', 'Validé par'),
        empty_message='Aucun écart constaté : le comptage est conforme au stock.',
    )
