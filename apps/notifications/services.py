"""
Qui est prévenu, et par quel canal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE MODÈLE EXISTAIT, LA LIVRAISON N'EXISTAIT PAS.                            │
│                                                                              │
│ `NotificationTemplate` déclare un `channel` (email, SMS, push, in-app),      │
│ `Notification`, `Alert`, `AlertRule` et `EmailLog` sont là depuis l'origine. │
│ Mais il y avait UN SEUL `Notification.objects.create` dans tout le backend   │
│ - celui de l'abonnement qui expire - et aucun canal branché. Six familles    │
│ d'alertes se créaient sans prévenir personne : elles ne se voyaient qu'en    │
│ ouvrant l'écran qui les liste, c'est-à-dire par celui qui les cherchait      │
│ déjà.                                                                        │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ **LA RÈGLE DE PÉRIMÈTRE EST POSÉE ICI, ET UNE SEULE FOIS.** Un gérant est
alerté pour SES dépôts, le propriétaire pour tous. Recopier ce calcul dans
chaque producteur d'alerte ferait, à la première divergence, qu'un magasinier
reçoit les ruptures du dépôt d'en face - ou, bien pire, qu'il ne reçoit pas les
siennes et croit que tout va bien.

⚠ **LE DROIT D'ÊTRE PRÉVENU EST LE DROIT DE VOIR.** On ne tient pas une seconde
table « quel rôle reçoit quel type d'alerte » : elle dériverait du catalogue de
permissions sans que rien ne le signale. Une alerte de stock va à qui peut voir
le stock, une alerte de créance à qui peut voir les clients, une alerte
d'abonnement à qui peut voir l'abonnement - c'est-à-dire au propriétaire seul,
et le catalogue le dit déjà.
"""
import logging

from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone
from django.db.models import Case, Q, TextField, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, NullIf

from apps.core.bulk import bulk_update_rows
from apps.core.services import PermissionService
from apps.notifications.models import Alert, Notification
from apps.organizations.models import OrganizationMembership

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Le droit de voir, par type d'alerte
# ---------------------------------------------------------------------------

#: La permission qu'il faut détenir pour être prévenu d'une alerte.
#:
#: ⚠ Ce sont des codes du CATALOGUE, pas des rôles. Un marchand qui accorde
#: `stock.view` à son caissier par `extra_permissions` veut qu'il soit prévenu
#: des ruptures, et c'est ce que cette table produit sans qu'on y pense.
PERMISSION_PAR_ALERTE = {
    Alert.AlertType.LOW_STOCK: 'stock.view',
    Alert.AlertType.OUT_OF_STOCK: 'stock.view',
    Alert.AlertType.EXPIRING_PRODUCT: 'stock.view',
    Alert.AlertType.EXPIRED_PRODUCT: 'stock.view',
    Alert.AlertType.PAYMENT_DUE: 'customers.view',
    Alert.AlertType.PAYMENT_OVERDUE: 'customers.view',
    Alert.AlertType.CREDIT_LIMIT: 'customers.view',
    Alert.AlertType.SUBSCRIPTION_EXPIRING: 'subscription.view',
    Alert.AlertType.SUBSCRIPTION_EXPIRED: 'subscription.view',
    # Une sortie qui ne rapporte rien - casse, péremption - concerne qui voit
    # le stock du dépôt, pas le seul propriétaire.
    Alert.AlertType.STOCK_EXIT: 'stock.view',
    # ⚠ `APPROVAL` n'entre PAS dans le fan-out par droit de VOIR : une demande
    # va à qui peut TRANCHER (`PERMISSION_DE_DECISION`) et une décision au seul
    # demandeur. Les deux passent par `_livrer_a_qui_peut` et `_notifier_un`,
    # jamais par `notifier`. La valeur reste déclarée pour que la LISTE des
    # alertes se borne correctement : la lire est bien un droit de voir.
    Alert.AlertType.APPROVAL: 'dashboard.view',
    # ⚠ `SYSTEM` n'a pas de droit NOMMÉ, et c'est délibéré : une alerte système
    # ne porte sur aucune rubrique. Elle va au propriétaire, qui est le seul à
    # pouvoir agir sur la configuration de l'établissement.
    Alert.AlertType.SYSTEM: None,
}

#: Le ton d'une notification, déduit de la gravité de l'alerte.
TON_PAR_GRAVITE = {
    Alert.Severity.LOW: Notification.NotificationType.INFO,
    Alert.Severity.MEDIUM: Notification.NotificationType.WARNING,
    Alert.Severity.HIGH: Notification.NotificationType.WARNING,
    Alert.Severity.CRITICAL: Notification.NotificationType.ERROR,
}


def _entrepot_de_lalerte(alert):
    """
    Le dépôt que l'alerte concerne, ou `None` si elle n'en vise aucun.

    ⚠ `None` ne veut PAS dire « tous les dépôts » : il veut dire que l'alerte
    ne porte sur aucun, comme un abonnement qui expire. Les confondre ferait
    taire une alerte d'établissement pour tout rôle borné.
    """
    if alert.resource_type == 'warehouse' and alert.resource_id:
        return str(alert.resource_id)
    valeur = (alert.data or {}).get('warehouse_id') or (alert.data or {}).get('warehouse')
    return str(valeur) if valeur else None


