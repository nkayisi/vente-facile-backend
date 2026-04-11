"""
Client HTTP pour MOKO / GoFreshPay API v2.
Documentation: https://moko.gofreshpay.com

Endpoints utilisés:
- POST /v1/payments - Initier un paiement
- GET /v1/payments/status?reference=xxx - Vérifier le statut
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from django.conf import settings

logger = logging.getLogger(__name__)


class MokoConfigurationError(RuntimeError):
    """Clé API MOKO manquante ou URL non configurée."""


def _require_config_v2() -> None:
    """Vérifie que la configuration MOKO v2 est présente."""
    if not getattr(settings, "MOKO_API_V2_URL", "").strip():
        raise MokoConfigurationError("MOKO_API_V2_URL is not set")
    if not getattr(settings, "MOKO_API_KEY", "").strip():
        raise MokoConfigurationError("MOKO_API_KEY is not set")


def _get_base_url() -> str:
    """Retourne l'URL de base de l'API MOKO v2."""
    return getattr(settings, "MOKO_API_V2_URL", "https://moko.gofreshpay.com").rstrip("/")


def _get_api_key() -> str:
    """Retourne la clé API MOKO."""
    return getattr(settings, "MOKO_API_KEY", "")


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, dict[str, Any] | list | str]:
    """Effectue une requête POST JSON."""
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        code = e.code
    try:
        parsed: dict[str, Any] | list | str = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        parsed = body
    return code, parsed


def _get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, dict[str, Any] | list | str]:
    """Effectue une requête GET JSON."""
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        code = e.code
    try:
        parsed: dict[str, Any] | list | str = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        parsed = body
    return code, parsed


def initiate_payment_v2(
    *,
    amount: str,
    currency: str,
    phone: str,
    method: str,
    reference: str,
    callback_url: str,
) -> tuple[int, dict[str, Any] | list | str]:
    """
    Initie un paiement Mobile Money via MOKO API v2.
    
    POST /v1/payments
    
    Args:
        amount: Montant du paiement (ex: "10000")
        currency: Devise (CDF ou USD)
        phone: Numéro de téléphone du client (ex: "0997057917")
        method: Opérateur (airtel, orange, mpesa, africell)
        reference: Référence unique du paiement
        callback_url: URL de callback pour notification
    
    Returns:
        Tuple (http_code, response_data)
        
    Response example:
        {
            "payment": {
                "reference": "INV-2026-00277HH",
                "status": "Submitted",
                "amount": 10000.0,
                ...
            },
            "gateway_response": {
                "Transaction_id": "pd20260410177583222304111d6b4",
                "Status": "Success",
                ...
            }
        }
    """
    _require_config_v2()
    
    base_url = _get_base_url()
    url = f"{base_url}/v1/payments"
    
    payload = {
        "amount": float(amount) if isinstance(amount, str) else amount,
        "currency": currency,
        "phone": phone,
        "method": method,
        "reference": reference,
        "callback_url": callback_url,
    }
    
    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
    }
    
    logger.info("MOKO v2 initiate payment: ref=%s method=%s amount=%s %s", 
                reference, method, amount, currency)
    
    code, data = _post_json(url, payload, headers=headers)
    
    if code >= 400:
        logger.warning("MOKO v2 initiate failed: code=%s data=%s", code, data)
    else:
        logger.info("MOKO v2 initiate success: code=%s", code)
    
    return code, data


def get_payment_status_v2(reference: str) -> tuple[int, dict[str, Any] | list | str]:
    """
    Vérifie le statut d'un paiement via MOKO API v2.
    
    GET /v1/payments/status?reference=xxx
    
    Args:
        reference: Référence du paiement à vérifier
    
    Returns:
        Tuple (http_code, response_data)
        
    Response example:
        {
            "payment": {
                "id": 47001649,
                "reference": "INV-2026-00277HH",
                "status": "Successful",  # ou Submitted, Pending, Failed
                "gateway_reference": "pd20260410177583222304111d6b4",
                ...
            }
        }
    
    Statuts possibles:
        - Submitted: Paiement soumis, en attente de traitement
        - Pending: Paiement en cours de traitement
        - Successful: Paiement réussi
        - Failed: Paiement échoué
    """
    _require_config_v2()
    
    base_url = _get_base_url()
    params = urlencode({"reference": reference})
    url = f"{base_url}/v1/payments/status?{params}"
    
    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
    }
    
    logger.debug("MOKO v2 check status: ref=%s", reference)
    
    code, data = _get_json(url, headers=headers)
    
    if code >= 400:
        logger.warning("MOKO v2 status check failed: ref=%s code=%s", reference, code)
    
    return code, data


def extract_transaction_id_v2(data: dict[str, Any] | list | str | None) -> str:
    """
    Extrait l'identifiant de transaction depuis la réponse d'initiation v2.
    
    Cherche dans gateway_response.Transaction_id
    """
    if not isinstance(data, dict):
        return ""
    
    # Chercher dans gateway_response
    gw = data.get("gateway_response") or data.get("Gateway_response") or {}
    if isinstance(gw, dict):
        tid = (
            gw.get("Transaction_id")
            or gw.get("transaction_id")
            or ""
        )
        if tid:
            return str(tid).strip()
    
    # Fallback: chercher dans payment
    payment = data.get("payment") or {}
    if isinstance(payment, dict):
        tid = payment.get("gateway_reference") or ""
        if tid:
            return str(tid).strip()
    
    return ""


def extract_payment_status_v2(data: dict[str, Any] | list | str | None) -> str:
    """
    Extrait le statut du paiement depuis la réponse de status v2.
    
    Retourne: Submitted, Pending, Successful, Failed, ou chaîne vide
    """
    if not isinstance(data, dict):
        return ""
    
    payment = data.get("payment") or {}
    if isinstance(payment, dict):
        status = payment.get("status") or payment.get("Status") or ""
        return str(status).strip()
    
    return ""


def is_payment_successful_v2(status: str) -> bool:
    """Vérifie si le statut indique un paiement réussi."""
    return status.lower() in ("successful", "success", "completed")


def is_payment_failed_v2(status: str) -> bool:
    """Vérifie si le statut indique un paiement échoué."""
    return status.lower() in ("failed", "error", "cancelled", "canceled", "rejected")


def is_payment_pending_v2(status: str) -> bool:
    """Vérifie si le statut indique un paiement en cours."""
    return status.lower() in ("submitted", "pending", "processing")


# =============================================================================
# Fonctions de compatibilité avec l'ancien code (à supprimer progressivement)
# =============================================================================

def extract_paydrc_transaction_id(data: dict[str, Any] | list | str | None) -> str:
    """Alias pour compatibilité avec l'ancien code."""
    return extract_transaction_id_v2(data)


