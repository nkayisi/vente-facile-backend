"""
Service pour créer automatiquement des mouvements de caisse
depuis les autres modules (ventes, achats, etc.).

Multi-devise : chaque ``CashMovement`` porte sa ``currency`` et son
``exchange_rate`` (unités de devise principale pour 1 unité de la devise). Le
solde courant ``balance_after`` est suivi INDÉPENDAMMENT PAR DEVISE, de sorte
que le tiroir-caisse reflète la réalité physique (ex. USD et CDF côte à côte).
"""
from decimal import Decimal
from django.db.models import Avg, DecimalField, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from apps.core.utils import ReferenceGenerator
from apps.core.clock import maintenant


def _primary_currency(organization):
    """Code de la devise principale de l'organisation (fallback 'CDF')."""
    return getattr(organization, 'currency', None) or 'CDF'


def resolve_currency_rate(organization, currency=None, exchange_rate=None, strict=True):
    """
    Devise + taux d'une ligne de caisse (dépense, mouvement).

    Délègue à ``CurrencyService.resolve``, point d'entrée unique du projet. Le
    défaut ``strict=True`` reflète l'usage local : ces lignes viennent d'une
    saisie utilisateur, une devise non activée doit être refusée.
    """
    from apps.settings.services import CurrencyService

    return CurrencyService.resolve(organization, currency, exchange_rate, strict=strict)


# ---------------------------------------------------------------------------
# Agrégation COMPTABLE : conversion en devise principale.
#
# À n'utiliser QUE pour les rapports/P&L (bénéfice, flux, dashboard), où un
# chiffre unique est attendu. Le livre de caisse, lui, reste ventilé par devise
# (le tiroir physique ne convertit rien) - voir `CashMovementViewSet`.
# ---------------------------------------------------------------------------

#: Précision de travail des agrégats convertis. Un taux porte douze décimales :
#: arrondir à deux AVANT de sommer perdrait les petites lignes d'une devise
#: faible, où l'unité vaut une fraction de centime de la principale.
_MONNAIE = DecimalField(max_digits=24, decimal_places=6)


def primary_sum(field='amount', filter=None, rate='exchange_rate'):
    """
    ``Sum(field × taux)`` ⇒ montant total en devise principale.

    ``rate`` désigne le chemin du taux, et il compte dès qu'on agrège autre
    chose que la ligne qui le porte :

    - ``Sale`` porte le sien, le défaut convient ;
    - ``SaleItem`` n'en a PAS : le taux est celui de sa vente, donc
      ``rate='sale__exchange_rate'`` ;
    - ``Payment`` en porte un, mais **ce n'est pas celui-là**. Le sien convertit
      le billet reçu vers la devise de la FACTURE, et ``amount`` est déjà dans
      cette devise : c'est encore ``sale__exchange_rate`` qu'il faut. Prendre
      celui du règlement appliquerait la conversion une seconde fois, ou aucune.
    """
    expr = Sum(
        F(field) * F(rate),
        filter=filter,
        output_field=_MONNAIE,
    )
    return Coalesce(expr, Decimal('0'), output_field=_MONNAIE)


def primary_avg(field='amount', filter=None, rate='exchange_rate'):
    """
    ``Avg(field × taux)`` ⇒ moyenne en devise principale.

    Une moyenne de montants bruts est pire qu'une somme brute : un panier moyen
    calculé sur des factures en francs et en dollars ne vaut rien du tout, alors
    qu'une somme au moins conserve un ordre de grandeur.
    """
    expr = Avg(
        F(field) * F(rate),
        filter=filter,
        output_field=_MONNAIE,
    )
    return Coalesce(expr, Decimal('0'), output_field=_MONNAIE)


def last_balance_by_currency(queryset):
    """Dernier ``balance_after`` de CHAQUE devise ⇒ ``{code: (solde, taux)}``.

    ``queryset`` doit être un queryset de ``CashMovement`` déjà filtré
    (organisation, périmètre, annulations…).
    """
    # `.order_by()` neutralise l'ordering du modèle : sinon DISTINCT inclut les
    # colonnes de tri et renvoie des devises en double.
    codes = queryset.order_by().values_list('currency', flat=True).distinct()
    result = {}
    for ccy in codes:
        last = queryset.filter(currency=ccy).order_by(
            '-movement_date', '-created_at'
        ).first()
        if last:
            result[ccy] = (last.balance_after, last.exchange_rate)
    return result


