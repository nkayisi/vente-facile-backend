"""
Gestionnaire d'exceptions DRF global.

Objectif « production » : garantir qu'une requête API ne renvoie JAMAIS une page
d'erreur HTML 500 non parsable côté frontend. Le client (axios + drf-error.ts)
s'attend toujours à du JSON structuré ; une 500 HTML casse ce contrat et dégrade
l'expérience (message d'erreur illisible, comportement imprévisible).

Comportement :
1. On délègue d'abord au handler DRF par défaut (gère APIException, 404, 403,
   validation, throttling, etc. - inchangé).
2. Si le handler retourne None (exception NON gérée, ex: KeyError/AttributeError
   dans une vue), on logue l'exception (remontée à Sentry si configuré) et on
   renvoie une réponse JSON 500 propre au lieu de laisser Django produire une
   page HTML.
"""

import logging

from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger("apps.api")


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if response is not None:
        return response

    # Exception non gérée par DRF → 500. On logue avec le contexte de la vue
    # (remonté automatiquement à Sentry via l'intégration Django logging/erreurs).
    view = context.get("view") if context else None
    request = context.get("request") if context else None
    logger.exception(
        "Erreur serveur non gérée dans %s (%s %s)",
        getattr(view, "__class__", type(view)).__name__ if view else "?",
        getattr(request, "method", "?"),
        getattr(request, "path", "?"),
        exc_info=exc,
    )

    return Response(
        {
            "detail": "Une erreur interne est survenue. Nos équipes ont été notifiées.",
            "code": "internal_error",
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
