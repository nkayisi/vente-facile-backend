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