def balance_in_primary(queryset):
    """Solde de caisse toutes devises confondues, converti en principale.

    Somme les derniers ``balance_after`` de chaque devise × leur taux. Ne jamais
    utiliser pour afficher le tiroir : c'est un chiffre comptable, pas physique.
    """
    return sum(
        (bal * rate for bal, rate in last_balance_by_currency(queryset).values()),
        Decimal('0'),
    )


def _get_last_balance(organization, currency):
    """Dernier solde de caisse POUR UNE DEVISE donnée."""
    from .models import CashMovement
    last = CashMovement.objects.filter(
        organization=organization,
        currency=currency,
        is_cancelled=False,
    ).order_by('-movement_date', '-created_at').first()
    return last.balance_after if last else Decimal('0.00')


def _movement(organization, *, direction, movement_type, amount, description,
              user, currency=None, exchange_rate=None, **links):
    """
    Crée un ``CashMovement`` en calculant ``balance_after`` par devise.

    ``currency`` défaut = devise principale de l'org ; ``exchange_rate`` défaut = 1.
    ``links`` : champs FK optionnels (sale, expense, customer, supplier,
    purchase_order, session, notes, income_category, expense_category,
    payment_method, movement_date).
    """
    from .models import CashMovement

    # `exchange_rate` sur un mouvement = unités de devise PRINCIPALE pour 1 unité
    # de `currency` (pour reconvertir en principale dans les rapports). Résolu
    # automatiquement depuis OrganizationCurrency si non fourni.
    # `strict=False` : `_movement` est appelé depuis des flux déjà validés
    # (vente encaissée, paiement fournisseur) - on n'y casse pas la transaction
    # si une devise historique n'est plus configurée.
    currency, exchange_rate = resolve_currency_rate(
        organization, currency, exchange_rate, strict=False
    )
    amount = Decimal(amount)

    previous_balance = _get_last_balance(organization, currency)
    delta = amount if direction == 'in' else -amount
    new_balance = previous_balance + delta

    # Faute de date au corps, l'heure de l'ACTE : un mouvement de caisse
    # saisi hors ligne porte l'heure du tiroir, pas celle de la poussée.
    links.setdefault('movement_date', maintenant())

    return CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction=direction,
        movement_type=movement_type,
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=description,
        balance_after=new_balance,
        created_by=user,
        **links,
    )