def initiate_payment(
    *,
    amount: str,
    currency: str,
    customer_number: str,
    firstname: str,
    lastname: str,
    email: str,
    reference: str,
    method: str,
    callback_url: str,
) -> tuple[int, dict[str, Any] | list | str]:
    """
    Fonction de compatibilité avec l'ancien code.
    Redirige vers initiate_payment_v2.
    """
    return initiate_payment_v2(
        amount=amount,
        currency=currency,
        phone=customer_number,
        method=method,
        reference=reference,
        callback_url=callback_url,
    )


def get_transaction_status(
    *,
    paydrc_reference: str | None = None,
    thirdparty_reference: str | None = None,
) -> tuple[int, dict[str, Any] | list | str]:
    """
    Fonction de compatibilité avec l'ancien code.
    Redirige vers get_payment_status_v2.
    """
    reference = thirdparty_reference or paydrc_reference or ""
    if not reference:
        raise ValueError("reference requis")
    return get_payment_status_v2(reference)


def parse_status_response_transaction(
    data: dict[str, Any] | list | str | None,
) -> dict[str, Any] | None:
    """
    Fonction de compatibilité avec l'ancien code.
    Retourne le dict payment depuis la réponse status v2.
    """
    if not isinstance(data, dict):
        return None
    payment = data.get("payment")
    return payment if isinstance(payment, dict) else None


def transaction_status_value(transaction: dict[str, Any] | None) -> str:
    """
    Fonction de compatibilité avec l'ancien code.
    Extrait le statut depuis le dict payment.
    """
    if not transaction:
        return ""
    return str(transaction.get("status") or transaction.get("Status") or "").strip()
