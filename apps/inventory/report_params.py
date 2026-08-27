"""
Traduction des paramètres de requête en contexte de rapport.

Un export doit rappeler sur quoi il porte : « Catégorie : Boissons », « Période :
août 2026 ». Les identifiants techniques (UUID d'entrepôt, code de type de
mouvement) ne disent rien à qui lit le document une fois imprimé, il faut donc
les résoudre en libellés. Ce module concentre cette résolution pour que les trois
actions d'export ne la réécrivent pas chacune.
"""
from datetime import datetime

MONTH_NAMES = [
    'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
    'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre',
]

STATUS_LABELS = {
    'out': 'En rupture',
    'low': 'Stock bas',
    'available': 'Disponible',
    'reserved': 'Avec réservation',
}

SOURCE_LABELS = {
    'all': 'Toutes les entrées',
    'receipts': 'Réceptions fournisseur uniquement',
}


def _format_day(value):
    """Rend une date ISO au format francophone, ou la valeur brute si illisible."""
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime('%d/%m/%Y')
    except (TypeError, ValueError):
        return value


def month_label(value):
    """« 2026-08 » devient « août 2026 »."""
    try:
        year, month = (int(part) for part in str(value).split('-')[:2])
        return f"{MONTH_NAMES[month - 1]} {year}"
    except (TypeError, ValueError, IndexError):
        return str(value)


def period_label(params):
    """
    Libellé de la période couverte, dans l'ordre de priorité des paramètres.

    Un rapport sans période affichée laisse le lecteur deviner s'il regarde le
    mois, l'année, ou tout l'historique : la mention est obligatoire.
    """
    if params.get('month'):
        return month_label(params['month'])

    date_from = params.get('date_from')
    date_to = params.get('date_to')
    if date_from and date_to:
        return f"du {_format_day(date_from)} au {_format_day(date_to)}"
    if date_from:
        return f"à partir du {_format_day(date_from)}"
    if date_to:
        return f"jusqu'au {_format_day(date_to)}"
    return 'Tout l\'historique'


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
