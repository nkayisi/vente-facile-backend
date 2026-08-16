"""
Tâches Celery - suivi des paiements MOKO en attente (API v2).
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='apps.subscriptions.tasks.poll_moko_pending_payments')
def poll_moko_pending_payments():
    """
    Vérifie le statut des paiements MOKO en attente via l'API v2.
    
    Si la file Redis des références pending est vide, ne fait rien.
    Sinon interroge GET /v1/payments/status?reference=xxx pour chaque entrée.
    
    Fréquence recommandée: toutes les 15 secondes (configurable via Celery Beat).
    """
    from django.conf import settings

    from apps.subscriptions.moko_client import (
        get_payment_status_v2,
        extract_payment_status_v2,
        is_payment_successful_v2,
        is_payment_failed_v2,
        is_payment_pending_v2,
    )
    from apps.subscriptions.moko_pending import (
        get_pending_meta,
        list_pending_references,
        pending_queue_empty,
        remove_pending_payment,
    )
    from apps.subscriptions.models import SubscriptionPayment
    from apps.subscriptions.services import MokoActivationDeferred, SubscriptionService

    if pending_queue_empty():
        return

    limit = int(getattr(settings, 'POLL_BATCH_SIZE', 50) or 50)
    processed = 0

    for ref in list_pending_references(limit):
        meta = get_pending_meta(ref)
        pid = meta.get('payment_id')

        if not pid:
            logger.debug('MOKO poll: no payment_id for ref=%s, removing', ref)
            remove_pending_payment(ref)
            continue

        try:
            payment = SubscriptionPayment.objects.get(id=pid)
        except SubscriptionPayment.DoesNotExist:
            logger.warning('MOKO poll: payment %s missing, dropping ref %s', pid, ref)
            remove_pending_payment(ref)
            continue

        if payment.status != SubscriptionPayment.Status.PENDING:
            logger.debug('MOKO poll: payment %s not pending (status=%s), removing ref', pid, payment.status)
            remove_pending_payment(ref)
            continue

        # Appeler l'API v2 pour vérifier le statut
        try:
            code, data = get_payment_status_v2(ref)
        except Exception:
            logger.exception('MOKO poll v2: status request failed for %s', ref)
            continue

        if code >= 400:
            logger.warning('MOKO poll v2: API error for ref=%s code=%s', ref, code)
            continue

        # Extraire le statut de la réponse
        moko_status = extract_payment_status_v2(data)
        if not moko_status:
            logger.debug('MOKO poll v2: no status in response for ref=%s', ref)
            continue

        # Déterminer l'action à prendre
        if is_payment_pending_v2(moko_status):
            # Toujours en attente, ne rien faire
            logger.debug('MOKO poll v2: ref=%s still pending (status=%s)', ref, moko_status)
            continue

        # Extraire la description pour les logs
        description = ''
        if isinstance(data, dict):
            payment_info = data.get('payment') or {}
            description = payment_info.get('description') or ''

        try:
            outcome = SubscriptionService.apply_moko_terminal_or_intermediate_status(
                payment,
                moko_status.lower(),
                paid_by=None,
                detail_note=f' | Poll MOKO v2: {moko_status}' + (f' - {description}' if description else ''),
            )
            logger.info('MOKO poll v2: ref=%s status=%s outcome=%s', ref, moko_status, outcome)
            processed += 1
        except MokoActivationDeferred as exc:
            # L'argent a été encaissé mais l'activation ne peut pas aboutir tout
            # de suite (plan supprimé, métadonnées perdues). On NE marque PAS
            # failed : le paiement reste pending pour la réconciliation ou un
            # traitement manuel, et l'anomalie est remontée.
            logger.error(
                'MOKO poll v2: activation différée pour ref=%s (%s) - '
                'paiement encaissé, abonnement non activé, intervention requise.',
                ref, exc,
            )
            continue
        except Exception:
            # Jamais de `fail_pending_moko_payment` ici : MOKO vient de confirmer
            # l'encaissement. Un paiement confirmé ne doit pas devenir FAILED à
            # cause d'une erreur de notre côté.
            logger.exception('MOKO poll v2: apply status failed for %s', ref)
            continue

        if outcome in ('completed', 'failed'):
            remove_pending_payment(ref)

    if processed > 0:
        logger.info('MOKO poll v2: processed %d payments', processed)


@shared_task(name='apps.subscriptions.tasks.reconcile_moko_payments')
def reconcile_moko_payments(max_age_days: int = 30):
    """
    Filet de sécurité périodique, indépendant de la file Redis.

    Le poller ne voit que ce que Redis lui présente : un flush, un redémarrage
    sans persistance ou l'expiration du TTL des métadonnées suffisent à orpheliner
    un paiement encaissé. Cette tâche repart de la BASE et rattrape :
    - les paiements ``PENDING`` que MOKO donne réussis,
    - les paiements ``FAILED`` à tort par l'ancienne logique de poll.
    """
    from apps.subscriptions.services import SubscriptionService

    report = SubscriptionService.reconcile_moko_payments(max_age_days=max_age_days)

    if report['reconciled']:
        logger.warning(
            'Réconciliation MOKO: %d paiement(s) encaissé(s) réactivé(s): %s',
            len(report['reconciled']), report['reconciled'],
        )
    if report['unresolved']:
        logger.error(
            'Réconciliation MOKO: %d paiement(s) non résolu(s), intervention '
            'requise: %s',
            len(report['unresolved']), report['unresolved'],
        )
    logger.info(
        'Réconciliation MOKO: %d vérifié(s), %d réactivé(s), %d en attente, '
        '%d échoué(s), %d non résolu(s)',
        report['checked'], len(report['reconciled']), len(report['still_pending']),
        len(report['really_failed']), len(report['unresolved']),
    )
    return {k: (v if isinstance(v, int) else len(v)) for k, v in report.items()}
