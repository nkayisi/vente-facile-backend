"""
Les jetons d'une session, et la marque qui permet de les couper tous à la fois.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UNE MARQUE DANS LE JETON, PAS UNE COMPARAISON D'HORLOGES.                    │
│                                                                              │
│ La réinitialisation d'un mot de passe par l'administration pose              │
│ `User.sessions_revoked_at`. La première règle comparait l'`iat` du jeton à   │
│ cet instant, et elle avait deux trous, tous deux sans erreur visible :       │
│                                                                              │
│ 1. `iat` est un entier : un jeton émis dans la MÊME SECONDE que la           │
│    réinitialisation, mais avant elle, passait.                               │
│ 2. Le rafraîchissement ne contrôlait rien. La réinitialisation met sur liste │
│    noire les jetons qu'elle LIT ; un rafraîchissement parti au même moment   │
│    en faisait naître un qu'elle ne lisait pas, et la session repartait pour  │
│    sept jours.                                                               │
│                                                                              │
│ Le jeton porte désormais la valeur EXACTE de `sessions_revoked_at` au moment │
│ de son émission (`srv`, en microsecondes). La rotation recopie toutes les    │
│ revendications : un jeton né d'un jeton d'avant la réinitialisation garde    │
│ l'ancienne marque, quel que soit l'instant où il naît. La règle devient une  │
│ ÉGALITÉ, et elle ne dépend plus d'aucune horloge.                            │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Un compte jamais réinitialisé n'a pas de marque, et ses jetons n'en portent
pas : rien ne change pour lui, ni la taille du jeton, ni ce qu'on contrôle.

⚠ Un jeton de prise en main (`imp`) n'est pas concerné : il ne naît pas ici
(`apps.platform_admin.impersonation`) et échappe à ces règles par décision.
"""
from datetime import datetime, timedelta, timezone as tz

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import BlacklistMixin, RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

#: Nom court : il voyage dans chaque requête du compte.
CLAIM_SESSION = 'srv'

_EPOQUE = datetime(1970, 1, 1, tzinfo=tz.utc)
_MICRO = timedelta(microseconds=1)


def marque_de_session(user) -> int | None:
    """
    La marque des sessions de ce compte, ou `None` s'il n'a jamais été coupé.

    ⚠ En microsecondes ENTIÈRES, calculées sans flottant : `timestamp() * 1e6`
    arrondit à ces magnitudes, et deux valeurs qu'on croit égales ne le
    seraient plus. Un entier sous 2**53 traverse aussi le JSON sans perte.
    """
    revoque = getattr(user, 'sessions_revoked_at', None)
    if revoque is None:
        return None
    return (revoque - _EPOQUE) // _MICRO


def session_ouverte(user, charge) -> bool:
    """Le jeton (sa charge utile) est-il de la session en cours de ce compte ?"""
    marque = marque_de_session(user)
    return marque is None or charge.get(CLAIM_SESSION) == marque


class JetonDeSession(RefreshToken):
    """
    Le jeton de rafraîchissement d'une session ouverte par un mot de passe ou un
    terminal. Tout endroit qui ouvre une session passe par lui
    (`test_jetons_de_session` le vérifie sur tout `apps/`).
    """

    @classmethod
    def for_user(cls, user):
        # ⚠ `Token.for_user`, SANS l'inscription de `BlacklistMixin` : la marque
        # doit être dans le jeton AVANT qu'il soit inscrit, sinon la copie que
        # garde `OutstandingToken` ne serait pas le jeton réellement remis.
        jeton = super(BlacklistMixin, cls).for_user(user)
        marque = marque_de_session(user)
        if marque is not None:
            jeton[CLAIM_SESSION] = marque
        # Le miroir exact de `BlacklistMixin.for_user` : une seule écriture.
        OutstandingToken.objects.create(
            user=user,
            jti=jeton[api_settings.JTI_CLAIM],
            token=str(jeton),
            created_at=jeton.current_time,
            expires_at=datetime_from_epoch(jeton['exp']),
        )
        return jeton


class RafraichissementSerializer(TokenRefreshSerializer):
    """
    `POST /auth/token/refresh/` refuse un jeton d'une session coupée.

    ⚠ AVANT la rotation : la contrôler après aurait déjà mis l'ancien jeton sur
    liste noire et inscrit le nouveau, pour une réponse qu'on refuse de toute
    façon. Le jeton est donc lu deux fois, ce qui ne coûte qu'une signature.
    """

    def validate(self, attrs):
        from apps.core.exceptions import SessionFermee

        charge = self.token_class(attrs['refresh']).payload
        user_id = charge.get(api_settings.USER_ID_CLAIM)
        if user_id:
            user = (
                get_user_model().objects
                .filter(**{api_settings.USER_ID_FIELD: user_id})
                .only('pk', 'sessions_revoked_at')
                .first()
            )
            if user is not None and not session_ouverte(user, charge):
                raise SessionFermee()
        return super().validate(attrs)
