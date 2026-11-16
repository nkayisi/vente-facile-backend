"""
Périmètre entrepôt d'un membre : ce qu'il peut LIRE, ce qu'il peut FAIRE.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DEUX PÉRIMÈTRES, ET CHAQUE APPEL DIT LEQUEL IL EMPLOIE.                     │
│                                                                              │
│ Par défaut un membre est ISOLÉ dans ses entrepôts affectés : il lit et il    │
│ agit là, nulle part ailleurs. Le propriétaire peut, membre par membre,       │
│ cocher « Consulter tous les entrepôts » (`consult_all_warehouses`) : ce      │
│ membre LIT alors tout, partout et quel qu'en soit l'auteur, mais il n'AGIT   │
│ toujours que dans ses entrepôts.                                             │
│                                                                              │
│ Une seule fonction ne pouvait pas porter les deux. Chaque filtre prend donc  │
│ une `nature` OBLIGATOIRE (`LECTURE` ou `ACTION`) : un appel qui l'oublie     │
│ lève `TypeError`, et un garde-fou AST refuse qu'on en ajoute un sans elle.   │
│ Le défaut d'un site mal classé n'est pas symétrique - un site de lecture     │
│ mis en action prive un membre d'une donnée, un site d'action mis en lecture  │
│ le laisse agir ailleurs - et c'est pourquoi on ne laisse aucun défaut.       │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ L'AUTEUR SUIT UNE PERMISSION, PAS UN RÔLE.                                  │
│                                                                              │
│ Voir le travail de ses collègues - ventes, dépenses, caisse, retours, devis  │
│ - tient à `sales.view_all`, que le gérant et le magasinier ont par leur rôle │
│ et qu'un caissier reçoit par la case « Voir tout ». La règle précédente      │
│ bornait « le caissier » par son RÔLE sur la caisse et les dépenses, et par   │
│ la PERMISSION sur les ventes : la case n'ouvrait donc que la moitié de ce    │
│ qu'elle promettait.                                                          │
│                                                                              │
│ ⚠ ET L'AUTEUR NE FRANCHIT JAMAIS LES ENTREPÔTS. Un membre borné par auteur   │
│ voit ses propres lignes DANS SES ENTREPÔTS, plus celles qui n'en ont aucun   │
│ (un mouvement de caisse poussé après la clôture du tiroir n'obtient jamais   │
│ de session) : les cacher le ferait ressaisir ce qu'il a déjà saisi.          │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QU'ON PEUT LIRE N'EST PAS CE QU'UNE LISTE MONTRE PAR DÉFAUT (2026-11-16).│
│                                                                              │
│ Une LISTE (et tout ce qui agrège : rapports, tableau de bord, exports) part  │
│ du DÉFAUT du membre :                                                        │
│                                                                              │
│ - le propriétaire voit tout l'établissement ;                               │
│ - le gérant et le magasinier, toutes les données de leurs entrepôts ;       │
│ - le caissier, SES données seulement.                                       │
│                                                                              │
│ Les deux cases n'élargissent que sur demande EXPLICITE de la requête         │
│ (l'`Etendue`) : `?warehouse=<id>` ou `?all_warehouses=1` pour la case        │
│ « Consulter tous les entrepôts », `?user=<id>` ou `?all_users=1` pour le     │
│ caissier qui a l'une des deux cases. Sans rien de cela, un caissier qui a    │
│ « Voir tout dans son entrepôt » ne voit que ses ventes, partout - y compris │
│ sur les écrans qui n'ont pas de filtre.                                      │
│                                                                              │
│ ⚠ L'OBJET QU'ON OUVRE N'EST PAS UNE LISTE. Une fiche (`get_object`) se       │
│ cherche dans tout ce qui est LISIBLE (`TOUT`) : un membre qui a élargi sa    │
│ liste doit pouvoir ouvrir la vente qu'il y voit. Le tirage du terminal lit  │
│ aussi `TOUT` : c'est le terminal qui applique le défaut sur sa base locale,  │
│ pour que ses filtres marchent hors ligne.                                    │
│                                                                              │
│ ⚠ EN LECTURE, L'ÉTENDUE EST OBLIGATOIRE. Une lecture sans étendue lève       │
│ `TypeError` et un garde-fou AST l'exige : un site qui l'oublierait montrerait│
│ tout par défaut, c'est-à-dire le défaut même qu'on ferme ici.                │
└──────────────────────────────────────────────────────────────────────────────┘

Convention des listes : `None` = aucune restriction (propriétaire, ou case
« consulter tous » élargie) ; `[]` = aucun accès, partout `.none()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from django.db.models import Q, QuerySet
from rest_framework.exceptions import ErrorDetail, ValidationError

from apps.organizations.models import OrganizationMembership

LECTURE = 'lecture'
ACTION = 'action'
NATURES = (LECTURE, ACTION)

#: La permission qui ouvre le travail des collègues, dans ses entrepôts.
PERMISSION_VOIR_TOUT = 'sales.view_all'

_SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def get_membership_for_request(request):
    """
    Membership actif pour ``request.user`` et ``X-Organization-ID``.

    Délègue au résolveur unique de ``api_permissions``, qui mémoïse sur l'objet
    ``request``. Cette fonction émettait auparavant sa propre requête, à chacun
    de ses sites d'appel, alors que `IsTenantMember` venait de résoudre
    exactement le même membership quelques lignes plus haut.
    """
    from apps.core.api_permissions import _get_membership

    return _get_membership(request)


def membership_for_organization(request, organization):
    """
    Le membership du demandeur DANS cette organisation, en-tête ou non.

    `_get_membership` dépend de `X-Organization-ID`. Or le tableau de bord
    d'organisation porte son identifiant dans l'URL, et le back-office
    n'envoie PAS cet en-tête sur ce chemin. Sans cette porte, le membership est
    `None` et le périmètre ne s'applique jamais.

    Mémoïsé sous la MÊME clé que `_get_membership` : deux chemins vers la même
    identité ne doivent pas pouvoir répondre différemment.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None

    org_id = getattr(organization, 'id', organization)
    cache_key = f'_membership_{org_id}'
    if not hasattr(request, cache_key):
        membership = user.memberships.filter(
            organization_id=org_id, is_active=True
        ).select_related('organization').first()
        setattr(request, cache_key, membership)
    from apps.core.api_permissions import exiger_etablissement_actif

    return exiger_etablissement_actif(request, getattr(request, cache_key))


