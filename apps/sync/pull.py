"""
Tirage des données vers un client hors ligne, par curseurs.

Ce que remplace ce module, et pourquoi.

`SyncPullService` découpait chaque table par `[:1000]` **sans `ORDER BY`**. Sur
PostgreSQL, l'ordre d'une requête non ordonnée n'est pas défini : au-delà de
mille lignes, la tranche était arbitraire, elle changeait d'un appel à l'autre,
et les lignes hors tranche n'étaient JAMAIS rattrapées puisque le point de
reprise avançait quand même. Une organisation de 1 200 produits en perdait 200,
définitivement, sans qu'aucun message ne le signale.

Trois principes ici :

1. **Le curseur est un couple `(updated_at, id)`**, pas un horodatage seul. Deux
   lignes écrites dans la même milliseconde seraient sinon soit sautées, soit
   renvoyées en boucle.
2. **`updated_at` ne recule jamais** (`auto_now`). Une ligne modifiée pendant un
   tirage se déplace donc vers la fin de l'ordre : elle sera revue, jamais
   sautée. La reprise est sûre à toute page.
3. **Le point de reprise n'avance que si la table est tirée en entier.** C'est au
   client d'en décider, d'où `has_more` : il conserve son curseur tant que le
   serveur annonce des pages restantes.

Réserve connue : `queryset.update()` ne déclenche pas `auto_now` et rend donc
une écriture invisible au tirage. Le défaut existait déjà avec `sync_updated_at`,
qui n'est posé que dans `save()`. Toute écriture en masse doit toucher
`updated_at` explicitement.
"""
import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.apps import apps
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_permissions import IsTenantMember, _get_membership

# Plafond par page. Ce n'est plus une troncature : au-delà, `has_more` invite le
# client à redemander. Il n'y a plus de perte possible.
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000


# --------------------------------------------------------------------- curseur


def encode_cursor(updated_at, row_id):
    """Curseur opaque. Opaque volontairement : sa forme doit pouvoir changer."""
    payload = {'t': updated_at.isoformat(), 'i': str(row_id)}
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(cursor):
    """Retourne `(datetime, id)` ou `None` si le curseur est absent/illisible."""
    if not cursor:
        return None
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode('utf-8'))
        return datetime.fromisoformat(payload['t']), payload['i']
    except Exception:
        # Un curseur corrompu repart du début plutôt que de faire échouer la
        # synchronisation : le pire cas est de retirer des lignes déjà connues,
        # et l'application cliente les écrase à l'identique.
        return None


#: Marqueur : toutes les colonnes concrètes du modèle.
ALL = ('*',)

#: Ne voyagent jamais. `organization` est implicite (le client n'en connaît
#: qu'une), et les fichiers ne se transportent pas dans une charge JSON.
NEVER_SENT = {'organization', 'password'}


def resolve_fields(model, declared):
    """
    Colonnes réellement lues.

    `ALL` plutôt qu'une liste écrite à la main : ~400 colonnes recopiées, c'est
    autant d'occasions de se tromper, et un champ ajouté au backend n'aurait
    jamais atteint le mobile sans qu'on y pense. Ce qui ne doit pas voyager est
    déclaré une fois, dans `NEVER_SENT`.
    """
    if declared != ALL:
        return tuple(declared)

    names = []
    for f in model._meta.concrete_fields:
        if f.name in NEVER_SENT:
            continue
        # Une clé étrangère voyage par son identifiant, jamais par son objet.
        names.append(f.attname)
    return tuple(names)


# -------------------------------------------------------------------- registre


@dataclass(frozen=True)
class PullTable:
    """Une table exposée au client, et la façon exacte de la lire."""

    name: str
    model: str
    #: Colonnes exposées, ou `ALL` pour toutes les colonnes concrètes.
    fields: tuple = ALL
    #: Comment borner à l'organisation : nom du champ, `'id'` pour
    #: l'organisation elle-même, `None` pour un référentiel global.
    org_field: str = 'organization'
    #: La table porte `is_deleted`/`deleted_at`, donc des pierres tombales.
    soft_delete: bool = False
    #: Chemin de l'entrepôt, pour borner au périmètre du membre. `''` si la
    #: table EST l'entrepôt.
    warehouse_path: str = None
    #: Enfants remplacés en bloc avec leur parent. Réservé aux tables sans
    #: suppression douce, dont aucune suppression ne se propagerait autrement.
    children: tuple = field(default_factory=tuple)

    def get_model(self):
        app_label, model_name = self.model.split('.')
        return apps.get_model(app_label, model_name)


