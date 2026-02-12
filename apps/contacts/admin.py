from django.contrib import admin
from .models import Customer, Supplier, SupplierProduct


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'customer_type', 'phone', 'current_balance', 'is_active']
    list_filter = ['customer_type', 'is_active', 'organization']
    search_fields = ['name', 'code', 'email', 'phone']
    raw_id_fields = ['organization', 'created_by']
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'code', 'customer_type', 'name', 'company_name')
        }),
        ('Contact', {
            'fields': ('email', 'phone', 'address')
        }),
        ('Entreprise', {
            'fields': ('tax_id',)
        }),
        ('Crédit', {
            'fields': ('credit_limit', 'current_balance')
        }),
        ('Statut', {
            'fields': ('is_active', 'notes')
        }),
        ('Métadonnées', {
            'fields': ('created_by',),
            'classes': ('collapse',)
        }),
    )


class SupplierProductInline(admin.TabularInline):
    model = SupplierProduct
    extra = 0
    raw_id_fields = ['product']


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'contact_person', 'phone', 'current_balance', 'is_active']
    list_filter = ['is_active', 'organization']
    search_fields = ['name', 'code', 'email', 'contact_person']
    raw_id_fields = ['organization', 'created_by']
    inlines = [SupplierProductInline]
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'code', 'name', 'company_name')
        }),
        ('Contact', {
            'fields': ('contact_person', 'email', 'phone', 'website')
        }),
        ('Adresse', {
            'fields': ('address',)
        }),
        ('Business', {
            'fields': ('tax_id', 'currency')
        }),
        ('Balance', {
            'fields': ('current_balance',)
        }),
        ('Banque', {
            'fields': ('bank_name', 'bank_account')
        }),
        ('Statut', {
            'fields': ('is_active',)
        }),
        ('Métadonnées', {
            'fields': ('created_by',),
            'classes': ('collapse',)
        }),
    )
