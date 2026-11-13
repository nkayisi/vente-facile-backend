"""
La prise en main d'un compte par le support.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE LE JETON PEUT, ET CE QU'IL NE PEUT PAS.                               │
│                                                                              │
│ Il PEUT : lire tout ce que le marchand lit, dans UNE organisation, pendant   │
│ vingt minutes.                                                               │
│                                                                              │
│ Il NE PEUT PAS : écrire quoi que ce soit (`apps.core.authentication` refuse  │
│ toute méthode non sûre), se rafraîchir (aucun jeton de rafraîchissement      │
│ n'est émis), changer d'organisation, enrôler un terminal - ce qui créerait   │
│ un jeton d'appareil de trente jours au nom du marchand - ni viser un autre   │
│ administrateur.                                                              │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ UNE SESSION SE VÉRIFIE À CHAQUE REQUÊTE, et la réponse est mise en cache
quelques secondes : une fin de prise en main doit couper l'accès tout de suite,
pas à l'échéance du jeton.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.core.adresse import adresse_client

from .models import AdminActionLog, ImpersonationSession
from .services import AuditLogService, RefusAdministration, profil_admin

CLE = 'vf:imp:v1:{}'
#: Assez court pour qu'une fin se voie presque aussitôt, assez long pour ne pas
#: relire la base à chacune des requêtes d'une même page.
TTL_SECONDES = 15

#: Les revendications posées dans le jeton.
CLAIM = 'imp'
CLAIM_SESSION = 'imp_session'
CLAIM_PAR = 'imp_by'
CLAIM_ORG = 'imp_org'

#: Ce qu'il faut charger avec une session pour la vérifier et la nommer au
#: journal sans relire la base.
LIENS = ('impersonator', 'target_user', 'organization')


def session_vivante(session_id):
    """
    Vrai si la session est ouverte, non échue, et son auteur encore habilité.

    Cachée `TTL_SECONDES`. ⚠ L'auteur se revérifie : un support dont on retire
    le rôle ou désactive le compte ne garde pas les minutes qui restent à sa
    prise en main. Une session qui ne vaut plus est CLOSE au passage, pour que
    le journal dise pourquoi elle a cessé.
    """
    if not session_id:
        return False
    cle = CLE.format(session_id)
    connu = cache.get(cle)
    if connu is not None:
        return connu
    session = (
        ImpersonationSession.objects.select_related(*LIENS)
        .filter(pk=session_id, ended_at__isnull=True).first()
    )
    vivante = session is not None and _encore_valable(session)
    cache.set(cle, vivante, TTL_SECONDES)
    return vivante


def _encore_valable(session):
    """Faux, et la session close, si elle a échu ou si son auteur n'est plus habilité."""
    from .services import PlatformPermissionService

    if session.expires_at <= timezone.now():
        _clore_si_ouverte(session, ImpersonationSession.EndReason.EXPIRED, fin=session.expires_at)
        return False
    auteur = session.impersonator
    if not auteur.is_active or not PlatformPermissionService.has(auteur, 'impersonation.start'):
        _clore_si_ouverte(session, ImpersonationSession.EndReason.REVOKED, journaliser=True)
        return False
    return True


def _oublier_apres_validation(session_id):
    """
    Purge le cache UNE FOIS la fin validée en base.

    Purgée avant, une requête concurrente relirait la base pendant la
    transaction, y verrait la session encore ouverte, et remettrait « vivante »
    en cache pour quinze secondes après la fin.
    """
    transaction.on_commit(lambda: cache.delete(CLE.format(session_id)))


def _clore_si_ouverte(session, motif, *, fin=None, acteur=None, raison='', request=None,
                      journaliser=False):
    """
    Clôt la session si personne ne l'a fait entre-temps ; rend vrai si c'est ce geste-ci.

    ⚠ Écriture CONDITIONNELLE (`ended_at IS NULL`) : deux fermetures simultanées -
    l'arrêt forcé et l'échéance, par exemple - ne doivent laisser qu'une ligne
    de journal et une seule raison.
    """
    from apps.core.bulk import bulk_update_rows

    ferme = bulk_update_rows(
        ImpersonationSession.objects.filter(pk=session.pk, ended_at__isnull=True),
        ended_at=fin or timezone.now(),
        ended_reason=motif,
    )
    if not ferme:
        return False
    _oublier_apres_validation(session.pk)
    if journaliser:
        AuditLogService.record(
            actor=acteur, action=AdminActionLog.Action.IMPERSONATE_END,
            target_type=AdminActionLog.Target.USER, target_id=session.target_user_id,
            target_repr=_libelle(session),
            after={'session': str(session.pk), 'ended_reason': motif,
                   'ended_by': getattr(acteur, 'email', None) or 'système'},
            reason=raison, request=request,
        )
    return True


def _libelle(session):
    return f'{session.target_user.email} · {session.organization.name}'


