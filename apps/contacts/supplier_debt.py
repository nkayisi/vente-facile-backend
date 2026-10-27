"""
Service de dette fournisseur - **seul point d'écriture** du solde d'un fournisseur.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE MODULE EXISTE PARCE QU'UNE DETTE FOURNISSEUR NE S'INSCRIVAIT NULLE PART.  │
│                                                                              │
│ Trois endroits écrivaient `supplier.current_balance` à la main, et tous les  │
│ trois le faisaient BAISSER : le règlement (`-= amount`), le retour           │
│ (`-= total_amount`), et son annulation en sens inverse. Rien ne le CRÉDITAIT │
│ à la réception des marchandises. Payer un fournisseur rendait donc son solde │
│ négatif, indéfiniment, et le marchand lisait un crédit là où il avait une    │
│ dette.                                                                       │
│                                                                              │
│ C'est le défaut exact que le côté client a déjà payé sur le devis converti : │
│ « créait une facture due sans inscrire la dette, alors que son règlement la  │
│ décrémentait ⇒ le client devenait artificiellement créditeur ».              │
└──────────────────────────────────────────────────────────────────────────────┘

Deux niveaux de vérité, à ne pas confondre :

- ``SupplierBalance`` : la dette RÉELLE, une ligne par devise. On peut devoir
  50 USD et 40 000 CDF au même fournisseur ; ces montants ne s'additionnent
  jamais.
- ``Supplier.current_balance`` : vue comptable agrégée, convertie en devise
  principale, pour le tri et les rapports.

⚠ ET IL NE FAUT PAS LES CONFONDRE AVEC ``PurchaseOrder.amount_due`` : celui-là
est l'enveloppe d'UNE commande (ce qu'on devra quand tout sera reçu), quand le
solde ci-dessus est ce qu'on doit MAINTENANT, tous ordres confondus. Les deux
coexistent, exactement comme ``Sale.amount_due`` coexiste avec
``CustomerBalance``.

Convention de signe, MIROIR de celle du client : positif = nous devons,
négatif = nous avons payé d'avance (acompte non encore consommé).
"""
from decimal import Decimal

from django.db import transaction

from apps.settings.services import CurrencyService

from .models import Supplier, SupplierBalance, SupplierTransaction

TWO_PLACES = Decimal('0.01')


def _quantize(amount):
    return Decimal(amount or 0).quantize(TWO_PLACES)


def _lock_supplier(supplier):
    """
    Sérialise toutes les écritures de dette d'un même fournisseur.

    Le verrou de `_move_balance` ne porte que sur la ligne de devise, ce qui ne
    suffit pas : `current_balance` est **recalculé** depuis toutes les lignes
    puis réécrit, donc deux règlements concurrents dans deux devises
    différentes ne se bloqueraient pas et écraseraient mutuellement leur
    recalcul. L'agrégat dériverait alors des `SupplierBalance` qu'il résume.
    C'est le même raisonnement que `contacts.services._lock_customer`, et le
    côté client a mesuré la dérive avant de poser ce verrou.

    `with_deleted()` : un fournisseur archivé conserve sa dette, on doit
    pouvoir la solder.
    """
    Supplier.objects.with_deleted().select_for_update().filter(pk=supplier.pk).first()


def get_balance(supplier, currency):
    """Ce que nous devons à ce fournisseur dans une devise donnée (0 si rien)."""
    row = SupplierBalance.objects.filter(
        supplier=supplier, currency=currency,
    ).first()
    return row.amount if row else Decimal('0.00')


def balances_by_currency(supplier):
    """Dict ``{devise: montant}`` des soldes non nuls.

    ⚠ AUCUN APPELANT DE PRODUCTION AUJOURD'HUI, et c'est écrit plutôt que tu.
    Les deux serializers lisent `obj.balances.all()` à la place, délibérément :
    ce helper émet un `filter()`, ce qui court-circuite le cache de prefetch et
    faisait payer une requête par ligne à la liste des fournisseurs. Il reste
    pour les appelants HORS contexte de préchargement - une commande de
    gestion, un rapport - et parce que l'avertissement de
    `_soldes_du_fournisseur` le désigne nommément.
    """
    return {
        row.currency: row.amount
        for row in SupplierBalance.objects.filter(supplier=supplier)
        if row.amount != 0
    }


def recompute_primary_balance(supplier, save=True):
    """
    Recalcule ``Supplier.current_balance`` = somme des soldes par devise,
    convertis en devise principale.

    La conversion suit la convention du projet : vers la principale on
    **multiplie** par le taux de la devise.
    """
    total = Decimal('0.00')
    for row in SupplierBalance.objects.filter(supplier=supplier):
        if row.amount == 0:
            continue
        total += CurrencyService.convert_to_primary(
            row.amount, row.currency, supplier.organization,
        )
    total = _quantize(total)
    if save and supplier.current_balance != total:
        supplier.current_balance = total
        supplier.save(update_fields=['current_balance'])
    else:
        supplier.current_balance = total
    return total