def get_open_session_for_user(organization, user):
    """Session de caisse actuellement ouverte par ``user`` (ou ``None``).

    Sert à rattacher un mouvement de caisse saisi au comptoir (dépense, apport,
    retrait) à la session en cours, pour le calcul de la caisse nette.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    from apps.sales.models import RegisterSession
    return (
        RegisterSession.objects.filter(
            organization=organization, opened_by=user, status='open'
        )
        .order_by('-opened_at')
        .first()
    )


def record_sale_payment_income(organization, sale, payment, user):
    """
    Enregistre l'entrée de caisse d'UN règlement de vente, dans la devise
    réellement remise par le client (``payment.tendered_amount`` / ``currency``).

    C'est ce qui entre physiquement dans le tiroir. Un règlement par devise ⇒
    un mouvement par devise, pour une caisse multi-devise fidèle.
    """
    tendered = payment.tendered_amount if payment.tendered_amount is not None else payment.amount
    return _movement(
        organization,
        direction='in',
        movement_type='sale',
        amount=tendered,
        currency=payment.currency,
        description=f"Vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=sale.customer,
        payment_method=payment.payment_method,
        user=user,
    )


def record_debt_collection_payment(organization, sale, payment, customer, user):
    """Recouvrement de dette pour UN règlement (devise réellement remise)."""
    tendered = payment.tendered_amount if payment.tendered_amount is not None else payment.amount
    return _movement(
        organization,
        direction='in',
        movement_type='debt_collection',
        amount=tendered,
        currency=payment.currency,
        description=f"Recouvrement dette - Vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=customer,
        payment_method=payment.payment_method,
        user=user,
    )


def record_change(organization, sale, amount, change_currency, user, exchange_rate=None):
    """Sortie de caisse : monnaie rendue au client, dans ``change_currency``."""
    if not amount or Decimal(amount) <= 0:
        return None
    return _movement(
        organization,
        direction='out',
        movement_type='change',
        amount=amount,
        currency=change_currency,
        exchange_rate=exchange_rate,
        description=f"Monnaie rendue - Vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=sale.customer,
        user=user,
    )


def record_sale_cancellation(organization, sale, amount, user, currency=None, exchange_rate=None):
    """
    Enregistre une sortie de caisse pour l'annulation d'une vente.
    Appelé quand une vente payée est annulée (remboursement).
    """
    return _movement(
        organization,
        direction='out',
        movement_type='sale_return',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Annulation vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=sale.customer,
        user=user,
    )


def record_customer_debt_payment(organization, customer, amount, user, notes='', currency=None, exchange_rate=None):
    """
    Enregistre une entrée de caisse pour un paiement de dette client.
    Appelé depuis CustomerViewSet.record_payment (gestion des contacts).
    """
    return _movement(
        organization,
        direction='in',
        movement_type='debt_collection',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Paiement dette client - {customer.name}",
        customer=customer,
        notes=notes,
        user=user,
    )


def record_customer_advance(organization, customer, amount, user, notes='', currency=None, exchange_rate=None):
    """
    Enregistre une entrée de caisse pour une avance/acompte client.
    Appelé depuis CustomerViewSet.record_advance.
    """
    return _movement(
        organization,
        direction='in',
        movement_type='other_in',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Avance/acompte client - {customer.name}",
        customer=customer,
        notes=notes,
        user=user,
    )


def record_sale_return_refund(organization, sale_return, amount, user, currency=None, exchange_rate=None):
    """
    Enregistre une sortie de caisse pour un remboursement suite à un retour de vente.
    Appelé depuis SaleReturnViewSet.approve quand refund_amount > 0.
    """
    original_sale = sale_return.original_sale
    customer = original_sale.customer

    return _movement(
        organization,
        direction='out',
        movement_type='sale_return',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Remboursement retour {sale_return.reference} (vente {original_sale.reference})",
        sale=original_sale,
        customer=customer,
        user=user,
    )


def record_purchase_return_refund(organization, purchase_return, amount, supplier, user, currency=None, exchange_rate=None):
    """
    Enregistre une entrée de caisse pour un remboursement fournisseur suite à un retour.
    Appelé depuis PurchaseReturnViewSet quand le retour est complété/expédié.
    """
    return _movement(
        organization,
        direction='in',
        movement_type='supplier_refund',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Remboursement retour fournisseur {purchase_return.reference} - {supplier.name}",
        purchase_order=purchase_return.purchase_order,
        supplier=supplier,
        user=user,
    )


def record_purchase_payment(organization, purchase_order, amount, supplier, user, currency=None, exchange_rate=None):
    """
    Enregistre une sortie de caisse pour un paiement fournisseur.
    """
    return _movement(
        organization,
        direction='out',
        movement_type='purchase',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Paiement fournisseur{' - ' + purchase_order.reference if purchase_order else ''} - {supplier.name}",
        purchase_order=purchase_order,
        supplier=supplier,
        user=user,
    )


# ---------------------------------------------------------------------------
# SAISIES MANUELLES : le corps partagé par la vue et par le journal.
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ CE QUI VIT DANS UN `perform_create` EST INACCESSIBLE AU TERMINAL.        │
# │                                                                          │
# │ Les deux handlers de synchronisation appelaient `serializer.save()` en   │
# │ direct, et ne rejouaient donc RIEN de ce que la vue posait :             │
# │ l'organisation (une FK non nulle : l'insertion échouait), la référence,  │
# │ la devise résolue avec son taux, le solde du tiroir par devise, la       │
# │ session ouverte, l'auteur. Mesuré : toute dépense et tout mouvement      │
# │ saisis sur un terminal partaient en quarantaine.                         │
# │                                                                          │
# │ La parité ne se surveille pas, elle se construit : le corps descend ici, │
# │ la vue et le handler l'appellent, et `test_parity_contract` interdit     │
# │ qu'un `perform_create` le reprenne.                                      │
# └──────────────────────────────────────────────────────────────────────────┘
# ---------------------------------------------------------------------------


def _taux_du_client(serializer):
    """
    Le taux SEULEMENT s'il a été fourni, jamais celui du défaut modèle.

    ``exchange_rate`` a un défaut de 1.000000 : ``validated_data`` en contient
    donc toujours un. Le lire sans regarder le payload brut enregistrerait une
    dépense en dollars au taux 1, et le rapport comptable la compterait pour
    douze francs.
    """
    if 'exchange_rate' not in getattr(serializer, 'initial_data', {}):
        return None
    return serializer.validated_data.get('exchange_rate')


def create_expense(serializer, *, organization, user, request=None,
                   reference=None, **extra):
    """
    Enregistre une dépense saisie à la main. Appelé par la vue ET par le journal.

    ``request`` sert au seul contrôle de périmètre entrepôt ; il est facultatif
    parce qu'une dépense peut n'être rattachée à aucun entrepôt
    (``warehouse_write_required = False`` sur la vue).

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ ``reference`` : LE NUMÉRO DU TERMINAL, ET LE SERVEUR LE REPREND.         │
    │                                                                          │
    │ Une dépense se règle au comptoir, souvent avant que le réseau ne         │
    │ revienne, et le bénéficiaire repart avec sa pièce justificative - qui    │
    │ porte une ligne de signature. Un numéro provisoire remplacé ensuite      │
    │ par celui du serveur rendrait ce papier muet : il ne désignerait plus    │
    │ rien. C'est exactement le défaut corrigé sur ``sale.add_payment`` au     │
    │ lot 6, où le règlement encaissé hors ligne sortait sous un numéro que    │
    │ le serveur remplaçait.                                                   │
    │                                                                          │
    │ ⚠ Il ne passe PAS par ``**extra`` : ``reference`` est déjà un argument   │
    │ nommé de ``serializer.save()`` juste en dessous, et un doublon lèverait  │
    │ ``TypeError: got multiple values for keyword argument``.                 │
    │                                                                          │
    │ ⚠ ``reference`` reste ``read_only`` sur le serializer : elle ne peut     │
    │ donc arriver que par ici, jamais d'un client REST. C'est voulu.          │
    │                                                                          │
    │ La collision reste impossible en silence : ``Expense`` porte une         │
    │ contrainte d'unicité sur (organisation, référence), donc un              │
    │ ``IntegrityError``, donc un verdict ``rejected``.                        │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from apps.core.warehouse_scope import assert_warehouse_allowed_for_request

    entrepot = serializer.validated_data.get('warehouse')
    if request is not None and entrepot is not None:
        assert_warehouse_allowed_for_request(request, entrepot.id, allow_none=True)

    devise, taux = resolve_currency_rate(
        organization,
        serializer.validated_data.get('currency'),
        _taux_du_client(serializer),
    )
    return serializer.save(
        organization=organization,
        reference=reference or ReferenceGenerator.generate_expense_reference(organization),
        currency=devise,
        exchange_rate=taux,
        created_by=user,
        **extra,
    )


