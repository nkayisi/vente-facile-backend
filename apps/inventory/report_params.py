"""
Traduction des paramètres de requête en contexte de rapport.

Un export doit rappeler sur quoi il porte : « Catégorie : Boissons », « Période :
août 2026 ». Les identifiants techniques (UUID d'entrepôt, code de type de
mouvement) ne disent rien à qui lit le document une fois imprimé, il faut donc
les résoudre en libellés. Ce module concentre cette résolution pour que les trois
actions d'export ne la réécrivent pas chacune.
"""
# Les aides GÉNÉRIQUES vivent dans `apps/core/report_params` : une période se
# libelle pareil sur un rapport de ventes et sur un journal de stock. Elles y
# étaient réexportées ici le temps que les appelants migrent ; ils l'ont tous
# fait (ventes, caisse, rapports), et la réexportation n'avait plus de
# consommateur. On n'importe donc plus que ce que ce module emploie lui-même.
from apps.core.report_params import period_label

STATUS_LABELS = {
    'out': 'En rupture',
    'low': 'Stock bas',
    'available': 'Disponible',
    'reserved': 'Avec réservation',
    # Les deux états exclusifs du terminal. Sans leur libellé, l'en-tête du
    # document afficherait le code nu, qui ne renseigne personne.
    'low_only': 'Stock bas',
    'healthy': 'En stock',
}

SOURCE_LABELS = {
    'all': 'Toutes les entrées',
    'receipts': 'Réceptions fournisseur uniquement',
}


def _warehouse_label(organization, warehouse_id):
    from .models import Warehouse

    return (
        Warehouse.objects.filter(organization=organization, id=warehouse_id)
        .values_list('name', flat=True)
        .first()
    )


def _category_label(organization, category_id):
    from apps.products.models import Category

    return (
        Category.objects.filter(organization=organization, id=category_id)
        .values_list('name', flat=True)
        .first()
    )


def _movement_type_labels(codes):
    from .models import StockMovement

    labels = dict(StockMovement.MovementType.choices)
    return ', '.join(
        labels.get(code.strip(), code.strip())
        for code in codes.split(',') if code.strip()
    )


def build_export_context(request, organization, *, include_period=False):
    """
    Renvoie ``(filters_applied, period)`` prêts à poser dans une ``ReportSpec``.

    Les identifiants invalides sont ignorés silencieusement : le queryset les a
    déjà écartés, et faire échouer un export pour un libellé manquant serait
    disproportionné.
    """
    params = request.query_params
    applied = []

    warehouse_id = params.get('warehouse')
    if warehouse_id:
        name = _warehouse_label(organization, warehouse_id)
        applied.append(('Entrepôt', name or 'inconnu'))
    else:
        applied.append(('Entrepôt', 'Tous'))

    category_id = params.get('category')
    if category_id:
        name = _category_label(organization, category_id)
        # La mention du sous-arbre évite qu'on croie à un total de la seule
        # catégorie choisie, alors que ses sous-catégories y sont comptées.
        applied.append(('Catégorie', f"{name or 'inconnue'} (sous-catégories incluses)"))
    else:
        applied.append(('Catégorie', 'Toutes'))

    if params.get('status'):
        applied.append(('État', STATUS_LABELS.get(params['status'], params['status'])))

    if params.get('movement_type'):
        applied.append(('Type', _movement_type_labels(params['movement_type'])))

    if params.get('source'):
        applied.append(('Source', SOURCE_LABELS.get(params['source'], params['source'])))

    if params.get('search'):
        applied.append(('Recherche', params['search']))

    period = ''
    if include_period:
        period = period_label(params)
        applied.insert(0, ('Période', period))

    return tuple(applied), period