class PerimetreAlertes:
    """
    Ce qu'un membre a le droit de VOIR. Deux emplois, un seul critère.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE MÊME PÉRIMÈTRE, EN SQL ET EN PYTHON - ET UN TEST QUI LES CROISE.     │
    │                                                                          │
    │ `destinataires` doit juger UNE alerte contre TOUS les membres ; la liste │
    │ doit juger TOUTES les alertes contre UN membre. Deux formes, donc, et le │
    │ dépôt a déjà payé ce qu'il arrive quand on les écrit séparément          │
    │ (`membership_targets_q` : « écrits séparément, ils avaient divergé »).   │
    │ `retient()` et `borner()` sortent donc de la même classe, et            │
    │ `PerimetreCroiseTests` les croise DANS LES DEUX SENS.                   │
    │                                                                          │
    │ ⚠ CE QUI RENDAIT LA LISTE INTENABLE. `AlertViewSet.filter_queryset`     │
    │   appelait `destinataires(a)` par alerte, AVANT la pagination (DRF       │
    │   filtre puis pagine) : chaque appel rejouait la requête d'appartenance  │
    │   et son `prefetch_related`. Sur la base de développement - 1042 alertes │
    │   de stock - un `GET /alerts/` chargeait toute la table, émettait ~2000  │
    │   requêtes et bâtissait un `id__in` de mille éléments. Sa docstring      │
    │   affirmait « la liste est bornée par la pagination » : c'était faux.    │
    └──────────────────────────────────────────────────────────────────────────┘

    Les deux lectures coûteuses - permissions effectives et entrepôts assignés
    - sont faites UNE fois, à la construction.
    """

    def __init__(self, membership):
        self.membership = membership
        self.est_proprietaire = (
            membership.role == OrganizationMembership.Role.OWNER
        )
        self.permissions = set(
            PermissionService.get_effective_permissions(membership)
        )
        # ⚠ Le propriétaire n'a AUCUNE affectation - son accès vient de son
        #   rôle - et un rôle borné sans affectation ne reçoit rien. `None` et
        #   l'ensemble vide sont donc deux situations OPPOSÉES.
        self.entrepots = (
            None if self.est_proprietaire
            else {str(w.id) for w in membership.assigned_warehouses.all()}
        )

    # --- Ce qu'il peut voir, par type ---------------------------------------

    def types_visibles(self):
        """Les types d'alerte que ce membre a le droit de lire.

        C'est une COLONNE RÉELLE (`alert_type`), donc le gros du tri se fait en
        SQL sans toucher au JSON.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ ON ITÈRE `Alert.AlertType`, PAS LES CLÉS DÉCLARÉES.                 │
        │                                                                      │
        │ Cette boucle parcourait `PERMISSION_PAR_ALERTE.items()` quand        │
        │ `retient()` lit `.get(alert_type, None)`. Un type ABSENT de la table │
        │ tombait donc dans la branche de `SYSTEM: None` - « le propriétaire,  │
        │ et lui seul » - côté Python, et n'apparaissait JAMAIS dans le        │
        │ `alert_type__in` côté SQL.                                           │
        │                                                                      │
        │ Un `AlertType` ajouté sans entrée dans la table serait donc NOTIFIÉ  │
        │ au propriétaire et INVISIBLE dans la liste de tout le monde, lui     │
        │ compris : l'`action_url` de la notification mènerait à un écran où   │
        │ l'alerte n'est pas. C'est exactement la divergence que l'encadré de  │
        │ cette classe affirme empêcher.                                        │
        │                                                                      │
        │ En partant de l'énumération et en appliquant le MÊME défaut, les     │
        │ deux côtés s'accordent PAR CONSTRUCTION, pour tout type présent ou   │
        │ futur. Le garde-fou de `test_livraison_alertes` exige en plus que la │
        │ table soit complète, pour qu'un type neuf reçoive une DÉCISION       │
        │ plutôt que de tomber en propriétaire-seul par accident.              │
        └──────────────────────────────────────────────────────────────────────┘
        """
        retenus = []
        for genre in Alert.AlertType.values:
            besoin = PERMISSION_PAR_ALERTE.get(genre, None)
            if besoin is None:
                # Aucune rubrique nommée : le propriétaire, et lui seul.
                if self.est_proprietaire:
                    retenus.append(genre)
            elif besoin in self.permissions:
                retenus.append(genre)
        return retenus

    # --- Le périmètre, en SQL -----------------------------------------------

    def borner(self, queryset):
        """Le même critère, appliqué par la base.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ ON CALCULE EN SQL CE QUE `_entrepot_de_lalerte` CALCULE EN PYTHON.  │
        │                                                                      │
        │ Trois sources, dans cet ordre : `resource_type='warehouse'` +        │
        │ `resource_id`, puis `data['warehouse_id']`, puis `data['warehouse']`.│
        │ L'ordre compte - le Python emploie un `or` - et c'est ce que le      │
        │ `Case`/`Coalesce` reproduit, plutôt qu'une disjonction de `Q` qui    │
        │ perdrait la précédence.                                              │
        └──────────────────────────────────────────────────────────────────────┘

        ⚠ `KeyTextTransform` ET NON `data__cle`, ET C'EST MESURÉ. Une première
        écriture employait `~Q(data__warehouse_id=None)` pour dire « l'alerte
        désigne un dépôt » : sur un `JSONField`, une égalité à `None` ne
        matche que le `null` JSON, donc une clé ABSENTE la satisfaisait par la
        négation et l'alerte était traitée comme désignant un dépôt. Les trois
        formes « aucun dépôt » disparaissaient alors de la liste de TOUT rôle
        borné, et c'est le test croisé qui l'a nommé.

        `KeyTextTransform` passe par l'opérateur `->>` : une clé absente ET un
        `null` JSON rendent tous deux un `NULL` SQL, et `NullIf` y joint la
        chaîne vide. Les trois cas limites se confondent donc en un seul, qui
        est exactement « cette alerte ne vise aucun dépôt ».
        """
        queryset = queryset.filter(alert_type__in=self.types_visibles())
        if self.entrepots is None:
            # Aucune restriction : son accès vient du RÔLE.
            return queryset

        depot = Case(
            When(
                Q(resource_type='warehouse') & Q(resource_id__isnull=False),
                then=Cast('resource_id', TextField()),
            ),
            default=Coalesce(
                NullIf(KeyTextTransform('warehouse_id', 'data'), Value('')),
                NullIf(KeyTextTransform('warehouse', 'data'), Value('')),
            ),
            output_field=TextField(),
        )
        return queryset.annotate(_depot_alerte=depot).filter(
            # ⚠ `isnull` ici porte sur l'ANNOTATION, pas sur du JSON : c'est un
            #   `NULL` SQL ordinaire, et il veut dire « aucun dépôt visé ».
            #   `None` ne veut PAS dire « tous les dépôts » - les confondre
            #   ferait taire une alerte d'établissement pour tout rôle borné.
            Q(_depot_alerte__isnull=True)
            | Q(_depot_alerte__in=sorted(self.entrepots))
        )

    # --- Le même critère, en Python -----------------------------------------

    def couvre_le_depot(self, alert):
        """Le dépôt que l'alerte vise est-il dans le périmètre de ce membre ?

        ⚠ UNE ALERTE QUI NE VISE AUCUN DÉPÔT EST COUVERTE. `None` ne veut pas
        dire « tous les dépôts » : il veut dire qu'elle n'en vise aucun, comme
        un abonnement qui expire. Les confondre ferait taire une alerte
        d'établissement pour tout rôle borné.
        """
        depot = _entrepot_de_lalerte(alert)
        if not depot or self.entrepots is None:
            return True
        return depot in self.entrepots

    def retient(self, alert):
        """Ce membre doit-il être prévenu de CETTE alerte ?"""
        besoin = PERMISSION_PAR_ALERTE.get(alert.alert_type, None)
        if besoin is None:
            if not self.est_proprietaire:
                return False
        elif besoin not in self.permissions:
            return False

        return self.couvre_le_depot(alert)


