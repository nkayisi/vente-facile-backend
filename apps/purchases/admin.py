from django.contrib import admin
from .models import (
    PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem,
    SupplierPayment, SupplierPaymentAllocation, PurchaseReturn, PurchaseReturnItem
)


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    raw_id_fields = ['product', 'variant']


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ['reference', 'supplier', 'total', 'status', 'order_date']
    list_filter = ['status', 'organization']
    search_fields = ['reference', 'supplier__name']
    raw_id_fields = ['organization', 'supplier', 'warehouse', 'created_by', 'approved_by']
    inlines = [PurchaseOrderItemInline]
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'reference', 'status')
        }),
        ('Supplier', {
            'fields': ('supplier', 'warehouse')
        }),
        ('Amounts', {
            'fields': ('subtotal', 'tax_amount', 'discount_amount', 'shipping_cost', 'total')
        }),
        ('Payment', {
            'fields': ('amount_paid', 'amount_due', 'currency', 'exchange_rate')
        }),
        ('Dates', {
            'fields': ('order_date', 'expected_date')
        }),
        ('Notes', {
            'fields': ('notes', 'terms')
        }),
        ('Metadata', {
            'fields': ('created_by', 'approved_by'),
            'classes': ('collapse',)
        }),
    )


class GoodsReceiptItemInline(admin.TabularInline):
    model = GoodsReceiptItem
    extra = 0
    raw_id_fields = ['purchase_order_item', 'product', 'variant']


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(admin.ModelAdmin):
    list_display = ['reference', 'purchase_order', 'warehouse', 'status', 'receipt_date']
    list_filter = ['status', 'organization']
    search_fields = ['reference', 'purchase_order__reference']
    raw_id_fields = ['organization', 'purchase_order', 'warehouse', 'received_by']
    inlines = [GoodsReceiptItemInline]


class SupplierPaymentAllocationInline(admin.TabularInline):
    model = SupplierPaymentAllocation
    extra = 0
    raw_id_fields = ['purchase_order']


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = ['reference', 'supplier', 'amount', 'status', 'payment_date']
    list_filter = ['status', 'organization']
    search_fields = ['reference', 'supplier__name']
    raw_id_fields = ['organization', 'supplier', 'payment_method', 'created_by']
    inlines = [SupplierPaymentAllocationInline]


class PurchaseReturnItemInline(admin.TabularInline):
    model = PurchaseReturnItem
    extra = 0
    raw_id_fields = ['product', 'variant', 'batch']


@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(admin.ModelAdmin):
    list_display = ['reference', 'supplier', 'total_amount', 'status', 'return_date']
    list_filter = ['status', 'organization']
    search_fields = ['reference', 'supplier__name']
    raw_id_fields = ['organization', 'supplier', 'purchase_order', 'warehouse', 'created_by']
    inlines = [PurchaseReturnItemInline]
