"""
Utility functions for the application.
"""
import uuid
import secrets
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone


def generate_reference(prefix='REF', length=8):
    """Generate a unique reference code."""
    timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
    random_part = secrets.token_hex(4).upper()
    return f"{prefix}-{timestamp}-{random_part}"[:length + len(prefix) + 1]


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


def calculate_percentage(part, whole):
    """Calculate percentage."""
    if whole == 0:
        return Decimal('0.00')
    return round_decimal((Decimal(str(part)) / Decimal(str(whole))) * 100)


def calculate_margin(cost, selling):
    """Calculate profit margin percentage."""
    if cost == 0:
        return Decimal('100.00') if selling > 0 else Decimal('0.00')
    return round_decimal(((selling - cost) / cost) * 100)


def calculate_markup(cost, selling):
    """Calculate markup percentage."""
    if selling == 0:
        return Decimal('0.00')
    return round_decimal(((selling - cost) / selling) * 100)


class ReferenceGenerator:
    """Generate sequential references for documents."""
    
    @staticmethod
    def generate_sale_reference(organization):
        """Generate sale reference: VT-YYYYMMDD-XXXX"""
        from apps.sales.models import Sale
        
        today = timezone.now()
        prefix = f"VT-{today.strftime('%Y%m%d')}"
        
        last = Sale.objects.filter(
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
