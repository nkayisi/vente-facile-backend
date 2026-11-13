"""
Les limites de débit, comptées par la VRAIE adresse de l'appelant.

DRF identifie un appelant anonyme par `X-Forwarded-For` et `NUM_PROXIES`. Or
toutes les connexions du back-office arrivent du serveur de Next : sans
l'adresse attestée par `apps.core.adresse`, la limite « 100 par heure et par
adresse » s'appliquait à TOUTE la plateforme réunie, et cent connexions
légitimes dans l'heure suffisaient à fermer la porte à tous les marchands.
"""
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle

from apps.core.adresse import adresse_client


class _ParAdresse:
    def get_ident(self, request):
        return adresse_client(request) or super().get_ident(request)


class AnonParAdresse(_ParAdresse, AnonRateThrottle):
    pass


class UserParAdresse(_ParAdresse, UserRateThrottle):
    pass


class ScopeParAdresse(_ParAdresse, ScopedRateThrottle):
    pass