# ─── L'étendue d'une lecture ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Etendue:
    """Ce qu'une lecture ÉLARGIT au-delà du défaut du membre."""

    #: Un entrepôt précis, ou « Tous les entrepôts ».
    entrepots: bool
    #: Un utilisateur précis, ou « Tous les utilisateurs ».
    auteurs: bool


#: Le défaut : une liste qui ne demande rien de plus.
DEFAUT = Etendue(entrepots=False, auteurs=False)
#: Tout ce qui est lisible : une fiche qu'on ouvre, le tirage, un contrôle d'existence.
TOUT = Etendue(entrepots=True, auteurs=True)

_VRAI = frozenset({'1', 'true', 'oui', 'yes', 'on'})


def _drapeau(valeur) -> bool:
    return str(valeur or '').strip().lower() in _VRAI


def etendue_de_la_requete(request) -> Etendue:
    """
    L'étendue que la requête DEMANDE.

    Un `?warehouse=` précis élargit aussi : un membre « consulter tous » qui vise
    l'entrepôt d'en face doit le lire, et son défaut ne contient que les siens.
    """
    params = getattr(request, 'query_params', None)
    if params is None:
        params = getattr(request, 'GET', {}) or {}
    return Etendue(
        entrepots=bool(params.get('warehouse')) or _drapeau(params.get('all_warehouses')),
        auteurs=bool(params.get('user')) or _drapeau(params.get('all_users')),
    )


def _verifier_etendue(nature, etendue):
    if nature == LECTURE and not isinstance(etendue, Etendue):
        raise TypeError(
            'Une lecture exige son étendue : `etendue=etendue_de_la_requete(request)` '
            'pour une liste, `etendue=TOUT` pour une fiche ou le tirage.'
        )


# ─── La nature d'une requête ──────────────────────────────────────────────────

def nature_de_la_requete(request) -> str:
    """
    LECTURE pour une méthode sûre, ACTION pour le reste, avec deux exceptions
    que la VUE déclare :

    - ``actions_de_lecture`` : un POST qui ne fait que lire (ex. marquer un
      ticket imprimé, qui réimprime ce qu'on a le droit de lire) ;
    - ``actions_preparatoires`` : un GET qui prépare une écriture (ex. la
      feuille d'approvisionnement) - elle ne doit montrer que ce qu'on pourra
      réellement saisir.
    """
    contexte = getattr(request, 'parser_context', None) or {}
    vue = contexte.get('view')
    action = getattr(vue, 'action', None)
    if action:
        if action in getattr(vue, 'actions_de_lecture', ()):
            return LECTURE
        if action in getattr(vue, 'actions_preparatoires', ()):
            return ACTION
    return LECTURE if getattr(request, 'method', 'GET') in _SAFE_METHODS else ACTION


