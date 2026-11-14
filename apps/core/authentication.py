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

┌──────────────────────────────────────────────────────────────────────────────┐
│ ET DEUX RÈGLES POUR LES JETONS ORDINAIRES : CELLES D'UN MOT DE PASSE         │
│ RÉINITIALISÉ PAR L'ADMINISTRATION (`reinitialiser_mot_de_passe`).            │
│                                                                              │
│ 1. Un jeton d'une session coupée est mort (401 `session_revoked`). Mettre    │
│    les jetons de rafraîchissement sur liste noire ne coupait rien avant      │
│    trente minutes ; un 401 fait escalader le terminal tout de suite          │
│    (rafraîchissement refusé, appareil révoqué, mot de passe redemandé).      │
│    « Coupée » se lit à la MARQUE du jeton, pas à son heure d'émission :      │
│    voir `apps.users.jetons`, qui la pose et contrôle aussi le               │
│    rafraîchissement.                                                         │
│ 2. Tant que `must_change_password` est vrai, seul le changement lui-même     │
│    passe (403 `password_change_required`). Les clients y conduisent          │
│    d'office ; cette règle est ce qui rend le passage OBLIGATOIRE.            │
│                                                                              │
│ ⚠ Un jeton de prise en main n'y est pas soumis : le support regarde, et      │
│ regarder un compte dont le mot de passe attend son changement est légitime.  │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import re

from rest_framework import exceptions
from rest_framework.permissions import SAFE_METHODS
from rest_framework_simplejwt.authentication import JWTAuthentication

#: Les seules écritures permises sous une prise en main : en sortir.
ECRITURES_PERMISES = (
    '/api/v1/platform-admin/impersonation/end/',
    '/api/v1/auth/logout/',
)

#: ┌────────────────────────────────────────────────────────────────────────┐
#: │ LES ÉCRANS, PAS LA BASE (décision du 2026-11-13).                      │
#: │                                                                        │
#: │ Lecture seule ne suffisait pas : un GET lit aussi la synchronisation   │
#: │ (toute la base de l'établissement, table par table), tous les exports │
#: │ PDF et classeurs, l'historique de connexion du marchand (adresses IP,  │
#: │ navigateurs) et ses appareils. Un support pouvait aspirer un           │
#: │ établissement entier en vingt minutes. Il voit les écrans, et rien de  │
#: │ ce qui sert à emporter des données ou à les recouper.                  │
#: │                                                                        │
#: │ ⚠ Une LISTE D'INTERDITS, et non une liste d'autorisés : les écrans     │
#: │ appellent quelque cent cinquante routes de lecture, et chaque écran    │
#: │ nouveau répondrait 403 au support sans que personne ne le voie. Le     │
#: │ point faible d'une liste d'interdits - un export ajouté demain - est   │
#: │ fermé par un test qui parcourt le routeur (`test_prise_en_main`).      │
#: └────────────────────────────────────────────────────────────────────────┘
INTERDITS = tuple(re.compile(motif) for motif in (
    r'^/api/v1/sync/',
    r'/[^/]*export[^/]*/',
    r'^/api/v1/users/activities/',
    r'^/api/v1/user-activities/',
    r'^/api/v1/auth/devices/',
))

#: Les seules lectures permises SANS en-tête d'organisation. Les autres
#: exigent celle de la prise en main : sans cette règle, une vue de niveau
#: compte (notifications, profil) appelée sans en-tête lirait toutes les
#: organisations du marchand.
#:
#: ⚠ Les deux CATALOGUES de la plateforme (devises, plans) y figurent : ils ne
#: lisent aucune organisation, et le back-office les appelle sans en-tête. Les
#: refuser rendait l'écran des réglages et celui de l'abonnement vides pendant
#: une prise en main, relevé en parcourant les quarante écrans du tableau de bord.
SANS_ORGANISATION = tuple(re.compile(motif) for motif in (
    r'^/api/v1/users/me/$',
    r'^/api/v1/organizations/',
    r'^/api/v1/platform-admin/impersonation/end/$',
    r'^/api/v1/auth/logout/$',
    r'^/api/v1/settings/currencies/',
    r'^/api/v1/plans/',
))


#: Ce qu'un compte au mot de passe à changer peut encore faire : le changer,
#: savoir qui il est, et partir. `path_info`, comparé en entier.
PERMIS_AVANT_CHANGEMENT = frozenset({
    ('POST', '/api/v1/users/me/change-password/'),
    ('GET', '/api/v1/users/me/'),
    ('POST', '/api/v1/auth/logout/'),
})


def _regles_du_mot_de_passe(request, user, jeton):
    """Les deux règles d'un mot de passe réinitialisé, pour un jeton ordinaire."""
    from apps.users.jetons import session_ouverte

    if not session_ouverte(user, jeton):
        from .exceptions import SessionFermee

        raise SessionFermee()
    if getattr(user, 'must_change_password', False):
        if (request.method, request.path_info) not in PERMIS_AVANT_CHANGEMENT:
            from .exceptions import MotDePasseAChanger

            raise MotDePasseAChanger()


class EnPriseEnMain(exceptions.PermissionDenied):
    default_detail = (
        "Prise en main en lecture seule : vous voyez ce que voit le marchand, "
        "mais rien ne s'enregistre en son nom."
    )
    default_code = 'impersonation_read_only'


class HorsDesEcrans(exceptions.PermissionDenied):
    default_detail = (
        "Prise en main : la synchronisation, les exports, l'historique de "
        "connexion et les appareils du marchand ne sont pas consultables."
    )
    default_code = 'impersonation_forbidden'


class OrganisationRequise(exceptions.PermissionDenied):
    default_detail = "Prise en main : précisez l'organisation consultée."
    default_code = 'impersonation_org_required'


class ImpersonationAwareJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        resultat = super().authenticate(request)
        if resultat is None:
            return None
        user, jeton = resultat
        if not jeton.get('imp'):
            _regles_du_mot_de_passe(request, user, jeton)
            return resultat

        from apps.platform_admin.impersonation import CLAIM_ORG, CLAIM_SESSION, session_vivante

        # 1. La session vit encore. Un jeton dont la prise en main a été close
        #    doit cesser d'ouvrir quoi que ce soit, sans attendre son échéance.
        if not session_vivante(jeton.get(CLAIM_SESSION)):
            raise exceptions.AuthenticationFailed(
                'La prise en main est terminée.', code='impersonation_ended',
            )

        # 2. Lecture seule. `path_info` et non `path` : ce dernier porte le
        #    préfixe de script quand l'application n'est pas servie à la racine.
        chemin = request.path_info
        if request.method not in SAFE_METHODS and not chemin.endswith(ECRITURES_PERMISES):
            raise EnPriseEnMain()

        # 3. Les écrans, pas la base.
        if any(motif.search(chemin) for motif in INTERDITS):
            raise HorsDesEcrans()

        # 4. Une seule organisation : celle pour laquelle la main a été prise,
        #    nommée explicitement hors des quelques routes de niveau compte.
        org = request.headers.get('X-Organization-ID')
        if org:
            if org != jeton.get(CLAIM_ORG):
                raise exceptions.PermissionDenied(
                    "Cette prise en main ne porte que sur une organisation."
                )
        elif not any(motif.search(chemin) for motif in SANS_ORGANISATION):
            raise OrganisationRequise()
        return resultat
