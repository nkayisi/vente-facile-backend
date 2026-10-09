"""
Le document imprimé d'une réquisition.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ELLE S'IMPRIME PARCE QU'ON L'EMPORTE.                                       │
│                                                                              │
│ Une réquisition approuvée s'emporte au marché : c'est du papier qu'on tient  │
│ d'une main en comparant les prix de l'autre. T12 le demande explicitement -  │
│ « enregistre la demande et l'imprime » - et c'est aussi ce qui justifie que  │
│ son numéro soit définitif dès l'impression.                                  │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ IL LIT L'INSTANTANÉ, JAMAIS LE STOCK DU JOUR. `stock_at_request` et
`reorder_point_at_request` ont été figés à la saisie : les relire dans `Stock`
donnerait le stock d'aujourd'hui, qui a bougé depuis, et le papier ne dirait
plus pourquoi la demande a été faite.
"""
from decimal import Decimal

from apps.core.exports import (
    KIND_MEASURE,
    KIND_NUMBER,
    KIND_TEXT,
    ReportColumn,
    ReportSpec,
)
from apps.inventory.packaging import PackagingService

#: Le radical du nom de fichier servi.
DOCUMENT_BASENAMES = {'requisition': 'requisition'}

COLUMNS = [
    ReportColumn('product_name', 'Article', 58, KIND_TEXT),
    ReportColumn('sku', 'Code', 26, KIND_TEXT),
    ReportColumn('stock', 'En stock', 30, KIND_MEASURE),
    ReportColumn('reorder_point', 'Seuil', 26, KIND_MEASURE),
    ReportColumn('quantity', 'À acheter', 30, KIND_MEASURE),
    # ⚠ Une colonne VIDE, et c'est délibéré : le magasinier y écrit le prix
    # qu'on lui annonce au marché. C'est la même intention que la feuille
    # d'inventaire, qu'on remplit au stylo.
    ReportColumn('observed_price', 'Prix constaté', 32, KIND_TEXT),
    ReportColumn('notes', 'Note', 40, KIND_TEXT),
]


def build_requisition_document(requisition, items, organization, *, currency) -> ReportSpec:
    """La réquisition, telle qu'on l'emporte."""
    rows = []
    for item in items:
        produit = item.product
        rows.append({
            'product_name': produit.name,
            'sku': produit.sku or '',
            # Les quantités s'écrivent en unité de détail NOMMÉE (« 5 BOITES »),
            # jamais en nombre nu : « 5 » ne dit pas 5 quoi, et c'est au marché
            # que la question se pose.
            'stock': PackagingService.format_base_total(produit, item.stock_at_request),
            'reorder_point': PackagingService.format_base_total(produit, item.reorder_point_at_request),
            'quantity': PackagingService.format_base_total(produit, item.quantity_requested),
            'observed_price': '',
            'notes': item.notes or '',
        })

    demandeur = requisition.requested_by
    nom_demandeur = (
        f"{demandeur.first_name} {demandeur.last_name}".strip() or demandeur.email
        if demandeur else '-'
    )

    synthese = [
        ('Articles', str(len(rows))),
        ('Statut', requisition.get_status_display()),
        ('Demandé par', nom_demandeur),
    ]
    if requisition.decided_at:
        decideur = requisition.decided_by
        synthese.append((
            'Décidé par',
            (f"{decideur.first_name} {decideur.last_name}".strip() or decideur.email)
            if decideur else '-',
        ))

    return ReportSpec(
        title='Réquisition de produits',
        organization=organization,
        columns=COLUMNS,
        rows=rows,
        subtitle=f"{requisition.reference} - à emporter",
        filters_applied=[
            ('Entrepôt', requisition.warehouse.name if requisition.warehouse_id else '-'),
            ('Statut', requisition.get_status_display()),
        ],
        summary=tuple(synthese),
        # Aucun total de colonne : les lignes mêlent des unités différentes -
        # des casiers et des bouteilles - et le socle interdit de les sommer.
        currency=currency,
        landscape_mode=True,
        signatures=('Signature demandeur', 'Signature responsable'),
        empty_message="Aucun article : cette réquisition ne demande rien.",
    )
