"""
File d'attente Redis des paiements MOKO à poller (références + métadonnées).
Utilise redis-py directement (SMEMBERS + HASH), pas le cache Django.
"""
from __future__ import annotations

import logging
from typing import Any

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

# Clés brutes Redis (sans préfixe Django cache)
PENDING_SET_KEY = "vf:moko:pending_payment_refs"
META_KEY_PREFIX = "vf:moko:pending_meta:"


def _client() -> redis.Redis | None:
    url = getattr(settings, "REDIS_URL", None) or getattr(
        settings, "REDIS_URL", ""
    )
    if not url:
        logger.warning("MOKO pending queue: no REDIS_URL / REDIS_URL")
        return None
    try:
        return redis.from_url(url, decode_responses=True)
    except Exception as e:
        logger.exception("MOKO pending queue: redis connect failed: %s", e)
        return None


def pending_queue_empty() -> bool:
    r = _client()
    if r is None:
        return True
    try:
        return r.scard(PENDING_SET_KEY) == 0
    except Exception as e:
        logger.warning("MOKO pending_queue_empty: %s", e)
        return True


def enqueue_pending_payment(
    *,
    reference: str,
    payment_id: str,
    paydrc_reference: str,
    thirdparty_reference: str,
) -> None:
    r = _client()
    if r is None:
        return
    ref = reference.strip()
    if not ref:
        return
    ttl = int(getattr(settings, "PENDING_META_TTL_SECONDS", 172800))
    meta_key = META_KEY_PREFIX + ref
    try:
        pipe = r.pipeline()
        pipe.sadd(PENDING_SET_KEY, ref)
        pipe.hset(
            meta_key,
            mapping={
                "payment_id": payment_id,
                "paydrc_reference": paydrc_reference or "",
                "thirdparty_reference": thirdparty_reference or ref,
            },
        )
        pipe.expire(meta_key, ttl)
        pipe.execute()
    except Exception as e:
        logger.exception("enqueue_pending_payment failed: %s", e)


def remove_pending_payment(reference: str) -> None:
    r = _client()
    if r is None:
        return
    ref = reference.strip()
    if not ref:
        return
    try:
        pipe = r.pipeline()
        pipe.srem(PENDING_SET_KEY, ref)
        pipe.delete(META_KEY_PREFIX + ref)
        pipe.execute()
    except Exception as e:
        logger.warning("remove_pending_payment failed: %s", e)


def list_pending_references(limit: int = 50) -> list[str]:
    r = _client()
    if r is None:
        return []
    try:
        members = r.smembers(PENDING_SET_KEY)
        if not members:
            return []
        out = list(members)
        return out[:limit]
    except Exception as e:
        logger.warning("list_pending_references failed: %s", e)
        return []


def get_pending_meta(reference: str) -> dict[str, Any]:
    r = _client()
    if r is None:
        return {}
    try:
        h = r.hgetall(META_KEY_PREFIX + reference.strip())
        return dict(h) if h else {}
    except Exception as e:
        logger.warning("get_pending_meta failed: %s", e)
        return {}
