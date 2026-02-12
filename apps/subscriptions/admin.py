from django.contrib import admin
from .models import Plan, PlanFeature, Subscription, SubscriptionUsage, Invoice, InvoiceItem, SubscriptionPayment


class PlanFeatureInline(admin.TabularInline):
    model = PlanFeature
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'price_monthly', 'price_yearly', 'max_users', 'is_active', 'is_featured']
    list_filter = ['is_active', 'is_featured']
    search_fields = ['name', 'code']
    inlines = [PlanFeatureInline]
    
    fieldsets = (
        (None, {
            'fields': ('name', 'code', 'description')
        }),
        ('Pricing', {
            'fields': ('price_monthly', 'price_yearly', 'currency')
        }),
        ('Limits', {
            'fields': ('max_users', 'max_branches', 'max_products', 'max_monthly_transactions', 'storage_limit_mb')
        }),
        ('Features', {
            'fields': ('features',)
        }),
        ('Status', {
            'fields': ('is_active', 'is_featured', 'trial_days', 'sort_order')
        }),
    )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ['organization', 'plan', 'status', 'billing_cycle', 'current_period_end']
    list_filter = ['status', 'billing_cycle', 'plan']
    search_fields = ['organization__name']
    raw_id_fields = ['organization', 'plan']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'plan', 'status')
        }),
        ('Billing', {
            'fields': ('billing_cycle', 'price', 'currency')
        }),
        ('Trial', {
            'fields': ('trial_start', 'trial_end')
        }),
        ('Period', {
            'fields': ('current_period_start', 'current_period_end')
        }),
        ('Cancellation', {
            'fields': ('cancelled_at', 'cancel_at_period_end')
        }),
        ('External', {
            'fields': ('external_id', 'metadata'),
            'classes': ('collapse',)
        }),
    )


@admin.register(SubscriptionUsage)
class SubscriptionUsageAdmin(admin.ModelAdmin):
    list_display = ['subscription', 'period_start', 'users_count', 'transactions_count', 'storage_used_mb']
    list_filter = ['subscription__organization']
    raw_id_fields = ['subscription']


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'organization', 'total', 'status', 'issue_date', 'due_date']
    list_filter = ['status']
    search_fields = ['invoice_number', 'organization__name']
    raw_id_fields = ['organization', 'subscription']
    inlines = [InvoiceItemInline]


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = ['organization', 'amount', 'payment_method', 'status', 'paid_at']
    list_filter = ['status', 'payment_method']
    search_fields = ['organization__name', 'reference']
    raw_id_fields = ['organization', 'invoice']
