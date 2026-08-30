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


# ---------------------------------------------------------------------------
# Actes complets : règlement d'un client, ajustement de son solde.
#
# Ils vivent ICI et non dans la vue parce que deux surfaces les déclenchent :
# le back-office par `CustomerViewSet`, et le terminal mobile par le journal
# d'opérations (`sync.handlers`). Tant que le corps de l'acte était écrit dans
# la vue, le gestionnaire de synchronisation le RÉÉCRIVAIT, et il en avait
# perdu trois pièces au passage : la devise d'imputation (`settle_currency`),
# la résolution stricte du taux, et le préfixe `AVC` d'une avance - un client
# sans facture ouverte recevait un reçu numéroté comme un règlement.
#
# La parité entre les deux surfaces n'est pas surveillée par un test : elle est
# structurelle, parce qu'il n'y a plus qu'un seul corps.
# ---------------------------------------------------------------------------


def _settle_open_invoices(customer, amount, currency, user,
                          payment_method='cash', reference='', notes='',
                          settle_currency=None, receipt_number=None):
    """
    Impute un règlement sur les factures ouvertes du client, de la plus
    ancienne à la plus récente, et renvoie ``(reliquat, factures_soldées)``.

    C'est le correctif central de la dette : auparavant ce chemin ne faisait
    que déplacer ``current_balance``, laissant les ventes en
    ``pending``/``partially_paid`` avec un ``amount_due`` non nul. Le solde
    du client et la somme des factures dues divergeaient dès le premier
    acompte, et comme la vente n'atteignait jamais ``completed``, les points
    de fidélité n'étaient jamais attribués.

    ``currency`` est la devise **remise** (celle qui entre au tiroir),
    ``settle_currency`` celle des **factures visées**. Les deux étaient
    confondues : un client devant en USD qui payait en CDF ne voyait pas sa
    dette bouger, le montant partant en avance CDF. Le reliquat reste
    exprimé dans la devise remise, puisque c'est l'argent réellement détenu.
    """
    from apps.sales.services import apply_payment_to_sale, get_loyalty_payment_method
    from apps.sales.models import PaymentMethod, Sale

    is_loyalty = payment_method == PaymentMethod.MethodType.LOYALTY

    if is_loyalty:
        # Surtout pas de repli ici : le repli « première méthode active »
        # renvoyait `cash` quand aucune méthode « fidélité » n'existait, et
        # `apply_payment_to_sale` n'exclut le mouvement de caisse que sur le
        # type `loyalty`. Des points se transformaient donc en entrée
        # d'argent réel au tiroir. Le helper crée la méthode au besoin.
        method = get_loyalty_payment_method(customer.organization)
    else:
        # Repli sur une autre méthode d'encaissement si le type demandé
        # n'existe pas dans l'organisation, mais jamais sur « fidélité » :
        # ce serait de l'argent encaissé qui n'entrerait pas en caisse.
        method = PaymentMethod.objects.filter(
            organization=customer.organization,
            method_type=payment_method,
            is_active=True,
        ).first() or PaymentMethod.objects.filter(
            organization=customer.organization, is_active=True,
        ).exclude(method_type=PaymentMethod.MethodType.LOYALTY).first()

    settle_currency = settle_currency or currency

    # On raisonne dans la devise des FACTURES pour décider combien chacune
    # absorbe, puis on reconvertit vers la devise remise pour l'encaissement
    # lui-même : `apply_payment_to_sale` attend un montant tendu et sait le
    # ramener à la devise de la vente.
    remaining_settle = CurrencyService.convert(
        amount, currency, settle_currency, customer.organization,
    )['converted_amount']

    remaining = amount
    touched = []
    for open_sale in open_credit_sales(customer, settle_currency):
        if remaining <= 0 or remaining_settle <= 0:
            break
        # `open_credit_sales` lit hors verrou. Sans cette relecture, deux
        # règlements concurrents imputaient chacun le même montant sur la
        # même facture : le second produisait un surplus rendu en monnaie
        # sur une facture déjà soldée.
        locked = Sale.objects.select_for_update().get(pk=open_sale.pk)
        if locked.amount_due <= 0 or locked.status in ('completed', 'cancelled', 'refunded'):
            continue

        applied_settle = min(remaining_settle, locked.amount_due)
        if applied_settle <= 0:
            continue

        # Part de l'argent remis que cette facture consomme. Même chemin de
        # conversion que la modale de paiement d'une facture : à devise
        # égale, `convert` renvoie le montant inchangé.
        applied_tendered = min(
            remaining,
            CurrencyService.convert(
                applied_settle, settle_currency, currency, customer.organization,
            )['converted_amount'],
        )
        if applied_tendered <= 0:
            continue

        apply_payment_to_sale(
            locked.id, user,
            payment_method_id=method.id if method else None,
            tendered_amount=applied_tendered,
            currency=currency,
            reference=reference,
            notes=notes or f"Règlement client {customer.name}",
            # Une facture réglée avec des points n'en rapporte pas de
            # nouveaux : elle en consomme.
            award_loyalty=not is_loyalty,
            # Toutes les factures soldées par ce versement portent le numéro
            # du reçu unique remis au client.
            receipt_number=receipt_number,
        )
        remaining -= applied_tendered
        remaining_settle -= applied_settle
        touched.append(locked.reference)

    return remaining, touched


