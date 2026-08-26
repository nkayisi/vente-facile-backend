"""
Service de dette client - **seul point d'écriture** du solde d'un client.

Avant ce lot, six endroits différents faisaient `customer.current_balance += ...`
à la main, avec des règles divergentes : la vente à crédit inscrivait la dette,
mais le règlement depuis la fiche client ne touchait aucune facture, et un
surpaiement créditait le client de la monnaie qu'on venait de lui rendre. Tout
passe désormais par ici.

Deux niveaux de vérité, à ne pas confondre :

- ``CustomerBalance`` : la dette RÉELLE, une ligne par devise. Un client peut
  devoir 50 USD et 40 000 CDF ; ces montants ne s'additionnent jamais.
- ``Customer.current_balance`` : vue comptable agrégée, convertie en devise
  principale, pour la limite de crédit, le tri et les rapports.

Convention de signe : positif = le client doit, négatif = le client est
créditeur (avance non encore consommée).
"""
from decimal import Decimal

from django.db import transaction

from apps.settings.services import CurrencyService

from .models import Customer, CustomerBalance, CustomerTransaction

TWO_PLACES = Decimal('0.01')


def _quantize(amount):
    return Decimal(amount or 0).quantize(TWO_PLACES)


def _lock_customer(customer):
    """
    Sérialise toutes les écritures de dette d'un même client.

    Le verrou de `_move_balance` ne porte que sur la ligne de devise, ce qui ne
    suffit pas pour deux raisons :

    - `current_balance` est **recalculé** depuis toutes les lignes de devise puis
      réécrit. Deux règlements concurrents dans deux devises différentes ne se
      bloquaient pas et écrasaient mutuellement leur recalcul (lost update) ;
      l'agrégat dérivait alors des `CustomerBalance` qu'il est censé résumer.
    - Le contrôle de limite de crédit de `apply_debt` lit le solde **avant** tout
      verrou : deux ventes à crédit simultanées pouvaient chacune valider la
      limite et la dépasser ensemble.

    Le verrou est pris sur la ligne `Customer`, avant toute lecture qui décide.
    `with_deleted()` : un client archivé conserve sa dette, on doit pouvoir la
    solder.
    """
    Customer.objects.with_deleted().select_for_update().filter(pk=customer.pk).first()


def get_balance(customer, currency):
    """Dette du client dans une devise donnée (0 si aucune ligne)."""
    row = CustomerBalance.objects.filter(
        customer=customer, currency=currency,
    ).first()
    return row.amount if row else Decimal('0.00')


def balances_by_currency(customer):
    """Dict ``{devise: montant}`` des soldes non nuls."""
    return {
        row.currency: row.amount
        for row in CustomerBalance.objects.filter(customer=customer)
        if row.amount != 0
    }


def recompute_primary_balance(customer, save=True):
    """
    Recalcule ``Customer.current_balance`` = somme des soldes par devise,
    convertis en devise principale.

    La conversion suit la convention du projet : vers la principale on
    **multiplie** par le taux de la devise.
    """
    total = Decimal('0.00')
    for row in CustomerBalance.objects.filter(customer=customer):
        if row.amount == 0:
            continue
        total += CurrencyService.convert_to_primary(
            row.amount, row.currency, customer.organization,
        )
    total = _quantize(total)
    if save and customer.current_balance != total:
        customer.current_balance = total
        customer.save(update_fields=['current_balance'])
    else:
        customer.current_balance = total
    return total


def _move_balance(customer, delta, currency, exchange_rate=None):
    """
    Applique un delta signé sur la ligne de devise et renvoie
    ``(solde_avant, solde_après, devise, taux)``.

    Le verrou porte sur la ligne `CustomerBalance` : deux règlements concurrents
    sur la même devise sont sérialisés, mais deux devises différentes ne se
    bloquent pas mutuellement.
    """
    currency, exchange_rate = CurrencyService.resolve(
        customer.organization, currency, exchange_rate,
    )
    row, _ = CustomerBalance.objects.get_or_create(
        organization=customer.organization,
        customer=customer,
        currency=currency,
        defaults={'amount': Decimal('0.00')},
    )
    row = CustomerBalance.objects.select_for_update().get(pk=row.pk)

    before = row.amount
    row.amount = _quantize(before + Decimal(delta))
    row.save(update_fields=['amount'])
    return before, row.amount, currency, exchange_rate