def create_manual_cash_movement(serializer, *, organization, user, **extra):
    """
    Enregistre une entrée ou une sortie de tiroir saisie à la main.

    **Le solde suit la DEVISE du mouvement.** Un tiroir contient des liasses
    distinctes ; reprendre le dernier solde toutes devises confondues ferait
    d'un apport de cent dollars une correction du solde en francs.

    **La session est résolue côté serveur**, jamais prise dans le payload : elle
    rattache le mouvement à une caisse, donc à un entrepôt, donc au périmètre de
    visibilité des magasiniers. Un client qui la choisirait pourrait désigner
    celle d'un autre.
    """
    devise, taux = resolve_currency_rate(
        organization,
        serializer.validated_data.get('currency'),
        _taux_du_client(serializer),
    )
    montant = serializer.validated_data['amount']
    sens = serializer.validated_data['direction']
    precedent = _get_last_balance(organization, devise)

    return serializer.save(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        currency=devise,
        exchange_rate=taux,
        balance_after=precedent + montant if sens == 'in' else precedent - montant,
        session=get_open_session_for_user(organization, user),
        created_by=user,
        **extra,
    )


# =============================================================================
# TRANSITIONS D'UNE DÉPENSE, ET ANNULATION D'UN MOUVEMENT
# =============================================================================
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ LE CORPS DESCEND ICI PARCE QUE DEUX SURFACES LE REJOUENT.               │
# │                                                                          │
# │ Ces six gestes vivaient dans `ExpenseViewSet` et `CashMovementViewSet`,  │
# │ donc hors d'atteinte du journal : un terminal ne pouvait ni approuver    │
# │ une dépense ni annuler un mouvement, et les y rejouer aurait demandé de  │
# │ les réécrire. C'est très exactement ainsi que la dette client avait      │
# │ divergé avant le lot 6. « La parité n'est pas surveillée, elle est       │
# │ structurelle » (§5.5) : elle ne l'est que si le corps est partagé.       │
# └──────────────────────────────────────────────────────────────────────────┘


