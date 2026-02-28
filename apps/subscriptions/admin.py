from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from .models import (
    Plan, PlanFeature, Subscription, SubscriptionUsage,
    Invoice, InvoiceItem, SubscriptionPayment, GlobalConfig,
)
from .services import SubscriptionService


# =============================================================================
# GLOBAL CONFIG (Singleton)
# =============================================================================

@admin.register(GlobalConfig)
class GlobalConfigAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'trial_days', 'grace_period_days']

    def has_add_permission(self, request):
        # Singleton : interdire la création si un objet existe déjà
        return not GlobalConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# =============================================================================
# PLANS
# =============================================================================

class PlanFeatureInline(admin.TabularInline):
    model = PlanFeature
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'price_monthly', 'price_yearly', 'currency', 'max_users', 'is_active', 'is_featured']
    list_filter = ['is_active', 'is_featured']
    search_fields = ['name', 'code']
    inlines = [PlanFeatureInline]
    
    fieldsets = (
        (None, {
            'fields': ('name', 'code', 'description')
        }),
        ('Tarification', {
            'fields': ('price_monthly', 'price_yearly', 'currency')
        }),
        ('Limites', {
            'fields': ('max_users', 'max_branches', 'max_products', 'max_monthly_transactions', 'storage_limit_mb')
        }),
        ('Fonctionnalités', {
            'fields': ('features',),
            'classes': ('collapse',)
        }),
        ('Paramètres', {
            'fields': ('is_active', 'is_featured', 'trial_days', 'sort_order')
        }),
    )


# =============================================================================
# SUBSCRIPTIONS
# =============================================================================

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        'organization', 'plan', 'status_badge', 'billing_cycle',
        'current_period_start', 'current_period_end', 'days_remaining_display',
    ]
    list_filter = ['status', 'billing_cycle', 'plan']
    search_fields = ['organization__name']
    raw_id_fields = ['organization', 'plan']
    readonly_fields = ['created_at', 'updated_at']
    actions = ['action_activate', 'action_expire', 'action_extend_30_days']
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'plan', 'status')
        }),
        ('Facturation', {
            'fields': ('billing_cycle', 'price', 'currency')
        }),
        ('Essai', {
            'fields': ('trial_start', 'trial_end'),
            'classes': ('collapse',)
        }),
        ('Période', {
            'fields': ('current_period_start', 'current_period_end')
        }),
        ('Annulation', {
            'fields': ('cancelled_at', 'cancel_at_period_end'),
            'classes': ('collapse',)
        }),
        ('Externe', {
            'fields': ('external_id', 'metadata'),
            'classes': ('collapse',)
        }),
        ('Dates', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def status_badge(self, obj):
        colors = {
            'trial': '#3B82F6',
            'active': '#10B981',
            'past_due': '#F59E0B',
            'expired': '#EF4444',
            'cancelled': '#6B7280',
            'suspended': '#EF4444',
        }
        color = colors.get(obj.status, '#6B7280')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Statut'

    def days_remaining_display(self, obj):
        days = obj.days_remaining
        if days <= 0:
            return format_html('<span style="color:red; font-weight:bold;">Expiré</span>')
        if days <= 5:
            return format_html('<span style="color:orange; font-weight:bold;">{} j</span>', days)
        return f'{days} j'
    days_remaining_display.short_description = 'Jours restants'

    @admin.action(description='✅ Activer les abonnements sélectionnés')
    def action_activate(self, request, queryset):
        now = timezone.now()
        count = queryset.update(status=Subscription.Status.ACTIVE, updated_at=now)
        self.message_user(request, f'{count} abonnement(s) activé(s).')

    @admin.action(description='❌ Expirer les abonnements sélectionnés')
    def action_expire(self, request, queryset):
        now = timezone.now()
        count = queryset.update(status=Subscription.Status.EXPIRED, updated_at=now)
        self.message_user(request, f'{count} abonnement(s) expiré(s).')

    @admin.action(description='📅 Prolonger de 30 jours')
    def action_extend_30_days(self, request, queryset):
        from datetime import timedelta
        now = timezone.now()
        count = 0
        for sub in queryset:
            # Prolonger à partir de maintenant ou de la fin actuelle (le plus tard)
            start = max(now, sub.current_period_end) if sub.current_period_end else now
            sub.current_period_end = start + timedelta(days=30)
            if sub.status in [Subscription.Status.EXPIRED, Subscription.Status.PAST_DUE]:
                sub.status = Subscription.Status.ACTIVE
            sub.save()
            count += 1
        self.message_user(request, f'{count} abonnement(s) prolongé(s) de 30 jours.')


# =============================================================================
# USAGE
# =============================================================================

@admin.register(SubscriptionUsage)
class SubscriptionUsageAdmin(admin.ModelAdmin):
    list_display = ['subscription', 'period_start', 'users_count', 'transactions_count', 'storage_used_mb']
    list_filter = ['subscription__organization']
    raw_id_fields = ['subscription']


# =============================================================================
# INVOICES
# =============================================================================

class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'organization', 'total', 'currency', 'status', 'issue_date', 'due_date', 'paid_date']
    list_filter = ['status']
    search_fields = ['invoice_number', 'organization__name']
    raw_id_fields = ['organization', 'subscription']
    inlines = [InvoiceItemInline]


# =============================================================================
# PAYMENTS
# =============================================================================

@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = ['organization', 'amount', 'currency', 'payment_method', 'status_badge', 'reference', 'paid_at', 'created_by']
    list_filter = ['status', 'payment_method']
    search_fields = ['organization__name', 'reference']
    raw_id_fields = ['organization', 'subscription', 'invoice', 'created_by']
    actions = ['action_mark_completed']

    def status_badge(self, obj):
        colors = {
            'pending': '#F59E0B',
            'completed': '#10B981',
            'failed': '#EF4444',
            'refunded': '#6B7280',
        }
        color = colors.get(obj.status, '#6B7280')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Statut'

    @admin.action(description='✅ Marquer comme payé et activer l\'abonnement')
    def action_mark_completed(self, request, queryset):
        for payment in queryset.filter(status=SubscriptionPayment.Status.PENDING):
            payment.status = SubscriptionPayment.Status.COMPLETED
            payment.paid_at = timezone.now()
            payment.save()

            # Activer l'abonnement lié
            if payment.subscription:
                sub = payment.subscription
                if sub.status != Subscription.Status.ACTIVE:
                    sub.status = Subscription.Status.ACTIVE
                    sub.save(update_fields=['status', 'updated_at'])

        self.message_user(request, f'{queryset.count()} paiement(s) marqué(s) comme payé(s).')
