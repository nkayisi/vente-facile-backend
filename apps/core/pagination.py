"""
Pagination DRF étendue.

Le ``PageNumberPagination`` natif ignore le query param ``page_size`` côté
client par défaut, ce qui rend impossible un override depuis le frontend.
Ce module définit une pagination par défaut qui :

- accepte ``?page_size=N`` (avec un max raisonnable pour éviter qu'un client
  malicieux demande 100 000 lignes d'un coup) ;
- garde ``page_size=20`` comme défaut (cohérent avec l'historique du projet).
"""
from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    """Pagination par défaut pour toutes les ViewSets DRF.

    Override de ``page_size`` autorisé via ``?page_size=...`` (borné par
    ``max_page_size``). Au-delà, DRF clamp silencieusement à ``max_page_size``.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200