def record_payment(customer, amount, *, user, currency=None, exchange_rate=None,
                   settle_currency=None, payment_method='cash', reference='',
                   notes='', receipt_number=None):
    """
    Enregistre un règlement du client et l'impute sur ses factures ouvertes.

    Le règlement solde d'abord les factures les plus anciennes ; un éventuel
    reliquat devient une avance (solde créditeur), dans la devise remise.

    ``currency`` est la devise remise, ``settle_currency`` celle des factures
    visées : un client qui doit en USD peut payer en francs congolais.

    ``receipt_number`` permet à un appelant d'imposer le numéro : c'est le cas
    du terminal mobile, qui a DÉJÀ imprimé le reçu quand l'acte remonte. Laisser
    le serveur en allouer un second donnerait deux numéros pour un versement, et
    le papier que le client détient ne désignerait plus rien.

    Rend le même dictionnaire que l'endpoint, pour que les deux surfaces
    répondent la même chose.
    """
    from apps.cashbook.services import record_customer_advance
    from apps.core.numbering import (
        PREFIX_ADVANCE, PREFIX_DEBT_PAYMENT, allocate_document_number,
    )

    currency, exchange_rate = CurrencyService.resolve(
        customer.organization, currency, exchange_rate, strict=True,
    )
    # Devise des factures à solder. Par défaut celle de l'argent remis, ce
    # qui préserve le comportement des appelants qui ne la connaissent pas.
    settle_currency, _ = CurrencyService.resolve(
        customer.organization, settle_currency or currency, None, strict=True,
    )
    amount = _quantize(amount)

    with transaction.atomic():
        # Numéro alloué AVANT le règlement, et une seule fois : le client
        # repart avec un papier, quel que soit le nombre de factures soldées
        # et de lignes écrites. Le préfixe dépend de ce que l'opération est
        # vraiment : un règlement s'il y a des factures ouvertes à solder,
        # une avance sinon.
        has_open_invoices = bool(open_credit_sales(customer, settle_currency))
        receipt_number = receipt_number or allocate_document_number(
            customer.organization,
            PREFIX_DEBT_PAYMENT if has_open_invoices else PREFIX_ADVANCE,
        )
        # Solde d'avant l'opération, dans la devise remise : c'est le
        # « Dette avant » du reçu. Le lire après coup donnerait le solde
        # d'arrivée pour les deux lignes.
        balance_before = get_balance(customer, currency)
        # `apply_payment_to_sale` met déjà à jour la dette de chaque facture
        # soldée : on n'enregistre ici que le reliquat, en avance.
        remaining, touched = _settle_open_invoices(
            customer, amount, currency, user,
            payment_method=payment_method, reference=reference, notes=notes,
            settle_currency=settle_currency, receipt_number=receipt_number,
        )

        txn = None
        if remaining > 0:
            txn = settle_debt(
                customer, remaining,
                currency=currency,
                exchange_rate=exchange_rate,
                transaction_type=CustomerTransaction.TransactionType.ADVANCE,
                reference=reference,
                notes=notes or "Avance client (aucune facture à solder)",
                user=user,
                payment_method=payment_method,
                receipt_number=receipt_number,
            )
            # Le reliquat n'a soldé aucune facture : il entre au tiroir ici.
            record_customer_advance(
                organization=customer.organization,
                customer=customer,
                amount=remaining,
                user=user,
                notes=notes,
                currency=currency,
                exchange_rate=exchange_rate,
            )

        customer.refresh_from_db()

    return {
        'transaction': txn,
        # Numéro et soldes au niveau de l'ENVELOPPE, et pas seulement sur
        # `transaction` : celle-ci est nulle quand le versement a soldé des
        # factures sans laisser de reliquat, c'est-à-dire dans le cas
        # nominal. Un reçu ne peut donc pas en dépendre.
        'receipt_number': receipt_number,
        'balance_before': str(balance_before),
        'balance_after': str(get_balance(customer, currency)),
        'settled_invoices': touched,
        'advance_amount': str(remaining),
        'currency': currency,
        'settle_currency': settle_currency,
        'new_balance': str(customer.current_balance),
        'balances': balances_by_currency(customer),
    }