@transaction.atomic
def demarrer(*, impersonator, target_user, organization, reason, request=None):
    """
    Ouvre une prise en main et rend `(session, jeton d'accès)`.

    Lève `RefusAdministration` avec une phrase : le support doit savoir pourquoi.
    """
    from rest_framework_simplejwt.tokens import AccessToken

    from apps.organizations.models import OrganizationMembership
    from apps.users.models import UserActivity

    reason = (reason or '').strip()
    if len(reason) < 5:
        raise RefusAdministration(
            "Indiquez pourquoi vous prenez la main : le motif est gardé au journal d'audit."
        )
    if target_user.pk == impersonator.pk:
        raise RefusAdministration("Vous ne pouvez pas vous prendre en main vous-même.")
    if not target_user.is_active:
        raise RefusAdministration("Ce compte est désactivé.")
    # ⚠ On ne se met JAMAIS à la place d'un autre administrateur : ce serait
    # emprunter un pouvoir qu'on n'a pas, et sortir du journal.
    if target_user.is_superuser or profil_admin(target_user) is not None:
        raise RefusAdministration("On ne prend pas la main sur un administrateur de la plateforme.")
    if organization.is_deleted:
        raise RefusAdministration("Cette organisation n'existe plus.")
    # Un établissement suspendu ne se consulte plus, même par le support : ses
    # écrans répondraient tous « suspendu », et la suspension ferme déjà les
    # prises en main en cours.
    if not organization.is_active:
        raise RefusAdministration("Cet établissement est suspendu : on n'y prend pas la main.")
    if not OrganizationMembership.objects.filter(
        user=target_user, organization=organization, is_active=True,
    ).exists():
        raise RefusAdministration("Ce compte n'est pas membre actif de cette organisation.")

    # Une seule prise en main à la fois par administrateur : la précédente
    # s'arrête, sinon deux jetons vivraient pour un seul geste. ⚠ Une session
    # déjà échue n'a pas été « remplacée » : elle avait cessé d'elle-même.
    maintenant = timezone.now()
    for anterieure in ImpersonationSession.objects.select_for_update().filter(
        impersonator=impersonator, ended_at__isnull=True,
    ):
        if anterieure.expires_at <= maintenant:
            _clore_si_ouverte(anterieure, ImpersonationSession.EndReason.EXPIRED, fin=anterieure.expires_at)
        else:
            _clore_si_ouverte(anterieure, ImpersonationSession.EndReason.REPLACED)

    ip = adresse_client(request)
    agent = request.META.get('HTTP_USER_AGENT', '') if request is not None else ''
    jeton = AccessToken.for_user(target_user)
    jeton.set_exp(from_time=maintenant, lifetime=timedelta(minutes=ImpersonationSession.DUREE_MINUTES))

    session = ImpersonationSession.objects.create(
        impersonator=impersonator,
        target_user=target_user,
        organization=organization,
        reason=reason,
        jti=str(jeton['jti']),
        started_at=maintenant,
        expires_at=maintenant + timedelta(minutes=ImpersonationSession.DUREE_MINUTES),
        ip_address=ip,
        user_agent=agent,
    )
    jeton[CLAIM] = True
    jeton[CLAIM_SESSION] = str(session.pk)
    jeton[CLAIM_PAR] = str(impersonator.pk)
    jeton[CLAIM_ORG] = str(organization.pk)

    AuditLogService.record(
        actor=impersonator, action=AdminActionLog.Action.IMPERSONATE_START,
        target_type=AdminActionLog.Target.USER, target_id=target_user.pk,
        target_repr=f'{target_user.email} · {organization.name}',
        after={'session': str(session.pk), 'organization': str(organization.pk),
               'expires_at': session.expires_at.isoformat()},
        reason=reason, request=request,
    )
    UserActivity.objects.create(
        user=impersonator,
        organization=organization,
        action=UserActivity.ActionType.IMPERSONATED,
        resource_type='impersonation',
        resource_id=str(session.pk),
        details={
            'impersonator_email': impersonator.email,
            'target_email': target_user.email,
            'reason': reason,
            'expires_at': session.expires_at.isoformat(),
        },
        ip_address=ip,
        user_agent=agent,
    )
    return session, str(jeton)


@transaction.atomic
def terminer(session, *, motif=ImpersonationSession.EndReason.MANUAL, request=None,
             acteur=None, raison=''):
    """
    Clôt une prise en main. Sans effet si elle l'est déjà.

    `acteur` : qui la clôt. Par défaut celui qui l'a ouverte ; un arrêt forcé
    l'attribue au super-administrateur qui l'a décidé - le journal disait sinon
    que le support avait terminé lui-même une session qu'on lui a retirée.
    """
    session = ImpersonationSession.objects.select_related(*LIENS).get(pk=session.pk)
    _clore_si_ouverte(
        session, motif, acteur=acteur or session.impersonator, raison=raison,
        request=request, journaliser=True,
    )
    session.refresh_from_db()
    return session


@transaction.atomic
def fermer_sessions(*, motif=ImpersonationSession.EndReason.REVOKED, impersonator=None,
                    target_user=None, organization=None, acteur=None, raison='', request=None):
    """
    Clôt toutes les prises en main ouvertes qui correspondent ; rend leur nombre.

    Appelée quand le pouvoir qui les justifiait disparaît : rôle retiré ou
    compte désactivé (de l'auteur comme de la cible), établissement suspendu.
    """
    filtre = {}
    if impersonator is not None:
        filtre['impersonator'] = impersonator
    if target_user is not None:
        filtre['target_user'] = target_user
    if organization is not None:
        filtre['organization'] = organization
    if not filtre:
        raise ValueError('fermer_sessions exige au moins un critère.')
    closes = 0
    for session in ImpersonationSession.objects.select_related(*LIENS).filter(ended_at__isnull=True, **filtre):
        closes += _clore_si_ouverte(
            session, motif, acteur=acteur, raison=raison, request=request, journaliser=True,
        )
    return closes