def _verifier_nature(nature):
    if nature not in NATURES:
        raise ValueError(f"nature inconnue : {nature!r} (attendu {NATURES})")


# ─── Les deux périmètres ──────────────────────────────────────────────────────

def _est_proprietaire(membership) -> bool:
    return membership.role == OrganizationMembership.Role.OWNER


def consulte_tout(membership) -> bool:
    """La case « Consulter tous les entrepôts », accordée par le propriétaire."""
    return bool(getattr(membership, 'consult_all_warehouses', False))


def entrepots_affectes(membership) -> list[UUID]:
    """
    Les entrepôts affectés, non supprimés, INACTIFS COMPRIS.

    Un entrepôt désactivé reste lisible (on relit son passé) et reste dans le
    périmètre d'action (une dette d'une vente faite là s'encaisse toujours) :
    ce qui le ferme aux opérations, c'est `exiger_entrepot_actionnable`, pas
    sa disparition des listes.

    Mémoïsé sur l'instance de membership, elle-même mémoïsée sur la requête.
    """
    cached = getattr(membership, '_entrepots_affectes', None)
    if cached is None:
        cached = list(
            membership.assigned_warehouses.filter(is_deleted=False).values_list(
                'id', flat=True
            )
        )
        membership._entrepots_affectes = cached
    return cached


def entrepots_lisibles(membership) -> Optional[list[UUID]]:
    """`None` pour le propriétaire et pour la case ; sinon les affectés."""
    if _est_proprietaire(membership) or consulte_tout(membership):
        return None
    return entrepots_affectes(membership)


def entrepots_actionnables(membership) -> Optional[list[UUID]]:
    """`None` pour le propriétaire ; sinon les affectés. La case n'y change rien."""
    if _est_proprietaire(membership):
        return None
    return entrepots_affectes(membership)


def entrepots_du_perimetre(
    membership, nature: str, etendue: Optional[Etendue] = None
) -> Optional[list[UUID]]:
    """
    Les entrepôts d'une requête. En LECTURE, l'étendue décide : par DÉFAUT, un
    membre « consulter tous » ne voit que ses entrepôts, et il ne voit les
    autres que s'il les demande.
    """
    _verifier_nature(nature)
    if nature == LECTURE:
        _verifier_etendue(nature, etendue)
        ids = entrepots_lisibles(membership)
        if ids is None and not etendue.entrepots and not _est_proprietaire(membership):
            return entrepots_affectes(membership)
        return ids
    return entrepots_actionnables(membership)


def permissions_effectives(membership) -> frozenset[str]:
    """Rôle + `extra_permissions`, mémoïsé sur le membership."""
    cached = getattr(membership, '_permissions_effectives', None)
    if cached is None:
        from apps.core.services import PermissionService

        cached = frozenset(PermissionService.get_effective_permissions(membership))
        membership._permissions_effectives = cached
    return cached


def est_borne_par_auteur(membership, nature: str) -> bool:
    """
    Le membre ne PEUT-il voir, ou toucher, que SES propres lignes ?

    Faux pour le propriétaire ; faux en LECTURE pour la case « consulter
    tous » (elle ouvre tous les auteurs) ; sinon, faux seulement avec
    `sales.view_all`. C'est un DROIT : ce qu'une liste montre par défaut se
    décide dans `auteur_borne`.
    """
    _verifier_nature(nature)
    if membership is None or _est_proprietaire(membership):
        return False
    if nature == LECTURE and consulte_tout(membership):
        return False
    return PERMISSION_VOIR_TOUT not in permissions_effectives(membership)


def auteur_borne(membership, nature: str, etendue: Optional[Etendue] = None) -> bool:
    """
    La requête ne montre-t-elle que SES propres lignes ?

    Le droit d'abord (`est_borne_par_auteur`), puis le DÉFAUT : un caissier ne
    voit que ses données tant qu'il ne demande pas un collègue ou « Tous les
    utilisateurs », même s'il a l'une des deux cases.
    """
    if est_borne_par_auteur(membership, nature):
        return True
    if nature != LECTURE or membership is None:
        return False
    _verifier_etendue(nature, etendue)
    return (
        not etendue.auteurs
        and membership.role == OrganizationMembership.Role.CASHIER
    )


