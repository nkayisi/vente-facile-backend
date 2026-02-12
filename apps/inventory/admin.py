from django.contrib import admin
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem
)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'branch', 'is_default', 'is_active']
    list_filter = ['is_active', 'is_default', 'organization']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization', 'branch', 'manager']


@admin.register(StockLocation)
class StockLocationAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'warehouse', 'parent', 'is_active']
    list_filter = ['is_active', 'warehouse']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization', 'warehouse', 'parent']


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ['product', 'warehouse', 'quantity', 'reserved_quantity', 'avg_cost']
    list_filter = ['warehouse', 'organization']
    search_fields = ['product__name', 'product__sku']
    raw_id_fields = ['organization', 'product', 'variant', 'warehouse', 'location']
    readonly_fields = ['last_counted_at', 'last_movement_at']


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ['batch_number', 'product', 'warehouse', 'quantity', 'expiry_date']
    list_filter = ['warehouse', 'organization']
    search_fields = ['batch_number', 'product__name']
    raw_id_fields = ['organization', 'product', 'variant', 'warehouse']


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ['product', 'movement_type', 'quantity', 'warehouse', 'created_at']
    list_filter = ['movement_type', 'warehouse', 'organization']
    search_fields = ['product__name', 'product__sku']
    raw_id_fields = ['organization', 'product', 'variant', 'warehouse', 'batch', 'created_by']
    readonly_fields = ['created_at', 'updated_at']


class StockTransferItemInline(admin.TabularInline):
    model = StockTransferItem
    extra = 0
    raw_id_fields = ['product', 'variant', 'batch']


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    list_display = ['reference', 'source_warehouse', 'destination_warehouse', 'status', 'requested_at']
    list_filter = ['status', 'organization']
    search_fields = ['reference']
    raw_id_fields = ['organization', 'source_warehouse', 'destination_warehouse', 'requested_by', 'approved_by']
    inlines = [StockTransferItemInline]


class StockAdjustmentItemInline(admin.TabularInline):
    model = StockAdjustmentItem
    extra = 0
    raw_id_fields = ['product', 'variant', 'batch']


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ['reference', 'warehouse', 'adjustment_type', 'status', 'created_at']
    list_filter = ['adjustment_type', 'status', 'organization']
    search_fields = ['reference']
    raw_id_fields = ['organization', 'warehouse', 'created_by', 'approved_by']
    inlines = [StockAdjustmentItemInline]
