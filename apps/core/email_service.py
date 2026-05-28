"""
Service centralisé d'envoi d'emails transactionnels.

Tous les envois passent par ce module pour garantir :
- une politique d'erreur uniforme (log mais ne bloque jamais la requête HTTP)
- un From cohérent (``DEFAULT_FROM_EMAIL``)
- des liens absolus construits à partir de ``FRONTEND_URL``

En dev avec ``EMAIL_BACKEND=...console.EmailBackend`` (défaut), les mails
sont affichés en stdout — pratique pour vérifier sans configurer SMTP.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

logger = logging.getLogger(__name__)


def _send(subject: str, recipient: str, text_body: str, html_body: Optional[str] = None) -> bool:
    """Envoie un email et retourne True en cas de succès.

    Ne lève jamais d'exception : un échec d'envoi (SMTP down, credentials
    invalides, etc.) est loggué mais ne propage pas. Les flows métier
    (création d'invitation, finalisation de vente) restent fonctionnels
    même si l'email échoue — l'invitation/le reçu peut être renvoyé.
    """
    if not recipient:
        logger.warning("Email skipped : pas de destinataire pour %r", subject)
        return False

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        if html_body:
            msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception as exc:
        logger.exception(
            "Échec envoi email %r vers %s : %s", subject, recipient, exc
        )
        return False


def send_invitation_email(invitation) -> bool:
    """
    Envoie une invitation à rejoindre une organisation.

    ``invitation`` = ``OrganizationInvitation`` avec ``email``, ``token``,
    ``organization``, ``role``, ``expires_at``.
    """
    org_name = invitation.organization.name
    accept_url = (
        f"{settings.FRONTEND_URL.rstrip('/')}"
        f"/auth/accept-invitation?token={invitation.token}"
    )
    role_label = invitation.get_role_display() if hasattr(invitation, 'get_role_display') else invitation.role

    subject = f"Invitation à rejoindre {org_name} sur Vente Facile"
    text_body = (
        f"Bonjour,\n\n"
        f"Vous avez été invité(e) à rejoindre l'équipe de {org_name} en tant que {role_label} "
        f"sur la plateforme Vente Facile.\n\n"
        f"Pour accepter cette invitation, cliquez sur le lien ci-dessous (valide 7 jours) :\n"
        f"{accept_url}\n\n"
        f"Si vous ne vous attendiez pas à cette invitation, vous pouvez ignorer ce message.\n\n"
        f"— L'équipe Vente Facile"
    )
    html_body = (
        f"<p>Bonjour,</p>"
        f"<p>Vous avez été invité(e) à rejoindre l'équipe de <strong>{org_name}</strong> "
        f"en tant que <strong>{role_label}</strong> sur Vente Facile.</p>"
        f"<p><a href=\"{accept_url}\" style=\"background:#16a34a;color:#fff;padding:12px 20px;"
        f"text-decoration:none;border-radius:6px;display:inline-block\">Accepter l'invitation</a></p>"
        f"<p>Ou copiez ce lien dans votre navigateur (valide 7 jours) :<br>"
        f"<small>{accept_url}</small></p>"
        f"<p style=\"color:#888;font-size:12px\">Si vous ne vous attendiez pas à cette invitation, "
        f"vous pouvez ignorer ce message.</p>"
    )
    return _send(subject, invitation.email, text_body, html_body)


def send_quotation_email(quotation, recipient_email: Optional[str] = None) -> bool:
    """
    Envoie un devis au client.

    Si ``recipient_email`` n'est pas fourni, utilise ``quotation.customer.email``.
    """
    if not recipient_email and getattr(quotation, 'customer', None):
        recipient_email = getattr(quotation.customer, 'email', None)
    if not recipient_email:
        logger.info("Devis %s : pas d'email client, envoi skipped", quotation.reference)
        return False

    org = quotation.organization
    total = quotation.total
    currency = getattr(quotation, 'currency', 'CDF')
    valid_until = quotation.valid_until.strftime('%d/%m/%Y') if getattr(quotation, 'valid_until', None) else 'N/A'

    subject = f"Devis {quotation.reference} — {org.name}"
    text_body = (
        f"Bonjour,\n\n"
        f"Veuillez trouver ci-joint le devis {quotation.reference} émis par {org.name}.\n\n"
        f"Total : {total} {currency}\n"
        f"Valide jusqu'au : {valid_until}\n\n"
        f"Cordialement,\n{org.name}"
    )
    return _send(subject, recipient_email, text_body)


def send_receipt_email(sale, recipient_email: Optional[str] = None) -> bool:
    """
    Envoie un reçu de vente au client.

    Si ``recipient_email`` n'est pas fourni, utilise ``sale.customer.email``.
    """
    if not recipient_email and getattr(sale, 'customer', None):
        recipient_email = getattr(sale.customer, 'email', None)
    if not recipient_email:
        logger.info("Vente %s : pas d'email client, reçu non envoyé", sale.reference)
        return False

    org = sale.organization
    total = sale.total
    currency = getattr(sale, 'currency', 'CDF')
    date_str = timezone.localtime(sale.sale_date).strftime('%d/%m/%Y %H:%M') if sale.sale_date else 'N/A'

    subject = f"Reçu {sale.reference} — {org.name}"
    text_body = (
        f"Bonjour,\n\n"
        f"Merci pour votre achat chez {org.name} le {date_str}.\n\n"
        f"Référence : {sale.reference}\n"
        f"Total : {total} {currency}\n"
        f"Payé : {sale.amount_paid} {currency}\n\n"
        f"À bientôt,\n{org.name}"
    )
    return _send(subject, recipient_email, text_body)
