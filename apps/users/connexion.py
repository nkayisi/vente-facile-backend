"""
La vérification d'un mot de passe, et le verrou qui la protège.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CINQ ÉCHECS, QUINZE MINUTES (décision du 2026-11-13).                        │
│                                                                              │
│ Rien ne bornait les essais : la seule limite était celle des requêtes        │
│ anonymes, cent par heure et par adresse, c'est-à-dire cent mots de passe     │
│ essayés par heure sur le compte d'un propriétaire - ou d'un administrateur   │
│ de la plateforme, qui se connecte par la même porte.                         │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ **UN POINT UNIQUE POUR LES DEUX PORTES.** Le back-office se connecte par
`/auth/token/`, le terminal par `/auth/login/` : un verrou posé sur une seule
laisserait l'autre ouverte, et c'est par elle qu'on essaierait.

⚠ **LE COUPLE (COMPTE, ADRESSE), PAS LE COMPTE SEUL.** Verrouiller le compte
partout donnerait à n'importe qui le moyen de fermer la caisse d'un marchand
en tapant cinq faux mots de passe depuis chez lui. L'adresse vient de
`apps.core.adresse`, qui ne croit ni l'en-tête du client ni une adresse non
attestée.

⚠ **UNE ADRESSE INCONNUE COMPTE COMME UNE AUTRE** : sans quoi tester l'existence
d'un compte se ferait en regardant lequel des deux verrouille.

⚠ **ON VÉRIFIE LE VERROU AVANT LE MOT DE PASSE** : un mot de passe juste tapé
pendant le verrou n'ouvre pas, sinon le verrou n'arrêterait que les maladroits.
"""
import hashlib
import math
import time

from django.core.cache import cache
from rest_framework import status

from apps.core.adresse import adresse_client
from apps.core.exceptions import RefusAvecCorps

SEUIL = 5
DUREE_SECONDES = 15 * 60

_CLE = 'vf:connexion:v1:{}:{}'


class ConnexionVerrouillee(RefusAvecCorps):
    """429 : trop d'échecs pour ce compte depuis cette adresse."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'login_locked'

    def __init__(self, secondes):
        minutes = max(1, math.ceil(secondes / 60))
        message = (
            "Trop de tentatives de connexion. Réessayez dans "
            f"{minutes} minute{'s' if minutes > 1 else ''}."
        )
        self.wait = secondes
        self.payload = {
            'detail': message,
            'code': self.default_code,
            'retry_after': int(math.ceil(secondes)),
        }
        super().__init__(message)


def _cles(email, request):
    """La clé du compteur et celle du verrou. L'adresse e-mail n'y paraît pas en clair."""
    empreinte = hashlib.sha256((email or '').strip().lower().encode()).hexdigest()
    ip = adresse_client(request) or 'inconnue'
    return _CLE.format('echecs', f'{empreinte}:{ip}'), _CLE.format('verrou', f'{empreinte}:{ip}')


def _verrou_restant(cle_verrou):
    echeance = cache.get(cle_verrou)
    if echeance is None:
        return 0
    return max(0, echeance - time.time())


def _noter_echec(cle_echecs, cle_verrou):
    """Compte l'échec ; pose le verrou au cinquième et le lève aussitôt."""
    # `add` ne pose la clé que si elle n'existe pas : la fenêtre part du
    # PREMIER échec, et `incr` ne la prolonge pas.
    cache.add(cle_echecs, 0, timeout=DUREE_SECONDES)
    try:
        echecs = cache.incr(cle_echecs)
    except ValueError:
        # La clé a expiré entre `add` et `incr` : c'est un premier échec.
        cache.set(cle_echecs, 1, timeout=DUREE_SECONDES)
        echecs = 1
    if echecs >= SEUIL:
        cache.set(cle_verrou, time.time() + DUREE_SECONDES, timeout=DUREE_SECONDES)
        cache.delete(cle_echecs)
        raise ConnexionVerrouillee(DUREE_SECONDES)


def authentifier(request, email, mot_de_passe):
    """
    Le compte si l'adresse et le mot de passe correspondent, `None` sinon.

    Lève `ConnexionVerrouillee` pendant un verrou, et au cinquième échec. Un
    compte désactivé dont le mot de passe est juste est RENDU : c'est à
    l'appelant de dire qu'il est désactivé, et ce n'est pas un échec de saisie.
    """
    from .models import User

    cle_echecs, cle_verrou = _cles(email, request)
    restant = _verrou_restant(cle_verrou)
    if restant:
        raise ConnexionVerrouillee(restant)

    user = User.objects.filter(email=email).first()
    if user is None:
        # ⚠ Le même travail qu'un mot de passe vérifié : sans ce hachage, un
        # compte inexistant répondrait plus vite, et le chronomètre dirait
        # quelles adresses existent.
        User().set_password(mot_de_passe)
        juste = False
    else:
        juste = user.check_password(mot_de_passe)

    if not juste:
        _noter_echec(cle_echecs, cle_verrou)
        return None
    cache.delete(cle_echecs)
    return user
