from django.contrib import admin
from .models import (
    Register, RegisterSession, Sale, SaleItem, PaymentMethod, Payment,
    SaleReturn, SaleReturnItem, Quotation, QuotationItem
)


@admin.register(Register)
class RegisterAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'branch', 'organization', 'is_active']
    list_filter = ['is_active', 'branch', 'organization']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization', 'branch', 'warehouse']


@admin.register(RegisterSession)
class RegisterSessionAdmin(admin.ModelAdmin):
    list_display = ['register', 'opened_by', 'status', 'opening_balance', 'closing_balance', 'opened_at']
    list_filter = ['status', 'register', 'organization']
    search_fields = ['register__name']
    raw_id_fields = ['organization', 'register', 'opened_by', 'closed_by']
    readonly_fields = ['opened_at']


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    raw_id_fields = ['product', 'variant', 'batch']


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    raw_id_fields = ['payment_method', 'received_by']


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ['reference', 'customer', 'total', 'status', 'sale_type', 'sale_date']
    list_filter = ['status', 'sale_type', 'is_pos', 'organization']
    search_fields = ['reference', 'customer__name']
    raw_id_fields = ['organization', 'session', 'register', 'warehouse', 'customer', 'price_list', 'sold_by']
    readonly_fields = ['sale_date', 'created_at', 'updated_at']
    inlines = [SaleItemInline, PaymentInline]
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'reference', 'status', 'sale_type', 'is_pos')
        }),
        ('Location', {
            'fields': ('session', 'register', 'warehouse')
        }),
        ('Customer', {
            'fields': ('customer', 'price_list')
        }),
        ('Amounts', {
            'fields': ('subtotal', 'tax_amount', 'discount_amount', 'discount_percentage', 'total')
        }),
        ('Payment', {
            'fields': ('amount_paid', 'amount_due', 'change_amount', 'currency', 'exchange_rate')
        }),
        ('Notes', {
            'fields': ('notes', 'internal_notes')
        }),
        ('Metadata', {
            'fields': ('sold_by', 'sale_date', 'due_date'),
            'classes': ('collapse',)
        }),
    )


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'method_type', 'organization', 'is_default', 'is_active']
    list_filter = ['method_type', 'is_active', 'organization']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization']


class SaleReturnItemInline(admin.TabularInline):
    model = SaleReturnItem
    extra = 0
    raw_id_fields = ['original_item']


@admin.register(SaleReturn)
class SaleReturnAdmin(admin.ModelAdmin):
    list_display = ['reference', 'original_sale', 'return_type', 'status', 'total_amount', 'return_date']
    list_filter = ['status', 'return_type', 'organization']
    search_fields = ['reference', 'original_sale__reference']
    raw_id_fields = ['organization', 'original_sale', 'created_by', 'approved_by']
    inlines = [SaleReturnItemInline]


class QuotationItemInline(admin.TabularInline):
    model = QuotationItem
    extra = 0
    raw_id_fields = ['product', 'variant']


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ['reference', 'customer', 'total', 'status', 'valid_until', 'created_at']
    list_filter = ['status', 'organization']
    search_fields = ['reference', 'customer__name']
    raw_id_fields = ['organization', 'customer', 'created_by', 'converted_sale']
    inlines = [QuotationItemInline]