def adjust_customer_balance(customer, signed_amount, *, user, currency=None,
                            exchange_rate=None, notes='', receipt_number=None):
    """
    Ajustement manuel du solde d'un client, dans une devise donnée.

    Positif : on inscrit une dette de plus (correction en faveur du marchand).
    Négatif : on la réduit, et **l'argent entre au tiroir** - réduire une dette
    sans contrepartie ferait sortir la somme des comptes sans que personne ne
    l'ait reçue.
    """
    from apps.cashbook.services import record_customer_debt_payment
    from apps.core.numbering import PREFIX_ADJUSTMENT, allocate_document_number

    currency, exchange_rate = CurrencyService.resolve(
        customer.organization, currency, exchange_rate, strict=True,
    )
    signed_amount = _quantize(signed_amount)

    with transaction.atomic():
        receipt_number = receipt_number or allocate_document_number(
            customer.organization, PREFIX_ADJUSTMENT,
        )
        balance_before = get_balance(customer, currency)

        if signed_amount > 0:
            txn = apply_debt(
                customer, signed_amount,
                currency=currency,
                exchange_rate=exchange_rate,
                transaction_type=CustomerTransaction.TransactionType.ADJUSTMENT,
                notes=notes,
                user=user,
                receipt_number=receipt_number,
            )
        else:
            txn = adjust_balance(
                customer, signed_amount,
                currency=currency,
                exchange_rate=exchange_rate,
                notes=notes,
                user=user,
                receipt_number=receipt_number,
            )
            # Réduire la dette = argent reçu : ça entre au tiroir.
            record_customer_debt_payment(
                organization=customer.organization,
                customer=customer,
                amount=abs(signed_amount),
                user=user,
                notes=f"Ajustement solde client - {notes}",
                currency=currency,
                exchange_rate=exchange_rate,
            )
        customer.refresh_from_db()

    return {
        'transaction': txn,
        'receipt_number': receipt_number,
        'balance_before': str(balance_before),
        'balance_after': str(get_balance(customer, currency)),
        'currency': currency,
        'new_balance': str(customer.current_balance),
        'balances': balances_by_currency(customer),
    }
