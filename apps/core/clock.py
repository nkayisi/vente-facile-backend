"""
L'HEURE DE L'ACTE, PAS CELLE DE SON ENREGISTREMENT.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UNE VENTE ENCAISSÉE HORS LIGNE ÉTAIT DATÉE DU JOUR DE SA POUSSÉE.           │
│                                                                              │
│ `Sale.sale_date` était un `auto_now_add` : posé à l'INSERTION serveur, et    │
│ `occurred_at` n'y arrivait pas. Un marchand resté trois jours sans réseau    │
│ voyait donc, le jour du retour, trois journées de ventes empilées sur        │
│ celle-ci. Ses rapports, son chiffre du jour, sa marge, son Z : tout se       │
│ rangeait au mauvais quantième, et rien ne le signalait. Le même piège        │
│ tenait `Payment.paid_at`, `RegisterSession.opened_at`, `SaleReturn           │
│ .return_date`, et - par `created_at` - le journal des mouvements de stock    │
│ comme les écritures au compte d'un client.                                   │
│                                                                              │
│ Le terminal est le SEUL à savoir quand l'argent est entré. Le serveur, lui,  │
│ ne sait que quand il l'a appris.                                             │
└──────────────────────────────────────────────────────────────────────────────┘

**Le mécanisme est UNIQUE, et il doit l'être.** Passer l'horodatage en paramètre
demanderait de le faire traverser `SaleCreateSerializer.create()`, puis
`apply_payment_to_sale`, puis `register_sale_debt`, puis le décrément de stock -
c'est-à-dire de modifier des corps que le back-office appelle aussi, pour une
valeur qu'il ne fournit jamais. Un seul oubli sur ce chemin, et une ligne se
range au mauvais jour sans que rien ne le dise. Ici il n'y a rien à se rappeler
d'appel en appel : le répartiteur du journal pose l'heure de l'acte, et tout ce
qui s'écrit pendant l'opération la lit.

**`updated_at` N'EST JAMAIS SUR CETTE HORLOGE, ET NE DOIT JAMAIS L'ÊTRE.** Le
tirage pagine sur un curseur `(updated_at, id)` : un `updated_at` reculé placerait
la ligne AVANT le point de reprise de terminaux déjà passés par là, qui ne la
verraient donc jamais - définitivement, et tous à la fois. C'est le défaut de
`CustomerBalance` du lot 6, pris par l'autre bout et en pire, puisqu'il serait
introduit exprès. `apps/core/tests/test_business_clock.py` l'interdit.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

#: Heure de l'acte en cours de rejeu, ou `None` quand on sert une requête
#: ordinaire du back-office - où l'acte et son enregistrement sont simultanés.
_horloge = ContextVar('horloge_metier', default=None)

#: Tolérance d'avance admise sur l'horloge d'un terminal.
#:
#: Un appareil dont l'heure est en avance daterait ses ventes dans le futur :
#: elles se rangeraient en tête de toutes les listes, pour toujours, et hors de
#: tout rapport borné à aujourd'hui. Quelques minutes couvrent une dérive
#: ordinaire ; au-delà, on retient l'heure du serveur, qui est fausse de
#: quelques secondes plutôt que de plusieurs jours.
AVANCE_TOLEREE = timedelta(minutes=5)


def maintenant():
    """
    L'heure à inscrire sur ce qui s'écrit.

    Celle de l'ACTE quand un acte du journal est en cours de rejeu, celle du
    serveur autrement. À employer comme `default=` de tout champ de date
    métier, à la place d'`auto_now_add`.
    """
    return _horloge.get() or timezone.now()


@contextmanager
def horloge_de_l_acte(quand):
    """
    Pose l'heure de l'acte pour la durée d'une opération du journal.

    `quand` à `None` (ou en avance sur le serveur au-delà de la tolérance)
    laisse l'horloge du serveur : une date absente ou aberrante ne doit pas
    empêcher l'acte d'aboutir, elle doit seulement cesser d'être crue.

    Le rétablissement passe par le JETON de `ContextVar`, jamais par une remise
    à `None` : les opérations d'un lot se rejouent l'une après l'autre, et une
    remise à zéro effacerait l'heure d'un appel englobant au lieu de rendre la
    précédente.
    """
    jeton = _horloge.set(_retenir(quand))
    try:
        yield
    finally:
        _horloge.reset(jeton)


def _retenir(quand):
    """
    L'heure du terminal, si elle est croyable. Sinon `None`.

    Accepte une CHAÎNE autant qu'un `datetime` : `occurred_at` arrive du corps
    JSON de la requête et n'est validé par aucun serializer. Une chaîne
    illisible - horloge d'appareil exotique, corps tronqué - rend `None`, donc
    l'heure du serveur : une date qu'on ne sait pas lire ne doit pas faire
    échouer un acte, elle doit seulement cesser d'être crue.
    """
    if quand is None:
        return None
    if isinstance(quand, str):
        quand = parse_datetime(quand)
        if quand is None:
            return None
    if not isinstance(quand, datetime):
        return None
    if timezone.is_naive(quand):
        quand = timezone.make_aware(quand, timezone.get_default_timezone())
    if quand > timezone.now() + AVANCE_TOLEREE:
        return None
    return quand
