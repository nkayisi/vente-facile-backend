from django.contrib import admin
from .models import NotificationTemplate, Notification, Alert, AlertRule, EmailLog


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'channel', 'organization', 'is_active']
    list_filter = ['channel', 'is_active', 'organization']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization']


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'notification_type', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read', 'organization']
    search_fields = ['title', 'user__email']
    raw_id_fields = ['organization', 'user']
    readonly_fields = ['created_at', 'read_at']


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ['title', 'alert_type', 'severity', 'status', 'organization', 'created_at']
    list_filter = ['alert_type', 'severity', 'status', 'organization']
    search_fields = ['title', 'message']
    raw_id_fields = ['organization', 'acknowledged_by', 'resolved_by']
    readonly_fields = ['created_at', 'acknowledged_at', 'resolved_at']


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ['name', 'alert_type', 'severity', 'organization', 'is_active']
    list_filter = ['alert_type', 'severity', 'is_active', 'organization']
    search_fields = ['name']
    raw_id_fields = ['organization']
    filter_horizontal = ['notify_users']


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ['to_email', 'subject', 'status', 'sent_at', 'created_at']
    list_filter = ['status', 'organization']
    search_fields = ['to_email', 'subject']
    raw_id_fields = ['organization', 'template']
    readonly_fields = ['created_at', 'sent_at']
