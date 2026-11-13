"""
L'authentification JWT, et les trois règles d'un jeton de prise en main.

Une sous-classe de `JWTAuthentication` et rien d'autre : un jeton ordinaire la
traverse SANS AUCUNE différence. Seul un jeton portant la revendication `imp`
(posée par `apps.platform_admin.impersonation`) rencontre les règles ci-dessous.

┌──────────────────────────────────────────────────────────────────────────────┐
│ POURQUOI ICI, ET NON UNE PERMISSION POSÉE VUE PAR VUE.                       │
│                                                                              │
│ Une permission par vue exigeait d'INVENTORIER les actions sensibles - mot de │
│ passe, adresse, enrôlement d'un terminal, paiement, transfert de propriété - │
│ et une seule omission était un vrai trou : un support changeant le mot de    │
│ passe du marchand. L'authentification, elle, est traversée par TOUTE vue qui │
│ accepte un JWT. La règle devient « aucune écriture », et il n'y a plus rien  │
│ à inventorier.                                                               │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from rest_framework import exceptions
from rest_framework.permissions import SAFE_METHODS
from rest_framework_simplejwt.authentication import JWTAuthentication

#: Les seules écritures permises sous une prise en main : en sortir.
ECRITURES_PERMISES = (
    '/api/v1/platform-admin/impersonation/end/',
    '/api/v1/auth/logout/',
)


class EnPriseEnMain(exceptions.PermissionDenied):
    default_detail = (
        "Prise en main en lecture seule : vous voyez ce que voit le marchand, "
        "mais rien ne s'enregistre en son nom."
    )
    default_code = 'impersonation_read_only'


class ImpersonationAwareJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        resultat = super().authenticate(request)
        if resultat is None:
            return None
        user, jeton = resultat
        if not jeton.get('imp'):
            return resultat

        from apps.platform_admin.impersonation import CLAIM_ORG, CLAIM_SESSION, session_vivante

        # 1. La session vit encore. Un jeton dont la prise en main a été close
        #    doit cesser d'ouvrir quoi que ce soit, sans attendre son échéance.
        if not session_vivante(jeton.get(CLAIM_SESSION)):
            raise exceptions.AuthenticationFailed(
                'La prise en main est terminée.', code='impersonation_ended',
            )

        # 2. Lecture seule.
        chemin = request.path
        if request.method not in SAFE_METHODS and not chemin.endswith(ECRITURES_PERMISES):
            raise EnPriseEnMain()

        # 3. Une seule organisation : celle pour laquelle la main a été prise.
        org = request.headers.get('X-Organization-ID')
        if org and org != jeton.get(CLAIM_ORG):
            raise exceptions.PermissionDenied(
                "Cette prise en main ne porte que sur une organisation."
            )
        return resultat