class TransitionRefusee(Exception):
    """
    Refus métier déterministe : à ne JAMAIS réessayer.

    `_classify` le traduit en verdict `rejected`. Réessayer une approbation
    déjà faite créerait un SECOND mouvement de caisse, et le tiroir sortirait
    deux fois la même dépense.
    """


def create_cash_movement_for_expense(expense, *, user=None, session=None):
    """
    La sortie de caisse d'une dépense approuvée ou payée.

    ⚠ La SESSION est résolue par l'appelant et passée ici, jamais devinée : au
    back-office c'est celle du caissier au comptoir (`request.user`), et depuis
    le journal celle de l'utilisateur du terminal. La deviner depuis
    `expense.created_by` rattacherait la sortie à la caisse de l'auteur de la
    dépense, qui peut être un gérant sans tiroir.

    La sortie hérite de la DEVISE de la dépense : `_movement` calcule
    `balance_after` par devise, et convertir ici ferait sortir du tiroir des
    billets qui n'y étaient pas.
    """
    return _movement(
        expense.organization,
        direction='out',
        movement_type='expense',
        amount=expense.amount,
        currency=expense.currency,
        exchange_rate=expense.exchange_rate,
        description=f"Dépense: {expense.description}",
        payment_method=expense.payment_method,
        expense=expense,
        session=session,
        user=user or expense.created_by,
    )


def _session_pour(organization, user):
    return get_open_session_for_user(organization, user) if user else None


def submit_expense(expense, user=None):
    """Passe une dépense en attente d'approbation. Ne bouge PAS le tiroir."""
    if expense.status != 'draft':
        raise TransitionRefusee(
            "Seules les dépenses en brouillon peuvent être soumises."
        )
    expense.status = 'pending'
    expense.save(update_fields=['status', 'updated_at'])
    return expense


def approve_expense(expense, user):
    """
    Approuve une dépense ET fait sortir l'argent du tiroir.

    L'approbation est le moment où la dépense devient une sortie réelle : c'est
    `stats` du serveur qui le dit, en ne comptant que `approved` et `paid`.
    """
    if expense.status != 'pending':
        raise TransitionRefusee(
            "Seules les dépenses en attente peuvent être approuvées."
        )
    expense.status = 'approved'
    expense.approved_by = user
    expense.save(update_fields=['status', 'approved_by', 'updated_at'])
    create_cash_movement_for_expense(
        expense, user=user, session=_session_pour(expense.organization, user)
    )
    return expense


