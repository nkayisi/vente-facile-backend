"""
Ce que l'administration garde, et combien de temps.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DOUZE MOIS, PUIS PURGE (décision du 2026-11-13).                             │
│                                                                              │
│ Le journal d'audit, les prises en main et l'activité des comptes portent    │
│ des adresses IP et des navigateurs. Les garder sans borne, c'est conserver   │
│ des données personnelles qui ne servent plus à rien : un litige commercial  │
│ se règle dans l'année. Rien ne purgeait ces tables, et elles grossissaient   │
│ à chaque connexion de chaque caissier.                                       │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Une prise en main encore OUVERTE n'est jamais purgée, quel que soit son âge :
elle est d'abord close par le balayage, et ne disparaît qu'un an après sa fin.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from apps.core.bulk import bulk_update_rows

logger = logging.getLogger(__name__)

RETENTION_JOURS = 365


def clore_sessions_expirees(maintenant=None) -> int:
    """
    Marque `EXPIRED` les prises en main échues que personne n'a terminées.

    Le jeton cesse de valoir à son échéance (simplejwt le refuse), mais la ligne
    restait ouverte pour toujours : le back-office restaure l'administrateur sans
    appeler la fin. `ended_at` reçoit l'ÉCHÉANCE, pas l'heure du balayage : c'est
    à ce moment-là que l'accès a réellement cessé.
    """
    from .models import ImpersonationSession

    maintenant = maintenant or timezone.now()
    return bulk_update_rows(
        ImpersonationSession.objects.filter(ended_at__isnull=True, expires_at__lte=maintenant),
        ended_at=F('expires_at'),
        ended_reason=ImpersonationSession.EndReason.EXPIRED,
    )


def purger_au_dela_de_la_retention(maintenant=None) -> dict:
    """Supprime ce qui a dépassé la rétention ; rend le nombre de lignes par table."""
    from apps.users.models import UserActivity

    from .models import AdminActionLog, ImpersonationSession

    limite = (maintenant or timezone.now()) - timedelta(days=RETENTION_JOURS)
    return {
        'admin_action_logs': AdminActionLog.objects.filter(created_at__lt=limite).delete()[0],
        'impersonation_sessions': ImpersonationSession.objects.filter(
            ended_at__isnull=False, ended_at__lt=limite,
        ).delete()[0],
        'user_activities': UserActivity.objects.filter(created_at__lt=limite).delete()[0],
    }


@shared_task(soft_time_limit=240, time_limit=300, ignore_result=True)
def purger_journaux():
    """Chaque nuit : clore les prises en main échues, puis purger au-delà d'un an."""
    closes = clore_sessions_expirees()
    purges = purger_au_dela_de_la_retention()
    logger.info('purger_journaux : %s session(s) close(s), purge %s', closes, purges)
