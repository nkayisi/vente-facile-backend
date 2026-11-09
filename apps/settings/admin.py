"""
Admin configuration for settings module.
"""
from django.contrib import admin
from .models import (
    Currency, OrganizationCurrency, LoyaltyProgram,
    CustomerLoyalty, LoyaltyTransaction, OrganizationSettings
)


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'symbol', 'is_active']
    list_filter = ['is_active']
    search_fields = ['code', 'name']


@admin.register(OrganizationCurrency)
class OrganizationCurrencyAdmin(admin.ModelAdmin):
    list_display = ['organization', 'currency', 'is_primary', 'exchange_rate', 'is_active']
    list_filter = ['is_primary', 'is_active', 'organization']
    search_fields = ['organization__name', 'currency__code']


@admin.register(LoyaltyProgram)
class LoyaltyProgramAdmin(admin.ModelAdmin):
    list_display = ['organization', 'name', 'is_active', 'earn_percent']
    list_filter = ['is_active']
    search_fields = ['organization__name', 'name']


@admin.register(CustomerLoyalty)
class CustomerLoyaltyAdmin(admin.ModelAdmin):
    list_display = ['customer', 'balance', 'total_earned', 'total_used']
    search_fields = ['customer__name']


@admin.register(LoyaltyTransaction)
class LoyaltyTransactionAdmin(admin.ModelAdmin):
    list_display = ['customer_loyalty', 'transaction_type', 'amount', 'balance_after', 'created_at']
    list_filter = ['transaction_type']
    search_fields = ['customer_loyalty__customer__name']


@admin.register(OrganizationSettings)
class OrganizationSettingsAdmin(admin.ModelAdmin):
    list_display = ['organization', 'show_loyalty_on_receipt', 'low_stock_threshold']
    search_fields = ['organization__name']
