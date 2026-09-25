"""
Ce qu'un rapport rappelle sur lui-même, quelle que soit la rubrique.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CES TROIS AIDES VIVAIENT DANS `apps/inventory`, ET N'Y AVAIENT RIEN À FAIRE. │
│                                                                              │
│ « du 03/08/2026 au 01/09/2026 » n'a rien d'un vocabulaire de stock : c'est ce │
│ que TOUT rapport doit écrire en tête. Les laisser là obligeait les ventes -   │
│ et demain les achats - à importer depuis l'inventaire, ou pire, à réécrire    │
│ leur propre version. C'est ainsi qu'on obtient deux formats de date sur deux  │
│ documents de la même liasse.                                                 │
│                                                                              │
│ `apps/inventory/report_params.py` les réexporte : ses appelants ne changent   │
│ pas, et son vocabulaire propre (entrepôt, catégorie, type de mouvement) y     │
│ reste, puisqu'il n'appartient qu'à lui.                                       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import datetime

MONTH_NAMES = [
    'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
    'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre',
]


def format_day(value):
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
        return f"du {format_day(date_from)} au {format_day(date_to)}"
    if date_from:
        return f"à partir du {format_day(date_from)}"
    if date_to:
        return f"jusqu'au {format_day(date_to)}"
    return "Tout l'historique"


# --------------------------------------------------------------------------
# Ce qu'un rapport accepte comme fenêtre
# --------------------------------------------------------------------------

# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ UNE SAISIE FAUTIVE SE REFUSE, ELLE NE RESSEMBLE PAS À UNE PANNE.             │
# │                                                                              │
# │ `format_day` ne valide RIEN : il rend la valeur inchangée quand elle est      │
# │ illisible, par choix - un libellé n'a pas à décider de la validité d'une      │
# │ requête. C'est ce qui faisait crever la requête bien plus loin, à la          │
# │ frontière de l'ORM : `?date=oops` sur un export de caisse remontait en 500,   │
# │ et `?month=13` aussi, ni `ValueError` ni la `ValidationError` de Django       │
# │ n'étant une `APIException` que DRF sache traduire.                            │
# │                                                                              │
# │ Le refus NOMME le champ fautif, faute de quoi l'appelant reçoit « Requête     │
# │ invalide » et doit deviner lequel de ses trois paramètres est en cause.       │
# └──────────────────────────────────────────────────────────────────────────────┘

def parse_day(value, *, champ='date'):
    """Une date de rapport (« AAAA-MM-JJ »), ou un refus nommant le champ."""
    from rest_framework.exceptions import ValidationError

    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValidationError({champ: "Date attendue au format AAAA-MM-JJ."})


def _entier_borne(value, champ, mini, maxi, message):
    """Un entier dans ses bornes, ou un refus nommant le champ."""
    from rest_framework.exceptions import ValidationError

    try:
        nombre = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError({champ: message})
    if not mini <= nombre <= maxi:
        raise ValidationError({champ: message})
    return nombre


def parse_year(value, defaut: int, *, champ='year') -> int:
    """
    L'année demandée, ou celle par défaut.

    Bornée à ce que `datetime.date` sait construire : au-delà, c'est la
    fabrication de la borne qui lèverait, une ligne plus bas et sans nommer le
    paramètre qui l'a provoquée.
    """
    if value in (None, ''):
        return defaut
    return _entier_borne(value, champ, 1, 9999, "Année attendue entre 1 et 9999.")


def parse_month(value, defaut: int, *, champ='month') -> int:
    """Le mois demandé, ou celui par défaut."""
    if value in (None, ''):
        return defaut
    return _entier_borne(value, champ, 1, 12, "Mois attendu entre 1 et 12.")


# --------------------------------------------------------------------------
# Le PÉRIMÈTRE d'un document
# --------------------------------------------------------------------------

# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ UN DOCUMENT FILTRÉ QUI NE LE DIT PAS EST INDISCERNABLE D'UN DOCUMENT COMPLET.│
# │                                                                              │
# │ Trois constructeurs appliquaient `warehouse` et `user` sans jamais les       │
# │ écrire en tête : l'export des ventes, les huit onglets de rapports et le     │
# │ rapport de caisse. Un papier voyage sans sa barre de filtres, et son lecteur │
# │ n'a aucun moyen de savoir qu'il ne tient qu'un dépôt sur trois.              │
# │                                                                              │
# │ Ces deux résolveurs sont GÉNÉRIQUES, et leur place est ici : « Entrepôt »    │
# │ n'est pas plus un vocabulaire de stock que « Période » ne l'était - c'est    │
# │ le motif exact pour lequel `period_label` a quitté `apps/inventory`.         │
# └──────────────────────────────────────────────────────────────────────────────┘


def warehouse_label(organization, warehouse_id):
    """Le nom d'un entrepôt, ou ``None``. Un UUID ne renseigne aucun lecteur."""
    from apps.inventory.models import Warehouse

    return (
        Warehouse.objects.filter(organization=organization, id=warehouse_id)
        .values_list('name', flat=True)
        .first()
    )


def user_label(organization, user_id):
    """Le nom d'un membre, avec son e-mail en repli.

    Un nom vide rendrait une ligne d'en-tête muette ; l'e-mail est le seul
    repli qui identifie encore quelqu'un. Même règle que `build_team_payload`.
    """
    from apps.organizations.models import OrganizationMembership

    membre = (
        OrganizationMembership.objects
        .filter(organization=organization, user_id=user_id)
        .select_related('user')
        .first()
    )
    if membre is None:
        return None
    return (membre.user.full_name or '').strip() or membre.user.email


def perimeter_filters(params, organization):
    """Les deux lignes de périmètre d'un document, toujours écrites.

    Le défaut ('Tous') s'écrit même en l'ABSENCE de filtre : un lecteur doit
    pouvoir distinguer « pas de filtre » de « filtre oublié dans l'en-tête ».
    C'est la règle de `describe_filters`, et elle vaut ici pour la même raison.

    ⚠ Un identifiant illisible rend « inconnu » et n'interrompt rien : le
    queryset l'a déjà écarté, et faire échouer un export pour un libellé
    manquant serait disproportionné.
    """
    lignes = []
    for cle, intitule, resolveur in (
        ('warehouse', 'Entrepôt', warehouse_label),
        ('user', 'Utilisateur', user_label),
    ):
        brut = params.get(cle)
        if not brut:
            lignes.append((intitule, 'Tous'))
            continue
        try:
            lignes.append((intitule, resolveur(organization, brut) or 'inconnu'))
        except Exception:
            lignes.append((intitule, 'inconnu'))
    return lignes
