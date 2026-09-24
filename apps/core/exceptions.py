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
from rest_framework.exceptions import APIException


class RefusMetier(ValueError):
    """
    Un refus que la règle métier oppose, et qui se reproduira à l'identique.

    Hérite de `ValueError` pour ne rien casser des appelants existants, qui
    l'attrapaient déjà sous ce nom. Le chemin web y gagne un 400 là où il
    rendait un 500 ; le chemin du journal y gagne une quarantaine lisible là
    où il rejouait sans fin.
    """


class AbonnementRequis(APIException):
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