def perimetre_de(membership):
    """Le périmètre d'un membre, ou `None` quand il n'y a pas de membre."""
    return PerimetreAlertes(membership) if membership else None


def destinataires(alert, perimetres=None):
    """
    Les membres à prévenir, et c'est la seule main qui en décide.

    Trois conditions, dans cet ordre :

    1. **Le membre est actif.** Un compte désactivé ne doit plus rien recevoir.
    2. **Il a le droit de VOIR** ce sur quoi porte l'alerte (voir
       `PERMISSION_PAR_ALERTE`). Prévenir quelqu'un d'un fait qu'il ne peut pas
       consulter l'envoie buter sur un 403.
    3. **Le dépôt est dans son périmètre**, quand l'alerte en vise un. Le
       propriétaire n'a aucune affectation - son accès vient de son RÔLE - donc
       il reçoit tout ; un rôle borné SANS affectation ne reçoit rien, et c'est
       la règle déjà tenue partout ailleurs.

    ⚠ LES DEUX DERNIÈRES CONDITIONS VIVENT DANS `PerimetreAlertes.retient`, et
    c'est tout l'intérêt : la LISTE des alertes les applique en SQL, et deux
    expressions de la même règle finissent par montrer une alerte reçue en
    notification et introuvable dans la liste. Seule la première reste ici -
    elle porte sur le COMPTE, pas sur l'alerte.

    ⚠ `perimetres` EST CE QUI REND CETTE FONCTION UTILISABLE EN LOT. Les deux
    lectures coûteuses - appartenance et entrepôts assignés - ne dépendent pas
    de l'alerte : `notifier_en_lot` les fait UNE fois pour tout son lot et les
    passe ici, donc juger cent alertes ne coûte pas cent fois le périmètre.
    Sans ce paramètre, la version en lot aurait dû recopier la règle, et ce
    nom-ci serait devenu une lecture que seuls les tests appellent.
    """
    couples = (
        perimetres if perimetres is not None
        else [(m, PerimetreAlertes(m)) for m in membres_actifs(alert.organization)]
    )
    return [membre for membre, perimetre in couples if perimetre.retient(alert)]


def membres_actifs(organization):
    """Les membres qu'on peut prévenir, avec ce qu'il faut pour en décider.

    ⚠ `prefetch_related` et non un `.filter()` sur le M2M : filtrer
    court-circuiterait le cache du préchargement, et on paierait le
    préchargement pour le jeter - le défaut déjà relevé sur
    `get_membership_for_request`.
    """
    return list(
        OrganizationMembership.objects
        .filter(organization=organization, is_active=True, user__is_active=True)
        .select_related('user')
        .prefetch_related('assigned_warehouses')
    )


