"""
Périmètre entrepôt par membership : helpers réutilisables pour filtres API.

Convention :
- role ``owner`` : accès à tous les entrepôts de l'organisation (pas de filtre warehouse).
- autres rôles : entrepôts listés via M2M ``assigned_warehouses`` ; liste vide = aucun accès aux données scoped.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from django.db.models import Q, QuerySet
from rest_framework.exceptions import ValidationError

from apps.organizations.models import OrganizationMembership


def get_membership_for_request(request):
    """
    Membership actif pour ``request.user`` et ``X-Organization-ID``.

    Délègue au résolveur unique de ``api_permissions``, qui mémoïse sur l'objet
    ``request``. Cette fonction émettait auparavant sa propre requête, à chacun
    de ses 37 sites d'appel, alors que `IsTenantMember` venait de résoudre
    exactement le même membership quelques lignes plus haut.

    Elle portait aussi un ``prefetch_related("assigned_warehouses")`` qui n'a
    jamais servi : `accessible_warehouse_ids` filtrait ensuite le manager M2M,
    et un ``.filter()`` court-circuite le cache de prefetch. Le préchargement
    était donc payé puis jeté, à chaque appel.
    """
    from apps.core.api_permissions import _get_membership

    return _get_membership(request)


def membership_for_organization(request, organization):
    """
    Le membership du demandeur DANS cette organisation, en-tête ou non.

    `_get_membership` dépend de `X-Organization-ID`. Or le tableau de bord
    d'organisation porte son identifiant dans l'URL, et le back-office
    n'envoie PAS cet en-tête sur ce chemin (vérifié :
    `getDashboardStats` ne pose que `Authorization`). Sans cette porte, le
    membership est `None`, `restrict_visibility_for_membership` rend le
    queryset INTACT, et le périmètre ne s'applique jamais - tout en faisant
    passer un test qui, lui, envoie l'en-tête.

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
    return getattr(request, cache_key)


def accessible_warehouse_ids(membership: OrganizationMembership) -> Optional[list[UUID]]:
    """
    Retourne ``None`` si pas de restriction (owner), sinon liste d'UUID d'entrepôts.

    Le résultat est mémoïsé sur l'instance de membership. Comme celle-ci est
    elle-même mémoïsée sur la requête, la liste n'est lue qu'une fois par
    requête HTTP, là où `reports/summary` la relisait six fois.
    """
    if membership.role == OrganizationMembership.Role.OWNER:
        return None
    cached = getattr(membership, "_accessible_warehouse_ids", None)
    if cached is None:
        cached = list(
            membership.assigned_warehouses.filter(is_deleted=False).values_list(
                "id", flat=True
            )
        )
        membership._accessible_warehouse_ids = cached
    return cached


def restrict_visibility_for_membership(
    queryset: QuerySet,
    membership: Optional[OrganizationMembership],
    *,
    warehouse_field: str,
    creator_field: str,
    include_null_warehouse: bool = False,
) -> QuerySet:
    """Visibilité par rôle pour les données financières/opérationnelles.

    - ``owner`` : voit tout (aucun filtre).
    - ``cashier`` : voit uniquement ses propres enregistrements
      (``creator_field == membership.user``), partout dans l'application.
    - ``manager`` / ``stock_keeper`` : périmètre entrepôt (``assigned_warehouses``)
      via ``warehouse_field``. Par défaut, les enregistrements sans entrepôt
      (``NULL``) ne leur sont pas visibles (réservés au owner) - passer
      ``include_null_warehouse=True`` pour les inclure (ex. ventes legacy).
    """
    if membership is None:
        return queryset
    role = membership.role
    if role == OrganizationMembership.Role.OWNER:
        return queryset
    if role == OrganizationMembership.Role.CASHIER:
        return queryset.filter(**{creator_field: membership.user})
    return filter_queryset_by_related_warehouse(
        queryset, membership, warehouse_field, include_null=include_null_warehouse
    )


