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