# ---------------------------------------------------------------------------
# La livraison
# ---------------------------------------------------------------------------

def _deja_notifie(alert, user):
    """
    Cette alerte a-t-elle déjà prévenu ce membre ?

    ⚠ L'IDEMPOTENCE PASSE PAR `data`, pas par un champ de liaison. `Notification`
    n'a aucune clé vers `Alert` - l'ajouter serait une migration sur une table
    chaude pour une relation que seul ce service emploie - et une tâche Celery
    qui repasse sur une alerte encore active ne doit pas sonner une seconde
    fois. Un marchand qui reçoit trois fois la même rupture cesse de les lire.
    """
    return Notification.objects.filter(
        organization=alert.organization,
        user=user,
        data__alert_id=str(alert.id),
    ).exists()


@transaction.atomic
def notifier_en_lot(organization, paires):
    """
    Produit les notifications de PLUSIEURS alertes, à coût constant.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ C'EST LE CORPS UNIQUE, ET `notifier` N'EN EST QU'UN CAS À UNE ALERTE.   │
    │                                                                          │
    │ Deux chemins de livraison finiraient par ne plus prévenir les mêmes      │
    │ personnes, et la divergence ne se verrait que sur le téléphone d'un      │
    │ marchand qui n'a pas reçu ce que son collègue a reçu.                    │
    └──────────────────────────────────────────────────────────────────────────┘

    `paires` est une liste de `(alerte, (action_url, action_label))`.

    ⚠ TROIS LECTURES, ET TROIS SEULEMENT, quel que soit le nombre d'alertes :
    les membres (une requête plus son préchargement), les notifications déjà
    posées (une requête pour TOUT le lot), et l'insertion en masse. Le
    battement bas-de-stock appelait `alerter` par rayon, donc par rayon : une
    requête d'appartenance, son préchargement, un contrôle d'idempotence PAR
    DESTINATAIRE, et un `urlopen` bloquant dès qu'un appareil portait un jeton.
    Cinquante établissements, cent rayons, cinq membres : environ quarante-cinq
    mille requêtes et jusqu'à cinq mille appels HTTP séquentiels, sur un VPS à
    deux cœurs.
    """
    if not paires:
        return []

    # Les membres, et leur périmètre : calculés UNE fois pour tout le lot.
    perimetres = [(m, PerimetreAlertes(m)) for m in membres_actifs(organization)]
    if not perimetres:
        return []

    # ⚠ L'IDEMPOTENCE EN UNE SEULE REQUÊTE. `_deja_notifie` en faisait une par
    #   couple (alerte, destinataire) : c'est elle qui multipliait le coût, et
    #   `data__alert_id` n'a aucun index GIN.
    ids = [str(a.id) for a, _ in paires]
    deja = set(
        Notification.objects
        .filter(organization=organization, data__alert_id__in=ids)
        .annotate(_aid=KeyTextTransform('alert_id', 'data'))
        .values_list('user_id', '_aid')
    )

    creees = []
    for alerte, (action_url, action_label) in paires:
        aid = str(alerte.id)
        # ⚠ PAR `destinataires`, et non par une seconde boucle : c'est le seul
        #   nom qui décide de qui reçoit quoi, et le lui passer déjà calculé
        #   est ce qui garde le coût constant.
        for membre in destinataires(alerte, perimetres):
            if (membre.user_id, aid) in deja:
                continue
            creees.append(Notification(
                organization=organization,
                user=membre.user,
                notification_type=TON_PAR_GRAVITE.get(
                    alerte.severity, Notification.NotificationType.WARNING
                ),
                title=alerte.title,
                message=alerte.message,
                action_url=action_url,
                action_label=action_label,
                # ⚠ `alert_id` EST la clé d'idempotence, et le lien que le
                # modèle n'a pas. L'écran s'en sert aussi pour mener à l'alerte.
                data={**(alerte.data or {}), 'alert_id': aid,
                      'alert_type': alerte.alert_type},
            ))
            # Un même membre n'est pas notifié deux fois par un lot qui
            # porterait deux fois la même alerte.
            deja.add((membre.user_id, aid))

    if creees:
        Notification.objects.bulk_create(creees)
        # UN seul envoi pour tout le lot : `envoyer()` sait déjà découper par
        # cent, ce qui est la taille qu'Expo accepte.
        _pousser(creees)
    return creees


def notifier(alert, *, action_url='', action_label=''):
    """
    Produit une notification par destinataire. Rend celles qui ont été créées.

    Appelée par les tâches Celery APRÈS la création de l'alerte, et par aucun
    autre chemin : une alerte qui prévient toute seule à la sauvegarde
    sonnerait aussi pour les alertes qu'une migration ou une fixture écrit.

    Ce n'est qu'un `notifier_en_lot` à une alerte.
    """
    return notifier_en_lot(
        alert.organization, [(alert, (action_url, action_label))]
    )


