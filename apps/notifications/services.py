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
from django.db import transaction

from apps.core.services import PermissionService
from apps.notifications.models import Alert, Notification
from apps.organizations.models import OrganizationMembership


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


def destinataires(alert):
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
    """
    depot = _entrepot_de_lalerte(alert)
    besoin = PERMISSION_PAR_ALERTE.get(alert.alert_type, None)

    membres = (
        OrganizationMembership.objects
        .filter(organization=alert.organization, is_active=True)
        .select_related('user')
        .prefetch_related('assigned_warehouses')
    )

    retenus = []
    for m in membres:
        if not m.user.is_active:
            continue

        if besoin is None:
            # Aucune rubrique nommée : le propriétaire, et lui seul.
            if m.role != OrganizationMembership.Role.OWNER:
                continue
        elif besoin not in PermissionService.get_effective_permissions(m):
            continue

        if depot and m.role != OrganizationMembership.Role.OWNER:
            permis = {str(w.id) for w in m.assigned_warehouses.all()}
            if depot not in permis:
                continue

        retenus.append(m)

    return retenus


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
def notifier(alert, *, action_url='', action_label=''):
    """
    Produit une notification par destinataire. Rend celles qui ont été créées.

    Appelée par les tâches Celery APRÈS la création de l'alerte, et par aucun
    autre chemin : une alerte qui prévient toute seule à la sauvegarde
    sonnerait aussi pour les alertes qu'une migration ou une fixture écrit.
    """
    creees = []
    for membre in destinataires(alert):
        if _deja_notifie(alert, membre.user):
            continue
        creees.append(Notification(
            organization=alert.organization,
            user=membre.user,
            notification_type=TON_PAR_GRAVITE.get(
                alert.severity, Notification.NotificationType.WARNING
            ),
            title=alert.title,
            message=alert.message,
            action_url=action_url,
            action_label=action_label,
            # ⚠ `alert_id` EST la clé d'idempotence, et le lien que le modèle
            # n'a pas. L'écran s'en sert aussi pour mener à l'alerte.
            data={**(alert.data or {}), 'alert_id': str(alert.id),
                  'alert_type': alert.alert_type},
        ))

    if creees:
        Notification.objects.bulk_create(creees)
    return creees


def alerter(organization, **champs):
    """
    Crée une alerte ET prévient, en un seul geste.

    Les sept producteurs appelaient `Alert.objects.create` et s'arrêtaient là.
    Ce point d'entrée unique est ce qui rend l'oubli impossible : il n'existe
    plus de chemin qui crée une alerte sans décider qui la reçoit.
    """
    action_url = champs.pop('action_url', '')
    action_label = champs.pop('action_label', '')
    alert = Alert.objects.create(organization=organization, **champs)
    notifier(alert, action_url=action_url, action_label=action_label)
    return alert


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
        title=f"{quoi} à approuver : {reference}",
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
        title=f"{quoi} {verdict} : {reference}",
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


def _notifier_un(alerte, membre_ou_user, *, action_url='', action_label=''):
    """Une notification pour une personne nommée, sans fan-out."""
    user = getattr(membre_ou_user, 'user', membre_ou_user)
    if _deja_notifie(alerte, user):
        return None
    return Notification.objects.create(
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


def _livrer_a_qui_peut(alerte, permission, *, sauf=None, action_url='',
                       action_label=''):
    """
    Prévient qui détient `permission`, dans le périmètre de l'alerte.

    `sauf` écarte l'auteur de la demande : il sait ce qu'il vient de faire.
    """
    depot = _entrepot_de_lalerte(alerte)
    membres = (
        OrganizationMembership.objects
        .filter(organization=alerte.organization, is_active=True)
        .select_related('user')
        .prefetch_related('assigned_warehouses')
    )

    for m in membres:
        if sauf is not None and m.user_id == sauf.id:
            continue
        if not m.user.is_active:
            continue
        if permission not in PermissionService.get_effective_permissions(m):
            continue
        if depot and m.role != OrganizationMembership.Role.OWNER:
            permis = {str(w.id) for w in m.assigned_warehouses.all()}
            if depot not in permis:
                continue
        _notifier_un(alerte, m, action_url=action_url, action_label=action_label)


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