def restrict_visibility_for_request(
    queryset: QuerySet,
    request,
    *,
    warehouse_field: str,
    creator_field: str,
    include_null_warehouse: bool = False,
) -> QuerySet:
    """Variante basée sur la requête (résout le membership via X-Organization-ID)."""
    return restrict_visibility_for_membership(
        queryset,
        get_membership_for_request(request),
        warehouse_field=warehouse_field,
        creator_field=creator_field,
        include_null_warehouse=include_null_warehouse,
    )


def filter_queryset_by_warehouse_ids(
    queryset: QuerySet,
    membership: OrganizationMembership,
    warehouse_field: str = "warehouse_id",
) -> QuerySet:
    """Filtre un queryset sur un champ FK warehouse si le membership est restreint."""
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(**{f"{warehouse_field}__in": ids})


def filter_queryset_by_related_warehouse(
    queryset: QuerySet,
    membership: OrganizationMembership,
    warehouse_field: str,
    *,
    include_null: bool = False,
) -> QuerySet:
    """Filtre via une relation indirecte (ex. ``original_sale__warehouse_id``,
    ``register__warehouse_id``, ``sale__warehouse_id``).

    Si ``include_null`` est ``True``, les lignes dont la relation est ``NULL``
    restent visibles (utile pour mouvements de caisse non liés à une vente).
    """
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    if include_null:
        return queryset.filter(
            Q(**{f"{warehouse_field}__in": ids})
            | Q(**{f"{warehouse_field}__isnull": True})
        )
    return queryset.filter(**{f"{warehouse_field}__in": ids})


def filter_stock_transfer_queryset(
    queryset: QuerySet, membership: OrganizationMembership
) -> QuerySet:
    """Transferts où source ou destination est dans le périmètre."""
    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(
        Q(source_warehouse_id__in=ids) | Q(destination_warehouse_id__in=ids)
    )