def borner_titre(valeur):
    """
    Le titre d'une alerte, borné à ce que la colonne accepte.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ DJANGO NE VALIDE PAS SUR `create()`, ET POSTGRESQL LÈVE.                │
    │                                                                          │
    │ Six titres concaténaient un nom de 255 signes dans un `CharField(255)` : │
    │                                                                          │
    │   f"Stock bas : {produit.name} ({rayon.warehouse.name})"   -> 526        │
    │   f"{libelle} : {mouvement.product.name}"                  -> 280        │
    │   f"Paiement en retard : {sale.customer.name}"             -> 275        │
    │                                                                          │
    │ `Product.name`, `Warehouse.name` et `Customer.name` sont tous à 255. Le  │
    │ `DataError` qui en résulte AVORTE le battement Celery - donc aucune      │
    │ alerte pour les organisations suivantes - et, sur le chemin d'une sortie │
    │ de stock, il annulait un mouvement que le magasinier venait de valider.  │
    │                                                                          │
    │ La borne est LUE DU MODÈLE, jamais recopiée : un `max_length` qui change │
    │ ne doit pas laisser deux vérités dans le dépôt.                          │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ L'ÉLISION EST AU MILIEU, PAS À DROITE. La fin d'un titre d'alerte porte
    l'entrepôt (« Stock bas : … (Dépôt Nord) »), et c'est lui qui la rend
    adressable : couper par la droite rendrait tous les titres longs d'un même
    article indistinguables.
    """
    borne = Alert._meta.get_field('title').max_length
    valeur = valeur or ''
    if len(valeur) <= borne:
        return valeur
    # Un seul caractère d'élision, et les deux moitiés de ce qui reste.
    reste = borne - 1
    tete = reste // 2
    return f"{valeur[:tete]}…{valeur[len(valeur) - (reste - tete):]}"


@transaction.atomic
def alerter_en_lot(organization, lots, *, resoudre=None):
    """
    Crée PLUSIEURS alertes et prévient, à coût constant.

    `lots` est une liste de dictionnaires de champs, `action_url` et
    `action_label` compris - exactement ce que `alerter` prend.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `resoudre` RÉSOUT LES PÉRIMÉES DANS LE MÊME `atomic` QUE LES NOUVELLES. │
    │                                                                          │
    │ Le battement bas-de-stock résolvait les alertes périmées de TOUT LE PARC │
    │ en une écriture, PUIS créait les remplaçantes établissement par          │
    │ établissement. Une organisation qui levait - un `DataError`, un          │
    │ interblocage, le SIGKILL d'une limite de tâche - laissait toutes les     │
    │ suivantes avec leur ancienne alerte RÉSOLUE et aucune remplaçante : la   │
    │ liste et la pastille du marchand ne montraient alors RIEN pour un rayon  │
    │ réellement vide.                                                         │
    │                                                                          │
    │ Résolu ici, un échec laisse l'ancienne alerte ACTIVE - une alerte qui    │
    │ s'attarde est le bon côté de l'erreur. C'est le même raisonnement que    │
    │ « les lignes de vente sont tirées imbriquées dans leur parent ».         │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ PAR `bulk_update_rows`, ET NON PAR UN `update()` NU. Le garde-fou de
    `test_bulk_write_visibility` l'a d'ailleurs attrapé ici, par l'AST, et il
    avait raison : « une règle qu'il faut se rappeler à chaque appel finit par
    être oubliée ». L'argument de l'exemption - les alertes ne sont pas au
    manifeste de tirage, elles se lisent EN LIGNE - est vrai AUJOURD'HUI, et
    c'est précisément celui que ce module existe pour refuser. `push.py` obéit
    déjà de la même main, dans cette application, motif écrit.
    """
    if resoudre:
        bulk_update_rows(
            Alert.objects.filter(id__in=resoudre),
            status=Alert.Status.RESOLVED,
            resolved_at=timezone.now(),
        )

    if not lots:
        return []

    objets, actions = [], []
    for brut in lots:
        champs = dict(brut)
        actions.append((
            champs.pop('action_url', ''), champs.pop('action_label', ''),
        ))
        # ⚠ LE TITRE EST BORNÉ ICI, au seul passage obligé des producteurs :
        #   aucun d'eux ne peut l'oublier. `notifier_en_lot` recopie ensuite
        #   `alerte.title` dans un champ de même largeur, qui est donc sûr du
        #   même coup.
        if 'title' in champs:
            champs['title'] = borner_titre(champs['title'])
        objets.append(Alert(organization=organization, **champs))

    # `bulk_create` est sûr ici : la clé primaire est un `uuid4` posé côté
    # client, `Alert` n'a aucun `save()` surchargé, et la table n'est PAS au
    # manifeste de tirage - une alerte se lit en ligne.
    alertes = Alert.objects.bulk_create(objets)
    notifier_en_lot(organization, list(zip(alertes, actions)))
    return alertes


def alerter(organization, **champs):
    """
    Crée une alerte ET prévient, en un seul geste.

    Les sept producteurs appelaient `Alert.objects.create` et s'arrêtaient là.
    Ce point d'entrée unique est ce qui rend l'oubli impossible : il n'existe
    plus de chemin qui crée une alerte sans décider qui la reçoit.

    Ce n'est qu'un `alerter_en_lot` à une alerte : le corps est partagé, donc
    les deux chemins ne peuvent pas prévenir des personnes différentes.
    """
    return alerter_en_lot(organization, [champs])[0]


