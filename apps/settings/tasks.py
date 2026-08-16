"""
Tâches Celery de l'app Settings.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='apps.settings.tasks.expire_loyalty_points')
def expire_loyalty_points():
    """
    Retire les points de fidélité arrivés à échéance.

    `LoyaltyProgram.points_expiry_days` était configurable dans l'interface
    mais totalement inerte : aucune tâche ne le lisait. Une organisation qui
    réglait « 90 jours » ne voyait jamais rien expirer.

    Expiration par lot, FIFO : chaque gain a sa propre durée de vie. Idempotente,
    la ligne ``EXPIRE`` écrite devient elle-même un débit du registre.
    """
    from apps.settings.services import LoyaltyExpiryService

    reports = LoyaltyExpiryService.expire_all()

    total_points = sum(r['points'] for r in reports)
    total_accounts = sum(r['accounts'] for r in reports)

    if total_points:
        logger.info(
            'Expiration fidélité: %d point(s) retiré(s) sur %d compte(s) '
            'dans %d organisation(s)',
            total_points, total_accounts,
            len([r for r in reports if r['points']]),
        )

    return {
        'organizations': len(reports),
        'accounts': total_accounts,
        'points': total_points,
    }