def filter_cash_movement_queryset(
    queryset: QuerySet, membership: Optional[OrganizationMembership]
) -> QuerySet:
    """
    Le périmètre d'un mouvement de caisse : il se DÉRIVE, par trois chemins.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ IL Y AVAIT DEUX RÈGLES, ET ELLES DONNAIENT DEUX SOLDES.                 │
    │                                                                          │
    │ `CashMovementViewSet` bornait STRICTEMENT (les trois jointures, rien     │
    │ d'autre) ; `reports/_scope_cash_movements` TOLÉRAIT en plus les          │
    │ mouvements rattachés à rien. Le même gérant lisait donc un solde au      │
    │ Livre de caisse et un AUTRE au tableau de bord, sur deux écrans voisins  │
    │ du même back-office, le même jour.                                       │
    │                                                                          │
    │ Le strict l'emporte, et c'est la règle déjà posée pour les dépenses : un │
    │ apport sans tiroir est une opération d'ÉTABLISSEMENT, au même titre      │
    │ qu'un loyer, et elle reste au propriétaire. La tolérance faisait         │
    │ apparaître le même apport chez chaque gérant.                            │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    if membership is None:
        return queryset
    if membership.role == OrganizationMembership.Role.OWNER:
        return queryset
    if membership.role == OrganizationMembership.Role.CASHIER:
        # Sa visibilité est par CRÉATEUR, partout : c'est la règle de
        # `restrict_visibility_for_membership`, et le serveur ne lui oppose
        # aucun entrepôt sur cette table.
        return queryset.filter(created_by=membership.user)
    ids = accessible_warehouse_ids(membership)
    if not ids:
        return queryset.none()
    return queryset.filter(
        Q(sale__warehouse_id__in=ids)
        | Q(expense__warehouse_id__in=ids)
        | Q(session__register__warehouse_id__in=ids)
    )


def assert_warehouse_allowed_for_membership(membership, warehouse_id, *, allow_none: bool = False):
    """
    Vérifie que `warehouse_id` est dans le périmètre de ce membership.

    Le corps de la règle vit ICI ; la variante `_for_request` n'en est que la
    porte d'entrée. Deux corps finiraient par diverger, et une règle de
    périmètre qui diverge est un écran qui affiche les chiffres d'un dépôt sous
    le nom d'un autre.
    """
    if warehouse_id is None:
        if allow_none:
            if membership and membership.role != OrganizationMembership.Role.OWNER:
                raise ValidationError(
                    {"warehouse": "Un entrepôt est requis pour votre compte."}
                )
            return
        raise ValidationError({"warehouse": "Entrepôt requis."})

    if not membership:
        raise ValidationError({"detail": "Organisation requise."})

    # ⚠ L'IDENTIFIANT SE LIT AVANT LE RÔLE, ET NON L'INVERSE.
    #
    # Cette garde était placée APRÈS la sortie du propriétaire : pour lui,
    # `?warehouse=oops` traversait la validation, atteignait
    # `qs.filter(warehouse_id='oops')` et remontait en 500. Un gérant, lui,
    # recevait un 400 correct - le même paramètre, deux réponses, selon le
    # rôle de qui le tape. Une saisie fautive se refuse, elle ne ressemble pas
    # à une panne.
    try:
        wid = warehouse_id if isinstance(warehouse_id, UUID) else UUID(str(warehouse_id))
    except (TypeError, ValueError):
        # Un identifiant illisible est REFUSÉ, jamais ignoré : l'ignorer rendrait
        # les chiffres de tout le périmètre sous une étiquette d'entrepôt.
        raise ValidationError({"warehouse": "Entrepôt inconnu."})

    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return
    if wid not in ids:
        raise ValidationError(
            {"warehouse": "Entrepôt non autorisé pour votre compte."}
        )


def assert_warehouse_allowed_for_request(
    request,
    warehouse_id,
    *,
    allow_none: bool = False,
):
    """
    Vérifie que ``warehouse_id`` est dans le périmètre du membership courant.
    ``warehouse_id`` peut être None si ``allow_none`` (ex. legacy réservé owner - éviter si possible).
    """
    return assert_warehouse_allowed_for_membership(
        get_membership_for_request(request), warehouse_id, allow_none=allow_none
    )


def membership_targets_q(membership):
    """
    Le prédicat SQL des membres que `membership` a le droit de viser.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN SEUL PRÉDICAT, DEUX EMPLOIS : LE SÉLECTEUR ET LA VALIDATION.         │
    │                                                                          │
    │ `build_team_payload` s'en sert pour ne PROPOSER que des cibles           │
    │ recevables, `assert_user_allowed_for_membership` pour REFUSER les        │
    │ autres. Écrits séparément, ils ont divergé : le roster rendait tous les  │
    │ membres de l'organisation pendant que la validation en refusait la       │
    │ moitié, et le marchand recevait un 400 sur un nom que l'application      │
    │ venait de lui proposer.                                                  │
    └──────────────────────────────────────────────────────────────────────────┘

    Rend ``None`` quand aucune borne ne s'applique (owner : tout le monde).
    """
    if membership is None:
        return Q(pk__in=[])
    if membership.role == OrganizationMembership.Role.CASHIER:
        # Lui-même, et lui seul : sa visibilité est par créateur, partout.
        # Ses deux appelants sortent avant d'arriver ici - l'un avec un message
        # propre, l'autre avec un roster fermé - mais une fonction de périmètre
        # qui répondrait « aucune borne » à un caissier serait un piège posé
        # pour son troisième appelant.
        return Q(user_id=membership.user_id)

    ids = accessible_warehouse_ids(membership)
    if ids is None:
        return None

    # ⚠ LE PROPRIÉTAIRE EST TOUJOURS VISABLE, et ce n'est pas une faveur.
    #
    # Il n'a AUCUN `assigned_warehouses` - c'est son RÔLE qui lui donne tout,
    # ce qu'`accessible_warehouse_ids` encode en rendant `None` - si bien
    # qu'une intersection sur le seul M2M le rejetait systématiquement. La
    # clause porte donc sur le rôle : en SQL, c'est la seule forme de
    # « aucune restriction », et une cible sans restriction partage tous les
    # entrepôts, donc les miens.
    #
    # ⚠ `ids == []` (rôle borné sans affectation) laisse `self | owner` : la
    # troisième clause ne retient personne, et c'est juste - ce membre ne
    # partage aucun dépôt avec quiconque.
    return (
        Q(user_id=membership.user_id)
        | Q(role=OrganizationMembership.Role.OWNER)
        | Q(assigned_warehouses__id__in=ids, assigned_warehouses__is_deleted=False)
    )


def assert_user_allowed_for_membership(membership, user_id):
    """
    Le demandeur a-t-il le droit de VISER cet utilisateur ?

    - `cashier` : lui-même, et lui seul.
    - `manager` / `stock_keeper` : un membre partageant au moins un de ses
      entrepôts, le propriétaire, ou lui-même.
    - `owner` : n'importe quel membre actif de l'organisation.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ VISER LE PROPRIÉTAIRE NE MONTRE RIEN DE NEUF, ET C'EST MESURÉ.          │
    │                                                                          │
    │ Le filtre volontaire est TOUJOURS superposé au périmètre du rôle -       │
    │ `_vouloir` dans les rapports, `borner_ventes` au tableau de bord, et     │
    │ `user_activity` lui-même passe par `_scope_sales`. Un gérant qui vise    │
    │ le propriétaire ne lit donc que « son activité DANS MES entrepôts »,     │
    │ c'est-à-dire un sous-ensemble de ce que ce gérant voit déjà en agrégat.  │
    │ Ce qui est neuf, c'est la DÉCOMPOSITION par auteur de lignes qu'il       │
    │ additionne déjà - et elle vaut pour n'importe quel collègue.             │
    │                                                                          │
    │ Le refuser, en revanche, avait un coût réel : les deux clients           │
    │ proposent le propriétaire dans leur sélecteur (il est « partout »,       │
    │ le croiser avec un dépôt le ferait disparaître de sa propre liste),      │
    │ donc le marchand choisissait un nom et recevait un 400.                  │
    │                                                                          │
    │ ⚠ CE QUI PROTÈGE N'EST PAS CETTE BORNE, C'EST LA COMPOSITION. Le jour    │
    │ où un endpoint filtrerait par `user` sans composer un `_scope_*`, la     │
    │ garantie disparaîtrait. C'est cela que le test épingle.                  │
    └──────────────────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN IDENTIFIANT HORS PÉRIMÈTRE LÈVE, IL NE REND JAMAIS UN ENSEMBLE VIDE. │
    │                                                                          │
    │ Rendre zéro ligne ferait lire « ce caissier n'a rien vendu » là où la    │
    │ vérité est « vous n'avez pas le droit de le regarder ». Le marchand      │
    │ n'aurait aucun moyen de distinguer les deux.                             │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    if user_id is None:
        return
    if not membership:
        raise ValidationError({"detail": "Organisation requise."})

    try:
        uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    except (TypeError, ValueError):
        raise ValidationError({"user": "Utilisateur inconnu."})

    if uid == membership.user_id:
        return

    if membership.role == OrganizationMembership.Role.CASHIER:
        raise ValidationError(
            {"user": "Vous ne pouvez consulter que vos propres données."}
        )

    cible = OrganizationMembership.objects.filter(
        organization_id=membership.organization_id, user_id=uid, is_active=True
    ).first()
    if cible is None:
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ UN MEMBRE INTROUVABLE N'EST PAS NOTRE AFFAIRE, ET C'EST MESURÉ. │
        # │                                                                  │
        # │ `user_activity` répond 404 sur un identifiant inconnu, et ce     │
        # │ contrat est publié : les deux clients en tirent leur message.    │
        # │ Lever ici le devancerait d'un 400, et le test qui l'épingle est  │
        # │ tombé au premier passage de la suite.                            │
        # │                                                                  │
        # │ Laisser passer ne coûte rien : le filtre ne trouvera aucune      │
        # │ ligne. Et cela ne renseigne personne - un magasinier reçoit déjà │
        # │ la liste complète de l'équipe par sa session.                    │
        # └──────────────────────────────────────────────────────────────────┘
        return

    cibles = membership_targets_q(membership)
    if cibles is None:
        return

    if not OrganizationMembership.objects.filter(
        cibles,
        organization_id=membership.organization_id,
        user_id=uid,
        is_active=True,
    ).exists():
        raise ValidationError(
            {"user": "Utilisateur hors de votre périmètre."}
        )


def assert_user_allowed_for_request(request, user_id):
    """Variante basée sur la requête (résout le membership via X-Organization-ID)."""
    return assert_user_allowed_for_membership(
        get_membership_for_request(request), user_id
    )
