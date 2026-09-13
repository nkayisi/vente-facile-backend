"""
Rejeu des opérations d'un terminal, par le chemin du back-office.

Ce que remplace ce module.

`SyncPushService` écrivait en base avec `objects.create()`. Une vente poussée
depuis le mobile n'inscrivait donc aucune dette, n'attribuait aucun point de
fidélité, n'entrait pas en caisse, ne recevait pas de numéro de document, ne
vérifiait aucun plafond de crédit et ne consommait aucun lot au coût réel. Le
commentaire du code le disait lui-même : « should be handled by the mobile app ».

Ici, le client n'envoie plus des LIGNES de table mais des ACTES métier, et
chacun est rejoué par le serializer ou le service que le back-office utilise
déjà. La parité n'est pas surveillée, elle est structurelle : il n'existe qu'un
seul chemin.

Trois règles de mise en œuvre, qu'on ne peut pas assouplir.

1. **Aucune transaction n'enveloppe la boucle.** Chaque opération a son propre
   point de sauvegarde. Sans cela, une seule vente fautive ferait échouer les
   deux cents autres, ce qui était exactement le défaut de l'ancienne file
   d'attente.
2. **La trace s'écrit DANS le même point de sauvegarde que l'effet.** Un
   plantage entre les deux laisserait soit un effet sans trace, rejoué au
   prochain envoi, soit une trace sans effet.
3. **`ATOMIC_REQUESTS` doit rester faux.** L'activer envelopperait tout dans une
   transaction unique et désactiverait ce mécanisme en silence. Un test l'affirme.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.db import DatabaseError, IntegrityError, OperationalError, transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import (
    PermissionDenied as DRFPermissionDenied,
    ValidationError as DRFValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_permissions import (
    HasActiveSubscription,
    IsTenantMember,
    _get_membership,
)
from apps.core.clock import horloge_de_l_acte
from apps.core.exceptions import RefusMetier

from .models import SyncOperation

logger = logging.getLogger(__name__)

#: Nombre d'opérations acceptées en un envoi. Au-delà, le client découpe : un
#: lot trop gros sur un lien instable ne se termine jamais.
MAX_OPERATIONS_PER_BATCH = 200


# ------------------------------------------------------------------- contexte


@dataclass
class OperationContext:
    """
    Ce dont un gestionnaire a besoin pour rejouer une opération.

    `request` est la VRAIE requête DRF, pas une imitation :
    `SaleCreateSerializer.create()` lit `self.context['request'].user` et
    l'en-tête `X-Organization-ID`. Fabriquer un objet ressemblant marcherait
    aujourd'hui et casserait au premier serializer qui lit autre chose.
    """

    request: object
    organization: object
    membership: object
    user: object
    #: Permissions effectives du membre, calculées UNE FOIS par lot. Le droit
    #: se vérifie par OPÉRATION (voir `_assert_kind_allowed`), mais le calculer
    #: à chaque opération ferait deux cents lectures pour un envoi de deux cents.
    permissions: frozenset = frozenset()
    device: object = None
    #: Identifiants attribués par les opérations déjà appliquées de ce lot,
    #: pour qu'un règlement puisse viser une vente créée juste avant.
    local_ids: dict = field(default_factory=dict)


def json_safe(value):
    """
    Rend une valeur stockable dans un champ JSON, et transportable.

    `Serializer.data` n'est PAS du JSON : un `PrimaryKeyRelatedField` sur une clé
    UUID rend un objet `UUID`, et une décimale rend un `Decimal`. Les écrire tels
    quels fait échouer l'enregistrement de la trace, donc échouer l'opération
    entière, avec un message (« Object of type UUID is not JSON serializable »)
    qui ne désigne pas sa cause.

    Les décimales partent en CHAÎNE, comme partout ailleurs.
    """
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


class OperationRejected(Exception):
    """Refus métier déterministe. Ne sera jamais réessayé."""

    def __init__(self, message, code='rejected', details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


# ---------------------------------------------------------------- gestionnaires

#: `kind` -> fonction. Ajouter une opération, c'est ajouter une ligne ici et une
#: fonction qui délègue au serializer existant. Rien d'autre.
HANDLERS = {}

#: kind -> code de permission, miroir des `action_permissions` de la vue qui
#: rejoue le même acte. Croisé avec elles par `test_parity_contract`.
#:
#: ┌──────────────────────────────────────────────────────────────────────────┐
#: │ UN ACTE SANS PERMISSION DÉCLARÉE EST REFUSÉ.                            │
#: │                                                                          │
#: │ C'est la règle de `HasPermission` transposée au journal : « action non   │
#: │ listée = accès refusé ». Un acte ajouté sans y penser sera bloqué, et    │
#: │ non ouvert à tous - c'est le défaut qu'a connu `product_supplies`, en    │
#: │ 403 pour tous les rôles pendant des mois faute d'entrée.                 │
#: └──────────────────────────────────────────────────────────────────────────┘
HANDLER_PERMISSIONS = {}


def handler(kind, *, permission):
    def register(fn):
        HANDLERS[kind] = fn
        HANDLER_PERMISSIONS[kind] = permission
        return fn
    return register


def _assert_kind_allowed(ctx, kind):
    """
    Le droit de poser CET acte, vérifié par OPÉRATION.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ SURTOUT PAS `HasPermission` DANS `permission_classes`.                  │
    │                                                                          │
    │ C'est une permission de VUE : un échec rendrait 403 pour tout l'envoi.  │
    │ Un caissier avec deux cents ventes en file et une seule opération        │
    │ d'inventaire les perdrait toutes. C'est le défaut exact de l'ancienne    │
    │ file, que §5.5 interdit : « verdict PAR OPÉRATION, jamais par lot ».     │
    │                                                                          │
    │ Levée ici, `PermissionDenied` devient le verdict `blocked` : conservée,  │
    │ non réessayée, message distinct - et repassera au prochain envoi, car    │
    │ `blocked` n'est pas `is_settled`. Le jour où la permission est accordée, │
    │ l'opération repart d'elle-même.                                          │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    besoin = HANDLER_PERMISSIONS.get(kind)
    if besoin is None:
        raise PermissionDenied(
            f"L'opération « {kind} » ne déclare aucune permission et ne peut pas être appliquée."
        )
    if besoin not in ctx.permissions:
        raise PermissionDenied(
            f"Votre compte n'a pas la permission « {besoin} », "
            f"nécessaire pour cette opération."
        )


