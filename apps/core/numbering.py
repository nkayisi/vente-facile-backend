"""
Allocation des numéros de documents imprimés.

Un numéro doit être : stable (une réimpression rend le même), unique par
organisation, lisible par un commerçant, et retrouvable côté serveur. D'où la
forme `RGL-2026-00042` : préfixe de type, année, rang.
"""
from django.db import transaction
from django.utils import timezone

from .models import DocumentSequence

# Préfixes par type de document. Ils doivent rester alignés sur
# `DOCUMENT_IDENTITIES` côté frontend (frontend/lib/receipt/documents/types.ts) :
# c'est ce préfixe qui rend un reçu de règlement identifiable au premier regard.
PREFIX_DEBT_PAYMENT = 'RGL'
PREFIX_ADVANCE = 'AVC'
PREFIX_ADJUSTMENT = 'AJU'
PREFIX_SALE_RETURN = 'RET'
PREFIX_CASH_SESSION = 'CZ'
PREFIX_EXPENSE = 'DEP'
#: Réquisition de produits (T12). Elle s'imprime : le magasinier l'emporte au
#: marché, et son numéro est ce qui permet d'y revenir.
PREFIX_REQUISITION = 'REQ'
#: Achats (lot 13). Les trois générateurs d'origine triaient leurs références
#: ALPHABÉTIQUEMENT (« K » passe au-dessus de « 0 », donc une référence
#: d'appareil trouait la série) et datait leur préfixe en UTC, donc au mauvais
#: jour entre 23 h et minuit à Kinshasa. `allocate_document_number` n'a ni l'un
#: ni l'autre : son rang vient d'un compteur verrouillé, par organisation.
PREFIX_PURCHASE_ORDER = 'CMD'
PREFIX_GOODS_RECEIPT = 'BR'
PREFIX_SUPPLIER_PAYMENT = 'RGF'
PREFIX_PURCHASE_RETURN = 'RTF'


@transaction.atomic
def allocate_document_number(organization, prefix, year=None):
    """
    Réserve le numéro suivant pour ce couple organisation / préfixe.

    Le `select_for_update` est enveloppé dans un `atomic` EXPLICITE. Sans lui,
    PostgreSQL lève `TransactionManagementError`, et le défaut reste invisible en
    développement (SQLite ignore le verrou) comme en test (`APITestCase` ouvre sa
    propre transaction). Le projet s'est déjà fait prendre par ce piège sur
    `apply_payment_to_sale`.

    Réentrant : appelé depuis une transaction en cours, l'`atomic` se réduit à un
    point de sauvegarde et le verrou tient jusqu'à la validation de l'appelant.
    """
    year = year or timezone.now().year

    sequence, _ = DocumentSequence.objects.get_or_create(
        organization=organization,
        prefix=prefix,
        year=year,
    )
    # Relecture sous verrou : `get_or_create` ne verrouille pas, deux requêtes
    # concurrentes obtiendraient sinon le même `last_value`.
    sequence = DocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    sequence.last_value += 1
    sequence.save(update_fields=['last_value'])

    return f"{prefix}-{year}-{sequence.last_value:05d}"


#: Les cinq séries QUOTIDIENNES du serveur : `TR-20261113-0001`.
PREFIX_TRANSFER = 'TR'
PREFIX_STOCK_ADJUSTMENT = 'ADJ'
PREFIX_BATCH = 'LOT'
PREFIX_INVENTORY = 'INV'
PREFIX_CASH_MOVEMENT = 'MC'

#: Le rang d'un document du jour s'écrit sur quatre chiffres, et SEULS ceux-là
#: comptent : une référence d'appareil (`DEP-20261113-K7QM-0042`) ne doit ni
#: relever ni trouer la série du serveur.
_RANG_DU_JOUR = r'[0-9]{4}'


def _rang_le_plus_haut(model, champ, organization, prefixe):
    """
    Le plus grand rang déjà émis aujourd'hui sous ce préfixe, ou 0.

    ⚠ `_base_manager`, PAS `objects` : une pièce supprimée en douceur a gardé son
    numéro, et le réémettre ferait deux documents sous la même référence.
    """
    import re

    motif = rf'^{re.escape(prefixe)}-{_RANG_DU_JOUR}$'
    rangs = (
        model._base_manager
        .filter(organization=organization, **{f'{champ}__regex': motif})
        .values_list(champ, flat=True)
    )
    return max((int(r.rsplit('-', 1)[1]) for r in rangs), default=0)


@transaction.atomic
def allocate_daily_reference(organization, code, model, field='reference'):
    """
    La référence suivante d'une série QUOTIDIENNE : `TR-20261113-0001`.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LES CINQ ANCIENS GÉNÉRATEURS AVAIENT TROIS DÉFAUTS, ET AUCUN NE LEVAIT.  │
    │                                                                          │
    │ - Le jour était celui de GREENWICH : entre 23 h et minuit à Kinshasa, le │
    │   transfert de la soirée portait la date du lendemain.                   │
    │ - Le rang suivait un tri ALPHABÉTIQUE de la dernière référence : une     │
    │   pièce dont la référence ne suivait pas la forme relevait ou trouait la │
    │   série.                                                                 │
    │ - Deux créations simultanées lisaient la même « dernière » référence et  │
    │   ressortaient sous le MÊME numéro : rien ne les en empêchait, aucune de │
    │   ces colonnes n'étant unique.                                           │
    └──────────────────────────────────────────────────────────────────────────┘

    Le rang vient désormais d'un `DocumentSequence` relu sous verrou, comme
    `allocate_document_number`, mais la FORME ne change pas : un marchand qui a
    déjà imprimé `INV-20261030-0002` continue de lire des références du même
    dessin. La séquence porte le préfixe DU JOUR (`TR-20261113`).

    ⚠ LE PREMIER NUMÉRO DU JOUR REPREND LE RANG DÉJÀ ÉMIS. Un déploiement qui
      tombe en pleine journée trouve des références produites par l'ancien
      générateur : repartir à 1 réémettrait `TR-…-0001`. La séquence naît donc
      au plus haut rang de ce préfixe, borné à la forme à quatre chiffres.
    """
    jour = timezone.localdate()
    prefixe = f'{code}-{jour:%Y%m%d}'

    sequence, _ = DocumentSequence.objects.get_or_create(
        organization=organization,
        prefix=prefixe,
        year=jour.year,
        defaults={'last_value': _rang_le_plus_haut(model, field, organization, prefixe)},
    )
    sequence = DocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    sequence.last_value += 1
    sequence.save(update_fields=['last_value'])
    return f'{prefixe}-{sequence.last_value:04d}'