# ---------------------------------------------------------------------------
# Les actes qui attendent une décision, et les décisions prises
# ---------------------------------------------------------------------------
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ ON PRÉVIENT DEPUIS LE SERVICE, PAS DEPUIS LA VUE.                       │
# │                                                                          │
# │ Un transfert se soumet au back-office ET par le journal du terminal ; le │
# │ brancher sur la vue laisserait muet tout ce qui vient du comptoir, c'est- │
# │ à-dire la moitié des demandes. C'est la doctrine de ce dépôt : l'effet   │
# │ vit dans le service que les deux surfaces appellent.                     │
# └──────────────────────────────────────────────────────────────────────────┘

def _nom(user):
    """
    Le nom d'une personne, ou « quelqu'un » : on n'affiche jamais un vide.

    ⚠ C'est `full_name`, une PROPRIÉTÉ, et non `get_full_name()` de Django :
    ce projet a son propre modèle d'utilisateur. L'appeler lève un
    `AttributeError` qui, dans un service de notification, se propage à l'acte
    qu'il accompagne - une approbation d'ajustement aurait échoué pour un
    message.
    """
    if user is None:
        return "quelqu'un"
    return user.full_name or user.email


def demande_approbation(
    organization, *, quoi, reference, par, entrepot=None, resource_type='',
    resource_id=None, detail='', action_url='',
):
    """
    « Une décision vous attend. »

    ⚠ ELLE N'EST PAS ADRESSÉE À CELUI QUI DEMANDE. Un magasinier qui soumet un
    transfert sait qu'il l'a soumis ; lui renvoyer sa propre demande fait du
    bruit, et le bruit est ce qui apprend à ne plus lire les alertes. Le fan-out
    s'en charge par le droit : `requisitions.approve` et `stock_transfers.ship`
    ne vont pas au magasinier.
    """
    alerte = Alert.objects.create(
        organization=organization,
        alert_type=Alert.AlertType.APPROVAL,
        severity=Alert.Severity.MEDIUM,
        title=borner_titre(f"{quoi} à approuver : {reference}"),
        message=(
            f"{_nom(par)} a soumis {quoi.lower()} {reference}"
            + (f" ({detail})" if detail else "")
            + ". Une décision est attendue."
        ),
        resource_type=resource_type,
        resource_id=resource_id,
        data={
            'kind': 'approval_request',
            'reference': reference,
            **({'warehouse_id': str(entrepot)} if entrepot else {}),
        },
    )
    # ⚠ LE DROIT D'APPROUVER, PAS LE DROIT DE VOIR : c'est le seul cas où les
    # deux diffèrent, et prévenir qui ne peut pas décider ne sert à rien.
    _livrer_a_qui_peut(alerte, _permission_de_decision(resource_type),
                       sauf=par, action_url=action_url,
                       action_label='Voir la demande')
    return alerte


def decision_prise(
    organization, *, quoi, reference, decidee_par, pour, approuvee,
    entrepot=None, motif='', action_url='',
):
    """
    « Votre demande a été tranchée. »

    Le pendant du précédent, et le retour de test le demande nommément : « plus
    un message quand la décision est prise ». Sans lui, celui qui a soumis
    rouvre son écran toutes les heures pour savoir où il en est.

    ⚠ ELLE VA À CELUI QUI A DEMANDÉ, et à lui seul : c'est SA demande, et le
    fan-out par droit enverrait la nouvelle à tous les gérants de l'organisation.
    """
    verdict = "approuvée" if approuvee else "rejetée"
    alerte = Alert.objects.create(
        organization=organization,
        alert_type=Alert.AlertType.APPROVAL,
        severity=Alert.Severity.LOW if approuvee else Alert.Severity.MEDIUM,
        title=borner_titre(f"{quoi} {verdict} : {reference}"),
        message=(
            f"{_nom(decidee_par)} a {verdict} {quoi.lower()} {reference}."
            + (f" Motif : {motif}" if motif else "")
        ),
        data={
            'kind': 'approval_decision',
            'reference': reference,
            'approved': approuvee,
            **({'warehouse_id': str(entrepot)} if entrepot else {}),
        },
    )
    if pour is not None and pour.is_active:
        _notifier_un(
            alerte, pour,
            action_url=action_url, action_label='Voir la décision',
        )
    return alerte


#: Le droit qui permet de TRANCHER, par nature de pièce.
#:
#: ⚠ Ce ne sont pas les mêmes codes que `PERMISSION_PAR_ALERTE` : voir une
#: demande et la trancher sont deux droits distincts, et c'est précisément la
#: séparation qu'un circuit d'approbation existe pour tenir.
PERMISSION_DE_DECISION = {
    'requisition': 'requisitions.approve',
    'stock_transfer': 'stock_transfers.ship',
    'stock_adjustment': 'stock_adjustments.approve',
    'expense': 'cashbook.approve_expense',
}


def _permission_de_decision(resource_type):
    return PERMISSION_DE_DECISION.get(resource_type, 'organization.settings')


