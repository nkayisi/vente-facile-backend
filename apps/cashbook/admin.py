"""
Admin pour le module Livre de Caisse.
"""
from django.contrib import admin
from .models import IncomeCategory, ExpenseCategory, Expense, CashMovement


@admin.register(IncomeCategory)
class IncomeCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'is_active', 'color']
    list_filter = ['is_active', 'organization']
    search_fields = ['name', 'code']


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'is_active', 'budget_monthly']
    list_filter = ['is_active', 'organization']
    search_fields = ['name', 'code']


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = [
        'reference', 'description', 'category', 'amount', 'status',
        'expense_date', 'organization',
    ]
    list_filter = ['status', 'category', 'organization', 'is_recurring']
    search_fields = ['reference', 'description', 'beneficiary']
    date_hierarchy = 'expense_date'
    readonly_fields = ['reference', 'created_by', 'approved_by']


@admin.register(CashMovement)
class CashMovementAdmin(admin.ModelAdmin):
    list_display = [
        'reference', 'direction', 'movement_type', 'amount',
        'balance_after', 'movement_date', 'is_cancelled', 'organization',
    ]
    list_filter = ['direction', 'movement_type', 'is_cancelled', 'organization']
    search_fields = ['reference', 'description']
    date_hierarchy = 'movement_date'
    readonly_fields = ['reference', 'created_by', 'cancelled_by']