def _move_balance(supplier, delta, currency, exchange_rate=None):
    """
    Applique un delta signé sur la ligne de devise et renvoie
    ``(solde_avant, solde_après, devise, taux)``.

    Le verrou porte sur la ligne `SupplierBalance` : deux écritures
    concurrentes sur la même devise sont sérialisées, deux devises différentes
    ne se bloquent pas mutuellement.
    """
    currency, exchange_rate = CurrencyService.resolve(
        supplier.organization, currency, exchange_rate,
    )
    row, _ = SupplierBalance.objects.get_or_create(
        organization=supplier.organization,
        supplier=supplier,
        currency=currency,
        defaults={'amount': Decimal('0.00')},
    )
    row = SupplierBalance.objects.select_for_update().get(pk=row.pk)

    before = row.amount
    row.amount = _quantize(before + Decimal(delta))
    row.save(update_fields=['amount'])
    return before, row.amount, currency, exchange_rate


@transaction.atomic
def _ecrire(supplier, delta, transaction_type, *, currency=None,
            exchange_rate=None, purchase_order=None, goods_receipt=None,
            supplier_payment=None, reference='', notes='', user=None):
    """
    Corps unique de toute écriture de solde fournisseur.

    Les quatre fonctions publiques ci-dessous ne font que NOMMER l'acte et son
    signe. Elles existent pour que l'appelant n'ait jamais à décider du signe :
    c'est la décision qu'on se trompe, et elle ne se voit pas - un retour qui
    augmenterait la dette au lieu de la réduire rend un solde plausible et
    faux.
    """
    amount = _quantize(abs(delta))
    if amount <= 0:
        return None

    _lock_supplier(supplier)

    before, after, currency, rate = _move_balance(
        supplier, delta, currency, exchange_rate,
    )
    txn = SupplierTransaction.objects.create(
        organization=supplier.organization,
        supplier=supplier,
        transaction_type=transaction_type,
        amount=amount,
        currency=currency,
        exchange_rate=rate,
        balance_before=before,
        balance_after=after,
        purchase_order=purchase_order,
        goods_receipt=goods_receipt,
        supplier_payment=supplier_payment,
        reference=reference,
        notes=notes,
        created_by=user,
    )
    recompute_primary_balance(supplier)
    return txn


def apply_debt(supplier, amount, **kwargs):
    """
    Inscrit une dette : nous devons davantage.

    Appelée à la VALIDATION d'une réception, pas à la création d'une commande.
    On ne doit rien pour une marchandise qu'on n'a pas reçue, et c'est aussi
    l'instant où le fournisseur remet sa facture. C'est le miroir exact de
    `register_sale_debt`, qui inscrit la dette du client quand la facture est
    émise.
    """
    return _ecrire(
        supplier, _quantize(amount),
        SupplierTransaction.TransactionType.RECEIPT, **kwargs
    )


def settle_debt(supplier, amount, **kwargs):
    """
    Réduit la dette : nous avons payé.

    Un excédent rend le solde négatif - nous avons versé un acompte, qu'une
    réception future consommera. C'est la symétrie de l'avance client, et on ne
    la borne pas : payer d'avance un fournisseur est un geste ordinaire.
    """
    return _ecrire(
        supplier, -_quantize(amount),
        SupplierTransaction.TransactionType.PAYMENT, **kwargs
    )


def apply_return_credit(supplier, amount, **kwargs):
    """
    Réduit la dette : la marchandise est repartie chez le fournisseur.

    ⚠ Un retour ne rend PAS d'argent au tiroir, il éteint d'abord ce qu'on
    doit. C'est la même règle que celle posée sur le retour de vente (« un
    retour sur facture encore due éteint d'abord la dette, seul le reliquat
    sort en espèces »), prise par l'autre bout.
    """
    return _ecrire(
        supplier, -_quantize(amount),
        SupplierTransaction.TransactionType.RETURN, **kwargs
    )


def reverse_payment(supplier, amount, **kwargs):
    """
    Restitue la dette qu'un règlement annulé avait éteinte.

    Le type reste `payment` : la ligne du grand livre doit se lire comme la
    contrepartie du règlement qu'elle défait, pas comme un ajustement dont
    personne ne retrouverait l'origine.
    """
    return _ecrire(
        supplier, _quantize(amount),
        SupplierTransaction.TransactionType.PAYMENT, **kwargs
    )


def adjust_balance(supplier, delta, **kwargs):
    """
    Correction d'écriture, sur décision humaine.

    Distincte des quatre actes ci-dessus : aucune pièce ne la justifie, et
    c'est précisément pourquoi elle porte son propre type. Le signe est à la
    charge de l'appelant - un delta positif creuse la dette.
    """
    return _ecrire(
        supplier, _quantize(delta),
        SupplierTransaction.TransactionType.ADJUSTMENT, **kwargs
    )