def resolve_refs(ctx, payload):
    """
    Remplace les renvois `{"$ref": {...}}` par les identifiants attribués.

    Un règlement peut viser une vente créée par une opération du même lot. Le
    cas ne se produit pas sur le chemin courant, `sale.create` portant déjà ses
    lignes et ses règlements ; il vaut pour les actes réellement décalés, comme
    un règlement ajouté plus tard à une facture émise hors ligne.
    """
    if isinstance(payload, dict):
        ref = payload.get('$ref')
        if isinstance(ref, dict):
            key = (ref.get('op'), ref.get('key'))
            if key not in ctx.local_ids:
                raise OperationRejected(
                    "L'opération dont celle-ci dépend n'a pas abouti.",
                    code='dependency_missing',
                )
            return ctx.local_ids[key]
        return {k: resolve_refs(ctx, v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [resolve_refs(ctx, item) for item in payload]
    return payload


# --------------------------------------------------------------- répartiteur


def _classify(exc):
    """
    Range une exception en verdict.

    La frontière qui compte : un refus MÉTIER ne sera jamais réessayé, une panne
    technique le sera. Se tromper de côté, c'est soit marteler le serveur avec
    une vente qu'il refusera toujours, soit abandonner une vente qu'un simple
    nouvel essai aurait fait passer.
    """
    if isinstance(exc, OperationRejected):
        return SyncOperation.Verdict.REJECTED, {
            'code': exc.code, 'detail': exc.message, 'errors': exc.details,
        }
    if isinstance(exc, (DRFValidationError, DjangoValidationError)):
        detail = getattr(exc, 'detail', None) or getattr(exc, 'message_dict', None) \
            or getattr(exc, 'messages', None) or str(exc)
        return SyncOperation.Verdict.REJECTED, {'code': 'invalid', 'errors': detail}
    if isinstance(exc, (DRFPermissionDenied, PermissionDenied)):
        return SyncOperation.Verdict.BLOCKED, {
            'code': 'forbidden', 'detail': str(exc),
        }
    if isinstance(exc, IntegrityError):
        # Une contrainte violée ne se répare pas en réessayant : c'est une
        # donnée fautive, pas un aléa.
        return SyncOperation.Verdict.REJECTED, {
            'code': 'integrity', 'detail': str(exc),
        }
    if isinstance(exc, RefusMetier):
        # Déterministe par construction : le renvoyer le referait refuser à
        # l'identique, à chaque synchronisation, indéfiniment.
        return SyncOperation.Verdict.REJECTED, {
            'code': 'refus_metier', 'detail': str(exc),
        }
    if isinstance(exc, (ImportError, AttributeError, NameError)):
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ UN DÉFAUT DE CODE NE SE RÉPARE PAS EN RÉESSAYANT.               │
        # │                                                                  │
        # │ Un symbole qui n'existe pas au premier essai n'existera pas au   │
        # │ millième. Ces trois-là tombaient dans le repli `unexpected`,     │
        # │ donc en `retry` : l'opération repartait à CHAQUE                 │
        # │ synchronisation, indéfiniment, pour un acte qui ne passera       │
        # │ jamais - en batterie et en données, sur le terminal d'un         │
        # │ marchand. C'est arrivé : deux handlers de caisse importaient un  │
        # │ serializer au nom inexistant.                                     │
        # │                                                                  │
        # │ ⚠ CE CLASSEMENT NE CORRIGE RIEN, il abrège. Le garde-fou qui     │
        # │ ferme réellement la porte est `test_handler_imports.py`, qui     │
        # │ résout statiquement chaque import différé des handlers. Les deux │
        # │ vont ensemble : sans le test, on enterre le défaut plus vite.    │
        # └──────────────────────────────────────────────────────────────────┘
        return SyncOperation.Verdict.REJECTED, {
            'code': 'handler_defect', 'detail': str(exc),
        }
    if isinstance(exc, (OperationalError, DatabaseError)):
        return SyncOperation.Verdict.RETRY, {'code': 'database', 'detail': str(exc)}
    return SyncOperation.Verdict.RETRY, {'code': 'unexpected', 'detail': str(exc)}


def _replay(existing):
    """Rejoue un verdict déjà rendu, sans rien réécrire."""
    return {
        'operation_id': str(existing.id),
        'verdict': (
            SyncOperation.Verdict.DUPLICATE
            if existing.verdict == SyncOperation.Verdict.APPLIED
            else existing.verdict
        ),
        'server_ids': (existing.result or {}).get('server_ids', {}),
        'authoritative': (existing.result or {}).get('authoritative'),
        'errors': existing.error,
    }


def dispatch(ctx, operations):
    """
    Applique un lot, opération par opération, et rend un verdict pour chacune.

    Aucune transaction n'enveloppe cette boucle : c'est délibéré, et c'est ce
    qui fait qu'une vente refusée n'emporte pas les deux cents autres.
    """
    results = []
    refused = set()

    for op in sorted(operations, key=lambda o: o.get('seq', 0)):
        op_id = op.get('operation_id')
        kind = op.get('kind')

        existing = SyncOperation.objects.filter(pk=op_id).first()
        if existing is not None and existing.is_settled:
            results.append(_replay(existing))
            if existing.verdict == SyncOperation.Verdict.REJECTED:
                refused.add(op_id)
            continue

        depends = op.get('depends_on') or []
        if any(dep in refused for dep in depends):
            # Court-circuit : inutile d'essayer un règlement dont la vente vient
            # d'être refusée. Le client verra la cause en amont.
            results.append({
                'operation_id': op_id,
                'verdict': SyncOperation.Verdict.REJECTED,
                'server_ids': {},
                'authoritative': None,
                'errors': {
                    'code': 'dependency_rejected',
                    'detail': "L'opération dont celle-ci dépend a été refusée.",
                },
            })
            refused.add(op_id)
            continue

        fn = HANDLERS.get(kind)
        if fn is None:
            results.append({
                'operation_id': op_id,
                'verdict': SyncOperation.Verdict.REJECTED,
                'server_ids': {},
                'authoritative': None,
                'errors': {'code': 'unknown_kind', 'detail': f"Opération inconnue : {kind}"},
            })
            refused.add(op_id)
            continue

        # L'HEURE DE L'ACTE, POSÉE POUR TOUTE LA DURÉE DE L'OPÉRATION.
        #
        # Le terminal est le seul à savoir quand l'argent est entré ; le serveur
        # ne sait que quand il l'a appris. Sans cela, `sale_date` et tous les
        # horodatages écrits en aval portaient le jour de la POUSSÉE : trois
        # jours hors ligne empilaient trois journées de recette sur celle du
        # retour, dans les rapports comme sur l'écran du marchand.
        #
        # Elle est posée AUTOUR du gestionnaire, pas passée en paramètre : la
        # traverser demanderait de modifier `SaleCreateSerializer.create()`,
        # `apply_payment_to_sale`, `register_sale_debt` et le décrément de
        # stock, corps que le back-office appelle aussi, pour une valeur qu'il
        # ne fournit jamais. Voir `apps.core.clock`.
        quand = op.get('occurred_at')

        try:
            with transaction.atomic():
                # L'horloge n'enveloppe QUE le gestionnaire, jamais la trace :
                # `SyncOperation` répond à « quand le serveur l'a appris », et
                # la backdater effacerait le seul repère qui reste pour dire
                # qu'une écriture est arrivée en retard. Elle reste dans le même
                # `atomic` que l'effet, comme le contrat l'exige.
                with horloge_de_l_acte(quand):
                    _assert_kind_allowed(ctx, kind)
                    payload = resolve_refs(ctx, op.get('payload') or {})
                    outcome = json_safe(fn(ctx, payload) or {})

                SyncOperation.objects.update_or_create(
                    pk=op_id,
                    defaults={
                        'organization': ctx.organization,
                        'device': ctx.device,
                        'user': ctx.user,
                        'kind': kind,
                        'seq': op.get('seq') or 0,
                        'payload': op.get('payload'),
                        'result': outcome,
                        'error': None,
                        'verdict': SyncOperation.Verdict.APPLIED,
                        'http_status': 201,
                        'occurred_at': quand or timezone.now(),
                    },
                )

            for key, value in (outcome.get('server_ids') or {}).items():
                ctx.local_ids[(op_id, key)] = value

            results.append({
                'operation_id': op_id,
                'verdict': SyncOperation.Verdict.APPLIED,
                'server_ids': outcome.get('server_ids', {}),
                'authoritative': outcome.get('authoritative'),
                'errors': None,
            })

        except Exception as exc:  # noqa: BLE001 - on classe, on ne masque pas
            verdict, error = _classify(exc)
            error = json_safe(error)
            logger.warning('Opération %s (%s) : %s', op_id, kind, error)

            if verdict != SyncOperation.Verdict.RETRY:
                # Le point de sauvegarde a été défait : la trace s'écrit dans sa
                # propre transaction, sinon elle emporterait le rejet avec elle.
                with transaction.atomic():
                    SyncOperation.objects.update_or_create(
                        pk=op_id,
                        defaults={
                            'organization': ctx.organization,
                            'device': ctx.device,
                            'user': ctx.user,
                            'kind': kind,
                            'seq': op.get('seq') or 0,
                            'payload': op.get('payload'),
                            'result': None,
                            'error': error,
                            'verdict': verdict,
                            'occurred_at': quand or timezone.now(),
                        },
                    )
                refused.add(op_id)

            results.append({
                'operation_id': op_id,
                'verdict': verdict,
                'server_ids': {},
                'authoritative': None,
                'errors': error,
            })

    return results


# ------------------------------------------------------------------------ vue


class SyncOperationsView(APIView):
    """
    `POST /api/v1/sync/operations/`

    Reçoit un lot d'actes métier et rend un verdict pour chacun.

    Soumis au contrôle d'abonnement, contrairement au tirage : lire ses données
    reste ouvert, écrire ne l'est pas.

    Le contrôle passe par `HasActiveSubscription`, comme sur `SaleViewSet`, et
    non par `SubscriptionMiddleware` : celui-ci s'arrête sur un utilisateur non
    authentifié, or un client JWT l'est encore quand les middlewares tournent,
    l'authentification étant faite plus tard par DRF. Le middleware est donc
    inerte pour toute l'API. Son exemption `/api/v1/sync/`, testée en
    `startswith`, a tout de même été resserrée : elle aurait couvert cet endpoint
    le jour où le middleware serait réparé.
    """

    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription]

    @extend_schema(
        summary="Rejouer un lot d'opérations",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        operations = request.data.get('operations')
        if not isinstance(operations, list) or not operations:
            return Response(
                {'detail': 'Aucune opération fournie.', 'code': 'no_operations'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(operations) > MAX_OPERATIONS_PER_BATCH:
            return Response(
                {
                    'detail': f'Au plus {MAX_OPERATIONS_PER_BATCH} opérations par envoi.',
                    'code': 'batch_too_large',
                    'max': MAX_OPERATIONS_PER_BATCH,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        manquants = [
            op.get('operation_id') for op in operations
            if not op.get('operation_id') or not op.get('kind')
        ]
        if manquants:
            return Response(
                {
                    'detail': "Chaque opération doit porter un `operation_id` et un `kind`.",
                    'code': 'malformed_operations',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership = _get_membership(request)
        device = None
        device_id = request.data.get('device_id')
        if device_id:
            from apps.users.models import Device
            device = Device.objects.filter(
                id=device_id, organization=membership.organization
            ).first()

        from apps.core.services import PermissionService

        ctx = OperationContext(
            request=request,
            organization=membership.organization,
            membership=membership,
            user=request.user,
            permissions=frozenset(
                PermissionService.get_effective_permissions(membership)
            ),
            device=device,
        )

        results = dispatch(ctx, operations)

        # Le terminal repousse l'échéance de sa session en envoyant : un
        # appareil qui travaille ne doit pas expirer.
        if device is not None:
            device.touch()

        return Response({
            'batch_id': request.data.get('batch_id'),
            'server_time': timezone.now().isoformat(),
            'results': results,
        })