@transaction.atomic
def apply_debt(customer, amount, *, currency=None, exchange_rate=None,
               transaction_type=CustomerTransaction.TransactionType.CREDIT_SALE,
               sale=None, reference='', notes='', user=None,
               enforce_credit_limit=True, receipt_number=''):
    """
    Inscrit une dette (le client doit davantage).

    ``enforce_credit_limit`` compare la dette totale convertie en devise
    principale à ``credit_limit``, qui est lui aussi exprimé en principale.
    Un ``credit_limit`` à 0 signifie **illimité**, pas « crédit interdit » :
    l'autorisation d'acheter à crédit est portée par ``Customer.allow_credit``.
    """
    amount = _quantize(amount)
    if amount <= 0:
        return None

    _lock_customer(customer)

    if enforce_credit_limit and not customer.allow_credit:
        from rest_framework.exceptions import ValidationError
        raise ValidationError({
            'customer': (
                f"{customer.name} n'est pas autorisé à acheter à crédit. "
                "Activez l'autorisation de crédit sur sa fiche pour lui laisser "
                "une facture ouverte."
            )
        })

    if enforce_credit_limit and customer.credit_limit > 0:
        added_primary = CurrencyService.convert_to_primary(
            amount, currency or CurrencyService.primary_code(customer.organization),
            customer.organization,
        )
        projected = recompute_primary_balance(customer, save=False) + added_primary
        if projected > customer.credit_limit:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({
                'customer': (
                    f"Cette opération dépasse la limite de crédit du client. "
                    f"Dette actuelle : {customer.current_balance}, "
                    f"Limite autorisée : {customer.credit_limit}, "
                    f"Montant ajouté : {amount}."
                )
            })

    before, after, currency, rate = _move_balance(
        customer, amount, currency, exchange_rate,
    )
    txn = CustomerTransaction.objects.create(
        organization=customer.organization,
        customer=customer,
        transaction_type=transaction_type,
        amount=amount,
        currency=currency,
        exchange_rate=rate,
        balance_before=before,
        balance_after=after,
        sale=sale,
        reference=reference,
        receipt_number=receipt_number,
        notes=notes,
        created_by=user,
    )
    recompute_primary_balance(customer)
    return txn


@transaction.atomic
def settle_debt(customer, amount, *, currency=None, exchange_rate=None,
                transaction_type=CustomerTransaction.TransactionType.PAYMENT,
                sale=None, reference='', notes='', user=None,
                payment_method='', receipt_number=''):
    """
    Réduit la dette (le client a payé). Un excédent rend le solde négatif :
    le client devient créditeur, l'avance sera consommée par une vente future.

    ``receipt_number`` est imposé par l'opération appelante, jamais alloué ici :
    un règlement qui solde trois factures crée trois mouvements et doit porter un
    seul numéro de reçu, celui du papier remis au client.
    """
    amount = _quantize(amount)
    if amount <= 0:
        return None

    _lock_customer(customer)

    before, after, currency, rate = _move_balance(
        customer, -amount, currency, exchange_rate,
    )
    txn = CustomerTransaction.objects.create(
        organization=customer.organization,
        customer=customer,
        transaction_type=transaction_type,
        amount=amount,
        currency=currency,
        exchange_rate=rate,
        balance_before=before,
        balance_after=after,
        sale=sale,
        reference=reference,
        notes=notes,
        payment_method=payment_method,
        receipt_number=receipt_number,
        created_by=user,
    )
    recompute_primary_balance(customer)
    return txn


@transaction.atomic
def adjust_balance(customer, signed_amount, *, currency=None, exchange_rate=None,
                   notes='', user=None, receipt_number=''):
    """Ajustement manuel : positif augmente la dette, négatif la réduit."""
    signed_amount = _quantize(signed_amount)
    if signed_amount == 0:
        return None

    _lock_customer(customer)

    before, after, currency, rate = _move_balance(
        customer, signed_amount, currency, exchange_rate,
    )
    txn = CustomerTransaction.objects.create(
        organization=customer.organization,
        customer=customer,
        transaction_type=CustomerTransaction.TransactionType.ADJUSTMENT,
        amount=abs(signed_amount),
        currency=currency,
        exchange_rate=rate,
        balance_before=before,
        balance_after=after,
        notes=notes,
        receipt_number=receipt_number,
        created_by=user,
    )
    recompute_primary_balance(customer)
    return txn


def open_credit_sales(customer, currency=None):
    """
    Factures du client encore dues, de la plus ancienne à la plus récente.

    C'est l'ordre d'imputation d'un règlement : on solde les dettes les plus
    vieilles d'abord. Filtré par devise quand elle est précisée, car un
    règlement en USD ne peut pas solder une facture libellée en CDF.
    """
    from apps.sales.models import Sale

    qs = Sale.objects.filter(
        customer=customer,
        organization=customer.organization,
        status__in=[Sale.Status.PENDING, Sale.Status.PARTIALLY_PAID],
        amount_due__gt=0,
    )
    if currency:
        qs = qs.filter(currency=currency)
    return qs.order_by('created_at')


def available_advance(customer, currency):
    """Avance disponible (solde négatif) dans une devise, en valeur positive."""
    balance = get_balance(customer, currency)
    return -balance if balance < 0 else Decimal('0.00')
