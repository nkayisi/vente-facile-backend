"""
Service pour créer automatiquement des mouvements de caisse
depuis les autres modules (ventes, achats, etc.).

Multi-devise : chaque ``CashMovement`` porte sa ``currency`` et son
``exchange_rate`` (unités de devise principale pour 1 unité de la devise). Le
solde courant ``balance_after`` est suivi INDÉPENDAMMENT PAR DEVISE, de sorte
que le tiroir-caisse reflète la réalité physique (ex. USD et CDF côte à côte).
"""
from decimal import Decimal
from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from apps.core.utils import ReferenceGenerator


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
# (le tiroir physique ne convertit rien) — voir `CashMovementViewSet`.
# ---------------------------------------------------------------------------

def primary_sum(field='amount', filter=None):
    """``Sum(field × exchange_rate)`` ⇒ montant total en devise principale."""
    expr = Sum(
        F(field) * F('exchange_rate'),
        filter=filter,
        output_field=DecimalField(max_digits=24, decimal_places=6),
    )
    return Coalesce(
        expr,
        Decimal('0'),
        output_field=DecimalField(max_digits=24, decimal_places=6),
    )


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
    # (vente encaissée, paiement fournisseur) — on n'y casse pas la transaction
    # si une devise historique n'est plus configurée.
    currency, exchange_rate = resolve_currency_rate(
        organization, currency, exchange_rate, strict=False
    )
    amount = Decimal(amount)

    previous_balance = _get_last_balance(organization, currency)
    delta = amount if direction == 'in' else -amount
    new_balance = previous_balance + delta

    links.setdefault('movement_date', timezone.now())

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


def record_sale_income(organization, sale, amount, user, currency=None, exchange_rate=None):
    """
    Enregistre une entrée de caisse pour une vente (paiement reçu).

    Conservé pour compatibilité (mono-devise). Les flux de vente passent
    désormais par ``record_sale_payment_income`` (un mouvement par devise remise).
    """
    return _movement(
        organization,
        direction='in',
        movement_type='sale',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Vente {sale.reference}",
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


def record_debt_collection(organization, sale, amount, customer, user, currency=None, exchange_rate=None):
    """
    Enregistre une entrée de caisse pour un recouvrement de dette client.
    Appelé quand un paiement est ajouté sur une vente à crédit.
    """
    return _movement(
        organization,
        direction='in',
        movement_type='debt_collection',
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
        description=f"Recouvrement dette - Vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=customer,
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