@dataclass(frozen=True)
class PullChild:
    """Enfant tiré avec son parent et remplacé en bloc côté client."""

    name: str
    model: str
    parent_field: str
    fields: tuple = ALL

    def get_model(self):
        app_label, model_name = self.model.split('.')
        return apps.get_model(app_label, model_name)


# ------------------------------------------------------------------ conversion


def _coerce(value):
    """
    Rend une valeur transportable en JSON.

    Les décimales partent en CHAÎNE, jamais en flottant : un panier en francs
    congolais à sept chiffres perd ses unités en virgule flottante, et le client
    applique la même discipline de son côté.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'isoformat'):  # date
        return value.isoformat()
    return value


def _rows(queryset, fields):
    return [
        {key: _coerce(val) for key, val in row.items()}
        for row in queryset.values(*fields)
    ]


# --------------------------------------------------------------------- lecture


def _scope_to_org(queryset, table, organization):
    """Borne à l'organisation. Un référentiel global n'est pas borné."""
    if table.org_field is None:
        return queryset
    if table.org_field == 'id':
        return queryset.filter(id=organization.id)
    return queryset.filter(**{table.org_field: organization})


def _scope_to_warehouses(queryset, table, membership):
    """
    Borne la lecture au périmètre d'entrepôt du membre.

    Un caissier qui tire le stock de toute l'entreprise, c'est à la fois un
    problème de volume et de confidentialité. Un `owner` n'a pas de périmètre :
    il voit tout, comme sur le web.
    """
    if table.warehouse_path is None or membership is None:
        return queryset
    if membership.role == 'owner':
        return queryset

    ids = list(membership.assigned_warehouses.values_list('id', flat=True))
    if not ids:
        # Aucun entrepôt assigné : on ne borne pas. Le web fait le même choix,
        # et borner ici viderait l'écran d'un membre mal configuré au lieu de
        # signaler la configuration.
        return queryset

    if table.warehouse_path == '':
        return queryset.filter(id__in=ids)
    return queryset.filter(**{f'{table.warehouse_path}__in': ids})


def _after_cursor(queryset, cursor, column='updated_at'):
    """Prédicat de reprise sur le couple `(colonne, id)`."""
    if cursor is None:
        return queryset
    moment, row_id = cursor
    return queryset.filter(
        Q(**{f'{column}__gt': moment})
        | Q(**{column: moment}, id__gt=row_id)
    )


def _attach_children(table, rows):
    """
    Ajoute les enfants, en UNE requête par type d'enfant.

    Les lignes de vente et les règlements n'ont pas de suppression douce : aucune
    pierre tombale ne circule, donc une suppression ne se propagerait jamais.
    Plutôt que trois migrations sur des tables chaudes, on les tire imbriqués et
    le client remplace l'ensemble. La cohérence entre une vente et ses lignes
    devient structurelle, au lieu d'être espérée.
    """
    if not table.children or not rows:
        return

    parent_ids = [row['id'] for row in rows]
    by_id = {row['id']: row for row in rows}

    for child in table.children:
        model = child.get_model()
        grouped = {pid: [] for pid in parent_ids}
        queryset = model.objects.filter(**{f'{child.parent_field}__in': parent_ids})
        for item in _rows(queryset, resolve_fields(model, child.fields)):
            parent = item.get(f'{child.parent_field}_id')
            if parent in grouped:
                grouped[parent].append(item)
        for pid, items in grouped.items():
            by_id[pid][child.name] = items


def read_page(table, organization, membership, cursor, limit):
    """
    Une page de la table, plus le curseur de la suivante.

    L'ordre `(updated_at, id)` est imposé : sans lui, la pagination sur
    PostgreSQL rend des tranches arbitraires, ce qui était le défaut central de
    l'ancien tirage.
    """
    model = table.get_model()

    # `_base_manager` : le gestionnaire par défaut masque les lignes supprimées,
    # or le client a besoin de les connaître pour les retirer.
    queryset = _scope_to_org(model._base_manager.all(), table, organization)

    queryset = _scope_to_warehouses(queryset, table, membership)
    if table.soft_delete:
        queryset = queryset.filter(is_deleted=False)
    queryset = _after_cursor(queryset, cursor).order_by('updated_at', 'id')

    # `updated_at` et `id` composent le curseur : ils doivent être lus, même si
    # la table ne les expose pas au client. On les retire ensuite.
    fields = resolve_fields(model, table.fields)
    columns = tuple(dict.fromkeys(fields + ('id', 'updated_at')))

    # Une ligne de plus que la page : sa présence dit s'il en reste, sans
    # compter la table entière à chaque appel.
    page = list(queryset.values(*columns)[: limit + 1])
    has_more = len(page) > limit
    page = page[:limit]

    next_cursor = (
        encode_cursor(page[-1]['updated_at'], page[-1]['id']) if page else None
    )

    exposed = set(fields)
    rows = [
        {key: _coerce(val) for key, val in row.items() if key in exposed}
        for row in page
    ]

    _attach_children(table, rows)
    return rows, next_cursor, has_more


def read_tombstones(table, organization, membership, cursor, limit):
    """
    Identifiants supprimés depuis le curseur.

    Sa propre pagination, sur `deleted_at` : mélanger suppressions et écritures
    dans un seul curseur ferait sauter les unes ou les autres.
    """
    if not table.soft_delete:
        return [], None, False

    model = table.get_model()
    queryset = _scope_to_org(model._base_manager.all(), table, organization)
    queryset = queryset.filter(is_deleted=True)
    queryset = _scope_to_warehouses(queryset, table, membership)
    queryset = _after_cursor(queryset, cursor, column='deleted_at')
    queryset = queryset.order_by('deleted_at', 'id')

    page = list(queryset.values('id', 'deleted_at')[: limit + 1])
    has_more = len(page) > limit
    page = page[:limit]

    next_cursor = (
        encode_cursor(page[-1]['deleted_at'], page[-1]['id']) if page else None
    )
    return [str(row['id']) for row in page], next_cursor, has_more


# ------------------------------------------------------------ tables exposées

#: Enfants d'une vente. Sans suppression douce, aucune pierre tombale ne
#: circulerait : on les remplace en bloc avec leur parent.
SALE_CHILDREN = (
    PullChild(name='items', model='sales.SaleItem', parent_field='sale'),
    PullChild(name='payments', model='sales.Payment', parent_field='sale'),
)

#: Ordre de tirage. Les référentiels d'abord : le point de vente s'ouvre dès que
#: l'organisation, les moyens de paiement, les produits et les stocks sont là,
#: le reste continue en arrière-plan.
PULL_TABLES = (
    # -- identité et paramètres, indispensables au premier écran
    PullTable('organizations', 'organizations.Organization', org_field='id', soft_delete=True),
    PullTable('organization_settings', 'settings.OrganizationSettings'),
    PullTable('currencies', 'settings.Currency', org_field=None),
    PullTable('organization_currencies', 'settings.OrganizationCurrency'),
    PullTable('memberships', 'organizations.OrganizationMembership'),

    # -- référentiels du catalogue
    PullTable('units', 'products.Unit'),
    PullTable('categories', 'products.Category', soft_delete=True),
    PullTable('brands', 'products.Brand', soft_delete=True),
    PullTable('warehouses', 'inventory.Warehouse', soft_delete=True, warehouse_path=''),
    PullTable('stock_locations', 'inventory.StockLocation', warehouse_path='warehouse_id'),
    PullTable('payment_methods', 'sales.PaymentMethod'),

    # -- ce qui fait vendre
    PullTable('products', 'products.Product', soft_delete=True),
    PullTable('product_variants', 'products.ProductVariant', soft_delete=True),
    PullTable('price_lists', 'products.PriceList'),
    PullTable('product_prices', 'products.ProductPrice'),
    PullTable('stocks', 'inventory.Stock', warehouse_path='warehouse_id'),
    PullTable('stock_batches', 'inventory.StockBatch', warehouse_path='warehouse_id'),

    # -- clients et fidélité
    PullTable('customers', 'contacts.Customer', soft_delete=True),
    PullTable('customer_balances', 'contacts.CustomerBalance'),
    PullTable('customer_transactions', 'contacts.CustomerTransaction'),
    PullTable('suppliers', 'contacts.Supplier', soft_delete=True),
    PullTable('loyalty_programs', 'settings.LoyaltyProgram'),
    PullTable('customer_loyalty', 'settings.CustomerLoyalty'),

    # -- caisse et ventes
    PullTable('registers', 'sales.Register', soft_delete=True, warehouse_path='warehouse_id'),
    PullTable('register_sessions', 'sales.RegisterSession'),
    PullTable('sales', 'sales.Sale', soft_delete=True, warehouse_path='warehouse_id',
              children=SALE_CHILDREN),
    PullTable('stock_movements', 'inventory.StockMovement', warehouse_path='warehouse_id'),

    # -- livre de caisse
    PullTable('income_categories', 'cashbook.IncomeCategory'),
    PullTable('expense_categories', 'cashbook.ExpenseCategory'),
    PullTable('expenses', 'cashbook.Expense'),
    PullTable('cash_movements', 'cashbook.CashMovement'),
)

PULL_TABLES_BY_NAME = {table.name: table for table in PULL_TABLES}


# ------------------------------------------------------------------------ vue

#: Version du contrat de tirage. Le client la compare à la sienne et refuse de
#: continuer si le serveur a pris de l'avance : mieux vaut demander une mise à
#: jour que d'écrire des colonnes qu'on ne sait pas lire.
PULL_SCHEMA_VERSION = 1


class SyncPullView(APIView):
    """
    `GET /api/v1/sync/pull/?table=&cursor=&deleted_cursor=&limit=`

    Une table, une page. Le client reboucle tant que `has_more` est vrai, et ne
    déplace son point de reprise qu'une fois la table tirée en entier.

    Volontairement exempté du contrôle d'abonnement : un marchand dont
    l'abonnement a expiré doit pouvoir consulter ses données. C'est l'écriture
    qui se ferme, pas la lecture.
    """

    permission_classes = [IsAuthenticated, IsTenantMember]

    @extend_schema(
        summary="Tirer une page d'une table",
        parameters=[
            OpenApiParameter('table', str, description="Nom de la table à tirer."),
            OpenApiParameter('cursor', str, description="Curseur opaque de la page précédente."),
            OpenApiParameter('deleted_cursor', str, description="Curseur des suppressions."),
            OpenApiParameter('limit', int, description=f"Défaut {DEFAULT_PAGE_SIZE}, plafond {MAX_PAGE_SIZE}."),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        name = request.query_params.get('table')
        table = PULL_TABLES_BY_NAME.get(name)
        if table is None:
            return Response(
                {
                    'detail': f"Table inconnue : {name}",
                    'code': 'unknown_table',
                    'available': sorted(PULL_TABLES_BY_NAME),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            limit = int(request.query_params.get('limit', DEFAULT_PAGE_SIZE))
        except (TypeError, ValueError):
            limit = DEFAULT_PAGE_SIZE
        limit = max(1, min(limit, MAX_PAGE_SIZE))

        membership = _get_membership(request)
        organization = membership.organization

        rows, next_cursor, has_more = read_page(
            table,
            organization,
            membership,
            decode_cursor(request.query_params.get('cursor')),
            limit,
        )
        deleted_ids, next_deleted, deleted_more = read_tombstones(
            table,
            organization,
            membership,
            decode_cursor(request.query_params.get('deleted_cursor')),
            limit,
        )

        return Response({
            'table': table.name,
            'rows': rows,
            'deleted_ids': deleted_ids,
            # Rendus tels quels quand il n'y a plus rien : le client conserve
            # ainsi sa position au lieu de repartir du début au tirage suivant.
            'next_cursor': next_cursor or request.query_params.get('cursor'),
            'next_deleted_cursor': next_deleted or request.query_params.get('deleted_cursor'),
            'has_more': has_more or deleted_more,
            'server_time': timezone.now().isoformat(),
            'schema_version': PULL_SCHEMA_VERSION,
        })


class SyncManifestView(APIView):
    """
    `GET /api/v1/sync/pull/manifest/`

    Ordre de tirage et forme de chaque table. Le client ne code pas cette liste
    en dur : ajouter une table au serveur suffit à la faire descendre, sans
    republier l'application.
    """

    permission_classes = [IsAuthenticated, IsTenantMember]

    @extend_schema(summary="Tables à tirer, dans l'ordre", responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response({
            'schema_version': PULL_SCHEMA_VERSION,
            'default_page_size': DEFAULT_PAGE_SIZE,
            'tables': [
                {
                    'name': t.name,
                    'has_tombstones': t.soft_delete,
                    'children': [c.name for c in t.children],
                }
                for t in PULL_TABLES
            ],
        })
