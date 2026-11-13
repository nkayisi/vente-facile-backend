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

from .models import AdminActionLog, ImpersonationSession
from .services import AuditLogService, RefusAdministration, _client_ip, profil_admin

CLE = 'vf:imp:v1:{}'
#: Assez court pour qu'une fin se voie presque aussitôt, assez long pour ne pas
#: relire la base à chacune des requêtes d'une même page.
TTL_SECONDES = 15

#: Les revendications posées dans le jeton.
CLAIM = 'imp'
CLAIM_SESSION = 'imp_session'
CLAIM_PAR = 'imp_by'
CLAIM_ORG = 'imp_org'


def session_vivante(session_id):
    """Vrai si la session est ouverte et non échue. Cachée `TTL_SECONDES`."""
    if not session_id:
        return False
    cle = CLE.format(session_id)
    connu = cache.get(cle)
    if connu is not None:
        return connu
    vivante = ImpersonationSession.objects.filter(
        pk=session_id, ended_at__isnull=True, expires_at__gt=timezone.now(),
    ).exists()
    cache.set(cle, vivante, TTL_SECONDES)
    return vivante


def _oublier(session_id):
    cache.delete(CLE.format(session_id))


def _terminer(session, motif):
    session.ended_at = timezone.now()
    session.ended_reason = motif
    session.save(update_fields=['ended_at', 'ended_reason', 'updated_at'])
    _oublier(session.pk)


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
            "Indiquez pourquoi vous prenez la main : le marchand pourra le lire."
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
    if not OrganizationMembership.objects.filter(
        user=target_user, organization=organization, is_active=True,
    ).exists():
        raise RefusAdministration("Ce compte n'est pas membre actif de cette organisation.")

    # Une seule prise en main à la fois par administrateur : la précédente
    # s'arrête, sinon deux jetons vivraient pour un seul geste.
    for anterieure in ImpersonationSession.objects.select_for_update().filter(
        impersonator=impersonator, ended_at__isnull=True,
    ):
        _terminer(anterieure, ImpersonationSession.EndReason.REPLACED)

    maintenant = timezone.now()
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
        ip_address=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request is not None else '',
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
        ip_address=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '') if request is not None else '',
    )
    return session, str(jeton)


@transaction.atomic
def terminer(session, *, motif=ImpersonationSession.EndReason.MANUAL, request=None):
    """Clôt une prise en main. Sans effet si elle l'est déjà."""
    session = ImpersonationSession.objects.select_for_update().select_related(
        'impersonator', 'target_user', 'organization',
    ).get(pk=session.pk)
    if session.ended_at is not None:
        return session
    _terminer(session, motif)
    AuditLogService.record(
        actor=session.impersonator, action=AdminActionLog.Action.IMPERSONATE_END,
        target_type=AdminActionLog.Target.USER, target_id=session.target_user_id,
        target_repr=f'{session.target_user.email} · {session.organization.name}',
        after={'session': str(session.pk), 'ended_reason': motif}, request=request,
    )
    return session
