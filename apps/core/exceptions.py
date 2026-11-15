"""
Exceptions métier communes.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN REFUS MÉTIER N'EST PAS UNE PANNE, ET LA DIFFÉRENCE SE PAIE CHER.         │
│                                                                              │
│ `apps/sync/operations.py::_classify` range une exception en verdict, et la   │
│ frontière qui compte est celle-ci : un refus DÉTERMINISTE ne doit jamais     │
│ être réessayé, une panne technique doit l'être. Tout ce que `_classify` ne   │
│ reconnaît pas tombe en `retry`.                                              │
│                                                                              │
│ Or deux refus parfaitement déterministes étaient levés en `ValueError` :     │
│ « Points insuffisants » et « La quantité totale est inférieure au contenu    │
│ des conditionnements vendus. » Un terminal les renvoyait donc indéfiniment,  │
│ à chaque synchronisation, pour une vente que le serveur refusera toujours.   │
│                                                                              │
│ Ranger `ValueError` en bloc du côté des refus aurait été pire : les erreurs  │
│ de programmation seraient parties en quarantaine, silencieusement, comme si  │
│ le marchand avait mal saisi. On NOMME donc le refus métier.                  │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, AuthenticationFailed


class RefusMetier(ValueError):
    """
    Un refus que la règle métier oppose, et qui se reproduira à l'identique.

    Hérite de `ValueError` pour ne rien casser des appelants existants, qui
    l'attrapaient déjà sous ce nom. Le chemin web y gagne un 400 là où il
    rendait un 500 ; le chemin du journal y gagne une quarantaine lisible là
    où il rejouait sans fin.
    """


class RefusAvecCorps(APIException):
    """
    Un refus dont le corps JSON est composé par l'exception elle-même.

    ⚠ `api_exception_handler` le rend TEL QUEL (`payload`), sans passer par
    `detail` : DRF convertirait sinon en chaînes les booléens et les nombres du
    corps. `wait`, s'il est posé, devient l'en-tête `Retry-After`.
    """

    payload: dict = {}
    wait = None


class AbonnementRequis(RefusAvecCorps):
    """
    402 : l'abonnement ne couvre plus l'écriture.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ 402 ET NON 403, PARCE QUE LES DEUX REFUS N'APPELLENT PAS LE MÊME GESTE.  │
    │                                                                          │
    │ Un 403 dit « il vous manque un droit » : on va voir son gérant. Un       │
    │ abonnement échu se règle en payant, et personne ne peut le faire à la    │
    │ place du marchand. Confondre les deux envoie chercher la solution au     │
    │ mauvais endroit.                                                         │
    │                                                                          │
    │ Le terminal ne peut PAS les distinguer autrement : il range 402 en       │
    │ `kind: "subscription"` et 403 en `kind: "auth"` (`src/api/errors.ts`).   │
    │ C'est ce qui décide si un lot d'opérations attend sagement le règlement  │
    │ ou se réessaie indéfiniment. Ce `kind: "subscription"` était écrit       │
    │ depuis toujours côté mobile et n'avait aucun producteur : le seul code   │
    │ qui rendait 402, `SubscriptionMiddleware`, est inerte pour toute l'API.  │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ **LE CORPS NE PASSE PAS PAR `detail`, ET C'EST OBLIGATOIRE.**
    `APIException.__init__` traverse `_get_error_details`, qui rend
    `ErrorDetail(force_str(v))` pour tout ce qui n'est ni chaîne, ni liste, ni
    dictionnaire. MESURÉ : `{'is_blocked': True, 'days_remaining': 0}` ressort
    `{"is_blocked": "True", "days_remaining": "0"}` - des CHAÎNES. Un client
    qui compare `is_blocked === true` lirait un corps qui a l'air juste et ne
    l'est pas, sans qu'aucune erreur ne le signale.

    Le corps voyage donc brut dans `payload`, et
    `apps/core/exception_handler.py` le rend tel quel avant de déléguer à DRF.
    """

    status_code = http_status.HTTP_402_PAYMENT_REQUIRED
    default_code = 'subscription_required'
    default_detail = "Votre abonnement est inactif. Veuillez le renouveler."

    def __init__(self, etat=None):
        etat = etat or {}
        message = etat.get('message') or self.default_detail
        # La forme exacte que `SubscriptionMiddleware` décrivait déjà : deux
        # corps différents pour le même refus obligeraient le client à savoir
        # lequel des deux chemins l'a produit.
        self.payload = {
            'detail': message,
            'code': self.default_code,
            'subscription_status': etat.get('status', 'none'),
            'is_blocked': True,
            'days_remaining': etat.get('days_remaining', 0),
        }
        super().__init__(message)


class EtablissementSuspendu(RefusAvecCorps):
    """
    403 : Vente Facile a suspendu cet établissement.

    ⚠ UN REFUS NOMMÉ, DISTINCT D'UN DROIT MANQUANT ET D'UN MOT DE PASSE ERRONÉ.
    Un 403 générique enverrait le caissier chercher son gérant, et le gérant
    chercher un rôle ; ni l'un ni l'autre n'y peut rien, seul le support le
    peut. Le `code` est ce que le back-office et le terminal lisent pour
    afficher l'écran de suspension plutôt qu'une erreur.
    """

    status_code = http_status.HTTP_403_FORBIDDEN
    default_code = 'organization_suspended'
    default_detail = "Cet établissement est suspendu par Vente Facile. Contactez le support."

    def __init__(self, organisation=None):
        self.payload = {
            'detail': self.default_detail,
            'code': self.default_code,
            'organization': str(organisation.pk) if organisation is not None else None,
        }
        super().__init__(self.default_detail)


class SessionFermee(RefusAvecCorps, AuthenticationFailed):
    """
    401 : la session a été coupée par une réinitialisation de mot de passe.

    ⚠ UN CORPS QUI PORTE SON `code`. Un `AuthenticationFailed` ordinaire ne rend
    que `{"detail": ...}` : le back-office ne pouvait donc pas distinguer cette
    fermeture d'un jeton simplement expiré, et il réessayait avec le même jeton,
    écran après écran, pendant trente minutes. Le `code` est ce que
    `lib/auth/api-helper.ts` lit pour déconnecter la page au premier refus.

    ⚠ ET UN `AuthenticationFailed` : c'est à ce titre que DRF pose l'en-tête
    `WWW-Authenticate` et garde le statut 401, au lieu de le changer en 403.
    """

    status_code = http_status.HTTP_401_UNAUTHORIZED
    default_code = 'session_revoked'
    default_detail = "Votre session a été fermée par l'administration. Reconnectez-vous."

    def __init__(self):
        self.payload = {'detail': self.default_detail, 'code': self.default_code}
        super().__init__(self.default_detail)


class MotDePasseAChanger(RefusAvecCorps):
    """
    403 : le mot de passe a été posé par l'administration, et le titulaire ne l'a
    pas encore remplacé.

    ⚠ UN REFUS NOMMÉ, et non un 401 : la session est VALIDE, elle vient de naître
    avec le mot de passe temporaire. Un 401 ferait rafraîchir puis déconnecter,
    et le titulaire tournerait en rond entre la connexion et son refus. Le `code`
    est ce que les deux clients lisent pour l'envoyer sur l'écran du changement.
    """

    status_code = http_status.HTTP_403_FORBIDDEN
    default_code = 'password_change_required'
    default_detail = (
        "Votre mot de passe a été réinitialisé : choisissez-en un nouveau avant de continuer."
    )

    def __init__(self):
        self.payload = {'detail': self.default_detail, 'code': self.default_code}
        super().__init__(self.default_detail)


class ConsultationSeule(RefusAvecCorps):
    """
    403 : la pièce relève d'un entrepôt que le membre peut LIRE, pas toucher.

    ⚠ JAMAIS UN 404. Un membre qui consulte tous les entrepôts ouvre une vente
    d'une autre boutique, appuie sur « Encaisser » : « introuvable » sur la
    pièce qu'il a sous les yeux le ferait chercher une panne. Le `code` est ce
    que les clients lisent pour dire « consultation seule ».
    """

    status_code = http_status.HTTP_403_FORBIDDEN
    default_code = 'consultation_seule'
    default_detail = (
        "Consultation seule : cette pièce relève d'un entrepôt qui ne vous est pas assigné."
    )

    def __init__(self):
        self.payload = {'detail': self.default_detail, 'code': self.default_code}
        super().__init__(self.default_detail)


class StockInsuffisant(RefusMetier):
    """
    Une sortie dépasserait le stock disponible.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE STOCK NÉGATIF N'EXISTE PLUS, ET CE REFUS EN EST LA FORME UNIQUE.     │
    │                                                                          │
    │ Une sortie manuelle ou une expédition au-delà du stock tombait sur la    │
    │ `ValidationError` Django de `Stock.save()`, qui n'est pas une erreur     │
    │ DRF : 500 au back-office, `retry` sans fin au journal. Ce refus est      │
    │ déterministe : 400 `{detail, code}`, verdict `rejected`.                 │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    code = 'stock_insuffisant'


# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ UN CODE DE CHAMP NE QUITTE PAS LE SERVEUR, SAUF CEUX-LÀ.                    │
# │                                                                              │
# │ DRF garde le `code` d'une erreur de champ en mémoire et le JETTE au rendu   │
# │ JSON : le client reçoit `{"register": "Cette caisse est tenue par…"}`, une  │
# │ phrase, et rien sur quoi réagir. Ces codes-là appellent un geste précis     │
# │ (relire ses droits, ouvrir sa propre session, choisir un autre entrepôt) :  │
# │ ils remontent au premier niveau, `{"code": …}`, comme un `RefusMetier`.     │
# └──────────────────────────────────────────────────────────────────────────────┘
CODES_REMARQUABLES = (
    'consultation_seule',
    'entrepot_inactif',
    'session_d_un_autre',
    'stock_insuffisant',
)


def code_remarquable(exc):
    """Le premier code remarquable porté par une `ValidationError`, ou `None`."""
    codes = exc.get_codes() if hasattr(exc, 'get_codes') else None

    def chercher(noeud):
        if isinstance(noeud, str):
            return noeud if noeud in CODES_REMARQUABLES else None
        if isinstance(noeud, dict):
            noeud = list(noeud.values())
        if isinstance(noeud, (list, tuple)):
            for enfant in noeud:
                trouve = chercher(enfant)
                if trouve:
                    return trouve
        return None

    return chercher(codes)
