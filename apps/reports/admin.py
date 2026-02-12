from django.contrib import admin
from .models import ReportTemplate, SavedReport, ReportExport, Dashboard, DashboardWidget


@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'report_type', 'organization', 'is_system', 'is_active']
    list_filter = ['report_type', 'is_system', 'is_active', 'organization']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization', 'created_by']


@admin.register(SavedReport)
class SavedReportAdmin(admin.ModelAdmin):
    list_display = ['name', 'template', 'organization', 'is_scheduled', 'frequency', 'last_run']
    list_filter = ['is_scheduled', 'frequency', 'organization']
    search_fields = ['name']
    raw_id_fields = ['organization', 'template', 'created_by']


@admin.register(ReportExport)
class ReportExportAdmin(admin.ModelAdmin):
    list_display = ['id', 'export_format', 'status', 'requested_by', 'created_at', 'completed_at']
    list_filter = ['export_format', 'status', 'organization']
    raw_id_fields = ['organization', 'saved_report', 'template', 'requested_by']
    readonly_fields = ['created_at', 'completed_at']


class DashboardWidgetInline(admin.TabularInline):
    model = DashboardWidget
    extra = 0


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'created_by', 'is_default', 'is_shared']
    list_filter = ['is_default', 'is_shared', 'organization']
    search_fields = ['name']
    raw_id_fields = ['organization', 'created_by']
    inlines = [DashboardWidgetInline]
