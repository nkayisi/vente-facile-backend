"""
Service pour créer automatiquement des mouvements de caisse
depuis les autres modules (ventes, achats, etc.).
"""
from decimal import Decimal
from django.utils import timezone
from apps.core.utils import ReferenceGenerator


def _get_last_balance(organization):
    """Récupère le dernier solde de caisse."""
    from .models import CashMovement
    last = CashMovement.objects.filter(
        organization=organization,
        is_cancelled=False,
    ).order_by('-movement_date', '-created_at').first()
    return last.balance_after if last else Decimal('0.00')


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


def record_sale_income(organization, sale, amount, user):
    """
    Enregistre une entrée de caisse pour une vente (paiement reçu).
    Appelé quand une vente est complétée ou qu'un paiement est ajouté.
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance + amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='in',
        movement_type='sale',
        amount=amount,
        description=f"Vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=sale.customer,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
    )


def record_sale_cancellation(organization, sale, amount, user):
    """
    Enregistre une sortie de caisse pour l'annulation d'une vente.
    Appelé quand une vente payée est annulée (remboursement).
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance - amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='out',
        movement_type='sale_return',
        amount=amount,
        description=f"Annulation vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=sale.customer,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
    )


def record_debt_collection(organization, sale, amount, customer, user):
    """
    Enregistre une entrée de caisse pour un recouvrement de dette client.
    Appelé quand un paiement est ajouté sur une vente à crédit.
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance + amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='in',
        movement_type='debt_collection',
        amount=amount,
        description=f"Recouvrement dette - Vente {sale.reference}",
        sale=sale,
        session=getattr(sale, 'session', None),
        customer=customer,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
    )


def record_customer_debt_payment(organization, customer, amount, user, notes=''):
    """
    Enregistre une entrée de caisse pour un paiement de dette client.
    Appelé depuis CustomerViewSet.record_payment (gestion des contacts).
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance + amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='in',
        movement_type='debt_collection',
        amount=amount,
        description=f"Paiement dette client - {customer.name}",
        customer=customer,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
        notes=notes,
    )


def record_customer_advance(organization, customer, amount, user, notes=''):
    """
    Enregistre une entrée de caisse pour une avance/acompte client.
    Appelé depuis CustomerViewSet.record_advance.
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance + amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='in',
        movement_type='other_in',
        amount=amount,
        description=f"Avance/acompte client - {customer.name}",
        customer=customer,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
        notes=notes,
    )


def record_sale_return_refund(organization, sale_return, amount, user):
    """
    Enregistre une sortie de caisse pour un remboursement suite à un retour de vente.
    Appelé depuis SaleReturnViewSet.approve quand refund_amount > 0.
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance - amount

    original_sale = sale_return.original_sale
    customer = original_sale.customer

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='out',
        movement_type='sale_return',
        amount=amount,
        description=f"Remboursement retour {sale_return.reference} (vente {original_sale.reference})",
        sale=original_sale,
        customer=customer,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
    )


def record_purchase_return_refund(organization, purchase_return, amount, supplier, user):
    """
    Enregistre une entrée de caisse pour un remboursement fournisseur suite à un retour.
    Appelé depuis PurchaseReturnViewSet quand le retour est complété/expédié.
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance + amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='in',
        movement_type='supplier_refund',
        amount=amount,
        description=f"Remboursement retour fournisseur {purchase_return.reference} - {supplier.name}",
        purchase_order=purchase_return.purchase_order,
        supplier=supplier,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
    )


def record_purchase_payment(organization, purchase_order, amount, supplier, user):
    """
    Enregistre une sortie de caisse pour un paiement fournisseur.
    """
    from .models import CashMovement

    previous_balance = _get_last_balance(organization)
    new_balance = previous_balance - amount

    CashMovement.objects.create(
        organization=organization,
        reference=ReferenceGenerator.generate_cash_movement_reference(organization),
        direction='out',
        movement_type='purchase',
        amount=amount,
        description=f"Paiement fournisseur{' - ' + purchase_order.reference if purchase_order else ''} - {supplier.name}",
        purchase_order=purchase_order,
        supplier=supplier,
        balance_after=new_balance,
        movement_date=timezone.now(),
        created_by=user,
    )
