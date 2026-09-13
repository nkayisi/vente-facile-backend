"""
Utility functions for the application.
"""
import uuid
import secrets
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone


def generate_token(length=32):
    """Generate a secure random token."""
    return secrets.token_urlsafe(length)


def round_decimal(value, places=2):
    """Round a decimal to specified places."""
    if value is None:
        return Decimal('0.00')
    return Decimal(str(value)).quantize(
        Decimal(10) ** -places,
        rounding=ROUND_HALF_UP
    )


class ReferenceGenerator:
    """Generate sequential references for documents."""
    
    @staticmethod
    def generate_sale_reference(organization):
        """Generate sale reference: VT-YYYYMMDD-XXXX"""
        from apps.sales.models import Sale
        
        today = timezone.now()
        prefix = f"VT-{today.strftime('%Y%m%d')}"
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ ON NE COMPTE QUE LES RÉFÉRENCES DE LA SÉRIE DU SERVEUR.          │
        # │                                                                  │
        # │ Un terminal alloue son propre numéro avant d'imprimer, et il     │
        # │ porte un code d'appareil : `VT-20260910-K7QM-0042`. Or le tri   │
        # │ est ALPHABÉTIQUE et « K » passe au-dessus de « 0 », si bien que  │
        # │ cette référence-là devenait le dernier rang connu, et            │
        # │ `split('-')[-1]` en tirait 42. La série du serveur sautait donc  │
        # │ à 0043 alors qu'elle en était à 0004.                            │
        # │                                                                  │
        # │ Ce n'est pas une collision - les deux séries ne se croisent pas  │
        # │ - c'est un TROU, et une série trouée porte le RCCM et le NIF.    │
        # │ Le motif ne retient que les rangs à quatre chiffres collés au    │
        # │ préfixe du jour, c'est-à-dire la seule forme que produit cette   │
        # │ fonction.                                                        │
        # └──────────────────────────────────────────────────────────────────┘
        last = Sale.objects.filter(
            organization=organization,
            reference__startswith=prefix,
        ).filter(
            reference__regex=r'^' + prefix + r'-[0-9]{4}$'
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_purchase_reference(organization):
        """Generate purchase order reference: PO-YYYYMMDD-XXXX"""
        from apps.purchases.models import PurchaseOrder
        
        today = timezone.now()
        prefix = f"PO-{today.strftime('%Y%m%d')}"
        
        last = PurchaseOrder.objects.filter(
            organization=organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_transfer_reference(organization):
        """Generate stock transfer reference: TR-YYYYMMDD-XXXX"""
        from apps.inventory.models import StockTransfer
        
        today = timezone.now()
        prefix = f"TR-{today.strftime('%Y%m%d')}"
        
        last = StockTransfer.objects.filter(
            organization=organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_adjustment_reference(organization):
        """Generate stock adjustment reference: ADJ-YYYYMMDD-XXXX"""
        from apps.inventory.models import StockAdjustment
        
        today = timezone.now()
        prefix = f"ADJ-{today.strftime('%Y%m%d')}"
        
        last = StockAdjustment.objects.filter(
            organization=organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_batch_number(organization):
        """Generate batch number: LOT-YYYYMMDD-XXXX"""
        from apps.inventory.models import StockBatch
        
        today = timezone.now()
        prefix = f"LOT-{today.strftime('%Y%m%d')}"
        
        last = StockBatch.objects.filter(
            organization=organization,
            batch_number__startswith=prefix
        ).order_by('-batch_number').first()
        
        if last:
            try:
                num = int(last.batch_number.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_inventory_reference(organization):
        """Generate inventory session reference: INV-YYYYMMDD-XXXX"""
        from apps.inventory.models import InventorySession
        
        today = timezone.now()
        prefix = f"INV-{today.strftime('%Y%m%d')}"
        
        last = InventorySession.objects.filter(
            organization=organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_customer_code(organization):
        """Generate customer code: CL-XXXXX"""
        from apps.contacts.models import Customer
        
        last = Customer.objects.filter(
            organization=organization
        ).order_by('-code').first()
        
        if last:
            try:
                num = int(last.code.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"CL-{num:05d}"

    @staticmethod
    def generate_supplier_code(organization):
        """Generate supplier code: FR-XXXXX"""
        from apps.contacts.models import Supplier
        
        last = Supplier.objects.filter(
            organization=organization
        ).order_by('-code').first()
        
        if last:
            try:
                num = int(last.code.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"FR-{num:05d}"

    @staticmethod
    def generate_expense_reference(organization):
        """Generate expense reference: DEP-YYYYMMDD-XXXX"""
        from apps.cashbook.models import Expense
        
        today = timezone.now()
        prefix = f"DEP-{today.strftime('%Y%m%d')}"
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ ON NE COMPTE QUE LES RÉFÉRENCES DE LA SÉRIE DU SERVEUR.          │
        # │                                                                  │
        # │ Un terminal alloue son propre numéro avant d'imprimer, et il     │
        # │ porte un code d'appareil : `DEP-20260910-K7QM-0042`. Or le tri   │
        # │ est ALPHABÉTIQUE et « K » passe au-dessus de « 0 », si bien que  │
        # │ cette référence-là devenait le dernier rang connu, et            │
        # │ `split('-')[-1]` en tirait 42. La série du serveur sautait donc  │
        # │ à 0043 alors qu'elle en était à 0004.                            │
        # │                                                                  │
        # │ Ce n'est pas une collision - les deux séries ne se croisent pas  │
        # │ - c'est un TROU, et une série trouée porte le RCCM et le NIF.    │
        # │ Le motif ne retient que les rangs à quatre chiffres collés au    │
        # │ préfixe du jour, c'est-à-dire la seule forme que produit cette   │
        # │ fonction.                                                        │
        # └──────────────────────────────────────────────────────────────────┘
        last = Expense.objects.filter(
            organization=organization,
            reference__startswith=prefix,
        ).filter(
            reference__regex=r'^' + prefix + r'-[0-9]{4}$'
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_cash_movement_reference(organization):
        """Generate cash movement reference: MC-YYYYMMDD-XXXX"""
        from apps.cashbook.models import CashMovement
        
        today = timezone.now()
        prefix = f"MC-{today.strftime('%Y%m%d')}"
        
        last = CashMovement.objects.filter(
            organization=organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"
