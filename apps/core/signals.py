"""
Django signals for core functionality.
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone


@receiver(post_save, sender='organizations.OrganizationMembership')
def handle_membership_created(sender, instance, created, **kwargs):
    """Handle new membership creation."""
    if created and instance.is_active:
        from apps.core.services import PermissionService
        PermissionService.assign_role_permissions(
            instance.user,
            instance.organization,
            instance.role
        )


@receiver(pre_save, sender='products.Product')
def generate_product_sku(sender, instance, **kwargs):
    """Auto-generate SKU if not provided."""
    if not instance.sku:
        from apps.products.models import Product
        prefix = instance.organization.slug[:3].upper() if instance.organization else 'PRD'
        last_product = Product.objects.filter(
            organization=instance.organization
        ).order_by('-created_at').first()
        
        if last_product and last_product.sku:
            try:
                last_num = int(last_product.sku.split('-')[-1])
                new_num = last_num + 1
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1
        
        instance.sku = f"{prefix}-{new_num:06d}"


@receiver(pre_save, sender='sales.Sale')
def generate_sale_reference(sender, instance, **kwargs):
    """Auto-generate sale reference if not provided."""
    if not instance.reference:
        from apps.sales.models import Sale
        today = timezone.now()
        prefix = f"VT{today.strftime('%Y%m%d')}"
        
        last_sale = Sale.objects.filter(
            organization=instance.organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last_sale:
            try:
                last_num = int(last_sale.reference.split('-')[-1])
                new_num = last_num + 1
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1
        
        instance.reference = f"{prefix}-{new_num:04d}"


@receiver(pre_save, sender='purchases.PurchaseOrder')
def generate_po_reference(sender, instance, **kwargs):
    """Auto-generate purchase order reference if not provided."""
    if not instance.reference:
        from apps.purchases.models import PurchaseOrder
        today = timezone.now()
        prefix = f"PO{today.strftime('%Y%m%d')}"
        
        last_po = PurchaseOrder.objects.filter(
            organization=instance.organization,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last_po:
            try:
                last_num = int(last_po.reference.split('-')[-1])
                new_num = last_num + 1
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1
        
        instance.reference = f"{prefix}-{new_num:04d}"


@receiver(post_save, sender='sales.Sale')
def update_stock_on_sale(sender, instance, created, **kwargs):
    """Update stock when sale is completed."""
    if instance.status == 'completed':
        from apps.inventory.models import Stock, StockMovement
        
        for item in instance.items.all():
            stock = Stock.objects.filter(
                organization=instance.organization,
                product=item.product,
                variant=item.variant,
                warehouse=instance.warehouse
            ).first()
            
            if stock:
                quantity_before = stock.quantity
                stock.quantity -= item.quantity
                stock.last_movement_at = timezone.now()
                stock.save()
                
                StockMovement.objects.create(
                    organization=instance.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=instance.warehouse,
                    batch=item.batch,
                    movement_type=StockMovement.MovementType.SALE,
                    quantity=-item.quantity,
                    unit_cost=item.cost_price,
                    quantity_before=quantity_before,
                    quantity_after=stock.quantity,
                    reference_type='sale',
                    reference_id=instance.id,
                    created_by=instance.sold_by
                )


@receiver(post_save, sender='purchases.GoodsReceipt')
def update_stock_on_receipt(sender, instance, created, **kwargs):
    """Update stock when goods are received."""
    if instance.status == 'completed':
        from apps.inventory.models import Stock, StockMovement, StockBatch
        
        for item in instance.items.all():
            stock, _ = Stock.objects.get_or_create(
                organization=instance.organization,
                product=item.product,
                variant=item.variant,
                warehouse=instance.warehouse,
                defaults={'quantity': 0}
            )
            
            quantity_before = stock.quantity
            stock.quantity += item.quantity_accepted
            stock.last_movement_at = timezone.now()
            
            total_value = (stock.avg_cost * quantity_before) + (item.unit_cost * item.quantity_accepted)
            new_quantity = quantity_before + item.quantity_accepted
            if new_quantity > 0:
                stock.avg_cost = total_value / new_quantity
            
            stock.save()
            
            if item.batch_number:
                StockBatch.objects.create(
                    organization=instance.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=instance.warehouse,
                    batch_number=item.batch_number,
                    quantity=item.quantity_accepted,
                    cost_price=item.unit_cost,
                    expiry_date=item.expiry_date
                )
            
            StockMovement.objects.create(
                organization=instance.organization,
                product=item.product,
                variant=item.variant,
                warehouse=instance.warehouse,
                movement_type=StockMovement.MovementType.PURCHASE,
                quantity=item.quantity_accepted,
                unit_cost=item.unit_cost,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                reference_type='goods_receipt',
                reference_id=instance.id,
                created_by=instance.received_by
            )