def _pousser(notifications):
    """
    Enfile le rappel APRÈS LE COMMIT, et jamais au prix de l'acte.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN POST DE DIX SECONDES TOURNAIT SOUS LES VERROUS DE L'ACTE.            │
    │                                                                          │
    │ `sync/operations.py` ouvre un `atomic` autour de CHAQUE gestionnaire -   │
    │ §5.5 l'exige, la trace et l'effet sont inséparables. Tout `atomic` de    │
    │ service n'y est donc qu'un POINT DE SAUVEGARDE : les deux                │
    │ `select_for_update` de `_ecrire_mouvement` (`Stock`, puis `Product`)     │
    │ tenaient jusqu'au commit EXTÉRIEUR, et le POST tournait dedans. Toute    │
    │ vente concurrente du même article attendait, sur un gunicorn à deux      │
    │ workers et quatre fils.                                                  │
    │                                                                          │
    │ AUCUNE SCISSION DE SERVICE NE POUVAIT CORRIGER CELA : l'enveloppe non    │
    │ atomique de `create_stock_movement` est ELLE AUSSI dans ce `atomic`.     │
    │ Huit fonctions préviennent - `transition_requisition`, les trois du      │
    │ livre de caisse, `approve_transfer`, `approve_adjustment`,               │
    │ `reject_adjustment`, `create_stock_movement` - et chacune mentait sur    │
    │ l'un de ses DEUX chemins d'appel. C'est donc ici que le réseau sort, au  │
    │ seul passage obligé des huit, et une fois pour toutes.                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ LES ÉCRITURES RESTENT, ELLES, DANS LA TRANSACTION DE L'APPELANT.
    `Alert` et ses `Notification` sont la source de vérité : elles doivent
    commiter avec l'effet qu'elles décrivent. Seul le POST attend.

    ⚠ UN ACTE DÉFAIT NE FAIT PLUS SONNER. Les rappels d'une transaction
    annulée sont jetés : une opération du journal qui finit `rejected` poussait
    jusqu'ici un rappel pour une notification que le rollback venait d'effacer.

    ⚠ AUCUNE GARDE `in_atomic_block`. `transaction.on_commit` exécute DÉJÀ
    immédiatement hors transaction : la recopier serait une seconde expression
    d'une règle que le moteur tient, et les deux finiraient par diverger.

    ⚠ PAS `on_commit(..., robust=True)`. Django y avale toute `Exception` -
    donc `SoftTimeLimitExceeded`, et le défaut de la limite douce reviendrait
    par la porte du moteur. L'avalement reste ÉCRIT, dans
    `_enfiler_le_rappel`, pour qu'il puisse faire une exception.
    """
    if not notifications:
        return
    # Les IDENTIFIANTS et non les objets : le serializer des tâches est JSON,
    # un objet Django ne traverse pas.
    ids = [str(n.id) for n in notifications]
    transaction.on_commit(lambda: _enfiler_le_rappel(ids))


def _enfiler_le_rappel(ids):
    """
    Met le rappel en file, et n'en laisse RIEN remonter.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ CE `except` EST PORTEUR, PAS DE L'HYGIÈNE.                              │
    │                                                                          │
    │ Une exception qui s'échappe d'un rappel de commit enregistré dans le     │
    │ `try` de `sync/operations.py` est levée depuis `Atomic.__exit__`, donc   │
    │ APRÈS le commit. Elle atterrit dans son `except Exception`, qui écrit    │
    │ alors une trace `rejected` PAR-DESSUS un effet appliqué et commité - et  │
    │ `rejected` ne se réessaie JAMAIS. Un hoquet du courtier mettrait donc en │
    │ quarantaine une vente qui est bel et bien passée, et le marchand         │
    │ chercherait pourquoi son ticket ne désigne rien.                         │
    │                                                                          │
    │ Un rappel perdu, lui, ne perd rien : la notification est en base, et la  │
    │ cloche la montre.                                                        │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ L'IMPORT EST DIFFÉRÉ pour ne pas croiser `tasks.py`, qui importe ce
    module. Il est éprouvé par `apps/sync/tests/test_handler_imports.py`, dont
    le balayage couvre ce fichier précisément parce qu'un import écrit dans un
    corps ne s'évalue qu'à l'appel.
    """
    try:
        from apps.notifications.tasks import pousser_le_rappel

        pousser_le_rappel.delay(ids)
    except SoftTimeLimitExceeded:
        # ⚠ CELLE-CI NE S'AVALE PAS, et c'est tout le sens d'une limite douce.
        # Elle descend de `CeleryError`, donc d'`Exception` : le `except`
        # ci-dessous la prenait, et les battements d'alerte ne pouvaient plus
        # s'arrêter - ils allaient au SIGKILL de la limite dure, worker mort,
        # tournée à moitié faite. Une limite douce existe pour être attrapée
        # par la TÂCHE, pas par le premier `except` du chemin.
        #
        # Elle ne peut pas se produire sous gunicorn, qui n'a pas de limite de
        # tâche : c'est ce qui rend cette seule exception sûre face à l'encadré
        # ci-dessus. N'EN AJOUTER AUCUNE AUTRE sans relire ce raisonnement.
        raise
    except Exception:  # noqa: BLE001 - voir l'encadré
        logger.warning("Rappel poussé non enfilé", exc_info=True)