def reject_expense(expense, reason='', user=None):
    """
    Rejette une dépense. Le motif REJOINT les notes, il ne les écrase pas.

    Écraser `notes` ferait disparaître ce que l'auteur avait écrit sur sa
    propre dépense - c'est le défaut qu'`annuler une vente` a dû corriger sur
    `sale.notes`.
    """
    if expense.status != 'pending':
        raise TransitionRefusee(
            "Seules les dépenses en attente peuvent être rejetées."
        )
    expense.status = 'rejected'
    if reason:
        expense.notes = f"{expense.notes}\n--- Rejet ---\n{reason}".strip()
    expense.save(update_fields=['status', 'notes', 'updated_at'])
    return expense


def pay_expense(expense, user, payment_method_id=None, payment_reference=''):
    """
    Marque une dépense payée, en l'approuvant au passage si besoin.

    C'est le raccourci des petites dépenses : un caissier qui sort deux mille
    francs pour du carburant n'ouvre pas un circuit d'approbation.

    ⚠ Le mouvement de caisse n'est créé QUE s'il n'en existe pas déjà un de
    vivant. Sans ce contrôle, payer une dépense déjà approuvée ferait sortir
    l'argent une seconde fois.
    """
    if expense.status in ('paid', 'cancelled'):
        raise TransitionRefusee(
            "Cette dépense ne peut pas être marquée comme payée."
        )

    champs = ['status', 'paid_date', 'updated_at']
    if expense.status in ('draft', 'pending'):
        expense.approved_by = user
        champs.append('approved_by')
        if not expense.cash_movements.filter(is_cancelled=False).exists():
            create_cash_movement_for_expense(
                expense, user=user, session=_session_pour(expense.organization, user)
            )

    if payment_method_id:
        from apps.sales.models import PaymentMethod
        # Un moyen de paiement introuvable est IGNORÉ, comme au back-office :
        # il ne doit pas empêcher d'enregistrer une sortie d'argent réelle.
        methode = PaymentMethod.objects.filter(
            id=payment_method_id, organization=expense.organization
        ).first()
        if methode is not None:
            expense.payment_method = methode
            champs.append('payment_method')
    if payment_reference:
        expense.payment_reference = payment_reference
        champs.append('payment_reference')

    expense.status = 'paid'
    expense.paid_date = timezone.localdate()
    expense.save(update_fields=champs)
    return expense


def cancel_expense(expense, user, reason=''):
    """
    Annule une dépense et CONTREPASSE sa sortie de caisse.

    Sans l'annulation du mouvement, le tiroir garde une sortie qui n'a plus
    lieu d'être et le caissier trouve le soir un manquant que rien n'explique.
    C'est le défaut exact relevé sur l'annulation d'une vente au lot 3.
    """
    if expense.status == 'cancelled':
        raise TransitionRefusee("Cette dépense est déjà annulée.")

    for movement in expense.cash_movements.filter(is_cancelled=False):
        cancel_cash_movement(movement, user, reason)

    expense.status = 'cancelled'
    expense.save(update_fields=['status', 'updated_at'])
    return expense


def cancel_cash_movement(movement, user, reason=''):
    """
    Annule un mouvement de tiroir.

    ⚠ On MARQUE, on ne supprime pas : un mouvement de caisse est une écriture
    comptable, elle se contrepasse et ne se rature pas. `is_cancelled` sort la
    ligne des soldes et des listes, et la laisse dans les rapports de caisse,
    où l'on vient justement chercher ce qui a été rectifié.

    ⚠ `balance_after` des mouvements SUIVANTS n'est PAS recalculé, et c'est le
    comportement du back-office depuis toujours : cette colonne est l'état du
    tiroir tel qu'il était à cet instant, pas un solde courant. La réécrire
    changerait l'histoire d'un tiroir déjà compté.
    """
    if movement.is_cancelled:
        raise TransitionRefusee("Ce mouvement est déjà annulé.")

    movement.is_cancelled = True
    movement.cancelled_at = timezone.now()
    movement.cancelled_by = user
    movement.cancel_reason = reason
    movement.save(update_fields=[
        'is_cancelled', 'cancelled_at', 'cancelled_by', 'cancel_reason', 'updated_at',
    ])
    return movement
