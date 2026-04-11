"""
Cache Redis du token Bearer MOKO (parent login), partagé entre Django et Celery.
"""
from __future__ import annotations

import base64
import json
import logging
import time

from django.conf import settings
from django.core.cache import caches

from .moko_client import login_parent

logger = logging.getLogger(__name__)

_CACHE_ALIAS = 'moko'
_TOKEN_KEY = 'parent_bearer_token'


def _ttl_from_jwt(token: str) -> int:
    """Déduit un TTL à partir du claim exp du JWT (marge 60 s)."""
    default = int(getattr(settings, 'TOKEN_CACHE_TTL', 3600))
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return default
        payload_b64 = parts[1]
        pad = '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        exp = payload.get('exp')
        if not exp:
            return default
        remaining = int(exp) - int(time.time()) - 60
        return max(120, min(default, remaining))
    except Exception:
        return default


def get_bearer_token() -> str:
    cache = caches[_CACHE_ALIAS]
    token = cache.get(_TOKEN_KEY)
    if token:
        return str(token)
    token = login_parent()
    ttl = _ttl_from_jwt(token)
    cache.set(_TOKEN_KEY, token, timeout=ttl)
    return token


def invalidate_bearer_token() -> None:
    try:
        caches[_CACHE_ALIAS].delete(_TOKEN_KEY)
    except Exception as e:
        logger.warning('MOKO token cache delete failed: %s', e)