def _notifier_un(alerte, membre_ou_user, *, action_url='', action_label='',
                 pousser=True):
    """Une notification pour une personne nommée, sans fan-out.

    ⚠ `pousser=False` n'a qu'un appelant, `_livrer_a_qui_peut`, et une seule
    raison : il émettait UN ENVOI PAR DESTINATAIRE. Quatre gérants qui peuvent
    trancher, c'étaient quatre appels séquentiels à `exp.host` pour une seule
    demande d'approbation. `envoyer()` découpe déjà par cent, qui est la taille
    qu'Expo accepte : un seul suffit.
    """
    user = getattr(membre_ou_user, 'user', membre_ou_user)
    if _deja_notifie(alerte, user):
        return None
    creee = Notification.objects.create(
        organization=alerte.organization,
        user=user,
        notification_type=TON_PAR_GRAVITE.get(
            alerte.severity, Notification.NotificationType.INFO
        ),
        title=alerte.title,
        message=alerte.message,
        action_url=action_url,
        action_label=action_label,
        data={**(alerte.data or {}), 'alert_id': str(alerte.id),
              'alert_type': alerte.alert_type},
    )
    if pousser:
        _pousser([creee])
    return creee


def _livrer_a_qui_peut(alerte, permission, *, sauf=None, action_url='',
                       action_label=''):
    """
    Prévient qui détient `permission`, dans le périmètre de l'alerte.

    `sauf` écarte l'auteur de la demande : il sait ce qu'il vient de faire.
    """
    creees = []
    # ⚠ LE PÉRIMÈTRE D'ENTREPÔT VIENT DE `PerimetreAlertes`, PAS D'UNE
    #   TROISIÈME COPIE. Cette boucle réécrivait la règle que `destinataires` et
    #   la liste appliquent déjà : trois expressions du même périmètre, dont la
    #   divergence ne se verrait que sur le téléphone de quelqu'un qui reçoit -
    #   ou ne reçoit pas - une demande à trancher.
    #
    #   Le DROIT, lui, reste propre à cette fonction : une demande va à qui peut
    #   TRANCHER (`PERMISSION_DE_DECISION`), pas à qui peut voir la rubrique.
    #   C'est la seule exception de tout le module, et elle est nommée.
    for membre in membres_actifs(alerte.organization):
        if sauf is not None and membre.user_id == sauf.id:
            continue
        perimetre = PerimetreAlertes(membre)
        if permission not in perimetre.permissions:
            continue
        if not perimetre.couvre_le_depot(alerte):
            continue
        creee = _notifier_un(
            alerte, membre, action_url=action_url, action_label=action_label,
            pousser=False,
        )
        if creee is not None:
            creees.append(creee)

    # UN seul envoi pour tout le fan-out - voir `_notifier_un`.
    _pousser(creees)
    return creees


# ---------------------------------------------------------------------------
# Les sorties de stock
# ---------------------------------------------------------------------------

#: Les sorties qu'on SIGNALE, et seulement celles-là.
#:
#: ⚠ ON NE SIGNALE PAS UNE VENTE. Chaque encaissement sort du stock : prévenir
#: à chaque fois ferait de l'alerte le bruit de fond du comptoir, et personne ne
#: verrait plus la perte qui compte. Ce qu'un marchand veut savoir, ce sont les
#: sorties qui ne rapportent rien - casse, péremption - et celles qu'une main a
#: saisies à l'écran.
SORTIES_SIGNALEES = {
    'damage': ('Perte ou dommage', Alert.Severity.HIGH),
    'expired': ('Sortie pour péremption', Alert.Severity.MEDIUM),
    'adjustment_out': ('Ajustement négatif', Alert.Severity.MEDIUM),
    'return_out': ('Retour fournisseur', Alert.Severity.LOW),
}


def sortie_de_stock(mouvement):
    """
    Signale une sortie de stock qui ne vient pas d'une vente.

    Appelée par `create_stock_movement`, donc par la vue comme par le journal.
    """
    nature = SORTIES_SIGNALEES.get(mouvement.movement_type)
    if nature is None:
        return None

    libelle, gravite = nature
    from apps.inventory.packaging import PackagingService

    quantite = PackagingService.format_base_total(
        mouvement.product, abs(mouvement.quantity)
    )
    return alerter(
        mouvement.organization,
        alert_type=Alert.AlertType.STOCK_EXIT,
        severity=gravite,
        title=f"{libelle} : {mouvement.product.name}",
        # ⚠ FORMULATION INVARIABLE. « 3 casiers sont sortis » contre « 1 casier
        # est sorti » : le nombre ET le genre dépendent d'un nom d'unité que le
        # marchand a saisi, et qu'aucun lexique ne permet d'accorder. C'est la
        # famille de faute déjà payée sur « Ouvrir un BOITE » et « 11 BOITES
        # scellés ». On nomme donc la sortie, et la quantité suit.
        message=(
            f"{libelle} : {quantite} de {mouvement.product.name}, "
            f"depuis {mouvement.warehouse.name}"
            + (f". Motif : {mouvement.notes}" if mouvement.notes else ".")
        ),
        resource_type='stock_movement',
        resource_id=mouvement.id,
        data={
            'kind': 'stock_exit',
            'movement_type': mouvement.movement_type,
            'product_id': str(mouvement.product_id),
            'product_name': mouvement.product.name,
            'quantity': float(abs(mouvement.quantity)),
            'quantity_display': quantite,
            'warehouse_id': str(mouvement.warehouse_id),
            'warehouse_name': mouvement.warehouse.name,
        },
        action_url='/dashboard/stock/movements',
        action_label='Voir le mouvement',
    )
