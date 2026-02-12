from django.contrib import admin
from guardian.admin import GuardedModelAdmin
from .models import Organization, OrganizationMembership, Branch, OrganizationInvitation


@admin.register(Organization)
class OrganizationAdmin(GuardedModelAdmin):
    list_display = ['name', 'slug', 'business_type', 'is_active', 'created_at']
    list_filter = ['business_type', 'is_active', 'country']
    search_fields = ['name', 'slug', 'email']
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ['id', 'created_at', 'updated_at']
    
    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'slug', 'business_type', 'logo')
        }),
        ('Contact', {
            'fields': ('email', 'phone', 'address', 'city', 'country')
        }),
        ('Legal', {
            'fields': ('tax_id', 'rccm', 'id_nat')
        }),
        ('Settings', {
            'fields': ('currency', 'timezone', 'is_active', 'settings')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'organization', 'role', 'is_active', 'joined_at']
    list_filter = ['role', 'is_active']
    search_fields = ['user__email', 'organization__name']
    raw_id_fields = ['user', 'organization', 'invited_by']


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'is_main', 'is_active']
    list_filter = ['is_main', 'is_active']
    search_fields = ['name', 'code', 'organization__name']
    raw_id_fields = ['organization', 'manager']


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    list_display = ['email', 'organization', 'role', 'status', 'expires_at']
    list_filter = ['status', 'role']
    search_fields = ['email', 'organization__name']
    raw_id_fields = ['organization', 'invited_by']