def empreinte_perimetre(membership) -> str:
    """
    L'empreinte de tout ce qui décide du périmètre d'un membre.

    Elle change dès que le rôle, la case, une permission effective, une
    affectation ou l'état d'un entrepôt affecté change. Un client qui la voit
    bouger relit ses droits ; le terminal retire ses tables.
    """
    import hashlib

    if membership is None:
        return ''
    affectes = sorted(
        f'{wid}:{int(actif)}'
        for wid, actif in membership.assigned_warehouses.filter(
            is_deleted=False
        ).values_list('id', 'is_active')
    )
    morceaux = [
        membership.role,
        '1' if consulte_tout(membership) else '0',
        ','.join(sorted(permissions_effectives(membership))),
        ','.join(affectes),
    ]
    return hashlib.blake2s('|'.join(morceaux).encode(), digest_size=8).hexdigest()


# ─── Les filtres : un corps chacun, une nature obligatoire ─────────────────────

def _borner_entrepot(queryset, ids, field, *, include_null):
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    condition = Q(**{f'{field}__in': ids})
    if include_null:
        condition |= Q(**{f'{field}__isnull': True})
    return queryset.filter(condition)


def filter_queryset_by_warehouse_ids(
    queryset: QuerySet,
    membership: OrganizationMembership,
    warehouse_field: str = 'warehouse_id',
    *,
    nature: str,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """Filtre direct sur un champ d'entrepôt, dans le périmètre de `nature`."""
    return _borner_entrepot(
        queryset,
        entrepots_du_perimetre(membership, nature, etendue),
        warehouse_field,
        include_null=False,
    )


def filter_queryset_by_related_warehouse(
    queryset: QuerySet,
    membership: OrganizationMembership,
    warehouse_field: str,
    *,
    include_null: bool = False,
    nature: str,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """
    Filtre via une relation (``original_sale__warehouse_id``,
    ``register__warehouse_id``…). ``include_null`` garde les lignes dont la
    relation est vide (ventes anciennes sans entrepôt).
    """
    return _borner_entrepot(
        queryset,
        entrepots_du_perimetre(membership, nature, etendue),
        warehouse_field,
        include_null=include_null,
    )


def borner_visibilite(
    queryset: QuerySet,
    membership: Optional[OrganizationMembership],
    *,
    nature: str,
    warehouse_field: Optional[str],
    author_field: Optional[str],
    include_null_warehouse: bool = False,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """
    Le périmètre complet d'une donnée qui a un AUTEUR : entrepôt, puis auteur.

    - ``warehouse_field`` ``None`` : la donnée n'a pas d'entrepôt (devis
      anciens) ; seul l'auteur borne.
    - Un membre borné par auteur voit ses propres lignes sans entrepôt même
      quand ``include_null_warehouse`` est faux (voir l'en-tête du module).
    """
    if membership is None:
        return queryset
    borne = bool(author_field) and auteur_borne(membership, nature, etendue)
    if warehouse_field:
        queryset = _borner_entrepot(
            queryset,
            entrepots_du_perimetre(membership, nature, etendue),
            warehouse_field,
            include_null=include_null_warehouse or borne,
        )
    if borne:
        # L'identifiant, pas l'objet : `membership.user` coûterait une requête.
        queryset = queryset.filter(**{author_field: membership.user_id})
    return queryset


def borner_visibilite_requete(
    queryset: QuerySet,
    request,
    *,
    warehouse_field: Optional[str],
    author_field: Optional[str],
    include_null_warehouse: bool = False,
    nature: Optional[str] = None,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """
    `borner_visibilite`, nature et étendue déduites de la requête sauf si elles
    sont imposées. Ce corps sert des LISTES : l'étendue par défaut est celle que
    la requête demande.
    """
    return borner_visibilite(
        queryset,
        get_membership_for_request(request),
        nature=nature or nature_de_la_requete(request),
        warehouse_field=warehouse_field,
        author_field=author_field,
        include_null_warehouse=include_null_warehouse,
        etendue=etendue or etendue_de_la_requete(request),
    )


def filter_stock_transfer_queryset(
    queryset: QuerySet,
    membership: OrganizationMembership,
    *,
    nature: str,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """
    Transferts où la source OU la destination est dans le périmètre.

    ⚠ Le OU est la traduction juste de « un transfert relie deux dépôts » : le
    borner sur la seule source cacherait au magasinier de destination ce qu'on
    lui expédie, sur la seule destination ce que l'expéditeur a envoyé. Qui
    peut faire QUOI sur un transfert (expédier côté source, recevoir côté
    destination) est tranché par `exiger_cote_du_transfert`.
    """
    ids = entrepots_du_perimetre(membership, nature, etendue)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(
        Q(source_warehouse_id__in=ids) | Q(destination_warehouse_id__in=ids)
    )


def filter_product_queryset(
    queryset: QuerySet,
    membership: Optional[OrganizationMembership],
    *,
    nature: str,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """
    Le périmètre d'un ARTICLE : il se dérive de ses lignes de stock.

    ⚠ UN ARTICLE NON SUIVI RESTE VISIBLE DE TOUS : un service n'a de ligne de
    stock dans AUCUN entrepôt, l'écarter le rendrait invendable partout.

    ⚠ NE PAS S'EN SERVIR POUR BORNER `products` AU TIRAGE. Le tirage pagine sur
    `(updated_at, id)` et `Stock.save()` ne touche pas `product.updated_at` :
    un article nouvellement stocké deviendrait éligible derrière le point de
    reprise et ne descendrait jamais.
    """
    if membership is None:
        return queryset
    return filter_products_by_warehouse_ids(
        queryset, entrepots_du_perimetre(membership, nature, etendue)
    )


def filter_products_by_warehouse_ids(queryset: QuerySet, ids) -> QuerySet:
    """
    La règle sur des identifiants DÉJÀ résolus. `None` : aucune restriction ;
    `[]` : seuls les articles non suivis.

    ⚠ `distinct()` EST OBLIGATOIRE : la condition traverse une relation
    inverse, et un article stocké dans deux dépôts sortirait deux fois.
    """
    if ids is None:
        return queryset
    return queryset.filter(
        Q(track_inventory=False) | Q(stocks__warehouse_id__in=ids)
    ).distinct()


def filter_cash_movement_queryset(
    queryset: QuerySet,
    membership: Optional[OrganizationMembership],
    *,
    nature: str,
    etendue: Optional[Etendue] = None,
) -> QuerySet:
    """
    Le périmètre d'un mouvement de caisse : il se DÉRIVE, par trois chemins
    (la vente, la dépense, la session et son comptoir), et il est STRICT.

    Un apport sans tiroir est une opération d'ÉTABLISSEMENT, au même titre
    qu'un loyer : il reste au propriétaire et à la case « consulter tous ».
    Seule exception, celle de l'en-tête : un membre borné par auteur voit ses
    PROPRES mouvements rattachés à rien.
    """
    if membership is None:
        return queryset
    ids = entrepots_du_perimetre(membership, nature, etendue)
    borne = auteur_borne(membership, nature, etendue)
    if ids is not None:
        if not ids:
            return queryset.none()
        dans_mes_entrepots = (
            Q(sale__warehouse_id__in=ids)
            | Q(expense__warehouse_id__in=ids)
            | Q(session__register__warehouse_id__in=ids)
        )
        if borne:
            dans_mes_entrepots |= Q(
                sale__isnull=True, expense__isnull=True, session__isnull=True
            )
        queryset = queryset.filter(dans_mes_entrepots)
    if borne:
        queryset = queryset.filter(created_by_id=membership.user_id)
    return queryset


# ─── Les assertions ───────────────────────────────────────────────────────────

def _lire_identifiant(warehouse_id, champ):
    # ⚠ L'IDENTIFIANT SE LIT AVANT LE RÔLE : un `?warehouse=oops` rendait 500 à
    # un propriétaire et 400 à un gérant. Une saisie fautive se refuse.
    try:
        return warehouse_id if isinstance(warehouse_id, UUID) else UUID(str(warehouse_id))
    except (TypeError, ValueError):
        raise ValidationError({champ: 'Entrepôt inconnu.'})


def exiger_entrepot_lisible(membership, warehouse_id, *, champ: str = 'warehouse'):
    """
    Le membre peut-il LIRE cet entrepôt ? Sert aux filtres volontaires.

    Un identifiant hors périmètre LÈVE, il ne rend jamais un ensemble vide :
    zéro ligne se lirait « rien à signaler » là où la vérité est « vous n'avez
    pas le droit de regarder ».
    """
    if warehouse_id in (None, ''):
        return
    if not membership:
        raise ValidationError({'detail': 'Organisation requise.'})
    wid = _lire_identifiant(warehouse_id, champ)
    ids = entrepots_lisibles(membership)
    if ids is not None and wid not in ids:
        raise ValidationError({champ: 'Entrepôt non autorisé pour votre compte.'})


def _entrepot_de_lorganisation(membership, wid):
    """`(nom, actif)` de l'entrepôt, mémoïsé : une livraison de masse en vise un seul."""
    cache = getattr(membership, '_entrepots_vus', None)
    if cache is None:
        cache = {}
        membership._entrepots_vus = cache
    if wid not in cache:
        from apps.inventory.models import Warehouse

        cache[wid] = Warehouse.objects.filter(
            id=wid, organization_id=membership.organization_id, is_deleted=False
        ).values_list('name', 'is_active').first()
    return cache[wid]


def exiger_entrepot_actionnable(
    membership,
    warehouse_id,
    *,
    allow_none: bool = False,
    champ: str = 'warehouse',
    exiger_actif: bool = True,
):
    """
    Le membre peut-il AGIR dans cet entrepôt ?

    Quatre refus, et chacun dit ce qui bloque :

    - entrepôt d'une autre organisation, supprimé ou inconnu ;
    - entrepôt DÉSACTIVÉ (`entrepot_inactif`) : fermé à toute opération ;
    - entrepôt seulement LISIBLE (`consultation_seule`) : la case « consulter
      tous » ouvre la lecture, jamais l'action ;
    - entrepôt hors périmètre.

    ``exiger_actif=False`` : la SEULE exception de la règle, l'encaissement
    d'une dette sur une vente faite dans un entrepôt désactivé.
    """
    if warehouse_id in (None, ''):
        if allow_none:
            if membership and not _est_proprietaire(membership):
                raise ValidationError({champ: 'Un entrepôt est requis pour votre compte.'})
            return
        raise ValidationError({champ: 'Entrepôt requis.'})
    if not membership:
        raise ValidationError({'detail': 'Organisation requise.'})

    wid = _lire_identifiant(warehouse_id, champ)
    trouve = _entrepot_de_lorganisation(membership, wid)
    if trouve is None:
        raise ValidationError({champ: 'Entrepôt inconnu.'})
    nom, actif = trouve

    ids = entrepots_actionnables(membership)
    if ids is not None and wid not in ids:
        lisibles = entrepots_lisibles(membership)
        if lisibles is None or wid in lisibles:
            raise ValidationError({champ: ErrorDetail(
                f"Consultation seule : l'entrepôt « {nom} » ne vous est pas assigné.",
                code='consultation_seule',
            )})
        raise ValidationError({champ: 'Entrepôt non autorisé pour votre compte.'})

    if exiger_actif and not actif:
        raise ValidationError({champ: ErrorDetail(
            f"L'entrepôt « {nom} » est désactivé : aucune opération n'y est possible.",
            code='entrepot_inactif',
        )})


def exiger_entrepot_actionnable_requete(request, warehouse_id, **options):
    """Variante basée sur la requête."""
    return exiger_entrepot_actionnable(
        get_membership_for_request(request), warehouse_id, **options
    )


def exiger_entrepot_lisible_requete(request, warehouse_id, **options):
    """Variante basée sur la requête."""
    return exiger_entrepot_lisible(
        get_membership_for_request(request), warehouse_id, **options
    )


def exiger_entrepot_de_destination(membership, warehouse_id, *, champ='destination_warehouse'):
    """
    La DESTINATION d'un transfert : tout entrepôt ACTIF de l'organisation.

    On expédie depuis SES entrepôts vers n'importe lequel : la boutique qui
    attend sa marchandise n'est pas affectée au magasinier du dépôt central.
    Ce que l'expéditeur obtient n'est pas un accès à la destination : il voit
    son transfert, pas le stock de la boutique.
    """
    if warehouse_id in (None, ''):
        raise ValidationError({champ: 'Entrepôt requis.'})
    if not membership:
        raise ValidationError({'detail': 'Organisation requise.'})
    wid = _lire_identifiant(warehouse_id, champ)
    trouve = _entrepot_de_lorganisation(membership, wid)
    if trouve is None:
        raise ValidationError({champ: 'Entrepôt inconnu.'})
    nom, actif = trouve
    if not actif:
        raise ValidationError({champ: ErrorDetail(
            f"L'entrepôt « {nom} » est désactivé : il ne peut pas recevoir de transfert.",
            code='entrepot_inactif',
        )})


def exiger_cote_du_transfert(membership, transfert, cote: str):
    """
    Qui peut faire quoi sur un transfert : approuver, expédier et annuler
    appartiennent à la SOURCE ; recevoir appartient à la DESTINATION.

    Partagé par la vue et par le journal du terminal : écrit deux fois, il
    avait dérivé, et un terminal pouvait recevoir le transfert de n'importe
    quel entrepôt de l'organisation.
    """
    if cote == 'source':
        exiger_entrepot_actionnable(
            membership, transfert.source_warehouse_id, champ='source_warehouse'
        )
    elif cote == 'destination':
        exiger_entrepot_actionnable(
            membership, transfert.destination_warehouse_id, champ='destination_warehouse'
        )
    else:  # pragma: no cover - garde de programmation
        raise ValueError(f'côté inconnu : {cote!r}')


# ─── Les membres qu'on peut viser ─────────────────────────────────────────────

def membership_targets_q(membership, *, nature: str = LECTURE):
    """
    Le prédicat SQL des membres que `membership` a le droit de viser.

    Un seul prédicat, deux emplois : le sélecteur (`build_team_payload`) et la
    validation (`assert_user_allowed_for_membership`). Écrits séparément, ils
    avaient divergé.

    Rend ``None`` quand aucune borne ne s'applique (propriétaire, case).
    """
    if membership is None:
        return Q(pk__in=[])
    if est_borne_par_auteur(membership, nature):
        # Lui-même, et lui seul : il ne voit que son propre travail.
        return Q(user_id=membership.user_id)

    # `TOUT` : on décide ici d'un DROIT (qui peut-on viser), pas de ce qu'une
    # liste montre par défaut.
    ids = entrepots_du_perimetre(membership, nature, TOUT)
    if ids is None:
        return None

    # ⚠ LE PROPRIÉTAIRE EST TOUJOURS VISABLE : il n'a aucune affectation - c'est
    # son rôle qui lui donne tout - et une intersection sur le seul M2M le
    # rejetterait systématiquement.
    return (
        Q(user_id=membership.user_id)
        | Q(role=OrganizationMembership.Role.OWNER)
        | Q(assigned_warehouses__id__in=ids, assigned_warehouses__is_deleted=False)
    )


def assert_user_allowed_for_membership(membership, user_id):
    """
    Le demandeur a-t-il le droit de VISER cet utilisateur dans un filtre ?

    ⚠ CE QUI PROTÈGE N'EST PAS CETTE BORNE, C'EST LA COMPOSITION : le filtre
    volontaire est toujours superposé au périmètre de lecture. Viser le
    propriétaire ne montre donc que « son activité DANS MES entrepôts ».

    ⚠ UN IDENTIFIANT HORS PÉRIMÈTRE LÈVE, IL NE REND JAMAIS UN ENSEMBLE VIDE.
    """
    if user_id is None:
        return
    if not membership:
        raise ValidationError({'detail': 'Organisation requise.'})

    try:
        uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    except (TypeError, ValueError):
        raise ValidationError({'user': 'Utilisateur inconnu.'})

    if uid == membership.user_id:
        return

    if est_borne_par_auteur(membership, LECTURE):
        raise ValidationError(
            {'user': 'Vous ne pouvez consulter que vos propres données.'}
        )

    cible = OrganizationMembership.objects.filter(
        organization_id=membership.organization_id, user_id=uid, is_active=True
    ).first()
    if cible is None:
        # Un membre introuvable n'est pas notre affaire : `user_activity`
        # répond 404, contrat publié dont les deux clients tirent leur message.
        return

    cibles = membership_targets_q(membership, nature=LECTURE)
    if cibles is None:
        return

    if not OrganizationMembership.objects.filter(
        cibles,
        organization_id=membership.organization_id,
        user_id=uid,
        is_active=True,
    ).exists():
        raise ValidationError({'user': 'Utilisateur hors de votre périmètre.'})


def assert_user_allowed_for_request(request, user_id):
    """Variante basée sur la requête (résout le membership via X-Organization-ID)."""
    return assert_user_allowed_for_membership(
        get_membership_for_request(request), user_id
    )
