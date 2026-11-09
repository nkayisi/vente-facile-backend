"""
L'arithmétique de la bascule points → argent, extraite pour être ÉPROUVABLE.

┌──────────────────────────────────────────────────────────────────────────────┐
│ POURQUOI UNE MIGRATION IMPORTE DU CODE D'APPLICATION ICI.                   │
│                                                                              │
│ La règle habituelle est de ne rien importer dans une migration : le code     │
│ change, la migration non, et un jour les deux ne disent plus la même chose.  │
│                                                                              │
│ Ces deux fonctions sont PURES - aucun modèle, aucune requête, deux           │
│ `Decimal` en entrée. Et la seule base qui rejouera un jour                   │
│ `0016_cagnotte_en_argent` est une base NEUVE, où il n'y a aucun solde à      │
│ convertir : le pire que puisse coûter une dérive future est donc nul.        │
│                                                                              │
│ Ce qu'on gagne, en revanche, est de pouvoir PROUVER la conversion. Elle      │
│ touche à de l'argent : un facteur oublié retire au client neuf dixièmes de   │
│ ce qu'il avait gagné, et une migration déjà passée ne se rejoue pas.         │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal, ROUND_HALF_UP

UN = Decimal('1.00')
CENTIEME = Decimal('0.01')

#: Bornes du champ `LoyaltyProgram.earn_percent`, recopiées de ses validateurs.
PERCENT_MIN = Decimal('0.01')
PERCENT_MAX = Decimal('100.00')


def facteur_de_conversion(point_value) -> Decimal:
    """
    Ce par quoi multiplier un solde en points pour obtenir un montant.

    Un `point_value` absent, nul ou négatif vaut UN : c'était la valeur par
    défaut du champ, et une division par zéro ou une multiplication par un
    négatif ne décrivent aucune cagnotte réelle. On préfère ne rien convertir
    plutôt que d'écrire un solde faux.
    """
    if point_value is None:
        return UN
    valeur = Decimal(point_value)
    return valeur if valeur > 0 else UN


def convertir_montant(montant, facteur) -> Decimal:
    """Un solde, un cumul ou une ligne de registre, passé en argent."""
    return (Decimal(montant or 0) * Decimal(facteur)).quantize(CENTIEME)


def pourcentage_depuis_bareme(points_per_unit, point_value, amount_per_unit) -> Decimal:
    """
    Le pourcentage équivalent à un barème par tranches.

    ⚠ C'EST UNE APPROXIMATION, ET ELLE PENCHE DU BON CÔTÉ. « X points pour
    CHAQUE Y dépensé » ne comptait pas les tranches entamées ; le pourcentage
    est continu. On retient le taux MOYEN - `points_per_unit × point_value ÷
    amount_per_unit × 100` - qui donne le même gain sur une tranche pleine et un
    peu plus sur une tranche entamée. Personne ne perd de cagnotte.

    Un barème sans tranche (`amount_per_unit` nul) ne peut pas se convertir :
    on retombe sur 1 %, l'ancien défaut du mode pourcentage, plutôt que de
    laisser le programme sans gain.
    """
    tranche = Decimal(amount_per_unit or 0)
    if tranche <= 0:
        return UN

    brut = (
        Decimal(points_per_unit or 0) * facteur_de_conversion(point_value) / tranche * 100
    ).quantize(CENTIEME, rounding=ROUND_HALF_UP)

    # Les validateurs du champ bornent à [0.01, 100] : un barème très généreux
    # (ou absurde) ne doit pas rendre le programme inéditable depuis l'écran.
    return min(max(brut, PERCENT_MIN), PERCENT_MAX)


def pourcentage_depuis_pourcentage(points_percentage, point_value) -> Decimal:
    """
    Le pourcentage équivalent à l'ancien mode « pourcentage ».

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ IL FAUT LE CONVERTIR AUSSI, ET C'EST CONTRE-INTUITIF.                   │
    │                                                                          │
    │ `points_percentage` était « pourcentage du montant converti en POINTS », │
    │ pas en argent : un programme réglé à 1 % avec `point_value = 10`         │
    │ donnait 100 points sur une facture de 10 000, soit 1 000 en valeur -     │
    │ c'est-à-dire 10 % de la facture. Reprendre le nombre tel quel divise     │
    │ donc le gain par `point_value` : les SOLDES étaient bien multipliés,     │
    │ pendant que le taux de gain futur tombait d'un facteur dix, sans         │
    │ qu'aucun écran ne le dise. Et dans l'autre sens - un point valant 0,10 - │
    │ le gain aurait été multiplié par dix.                                    │
    │                                                                          │
    │ Seul `point_value = 1`, l'ancien défaut du champ, se reprenait tel quel. │
    └──────────────────────────────────────────────────────────────────────────┘

    Même bornage que le barème par tranches, et pour la même raison : un
    programme converti hors des validateurs du champ serait INÉDITABLE depuis
    l'écran.
    """
    brut = (
        Decimal(points_percentage or 0) * facteur_de_conversion(point_value)
    ).quantize(CENTIEME, rounding=ROUND_HALF_UP)
    if brut <= 0:
        return UN
    return min(max(brut, PERCENT_MIN), PERCENT_MAX)
