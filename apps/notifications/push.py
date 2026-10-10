"""
Envoyer une notification poussée, par le service Expo.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ON EMPLOIE `urllib.request`, PAS `requests`.                                │
│                                                                              │
│ C'est le choix déjà fait par `moko_client.py`, et pour la même raison : une  │
│ dépendance HTTP de moins dans une image qui tourne sur un VPS à deux cœurs.  │
│ L'appel est un POST JSON, il n'a besoin de rien d'autre.                     │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ **UN ÉCHEC D'ENVOI N'EST JAMAIS UNE ERREUR MÉTIER.** Le service Expo peut
être injoignable, lent, ou refuser un jeton périmé. Rien de tout cela ne doit
faire échouer l'acte qui a produit l'alerte : une approbation d'ajustement ne se
défait pas parce qu'un téléphone n'a pas sonné. Toutes les erreurs sont donc
avalées et JOURNALISÉES, jamais propagées.

⚠ **LA NOTIFICATION DANS L'APPLICATION EST LA SOURCE DE VÉRITÉ.** Le push est un
RAPPEL : il fait sonner le téléphone, il ne porte pas l'information. C'est ce
qui rend l'envoi sans risque - un push perdu ne perd rien, la notification est
en base - et c'est aussi pourquoi son corps reste court.
"""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

#: Le service d'envoi d'Expo. Aucune clé n'est requise pour un projet non
#: protégé ; `EXPO_ACCESS_TOKEN` l'est si le compte active la vérification.
EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'

#: Expo accepte 100 messages par requête. Au-delà, on découpe.
TAILLE_LOT = 100

#: Un envoi ne doit pas retenir une requête HTTP ni un battement de Celery.
DELAI = 10


def _jeton_valide(jeton):
    """
    La forme qu'Expo délivre.

    ⚠ ON CONTRÔLE AVANT D'ENVOYER : un jeton malformé fait répondre 400 pour
    TOUT LE LOT, donc une seule adresse abîmée ferait taire les quatre-vingt-dix
    neuf autres.
    """
    return isinstance(jeton, str) and (
        jeton.startswith('ExponentPushToken[') or jeton.startswith('ExpoPushToken[')
    )


def envoyer(messages):
    """
    Pousse une liste de messages. Rend le nombre réellement accepté.

    Chaque message est un dict `{to, title, body, data}`. Les jetons malformés
    sont écartés AVANT l'appel.
    """
    retenus = [m for m in messages if _jeton_valide(m.get('to'))]
    if not retenus:
        return 0

    envoyes = 0
    for debut in range(0, len(retenus), TAILLE_LOT):
        lot = retenus[debut:debut + TAILLE_LOT]
        if _envoyer_un_lot(lot):
            envoyes += len(lot)
    return envoyes


def _envoyer_un_lot(lot):
    corps = json.dumps(lot).encode('utf-8')
    entetes = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    jeton_compte = getattr(settings, 'EXPO_ACCESS_TOKEN', '') or ''
    if jeton_compte:
        entetes['Authorization'] = f'Bearer {jeton_compte}'

    requete = urllib.request.Request(
        EXPO_PUSH_URL, data=corps, headers=entetes, method='POST'
    )
    try:
        with urllib.request.urlopen(requete, timeout=DELAI) as reponse:
            charge = json.loads(reponse.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Journalisé, jamais propagé : voir l'encadré du module.
        logger.warning("Envoi poussé impossible : %s", exc)
        return False

    # Expo rend un verdict PAR MESSAGE. Un jeton devenu invalide se signale
    # ici, et c'est la seule façon de le savoir : il faut le détacher, sinon on
    # pousse dans le vide à chaque alerte, indéfiniment.
    for i, verdict in enumerate(charge.get('data') or []):
        if verdict.get('status') == 'error':
            details = verdict.get('details') or {}
            if details.get('error') == 'DeviceNotRegistered':
                _detacher(lot[i].get('to'))
            else:
                logger.warning("Message poussé refusé : %s", verdict.get('message'))
    return True


def _detacher(jeton):
    """
    Oublie une adresse que le service déclare morte.

    ⚠ ON NE RÉVOQUE PAS L'APPAREIL. Une application désinstallée rend
    `DeviceNotRegistered`, mais le terminal peut très bien être réinstallé
    demain avec les mêmes ventes en file : révoquer sa session l'enfermerait
    dehors. On oublie l'adresse, et le prochain réveil en posera une neuve.
    """
    from django.utils import timezone

    from apps.users.models import Device

    Device.objects.filter(push_token=jeton).update(
        push_token='', updated_at=timezone.now(),
    )


def pousser_notifications(notifications):
    """
    Fait sonner les téléphones des destinataires de ces notifications.

    Appelée APRÈS la création des notifications, jamais à leur place.
    """
    if not notifications:
        return 0

    from apps.users.models import Device

    par_utilisateur = {}
    for n in notifications:
        par_utilisateur.setdefault(n.user_id, []).append(n)

    # Les appareils UTILISABLES de ces personnes, dans CETTE organisation : une
    # alerte de la boutique A n'a rien à faire sur le terminal que la même
    # personne emploie chez B.
    organisations = {n.organization_id for n in notifications}
    appareils = Device.objects.filter(
        user_id__in=par_utilisateur,
        organization_id__in=organisations,
        revoked_at__isnull=True,
    ).exclude(push_token='')

    messages = []
    for appareil in appareils:
        for n in par_utilisateur.get(appareil.user_id, []):
            if n.organization_id != appareil.organization_id:
                continue
            messages.append({
                'to': appareil.push_token,
                'title': n.title,
                # ⚠ LE CORPS EST COURT : une notification système se tronque,
                # et le message complet est dans l'application. Le push est un
                # RAPPEL, pas le porteur de l'information.
                'body': n.message[:140],
                'data': {
                    'notification_id': str(n.id),
                    'alert_id': (n.data or {}).get('alert_id'),
                    'action_url': n.action_url,
                },
                'sound': 'default',
                # Expo range les notifications d'un même sujet ensemble : trois
                # ruptures ne font qu'une ligne dans le volet du téléphone.
                'channelId': 'alertes',
            })

    return envoyer(messages)
